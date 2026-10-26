# Canali Sport

App con tutti i canali che vuoi (ordinati per spettatori, con preferiti e partite del giorno) che mostra **dentro l'app** le dirette dei tuoi canali Telegram privati, senza aprire Telegram.

Come funziona: un piccolo server, collegato al tuo account Telegram, entra nella diretta come spettatore muto, scarica il flusso a pezzi da 1 secondo e lo trasforma in uno stream video (HLS) che il telefono riproduce. Il server è protetto da password ed è pensato per uso personale.

```
Canale Telegram ──► server (tuo account) ──► ffmpeg ──► HLS ──► app sul telefono
```

## Cosa serve

- Un computer acceso mentre guardi: il tuo PC, un mini PC, un Raspberry Pi 4/5 o un VPS.
- **Python 3.10+** e **ffmpeg** installati (oppure Docker).
- Le credenziali API di Telegram: vai su <https://my.telegram.org> → *API development tools* → crea un'app e copia `api_id` e `api_hash`.

## Installazione

```bash
cd canali-sport
python -m venv .venv
# Windows: .venv\Scripts\activate    Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # su Windows: copy .env.example .env
```

Apri `.env` e compila `TG_API_ID`, `TG_API_HASH` e `APP_PASSWORD`.

ffmpeg: su Windows `winget install Gyan.FFmpeg`, su Mac `brew install ffmpeg`, su Linux `sudo apt install ffmpeg`.

### 1. Prova la demo (facoltativo, senza Telegram)

Metti `DEMO=1` in `.env`, avvia `python server.py` e apri l'app: i 5 canali trasmettono un segnale di prova. Se lo vedi, la parte video funziona. Poi rimetti `DEMO=0`.

### 2. Collega il tuo account Telegram (una volta sola)

```bash
python login.py
```

Ti chiede numero di telefono, il codice che arriva su Telegram e l'eventuale password a due fattori. La sessione resta salvata in `data/`. Tieni privata questa cartella: dà accesso al tuo account.

### 3. Avvia il server

```bash
.venv/bin/python avvia.py
```

`avvia.py` avvia il server, lo riavvia se si ferma e, se la cartella è collegata a GitHub, ogni minuto scarica da solo le nuove versioni e riavvia. Sul Mac impedisce anche lo stop automatico. Funziona da qualsiasi shell, fish compresa. Per avviare solo il server senza aggiornamenti: `python server.py`.

### 4. Apri l'app dal telefono

- Stessa Wi-Fi: `http://IP-DEL-COMPUTER:8080` (l'IP lo trovi con `ipconfig` su Windows o `ifconfig`/`ip a` su Mac/Linux).
- Inserisci la password di `.env`.
- Tocca **+ Aggiungi canale**, scegli il canale Telegram dall'elenco, dai un nome e salva. Aggiungine quanti ne vuoi.
- Per averla come app: in Safari *Condividi → Aggiungi alla schermata Home*, in Chrome *⋮ → Installa app*.

### Fuori casa

Non aprire la porta del router su internet. Il modo più semplice e sicuro è **Tailscale** (gratis): installalo sul computer e sul telefono, poi apri `http://NOME-COMPUTER:8080` dal telefono ovunque tu sia.

## Sito online privato (server VPS con HTTPS)

Per aprire l'app da qualsiasi rete con un indirizzo `https://…`, senza tenere acceso il tuo computer.

1. Prendi un VPS con **Ubuntu 24.04** (es. Hetzner CAX11 o CX22, pochi euro al mese) e annota il suo IP pubblico.
2. Dal tuo computer copia il progetto sul server ed entra:
   ```bash
   scp canali-sport.zip root@IP-DEL-SERVER:
   ssh root@IP-DEL-SERVER
   ```
3. Sul server:
   ```bash
   apt-get update && apt-get install -y unzip
   unzip canali-sport.zip && cd canali-sport
   bash install.sh
   ```
   Lo script installa Docker, ti chiede `api_id`, `api_hash`, la password del sito e il dominio, collega Telegram e avvia tutto.
4. Apri dal telefono l'indirizzo che stampa alla fine.

**Dominio:** se non ne hai uno, premi Invio e lo script usa `IP-con-trattini.sslip.io` (es. `https://5-75-1-20.sslip.io`), che funziona subito con HTTPS. Se hai un dominio tuo, crea un record **A** (es. `tv.tuodominio.it`) che punta all'IP del server e scrivilo quando lo script lo chiede.

**Sicurezza:** il sito vuole la password (obbligatoria online), blocca un indirizzo dopo 5 tentativi sbagliati per 15 minuti, non è indicizzato dai motori di ricerca e l'app non è esposta direttamente: passa tutto da Caddy in HTTPS. La cartella `data/` sul server contiene la sessione del tuo account Telegram, quindi non darla a nessuno e usa una password del server robusta (o meglio una chiave SSH).

Comandi utili sul server, dentro `canali-sport`:

| Cosa | Comando |
|---|---|
| Vedere i log | `docker compose logs -f canali-sport` |
| Riavviare | `docker compose restart` |
| Aggiornare dopo aver sostituito i file | `docker compose up -d --build` |
| Spegnere | `docker compose down` |

## Cosa aspettarsi

- **Quali dirette funzionano:** quelle trasmesse in streaming RTMP (da OBS, vMix o un encoder: in Telegram si avviano con *Trasmetti con…*). Le video chat avviate dal telefono con la fotocamera usano un altro sistema (WebRTC) e l'app ti avvisa che non sono leggibili.
- **Ritardo:** circa 5–8 secondi rispetto a Telegram.
- **Spettatore:** il tuo account compare tra chi sta guardando la diretta, come se la aprissi da Telegram.
- **Consumo:** il server non ricodifica se la diretta è già H.264/AAC (il caso normale), quindi gira anche su un Raspberry Pi. Ogni canale si ferma da solo 45 secondi dopo che smetti di guardarlo.
- **Qualità:** `STREAM_QUALITY` in `.env` (0 bassa, 1 media, 2 alta). Usa 1 se guardi spesso con i dati mobili.

## Problemi comuni

| Messaggio nell'app | Cosa fare |
|---|---|
| Account Telegram non collegato | Esegui `python login.py` sul server e riavvialo. |
| Nessuna diretta ora | Il canale non sta trasmettendo. Lo stato sul telecomando si aggiorna ogni 20 s. |
| Diretta non leggibile | È una video chat dal telefono, non uno stream RTMP. |
| ffmpeg non riesce a leggere i chunk | Controlla che `ffmpeg -version` funzioni nel terminale del server. Manda le ultime righe del terminale per capire il formato. |
| Il video non parte su iPhone | Aggiorna iOS. L'app usa il lettore HLS integrato di Safari. |

## File

- `server.py` – server web, password, API e stream HLS
- `telegram_live.py` – trova la diretta, entra come spettatore muto, scarica i pezzi del flusso
- `chunks.py` – decodifica il formato dei pezzi delle dirette Telegram
- `streamer.py` – trasforma i pezzi in HLS con ffmpeg (e la sorgente demo)
- `static/index.html` – l'app con telecomando e lettore video
- `login.py` – primo accesso a Telegram
- `install.sh`, `docker-compose.yml`, `Caddyfile` – installazione sul server online con HTTPS
