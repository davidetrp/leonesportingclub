#!/usr/bin/env bash
# Installa Canali Sport su un server Ubuntu (es. Oracle Cloud «Always Free») con HTTPS automatico
# e aggiornamenti automatici da GitHub.
#
# Uso, sul server:
#   git clone https://github.com/davidetrp/leonesportingclub.git canali-sport
#   cd canali-sport && sudo bash install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

say() { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Esegui come root:  sudo bash install.sh"; exit 1; fi

say "1/6 Installo Docker e git (se mancano)"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ca-certificates curl git
  curl -fsSL https://get.docker.com | sh
fi
command -v git >/dev/null 2>&1 || apt-get install -y git
docker compose version >/dev/null
git config --global --add safe.directory "$DIR"   # la cartella è dell'utente, lo script gira come root

say "2/6 Apro le porte 80 e 443 (sito e certificato HTTPS)"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow OpenSSH; ufw allow 80/tcp; ufw allow 443/tcp
fi
# Le immagini Ubuntu di Oracle Cloud bloccano tutto con iptables tranne SSH: apro 80 e 443.
if command -v iptables >/dev/null 2>&1 && iptables -S INPUT 2>/dev/null | grep -q "REJECT"; then
  for p in 80 443; do
    iptables -C INPUT -p tcp --dport "$p" -m state --state NEW -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 5 -p tcp --dport "$p" -m state --state NEW -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then netfilter-persistent save >/dev/null 2>&1 || true; fi
fi

say "3/6 Impostazioni"
PUBLIC_IP="$(curl -4 -fsS https://api.ipify.org || curl -4 -fsS https://ifconfig.me || true)"
DEFAULT_DOMAIN=""
[ -n "$PUBLIC_IP" ] && DEFAULT_DOMAIN="$(echo "$PUBLIC_IP" | tr . -).sslip.io"
echo "IP pubblico del server: ${PUBLIC_IP:-sconosciuto}"

if [ -f .env ] && grep -q '^TG_API_ID=[1-9]' .env; then
  echo "Trovo già un file .env: lo tengo."
else
  read -rp "api_id di Telegram (da my.telegram.org/apps): " API_ID
  read -rp "api_hash di Telegram: " API_HASH
  while true; do
    read -rsp "Password per entrare nel sito (almeno 10 caratteri): " PASS; echo
    [ ${#PASS} -ge 10 ] && break
    echo "Troppo corta, riprova."
  done
  echo "Dominio: se hai creato un nome su duckdns.org scrivilo (es. leonesport.duckdns.org),"
  echo "altrimenti premi invio e uso quello automatico gratuito."
  read -rp "Dominio del sito [invio = $DEFAULT_DOMAIN]: " DOMAIN
  DOMAIN="${DOMAIN:-$DEFAULT_DOMAIN}"
  if [ -z "$DOMAIN" ]; then echo "Serve un dominio o l'IP pubblico del server."; exit 1; fi
  umask 077
  cat > .env <<ENV
TG_API_ID=$API_ID
TG_API_HASH=$API_HASH
APP_PASSWORD=$PASS
DOMAIN=$DOMAIN
PORT=8080
STREAM_QUALITY=2
DEMO=0
EVENTS_BOT=
ENV
fi
DOMAIN="$(grep '^DOMAIN=' .env | cut -d= -f2-)"
mkdir -p data && chmod 700 data

if [ -n "$PUBLIC_IP" ] && command -v getent >/dev/null 2>&1; then
  RESOLVED="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
  if [ "$RESOLVED" != "$PUBLIC_IP" ]; then
    echo "⚠️  $DOMAIN punta a '${RESOLVED:-niente}', non a $PUBLIC_IP."
    echo "   Su duckdns.org scrivi $PUBLIC_IP nel campo «current ip» del tuo dominio e premi «update ip»."
  fi
fi

say "4/6 Preparo l'app e collego Telegram"
docker compose build
if [ ! -f data/telegram.session ]; then
  echo "Ora Telegram ti chiede numero (con +39), codice (arriva nell'app Telegram) ed eventuale password 2FA."
  docker compose run --rm canali-sport python login.py
fi

say "5/6 Avvio"
docker compose up -d

say "6/6 Aggiornamenti automatici da GitHub (ogni 5 minuti)"
cat > /usr/local/bin/canali-sport-update <<UPD
#!/usr/bin/env bash
cd "$DIR" || exit 0
git fetch -q origin || exit 0
if [ "\$(git rev-parse HEAD)" != "\$(git rev-parse @{u})" ]; then
  git pull -q --ff-only && docker compose up -d --build && echo "Canali Sport aggiornato: \$(git log -1 --format=%s)"
fi
UPD
chmod +x /usr/local/bin/canali-sport-update
cat > /etc/systemd/system/canali-sport-update.service <<'SVC'
[Unit]
Description=Aggiorna Canali Sport da GitHub
[Service]
Type=oneshot
ExecStart=/usr/local/bin/canali-sport-update
SVC
cat > /etc/systemd/system/canali-sport-update.timer <<'TMR'
[Unit]
Description=Controlla gli aggiornamenti di Canali Sport ogni 5 minuti
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
TMR
systemctl daemon-reload
systemctl enable --now canali-sport-update.timer >/dev/null

sleep 5
docker compose ps

printf '\n\033[1;32m✔ Fatto! Apri dal telefono:  https://%s\033[0m\n' "$DOMAIN"
echo "Il certificato HTTPS si crea da solo al primo accesso (può servire un minuto)."
echo "Log:            sudo docker compose logs -f canali-sport"
echo "Aggiornamenti:  automatici ogni 5 minuti (log: journalctl -u canali-sport-update)"
