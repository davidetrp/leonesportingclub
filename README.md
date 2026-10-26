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

## Sito online gratuito (Oracle Cloud + dominio gratuito)

Così l'app è sempre accesa, anche a Mac spento, con un indirizzo `https://…` da aprire ovunque.

**1. Server gratuito.** Crea un account su [cloud.oracle.com](https://cloud.oracle.com) (piano «Always Free»: chiede una carta solo per verifica, non addebita). Poi *Compute → Instances → Create instance*:
- Image: **Ubuntu 24.04** (o 22.04)
- Shape: **Ampere VM.Standard.A1.Flex**, 1 OCPU e 6 GB di RAM (rientra nel gratuito). Se dice «Out of capacity», riprova più tardi o cambia «Availability domain».
- SSH keys: «Generate a key pair» e **scarica la chiave privata** (file `.key`).
- Dopo la creazione annota il **Public IP address**.

**2. Apri le porte su Oracle.** Nella pagina dell'istanza: *Subnet → Security List → Add Ingress Rules*: Source CIDR `0.0.0.0/0`, protocollo TCP, porte **80,443**. (Il firewall interno del server lo apre lo script.)

**3. Dominio gratuito (facoltativo).** Su [duckdns.org](https://www.duckdns.org) entra con Google/GitHub, crea un nome (es. `leonesport`) e nel campo *current ip* metti l'IP del server → *update ip*. Indirizzo: `leonesport.duckdns.org`. Se salti questo passo, lo script usa `IP-con-trattini.sslip.io`, che funziona subito.

**4. Installa.** Dal Mac:
```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@IP-DEL-SERVER
```
Sul server:
```bash
git clone https://github.com/davidetrp/leonesportingclub.git canali-sport
cd canali-sport && sudo bash install.sh
```
Lo script installa tutto, chiede `api_id`, `api_hash`, la password del sito e il dominio, collega Telegram (numero + codice) e avvia. Si aggiorna da solo da GitHub ogni 5 minuti.

**5. (Facoltativo) Porta i tuoi canali dal Mac**, da un terminale del Mac:
```bash
scp -i ~/Downloads/ssh-key-*.key ~/Downloads/canali-sport/data/canali.json ubuntu@IP-DEL-SERVER:canali-sport/data/
```

Comandi utili sul server, dentro `canali-sport`:

| Cosa | Comando |
|---|---|
| Vedere i log | `sudo docker compose logs -f canali-sport` |
| Riavviare | `sudo docker compose restart` |
| Spegnere | `sudo docker compose down` |

**Sicurezza:** il sito vuole la password, blocca un indirizzo dopo 5 tentativi sbagliati per 15 minuti e non è indicizzato. La cartella `data/` sul server contiene la sessione del tuo account Telegram: non condividerla.

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
