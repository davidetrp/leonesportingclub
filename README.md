# Leone Sporting Club

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

Apri `.env` e compila `TG_API_ID` e `TG_API_HASH`.

ffmpeg: su Windows `winget install Gyan.FFmpeg`, su Mac `brew install ffmpeg`, su Linux `sudo apt install ffmpeg`.

### 1. Prova la demo (facoltativo, senza Telegram)

Metti `DEMO=1` in `.env`, avvia `python server.py` e apri l'app: i 5 canali trasmettono un segnale di prova. Se lo vedi, la parte video funziona. Poi rimetti `DEMO=0`.

### 2. Collega il tuo account Telegram (una volta sola)

Dall'app, con il telefono sulla Wi-Fi di casa: **⚙️ Impostazioni → Account Telegram → Collega account**, scrivi il numero, il codice che arriva su Telegram e l'eventuale password a due fattori. Da lì puoi anche cambiare account quando vuoi. In alternativa, sul server: `python login.py`.

 La sessione resta salvata in `data/`. Tieni privata questa cartella: dà accesso al tuo account.

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

## App per TV (Fire TV Stick e LG webOS)

Le app per TV mostrano gli stessi canali e le stesse partite del sito, con il telecomando:

| Tasto | Cosa fa |
|---|---|
| Frecce | si spostano tra canali, partite e impostazioni (riquadro arancione) |
| OK | apre il canale; OK di nuovo sullo stesso canale = video a tutto schermo |
| Su / Giù (a schermo intero) o CH+ / CH− | canale precedente / successivo |
| Indietro | esce dallo schermo intero; dalla schermata dei canali chiude l'app |
| Menu ≡ (solo Fire TV) | cambia indirizzo del server |

Le app vengono compilate da GitHub a ogni modifica (release «tv-app»). Il tuo server ha due indirizzi corti per scaricarle:
`http://IP-DEL-SERVER:8080/tv` (Fire TV) e `http://IP-DEL-SERVER:8080/tv/webos` (LG). Con il server online usa il tuo dominio, es. `https://leonesport.duckdns.org/tv`.

### Fire TV Stick (con Downloader)

1. **Attiva le opzioni sviluppatore** (una volta sola): *Impostazioni → La mia Fire TV → Informazioni* → seleziona il nome del dispositivo (es. «Fire TV Stick 4K») e premi **OK 7 volte** finché compare «Ora sei uno sviluppatore».
2. **Installa Downloader**: dalla schermata principale cerca **Downloader** (icona arancione, di AFTVnews) → *Scarica*.
3. **Consenti a Downloader di installare app**: *Impostazioni → La mia Fire TV → Opzioni sviluppatore → Installa app sconosciute* → metti **Downloader** su **ON**. (Sui modelli più vecchi: *Opzioni sviluppatore → App di origine sconosciuta → ON*.)
4. **Scarica l'app**: apri Downloader, consenti l'accesso ai file se lo chiede, e nella casella dell'indirizzo scrivi
   `http://IP-DEL-SERVER:8080/tv` (es. `http://192.168.1.20:8080/tv`) → **Go**.
5. Finito il download si apre l'installazione: **Installa** → **Fine**. Quando Downloader chiede se cancellare il file, rispondi **Elimina** (non serve più).
6. **Apri Canali Sport** (lo trovi in *App e canali*; tieni premuto il tasto Home per vederle tutte). La prima volta scrivi l'indirizzo del server, es. `http://192.168.1.20:8080`, poi **Collega** e inserisci la password del sito.
7. **Aggiornare**: ripeti il passo 4 quando vuoi la versione nuova; l'app si installa sopra quella vecchia e tiene le impostazioni.

### TV LG webOS (modalità sviluppatore)

LG permette di installare app fuori dallo store solo con la sua **modalità sviluppatore** (gratuita). Serve il Mac una volta.

1. **Account sviluppatore LG**: crealo gratis su <https://webostv.developer.lge.com> (*Login → Create Account*).
2. **Sulla TV**: apri il **LG Content Store**, cerca **Developer Mode**, installala e aprila. Accedi con l'account del passo 1, attiva **Dev Mode Status** (la TV si riavvia), riaprila e attiva **Key Server**. Annota l'**IP** e la **passphrase** che mostra.
3. **Sul Mac**, nel Terminale (TV e Mac sulla stessa rete):
   ```bash
   npm install -g @webos-tools/cli
   ares-setup-device -a tv -i "host=IP-DELLA-TV" -i "port=9922" -i "username=prisoner"
   ares-novacom --device tv --getkey        # quando chiede la passphrase, scrivi quella mostrata dalla TV
   curl -L -o canali-sport-webos.ipk http://localhost:8080/tv/webos
   ares-install --device tv canali-sport-webos.ipk
   ```
4. Sulla TV compare **Canali Sport** tra le app. La prima volta scrivi l'indirizzo del server, poi **Collega**.
5. **La modalità sviluppatore scade dopo 50 ore**: apri l'app *Developer Mode* sulla TV e premi **Extend** (basta farlo ogni tanto). Se scade, l'app viene tolta e va reinstallata col passo 3.

## Cosa aspettarsi

- **Quali dirette funzionano:** quelle trasmesse in streaming RTMP (da OBS, vMix o un encoder: in Telegram si avviano con *Trasmetti con…*). Le video chat avviate dal telefono con la fotocamera usano un altro sistema (WebRTC) e l'app ti avvisa che non sono leggibili.
- **Ritardo:** circa 5–8 secondi rispetto a Telegram.
- **Spettatore:** il tuo account compare tra chi sta guardando la diretta, come se la aprissi da Telegram.
- **Consumo:** il server non ricodifica se la diretta è già H.264/AAC (il caso normale), quindi gira anche su un Raspberry Pi. Ogni canale si ferma da solo 45 secondi dopo che smetti di guardarlo.
- **Qualità:** `STREAM_QUALITY` in `.env` (0 bassa, 1 media, 2 alta). Usa 1 se guardi spesso con i dati mobili.

## Nome della partita dal video

Mentre usi l'app, il server guarda il riquadro del punteggio **in alto a sinistra** del video di ogni canale in diretta e lo abbina alle partite di oggi: nella lista compare «Juventus – Inter 1-0» al posto del nome del canale, con il nome Telegram sotto (📡). I canali aperti si rileggono ogni 2 minuti; quelli non aperti ogni 10 minuti, entrando un attimo nella diretta per prendere un solo pezzo di video.

Sul Mac usa il riconoscimento testo di macOS (lo prepara da solo al primo avvio, se ci sono gli strumenti per sviluppatori di Apple, gli stessi che chiede Homebrew). Se non riesce, usa Tesseract: `brew install tesseract`.

## Problemi comuni

| Messaggio nell'app | Cosa fare |
|---|---|
| Account Telegram non collegato | ⚙️ Impostazioni → Account Telegram → Collega account (da casa). |
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
