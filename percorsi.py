"""
Tracce GPS (dai file .FIT originali) e riconoscimento dei percorsi ricorrenti.

Regole per i percorsi:
  - Due uscite sono lo stesso percorso se: stessa famiglia di sport, partenza
    entro 300 m, arrivo entro 300 m, distanza entro ±12 % e tracciato medio
    entro 150 m (confronto su 40 punti equidistanti). Il verso conta.
  - Nome manuale: rinomina un'uscita in Garmin Connect iniziando con "#",
    es. "#Giro Zugna". Tutte le uscite con lo stesso #nome finiscono nello
    stesso percorso, che prende quel nome; le uscite riconosciute in automatico
    come uguali si aggiungono.
  - "#escludi" all'inizio del nome: l'uscita non entra in nessun percorso.
"""
from __future__ import annotations

import io
import math
import zipfile

import fitdecode

SEMICERCHI = 180 / 2**31
PUNTI_FIRMA = 40
TOLL_PARTENZA_M = 300
TOLL_DISTANZA = 0.12
TOLL_TRACCIATO_M = 150
MAX_PUNTI = 500
FAMIGLIE_PERCORSO = {"corsa", "scialpinismo", "trekking"}   # lo sci in pista non ha "percorsi"
TIPI_SENZA_GPS = ("treadmill", "indoor", "virtual", "strength", "cardio", "yoga", "pilates", "elliptical")


# --------------------------------------------------------------------------- #
# Geometria
# --------------------------------------------------------------------------- #
def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _rdp(xy: list[tuple[float, float]], toll: float) -> list[int]:
    """Douglas-Peucker iterativo: indici dei punti da tenere."""
    n = len(xy)
    if n < 3:
        return list(range(n))
    tieni = [False] * n
    tieni[0] = tieni[-1] = True
    pila = [(0, n - 1)]
    while pila:
        i, j = pila.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        dmax, kmax = -1.0, -1
        for k in range(i + 1, j):
            px, py = xy[k]
            if l2 == 0:
                d = math.hypot(px - x1, py - y1)
            else:
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
                d = math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
            if d > dmax:
                dmax, kmax = d, k
        if dmax > toll and kmax > 0:
            tieni[kmax] = True
            pila += [(i, kmax), (kmax, j)]
    return [k for k in range(n) if tieni[k]]


def semplifica(punti: list[list]) -> list[list]:
    """Riduce la traccia mantenendone la forma (tolleranza crescente fino a MAX_PUNTI)."""
    if len(punti) <= MAX_PUNTI:
        return punti
    lat0 = math.radians(punti[0][0])
    xy = [(p[1] * 111_320 * math.cos(lat0), p[0] * 110_540) for p in punti]
    toll = 4.0
    while True:
        idx = _rdp(xy, toll)
        if len(idx) <= MAX_PUNTI or toll > 200:
            break
        toll *= 1.6
    if len(idx) > MAX_PUNTI:
        passo = len(idx) / MAX_PUNTI
        idx = [idx[int(i * passo)] for i in range(MAX_PUNTI - 1)] + [idx[-1]]
    return [punti[i] for i in idx]


# --------------------------------------------------------------------------- #
# Lettura FIT
# --------------------------------------------------------------------------- #
def _fit_bytes(contenuto: bytes) -> bytes | None:
    try:
        with zipfile.ZipFile(io.BytesIO(contenuto)) as z:
            nomi = [n for n in z.namelist() if n.lower().endswith(".fit")]
            return z.read(nomi[0]) if nomi else None
    except zipfile.BadZipFile:
        return contenuto if contenuto[8:12] == b".FIT" else None


def estrai_traccia(contenuto: bytes) -> list[list] | None:
    """
    Restituisce i punti [lat, lon, quota_m, distanza_m, secondi_da_inizio, fc]
    oppure None se il file non ha posizioni GPS (es. tapis roulant).
    """
    dati = _fit_bytes(contenuto)
    if not dati:
        return None
    grezzi = []
    with fitdecode.FitReader(io.BytesIO(dati), check_crc=fitdecode.CrcCheck.DISABLED) as fr:
        for fr_ in fr:
            if not (isinstance(fr_, fitdecode.FitDataMessage) and fr_.name == "record"):
                continue
            lat = fr_.get_value("position_lat", fallback=None)
            lon = fr_.get_value("position_long", fallback=None)
            ts = fr_.get_value("timestamp", fallback=None)
            if lat is None or lon is None or ts is None:
                continue
            alt = fr_.get_value("enhanced_altitude", fallback=None)
            if alt is None:
                alt = fr_.get_value("altitude", fallback=None)
            grezzi.append((lat * SEMICERCHI, lon * SEMICERCHI, alt,
                           fr_.get_value("distance", fallback=None), ts,
                           fr_.get_value("heart_rate", fallback=None)))
    if len(grezzi) < 2:
        return None

    t0 = grezzi[0][4]
    punti, cum = [], 0.0
    for i, (lat, lon, alt, dist, ts, fc) in enumerate(grezzi):
        if i:
            cum += haversine(grezzi[i - 1][0], grezzi[i - 1][1], lat, lon)
        d = dist if isinstance(dist, (int, float)) else cum
        punti.append([round(lat, 5), round(lon, 5),
                      round(alt) if isinstance(alt, (int, float)) else None,
                      round(d), round((ts - t0).total_seconds()),
                      int(fc) if isinstance(fc, (int, float)) else None])
    return semplifica(punti)


# --------------------------------------------------------------------------- #
# Riconoscimento percorsi
# --------------------------------------------------------------------------- #
def _firma(punti: list[list], n: int = PUNTI_FIRMA) -> list[tuple[float, float]]:
    """n punti equidistanti lungo la traccia (per distanza percorsa)."""
    cum = [0.0]
    for a, b in zip(punti, punti[1:]):
        cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
    tot = cum[-1] or 1.0
    out, j = [], 0
    for i in range(n):
        obiettivo = tot * i / (n - 1)
        while j < len(cum) - 2 and cum[j + 1] < obiettivo:
            j += 1
        seg = cum[j + 1] - cum[j] or 1.0
        f = max(0.0, min(1.0, (obiettivo - cum[j]) / seg))
        a, b = punti[j], punti[min(j + 1, len(punti) - 1)]
        out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out


def _uguali(a: dict, b: dict) -> bool:
    if a["sport"] != b["sport"]:
        return False
    da, db = a["dist"] or 0, b["dist"] or 0
    if not da or not db or abs(da - db) / max(da, db) > TOLL_DISTANZA:
        return False
    fa, fb = a["_firma"], b["_firma"]
    if haversine(*fa[0], *fb[0]) > TOLL_PARTENZA_M or haversine(*fa[-1], *fb[-1]) > TOLL_PARTENZA_M:
        return False
    media = sum(haversine(*p, *q) for p, q in zip(fa, fb)) / len(fa)
    return media <= TOLL_TRACCIATO_M


def _nome_manuale(nome: str) -> str | None:
    s = (nome or "").strip()
    return s[1:].strip() if s.startswith("#") and len(s) > 1 else None


def riconosci_percorsi(attivita: list[dict], tracce: dict[str, list]) -> list[dict]:
    cand = []
    for a in sorted(attivita, key=lambda x: x["inizio"]):           # dalla più vecchia
        manuale = _nome_manuale(a.get("nome", ""))
        if manuale and manuale.lower().startswith("escludi"):
            continue
        pts = tracce.get(str(a["id"]))
        if a["sport"] not in FAMIGLIE_PERCORSO and not manuale:
            continue
        cand.append({**a, "_manuale": manuale, "_firma": _firma(pts) if pts and len(pts) >= 10 else None})

    gruppi: list[dict] = []
    per_nome: dict[str, dict] = {}
    # 1) gruppi per nome manuale
    for a in cand:
        if a["_manuale"]:
            k = a["_manuale"].casefold()
            g = per_nome.get(k)
            if not g:
                g = per_nome[k] = {"nome": a["_manuale"], "manuale": True, "membri": [], "rif": None}
                gruppi.append(g)
            g["nome"] = a["_manuale"]                              # vince la grafia più recente
            g["membri"].append(a)
            if g["rif"] is None and a["_firma"]:
                g["rif"] = a
    # 2) riconoscimento automatico per le altre
    for a in cand:
        if a["_manuale"] or not a["_firma"]:
            continue
        for g in gruppi:
            if g["rif"] is not None and _uguali(a, g["rif"]):
                g["membri"].append(a)
                break
        else:
            gruppi.append({"nome": None, "manuale": False, "membri": [a], "rif": a})

    percorsi = []
    for g in gruppi:
        if len(g["membri"]) < 2 and not g["manuale"]:
            continue
        rif = g["rif"] or g["membri"][0]
        nome = g["nome"]
        if not nome:
            km = (rif["dist"] or 0) / 1000
            nome = f"{rif.get('luogo') or 'Percorso'} · {km:.1f} km".replace(".", ",")
        percorsi.append({
            "id": rif["id"],
            "nome": nome,
            "manuale": g["manuale"],
            "sport": rif["sport"],
            "rif": rif["id"],
            "attivita": [m["id"] for m in sorted(g["membri"], key=lambda x: x["inizio"])],
        })
    percorsi.sort(key=lambda p: (-len(p["attivita"]), p["nome"]))
    return percorsi


def da_scaricare(a: dict) -> bool:
    tipo = (a.get("tipo") or "").lower()
    return not any(t in tipo for t in TIPI_SENZA_GPS)
