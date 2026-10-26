"""Avvia Canali Sport e lo tiene aggiornato da GitHub.

Uso (da qualsiasi shell, anche fish):
    ~/Downloads/canali-sport/.venv/bin/python ~/Downloads/canali-sport/avvia.py

Cosa fa:
- avvia il server (server.py) e lo riavvia se si ferma;
- ogni minuto controlla GitHub: se c'è una versione nuova la scarica (git pull),
  reinstalla le dipendenze se sono cambiate e riavvia il server;
- sul Mac impedisce lo stop automatico finché è acceso (caffeinate).
"""
from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK_EVERY_S = int(os.environ.get("CHECK_EVERY_S", "60"))
PY = sys.executable


def say(msg: str) -> None:
    print(f"[avvia {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=60)


def has_git() -> bool:
    try:
        return git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def update_available() -> bool:
    if git("fetch", "--quiet").returncode != 0:
        return False
    local = git("rev-parse", "HEAD").stdout.strip()
    remote = git("rev-parse", "@{u}").stdout.strip()
    return bool(local and remote and local != remote)


def pull() -> bool:
    before = git("rev-parse", "HEAD").stdout.strip()
    res = git("pull", "--ff-only", "--quiet")
    if res.returncode != 0:
        say("Aggiornamento non riuscito: " + (res.stderr.strip() or res.stdout.strip()))
        return False
    changed = git("diff", "--name-only", before, "HEAD").stdout.split()
    msg = git("log", "-1", "--format=%s").stdout.strip()
    say(f"Aggiornato: {msg}")
    if "requirements.txt" in changed:
        say("Dipendenze cambiate: le reinstallo…")
        subprocess.run([PY, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=HERE)
    return True


def start_server() -> subprocess.Popen:
    say("Avvio il server")
    return subprocess.Popen([PY, "server.py"], cwd=HERE)


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    os.chdir(HERE)
    keep_awake = None
    if platform.system() == "Darwin":
        keep_awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])

    auto_update = has_git()
    if auto_update:
        say("Aggiornamenti automatici da GitHub: attivi")
        if update_available():
            pull()
    else:
        say("Questa cartella non è collegata a GitHub: aggiornamenti automatici spenti")

    proc = start_server()
    last_check = time.monotonic()
    try:
        while True:
            time.sleep(2)
            if proc.poll() is not None:
                say(f"Il server si è fermato (codice {proc.returncode}): lo riavvio tra 5 secondi")
                time.sleep(5)
                proc = start_server()
            if auto_update and time.monotonic() - last_check > CHECK_EVERY_S:
                last_check = time.monotonic()
                try:
                    if update_available() and pull():
                        stop_server(proc)
                        proc = start_server()
                except subprocess.SubprocessError as e:
                    say(f"Controllo aggiornamenti saltato: {e}")
    except KeyboardInterrupt:
        say("Chiudo")
    finally:
        stop_server(proc)
        if keep_awake:
            keep_awake.terminate()


if __name__ == "__main__":
    main()
