# Dashboard allenamenti – Garmin → telefono

Come funziona: ogni 2 ore GitHub esegue `sync.py`. Lo script scarica le attività dalla **Garmin Connect Personal API** ufficiale, le cifra con la tua password e pubblica il file `docs/data.enc`. Il telefono apre la dashboard (`docs/index.html`), che decifra i dati in locale. Il servizio è gratuito e non richiede server da gestire.

```
Orologio → Garmin Connect → (ogni 2h) GitHub Actions: sync.py → docs/data.enc (cifrato) → GitHub Pages → telefono
```

Famiglie di sport riconosciute dal `typeKey` Garmin:

| Famiglia | Tipi Garmin inclusi |
|---|---|
| Corsa | tutti i tipi con "run" (running, trail_running, treadmill_running…) |
| Scialpinismo | backcountry / touring / skimo |
| Sci | gli altri tipi con "ski" o "snowboard" (pista, fondo) |
| Trekking | hiking, mountaineering |
| Altro | tutto il resto (camminate, bici, palestra…): compare in "Tutto" |

---

## 1. Crea la chiave Garmin (5 minuti)

1. Accedi a Garmin Connect **da computer** e apri: <https://connect.garmin.com/app/settings/personalApiToken>
2. Clicca **Create Key**, accetta i termini e scegli una scadenza. Le opzioni sono 30, 90 o 180 giorni oppure 1 anno: conviene **1 anno**.
3. **Copia subito la chiave**, perché Garmin la mostra una sola volta. Conservala come una password.

## 2. Crea il repository su GitHub

1. Crea un account su <https://github.com> se non lo hai.
2. Crea un nuovo repository **New repository**, chiamato ad esempio `allenamenti`, con visibilità **Public**.
   GitHub Pages è gratuito solo sui repository pubblici. Il codice sarà visibile a chiunque, ma i tuoi dati restano cifrati con AES-256 e senza password sono illeggibili.
3. Nel repository clicca **Add file → Upload files** e trascina **tutto il contenuto** della cartella `garmin-dashboard`, compresa la cartella nascosta `.github`.
   Su Mac il Finder nasconde le cartelle che iniziano con il punto: premi `Cmd + Shift + .` per mostrarle.
   Controlla che dopo il caricamento esista il file `.github/workflows/aggiorna.yml`.

## 3. Inserisci i due "secrets"

Vai su **Settings → Secrets and variables → Actions → New repository secret** e crea:

| Nome | Valore |
|---|---|
| `GARMIN_PERSONAL_API_KEY` | la chiave Garmin del punto 1 |
| `DASHBOARD_PASSWORD` | una password lunga a tua scelta (almeno 16 caratteri, ad esempio una frase) |

## 4. Primo scaricamento completo

1. Apri la scheda **Actions** e, se richiesto, abilita i workflow.
2. Seleziona **Aggiorna dashboard → Run workflow**, spunta **Riscarica tutto lo storico** e conferma.
3. Dopo 1–2 minuti l'esecuzione deve risultare verde ✅ e nel repository compare `docs/data.enc`.
   Se è rossa ❌, aprila e leggi l'errore. Il caso più frequente è una chiave copiata male. I log non contengono mai i tuoi dati, solo dei conteggi.

## 5. Attiva la pagina web

1. Vai su **Settings → Pages**.
2. In **Source** seleziona **Deploy from a branch**, poi branch `main` e cartella `/docs`. Clicca **Save**.
3. Dopo 1–2 minuti la dashboard è disponibile su `https://<tuo-utente>.github.io/allenamenti/`.

## 6. Installala sul telefono

- **iPhone (Safari):** apri l'indirizzo, tocca **Condividi → Aggiungi alla schermata Home**.
- **Android (Chrome):** apri l'indirizzo, poi **⋮ → Installa app** (oppure "Aggiungi a schermata Home").

Al primo avvio inserisci la `DASHBOARD_PASSWORD` e lascia spuntato "Ricorda su questo dispositivo".

---

## Cosa mostra

- **Filtri:** sport (Tutto, Corsa, Scialpinismo, Sci, Trekking) e periodo (7 giorni, 30 giorni, Anno, Stagione invernale dal 1° novembre, 12 mesi).
- **KPI** confrontati con lo stesso periodo dell'anno precedente:
  - Corsa: distanza, uscite, tempo, D+, passo medio, FC media
  - Scialpinismo: D+, uscite, tempo, distanza, D+ per ora, quota massima
  - Sci: D−, giorni, distanza, velocità massima
  - Trekking: distanza, D+, tempo, uscite, quota massima, FC media
- **Volume per settimana:** ultime 16 settimane. Nella vista "Tutto" sono ore impilate per sport.
- **Mese per mese:** anno in corso contro anno precedente. Per sci e scialpinismo il confronto è tra stagioni, da novembre a ottobre.
- **Andamento:** passo per uscita (corsa) e D+ per ora (scialpinismo), con media mobile sulle ultime 5 uscite. Nella vista "Tutto" compare la HRV notturna, se disponibile.
- **Record** su tutto lo storico e **ultime 20 attività**.

## Note tecniche e limiti

- **Campi delle attività:** la documentazione pubblica di Garmin elenca gli endpoint ma non lo schema delle risposte. I campi usati in `sync.py` (`distance`, `duration`, `movingDuration`, `elevationGain`, `elevationLoss`, `maxElevation`, `averageHR`, `maxSpeed`, …) sono quelli del formato attività di Garmin Connect. Vanno quindi verificati al primo giro: se un valore risulta vuoto o sbagliato, esegui il controllo descritto sotto.
- **HRV:** è opzionale. Se l'endpoint non risponde o ha un formato diverso, la sezione viene saltata senza bloccare il resto.
- **Controllo dei campi (facoltativo, solo dal PC con Python 3.12 o superiore):**
  ```
  pip install -r requirements.txt
  export GARMIN_PERSONAL_API_KEY="la-tua-chiave"      # su Windows: set GARMIN_PERSONAL_API_KEY=...
  python sync.py --esplora
  ```
  Il comando crea la cartella `esplora/` con le risposte grezze dell'API. Quella cartella non va mai caricata su GitHub ed è già esclusa da `.gitignore`.
- **Scadenza della chiave:** Garmin invia un'email circa una settimana prima. Per rinnovarla, crea una nuova chiave e aggiorna il secret `GARMIN_PERSONAL_API_KEY`.
- **Errori:** se il workflow fallisce, GitHub ti avvisa via email.
- **Workflow sospeso:** sui repository pubblici GitHub sospende i workflow programmati dopo 60 giorni senza attività nel repository. Ogni nuovo allenamento genera un aggiornamento, quindi succede solo in caso di lunghe pause. In quel caso basta riattivarlo dalla scheda Actions.
- **Cambio password:** aggiorna il secret `DASHBOARD_PASSWORD` e lancia il workflow con "Riscarica tutto lo storico".
- **Aggiornamento manuale:** Actions → Run workflow. Sulla dashboard il tasto ↻ ricarica i dati.
- Strava non viene usato. Garmin è la fonte originale dei dati e la sua Personal API non richiede abbonamenti.
