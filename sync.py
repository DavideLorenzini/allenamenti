#!/usr/bin/env python3
"""
Sincronizza le attività Garmin Connect (Personal API ufficiale) e genera
docs/data.enc (riepiloghi) e docs/tracce.enc (tracce GPS): file JSON compressi e CIFRATI (AES-256-GCM, chiave derivata
dalla password con PBKDF2-SHA256), letto dalla dashboard sul telefono.

Uso:
    python sync.py              aggiornamento (elenco attività completo, HRV e tracce GPS incrementali)
    python sync.py --completo   riscarica anche tutto lo storico HRV
    python sync.py --esplora    (solo sul PC, mai su GitHub) salva in ./esplora
                                le risposte grezze dell'API per verificarne i campi

Variabili d'ambiente richieste:
    GARMIN_PERSONAL_API_KEY   chiave creata su Garmin Connect Web
    DASHBOARD_PASSWORD        password con cui cifrare i dati

IMPORTANTE: il repository è pubblico e anche i log di GitHub Actions lo sono.
Questo script non stampa MAI dati personali, solo conteggi.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from garmin_personal_api import (
    GarminPersonalApiClient,
    GarminPersonalApiError,
    HttpError,
    PersonalApiAuthenticator,
)

import percorsi as perc
import sonno as snn

ROOT = Path(__file__).resolve().parent
OUT_FILE = ROOT / "docs" / "data.enc"
TRACCE_FILE = ROOT / "docs" / "tracce.enc"
ABBINAMENTI_FILE = ROOT / "docs" / "abbinamenti.enc"   # scritto dall'app quando modifichi i percorsi
PBKDF2_ITER = 310_000
PAGE_SIZE = 100
MAX_TRACCE_PER_GIRO = 40        # download .FIT per esecuzione: lo storico si completa in più giri
MAX_TENTATIVI_TRACCIA = 3
STORICO_HRV_GG = 365
BLOCCO_HRV_GG = 28              # richieste HRV a blocchi di 4 settimane
PAUSA_S = 0.5                   # pausa tra richieste, per non stressare l'API


# --------------------------------------------------------------------------- #
# Classificazione sport
# --------------------------------------------------------------------------- #
def classifica(type_key: str | None) -> str:
    """Raggruppa i typeKey Garmin nelle 4 famiglie della dashboard."""
    k = (type_key or "").lower()
    if "run" in k:                                   # running, trail_running, treadmill_running...
        return "corsa"
    if "backcountry" in k or "touring" in k or "skimo" in k:
        return "scialpinismo"
    if "ski" in k or "snowboard" in k:               # resort_skiing..., cross_country_skiing...
        return "sci"
    if "hik" in k or "mountaineer" in k:             # hiking, mountaineering
        return "trekking"
    return "altro"


def _num(a: dict, *chiavi: str) -> float | None:
    for k in chiavi:
        v = a.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(float(v), 2)
    return None


def normalizza(a: dict) -> dict | None:
    """Estrae solo i campi usati dalla dashboard. Unità: metri, secondi, m/s."""
    inizio = a.get("startTimeLocal")
    if not inizio or a.get("activityId") is None:
        return None
    t = a.get("activityType")
    type_key = t.get("typeKey") if isinstance(t, dict) else (str(t) if t else None)
    return {
        "id": a.get("activityId"),
        "nome": a.get("activityName") or "",
        "inizio": str(inizio)[:19],                  # "YYYY-MM-DD HH:MM:SS" ora locale
        "tipo": type_key,
        "sport": classifica(type_key),
        "dist": _num(a, "distance"),
        "durata": _num(a, "duration", "elapsedDuration"),
        "mov": _num(a, "movingDuration", "duration"),
        "dplus": _num(a, "elevationGain"),
        "dminus": _num(a, "elevationLoss"),
        "qmax": _num(a, "maxElevation"),
        "fc": _num(a, "averageHR"),
        "fcmax": _num(a, "maxHR"),
        "vmax": _num(a, "maxSpeed"),
        "kcal": _num(a, "calories"),
        "te": _num(a, "aerobicTrainingEffect"),
        "carico": _num(a, "activityTrainingLoad"),
        "vo2": _num(a, "vO2MaxValue"),
        "luogo": a.get("locationName"),
    }


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _lista(resp) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("activityList", "activities", "data"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def scarica_attivita(client: GarminPersonalApiClient, dal: date | None) -> list[dict]:
    grezze, start = [], 0
    for _ in range(500):                             # tetto di sicurezza: 50.000 attività
        pagina = _lista(client.search_activities(start=start, limit=PAGE_SIZE, start_date=dal))
        if not pagina:
            break
        grezze.extend(pagina)
        start += len(pagina)
        time.sleep(PAUSA_S)
    return [n for n in (normalizza(a) for a in grezze) if n]


def scarica_hrv(client: GarminPersonalApiClient, dal: date, al: date) -> list[dict]:
    """HRV notturna. Se l'endpoint non risponde o il formato è diverso, restituisce []."""
    out, inizio = [], dal
    while inizio <= al:
        fine = min(inizio + timedelta(days=BLOCCO_HRV_GG - 1), al)
        try:
            resp = client.get_daily_hrv_summaries(inizio, fine)
        except (HttpError, GarminPersonalApiError) as e:
            print(f"  HRV non disponibile ({type(e).__name__}); sezione saltata.")
            return []
        righe = resp.get("hrvSummaries") if isinstance(resp, dict) else resp
        for r in righe or []:
            if isinstance(r, dict) and r.get("calendarDate"):
                out.append({
                    "data": r["calendarDate"],
                    "notte": _num(r, "lastNightAvg"),
                    "sett": _num(r, "weeklyAvg"),
                    "stato": r.get("status"),
                })
        inizio = fine + timedelta(days=1)
        time.sleep(PAUSA_S)
    return out


def scarica_sonno(client: GarminPersonalApiClient, dal: date, al: date) -> tuple[list[dict], list[str] | None]:
    """Riepiloghi del sonno. Se il formato non è riconosciuto restituisce anche i nomi dei campi trovati."""
    out, diag, inizio = [], None, dal
    while inizio <= al:
        fine = min(inizio + timedelta(days=BLOCCO_HRV_GG - 1), al)
        try:
            resp = client.get_daily_sleep_summaries(inizio, fine)
        except (HttpError, GarminPersonalApiError) as e:
            print(f"  Sonno non disponibile ({type(e).__name__} {getattr(e, 'status_code', '')}); sezione saltata.")
            return out, diag
        righe = snn.normalizza(resp)
        if not righe and resp and diag is None:
            diag = snn.chiavi(resp)
        out += righe
        inizio = fine + timedelta(days=1)
        time.sleep(PAUSA_S)
    return out, (diag if not out else None)


# --------------------------------------------------------------------------- #
# Cifratura (compatibile WebCrypto: PBKDF2-SHA256 -> AES-256-GCM)
# --------------------------------------------------------------------------- #
def _chiave(password: str, salt: bytes, iterazioni: int) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=iterazioni).derive(password.encode("utf-8"))


def cifra(payload: dict, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    chiaro = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    ct = AESGCM(_chiave(password, salt, PBKDF2_ITER)).encrypt(iv, chiaro, None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITER, "gzip": True,
            "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decifra(blob: dict, password: str) -> dict:
    d = lambda s: base64.b64decode(s)
    chiaro = AESGCM(_chiave(password, d(blob["salt"]), int(blob["iter"]))).decrypt(d(blob["iv"]), d(blob["ct"]), None)
    if blob.get("gzip"):
        chiaro = gzip.decompress(chiaro)
    return json.loads(chiaro)


def leggi_precedente(password: str, file: Path = OUT_FILE) -> dict | None:
    if not file.exists():
        return None
    try:
        return decifra(json.loads(file.read_text("utf-8")), password)
    except Exception:
        print(f"  {file.name} non decifrabile (password cambiata?): lo rigenero.")
        return None


def scrivi_se_cambiato(file: Path, payload: dict, prec: dict | None, password: str) -> bool:
    if prec is not None and {k: prec.get(k) for k in payload} == payload:
        return False
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "aggiornato": datetime.now().astimezone().isoformat(timespec="minutes")}
    file.write_text(json.dumps(cifra(payload, password)), "utf-8")
    return True


def aggiorna_tracce(client: GarminPersonalApiClient, attivita: list[dict], prec: dict | None) -> dict:
    """Scarica i .FIT mancanti (al massimo MAX_TRACCE_PER_GIRO per esecuzione)."""
    ids_validi = {str(a["id"]) for a in attivita}
    tracce = {k: v for k, v in (prec or {}).get("tracce", {}).items() if k in ids_validi}
    senza = {k: v for k, v in (prec or {}).get("senza", {}).items() if k in ids_validi}
    mancanti = [a for a in attivita                           # dalla più recente
                if str(a["id"]) not in tracce and senza.get(str(a["id"]), 0) < MAX_TENTATIVI_TRACCIA]
    scaricate = 0
    for a in mancanti:
        k = str(a["id"])
        if not perc.da_scaricare(a):
            senza[k] = 99                                     # attività indoor: niente GPS
            continue
        if scaricate >= MAX_TRACCE_PER_GIRO:
            break
        try:
            contenuto = client.download_activity(a["id"])
            scaricate += 1
        except HttpError as e:
            if e.status_code == 429:
                print("  Limite di richieste Garmin raggiunto: riprendo al prossimo giro.")
                break
            senza[k] = senza.get(k, 0) + 1
            continue
        except GarminPersonalApiError:
            senza[k] = senza.get(k, 0) + 1
            continue
        try:
            pts = perc.estrai_traccia(contenuto)
        except Exception:
            pts = None
            senza[k] = senza.get(k, 0) + 1
        if pts:
            tracce[k] = pts
            senza.pop(k, None)
        elif k not in senza:
            senza[k] = 99                                     # file senza posizioni GPS
        time.sleep(PAUSA_S)
    restano = sum(1 for a in attivita if str(a["id"]) not in tracce and senza.get(str(a["id"]), 0) < MAX_TENTATIVI_TRACCIA)
    print(f"  Tracce: {scaricate} scaricate in questo giro, {len(tracce)} totali, {restano} ancora da scaricare.")
    return {"tracce": tracce, "senza": senza}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def env_obbligatoria(nome: str) -> str:
    v = os.environ.get(nome, "").strip()
    if not v:
        sys.exit(f"ERRORE: variabile d'ambiente {nome} mancante.")
    return v


def connetti() -> GarminPersonalApiClient:
    api_key = env_obbligatoria("GARMIN_PERSONAL_API_KEY")
    try:
        token = PersonalApiAuthenticator().obtain_token(api_key)
    except HttpError as e:
        sys.exit(f"ERRORE autenticazione Garmin (HTTP {e.status_code}): chiave errata o scaduta? "
                 "Rigenerala su Garmin Connect e aggiorna il secret GARMIN_PERSONAL_API_KEY.")
    return GarminPersonalApiClient(token.access_token)


def esplora() -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        sys.exit("--esplora va usato solo sul tuo PC: i log di GitHub Actions sono pubblici.")
    cartella = ROOT / "esplora"
    cartella.mkdir(exist_ok=True)
    oggi = date.today()
    sett_fa = oggi - timedelta(days=7)
    with connetti() as c:
        prove = {
            "attivita_ultime5": lambda: c.search_activities(limit=5),
            "tipi_attivita": c.get_activity_types,
            "hrv_7gg": lambda: c.get_daily_hrv_summaries(sett_fa, oggi),
            "sonno_7gg": lambda: c.get_daily_sleep_summaries(sett_fa, oggi),
            "riepiloghi_giornalieri_7gg": lambda: c.get_daily_summaries(sett_fa, oggi),
            "vo2max_90gg": lambda: c.get_daily_vo2_max(oggi - timedelta(days=90), oggi),
        }
        for nome, f in prove.items():
            try:
                (cartella / f"{nome}.json").write_text(json.dumps(f(), indent=2, ensure_ascii=False), "utf-8")
                print(f"  ok  {nome}.json")
            except Exception as e:
                print(f"  --  {nome}: {type(e).__name__} {getattr(e, 'status_code', '')}")
            time.sleep(PAUSA_S)
    print(f"Fatto. File in {cartella} (non vengono mai caricati su GitHub).")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--completo", action="store_true", help="riscarica tutto lo storico")
    p.add_argument("--esplora", action="store_true", help="salva risposte grezze in ./esplora (solo PC)")
    args = p.parse_args()

    if args.esplora:
        return esplora()

    password = env_obbligatoria("DASHBOARD_PASSWORD")
    prec = leggi_precedente(password)
    prec_tracce = leggi_precedente(password, TRACCE_FILE)       # le tracce già scaricate non si riscaricano mai
    oggi = date.today()

    with connetti() as client:
        # l'elenco attività si riscarica sempre per intero (poche richieste da 100 attività):
        # così rinomine (#percorso) e cancellazioni vengono sempre recepite
        attivita = scarica_attivita(client, None)
        if prec and not args.completo:
            hrv_dal = oggi - timedelta(days=BLOCCO_HRV_GG)
            hrv_nuovo = scarica_hrv(client, hrv_dal, oggi)
            hrv_vecchio = [h for h in prec.get("hrv", []) if h["data"] < hrv_dal.isoformat()]
            hrv = hrv_vecchio + hrv_nuovo if hrv_nuovo else prec.get("hrv", [])
        else:
            hrv = scarica_hrv(client, oggi - timedelta(days=STORICO_HRV_GG), oggi)

        if prec and prec.get("sonno") and not args.completo:
            s_dal = oggi - timedelta(days=BLOCCO_HRV_GG)
            s_nuovo, sonno_diag = scarica_sonno(client, s_dal, oggi)
            sonno = [x for x in prec["sonno"] if x["data"] < s_dal.isoformat()] + s_nuovo if s_nuovo else prec["sonno"]
        else:
            sonno, sonno_diag = scarica_sonno(client, oggi - timedelta(days=STORICO_HRV_GG), oggi)
        sonno = sorted({x["data"]: x for x in sonno}.values(), key=lambda x: x["data"])

        # deduplica per id, ordina dalla più recente
        attivita = sorted({a["id"]: a for a in attivita}.values(), key=lambda a: a["inizio"], reverse=True)
        hrv = sorted({h["data"]: h for h in hrv}.values(), key=lambda h: h["data"])
        tracce = aggiorna_tracce(client, attivita, prec_tracce)

    abb = leggi_precedente(password, ABBINAMENTI_FILE) or {}
    percorsi = perc.riconosci_percorsi(attivita, tracce["tracce"], abb)
    con_traccia = set(tracce["tracce"])
    for a in attivita:
        a["gps"] = str(a["id"]) in con_traccia

    cambiati = []
    if scrivi_se_cambiato(OUT_FILE, {"attivita": attivita, "hrv": hrv, "percorsi": percorsi,
                                     "sonno": sonno, "sonno_diag": sonno_diag}, prec, password):
        cambiati.append("data.enc")
    if scrivi_se_cambiato(TRACCE_FILE, tracce, prec_tracce, password):
        cambiati.append("tracce.enc")
    if sonno_diag:
        print(f"  Sonno: formato non riconosciuto, {len(sonno_diag)} nomi di campo salvati per la diagnostica.")
    print(f"{len(attivita)} attività, {len(hrv)} giorni HRV, {len(sonno)} notti, {len(percorsi)} percorsi ricorrenti. "
          f"File aggiornati: {', '.join(cambiati) or 'nessuno'}.")


if __name__ == "__main__":
    main()
