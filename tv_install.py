"""Installa l'app Canali Sport su una TV LG webOS in modalità sviluppatore, dal server (che è nella stessa rete).

Fa quello che farebbero gli strumenti LG (ares-novacom --getkey + ares-install):
1. scarica la chiave SSH dalla TV (Key Server, porta 9991) e la apre con la passphrase mostrata dalla TV;
2. si collega in SSH alla TV (porta 9922, utente «prisoner»);
3. copia il pacchetto .ipk dell'ultima versione e lo installa con il servizio di installazione di webOS;
4. avvia l'app passandole già l'indirizzo di questo server.
"""
from __future__ import annotations

import io
import json
import logging
import shlex
import socket
import time
import urllib.request

import config
from matches import SSL_CTX

log = logging.getLogger("tv_install")
IPK_URL = "https://github.com/davidetrp/leonesportingclub/releases/download/tv-app/canali-sport-webos.ipk"
APP_ID = "it.leonesport.canalisport"
REMOTE_IPK = "/media/developer/temp/canali-sport.ipk"


class InstallError(Exception):
    pass


def _server_url_for(tv_ip: str) -> str:
    """Indirizzo di questo server visto dalla TV (IP della scheda di rete usata per raggiungerla)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((tv_ip, 9922))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return f"http://{ip}:{config.PORT}"


def install(tv_ip: str, passphrase: str, steps: list[str]) -> dict:
    def step(t):
        steps.append(t)
        log.info("TV LG: %s", t)

    try:
        import paramiko
    except ImportError as e:
        raise InstallError("Manca il pacchetto paramiko: riavvia il server (si installa da solo con l'aggiornamento).") from e

    step("Chiedo la chiave alla TV…")
    try:
        with urllib.request.urlopen(f"http://{tv_ip}:9991/webos_rsa", timeout=8) as r:
            key_pem = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        raise InstallError(f"La TV non risponde ({e}). Controlla l'IP e che «Key Server» sia ON nell'app Developer Mode.") from e
    try:
        key = paramiko.RSAKey.from_private_key(io.StringIO(key_pem), password=passphrase.strip())
    except Exception as e:  # noqa: BLE001
        raise InstallError("Passphrase sbagliata: ricopiala dall'app Developer Mode (6 caratteri, maiuscole).") from e

    step("Mi collego alla TV…")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    last = None
    # Il server SSH delle TV LG è vecchio: se l'autenticazione moderna (rsa-sha2) fallisce, uso ssh-rsa classico.
    for disabled in (None, {"pubkeys": ["rsa-sha2-512", "rsa-sha2-256"]}):
        try:
            ssh.connect(tv_ip, port=9922, username="prisoner", pkey=key, allow_agent=False, look_for_keys=False,
                        timeout=10, banner_timeout=15, auth_timeout=15, disabled_algorithms=disabled)
            last = None
            break
        except Exception as e:  # noqa: BLE001
            last = e
            log.info("TV LG: collegamento SSH non riuscito (%s): %s", "classico" if disabled else "moderno", e)
            ssh.close()
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if last is not None:
        if "host key" in str(last) and int(paramiko.__version__.split(".")[0]) >= 4:
            raise InstallError(f"Collegamento alla TV non riuscito: paramiko {paramiko.__version__} è troppo nuovo per la TV. "
                               "Riavvia il server: installa da solo la versione 3.") from last
        raise InstallError(f"Collegamento alla TV non riuscito: {last}")

    try:
        step("Scarico l'ultima versione dell'app…")
        req = urllib.request.Request(IPK_URL, headers={"User-Agent": "CanaliSport"})
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            ipk = r.read()

        step("Copio l'app sulla TV…")
        ssh.exec_command("mkdir -p /media/developer/temp")[1].channel.recv_exit_status()
        try:
            sftp = ssh.open_sftp()
            with sftp.file(REMOTE_IPK, "wb") as f:
                f.write(ipk)
            sftp.close()
        except Exception:  # noqa: BLE001 - senza SFTP copio passando dal comando cat
            stdin, stdout, _ = ssh.exec_command(f"cat > {REMOTE_IPK}")
            stdin.write(ipk)
            stdin.channel.shutdown_write()
            stdout.channel.recv_exit_status()

        step("Installo…")
        payload = json.dumps({"id": "com.ares.defaultName", "ipkUrl": REMOTE_IPK, "subscribe": True})
        _, stdout, stderr = ssh.exec_command(
            f"luna-send-pub -i -f luna://com.webos.appInstallService/dev/install {shlex.quote(payload)}", timeout=120)
        ok, out, t0 = False, "", time.monotonic()
        for line in iter(stdout.readline, ""):
            out += line
            low = out.lower()
            if '"installed"' in low or "installed" in line.lower() and "state" in low:
                ok = True
                break
            if "fail" in line.lower() or '"returnvalue": false' in low:
                break
            if time.monotonic() - t0 > 110:
                break
        stdout.channel.close()
        if not ok:
            # Il formato dei messaggi cambia tra versioni di webOS: controllo direttamente se l'app ora c'è.
            time.sleep(2)
            q = json.dumps({"id": APP_ID})
            _, so, _ = ssh.exec_command(
                f"luna-send-pub -n 1 luna://com.webos.applicationManager/getAppInfo {shlex.quote(q)}", timeout=20)
            info = so.read().decode("utf-8", "replace")
            log.info("TV LG: risposta installazione %s | getAppInfo %s", out[-300:].replace("\n", " "), info[:200])
            ok = '"returnValue": true' in info or '"returnValue":true' in info
        if not ok:
            err = out[-400:]
            raise InstallError(f"La TV non ha confermato l'installazione: {err.strip() or 'nessuna risposta'}")

        ssh.exec_command(f"rm -f {REMOTE_IPK}")
        server = _server_url_for(tv_ip)
        step(f"Avvio l'app sulla TV (server {server})…")
        params = json.dumps({"id": APP_ID, "params": {"server": server}})
        ssh.exec_command(f"luna-send-pub -n 1 luna://com.webos.applicationManager/launch {shlex.quote(params)}")[1] \
            .channel.recv_exit_status()
        step("✅ Fatto! Canali Sport è installata sulla TV.")
        return {"ok": True, "server": server}
    finally:
        ssh.close()
