#!/usr/bin/env python3
"""
Sincronizza le attività Garmin Connect (Personal API ufficiale) e genera
docs/data.enc: un file JSON compresso e CIFRATO (AES-256-GCM, chiave derivata
dalla password con PBKDF2-SHA256), letto dalla dashboard sul telefono.

Uso:
    python sync.py              aggiornamento incrementale (ultimi 30 giorni)
    python sync.py --completo   riscarica tutto lo storico
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

ROOT = Path(__file__).resolve().parent
OUT_FILE = ROOT / "docs" / "data.enc"
PBKDF2_ITER = 310_000
PAGE_SIZE = 100
FINESTRA_INCREMENTALE_GG = 30   # attività riscaricate a ogni giro (cattura modifiche/cancellazioni)
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


def leggi_precedente(password: str) -> dict | None:
    if not OUT_FILE.exists():
        return None
    try:
        return decifra(json.loads(OUT_FILE.read_text("utf-8")), password)
    except Exception:
        print("  File precedente non decifrabile (password cambiata?): riscarico tutto.")
        return None


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
    prec = None if args.completo else leggi_precedente(password)
    oggi = date.today()

    with connetti() as client:
        if prec:
            taglio = oggi - timedelta(days=FINESTRA_INCREMENTALE_GG)
            nuove = scarica_attivita(client, taglio)
            vecchie = [a for a in prec.get("attivita", []) if a["inizio"][:10] < taglio.isoformat()]
            attivita = vecchie + nuove
            hrv_dal = oggi - timedelta(days=BLOCCO_HRV_GG)
            hrv_nuovo = scarica_hrv(client, hrv_dal, oggi)
            hrv_vecchio = [h for h in prec.get("hrv", []) if h["data"] < hrv_dal.isoformat()]
            hrv = hrv_vecchio + hrv_nuovo if hrv_nuovo else prec.get("hrv", [])
        else:
            attivita = scarica_attivita(client, None)
            hrv = scarica_hrv(client, oggi - timedelta(days=STORICO_HRV_GG), oggi)

    # deduplica per id, ordina dalla più recente
    attivita = sorted({a["id"]: a for a in attivita}.values(), key=lambda a: a["inizio"], reverse=True)
    hrv = sorted({h["data"]: h for h in hrv}.values(), key=lambda h: h["data"])

    payload = {"attivita": attivita, "hrv": hrv}
    if prec and {"attivita": prec.get("attivita"), "hrv": prec.get("hrv")} == payload:
        print(f"Nessuna novità ({len(attivita)} attività). File invariato.")
        return

    payload["aggiornato"] = datetime.now().astimezone().isoformat(timespec="minutes")
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(cifra(payload, password)), "utf-8")
    print(f"Aggiornato: {len(attivita)} attività, {len(hrv)} giorni HRV.")


if __name__ == "__main__":
    main()
