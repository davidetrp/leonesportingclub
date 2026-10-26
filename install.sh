#!/usr/bin/env bash
# Installa Canali Sport su un server Ubuntu/Debian (VPS) con HTTPS automatico.
# Uso (come root, dentro la cartella canali-sport):   bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Esegui come root:  sudo bash install.sh"; exit 1; fi

say "1/5 Installo Docker (se manca)"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ca-certificates curl
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

say "2/5 Apro le porte 80 e 443 nel firewall (se attivo)"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow OpenSSH; ufw allow 80/tcp; ufw allow 443/tcp
fi

say "3/5 Impostazioni"
PUBLIC_IP="$(curl -4 -fsS https://api.ipify.org || curl -4 -fsS https://ifconfig.me || true)"
DEFAULT_DOMAIN=""
[ -n "$PUBLIC_IP" ] && DEFAULT_DOMAIN="$(echo "$PUBLIC_IP" | tr . -).sslip.io"

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
ENV
fi
DOMAIN="$(grep '^DOMAIN=' .env | cut -d= -f2-)"
mkdir -p data && chmod 700 data

say "4/5 Preparo l'app e collego Telegram"
docker compose build
if [ ! -f data/telegram.session ]; then
  echo "Ora Telegram ti chiede numero (con +39), codice (arriva nell'app Telegram) ed eventuale password 2FA."
  docker compose run --rm canali-sport python login.py
fi

say "5/5 Avvio"
docker compose up -d
sleep 5
docker compose ps

printf '\n\033[1;32m✔ Fatto! Apri dal telefono:  https://%s\033[0m\n' "$DOMAIN"
echo "Il certificato HTTPS si crea da solo al primo accesso (può servire un minuto)."
echo "Log:       docker compose logs -f canali-sport"
echo "Aggiorna:  docker compose up -d --build"
