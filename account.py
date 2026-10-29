"""Impostazioni salvate sul server e cambio dell'account Telegram dall'app (numero → codice → password 2FA).

La nuova sessione si prepara in un file a parte: quella in uso viene sostituita solo quando l'accesso è riuscito.
"""
from __future__ import annotations

import json
import logging
import os
import re

import config

log = logging.getLogger("account")

SETTINGS_FILE = config.DATA_DIR / "impostazioni.json"
NEW_SESSION = config.DATA_DIR / "telegram-nuovo"
SUGGESTED_BOT = "CherryStreamingDbot"
BOT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


# --------------------------------------------------------------------------- impostazioni

def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes) -> dict:
    data = {**load_settings(), **changes}
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, SETTINGS_FILE)
    return data


def events_bot() -> str:
    """Bot con la lista eventi: quello scelto nell'app, altrimenti EVENTS_BOT di .env. Vuoto = spento."""
    s = load_settings()
    if "events_bot" in s:
        return str(s["events_bot"] or "")
    return config.EVENTS_BOT


def clean_bot(name: str) -> str | None:
    name = (name or "").strip().lstrip("@")
    m = re.match(r"^(?:https?://)?t\.me/([^/?#]+)", name)
    if m:
        name = m.group(1)
    return name if BOT_RE.match(name) else None


def mask_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 6:
        return ""
    return f"+{digits[:2]} ••• ••• {digits[-3:]}"


def clean_phone(phone: str) -> str | None:
    p = re.sub(r"[\s().-]", "", phone or "")
    if p.startswith("00"):
        p = "+" + p[2:]
    if not p.startswith("+"):
        p = "+39" + p.lstrip("0") if len(p) == 10 and p.startswith("3") else "+" + p
    return p if re.fullmatch(r"\+\d{8,15}", p) else None


# --------------------------------------------------------------------------- accesso a Telegram

class AccountLogin:
    """Un accesso in corso alla volta: send_code → code → (password)."""

    def __init__(self) -> None:
        self.client = None
        self.phone = ""
        self.code_hash = ""
        self.step = ""          # "", "code", "password"

    async def cancel(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.client, self.phone, self.code_hash, self.step = None, "", "", ""
        for ext in (".session", ".session-journal"):
            try:
                os.remove(str(NEW_SESSION) + ext)
            except OSError:
                pass

    async def send_code(self, phone: str) -> dict:
        from telethon import TelegramClient, errors

        if not config.TG_API_ID or not config.TG_API_HASH:
            return {"error": "Mancano TG_API_ID e TG_API_HASH nel file .env del server."}
        await self.cancel()
        client = TelegramClient(str(NEW_SESSION), config.TG_API_ID, config.TG_API_HASH)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except errors.PhoneNumberInvalidError:
            await client.disconnect()
            return {"error": "Numero non valido. Scrivilo con il prefisso, es. +39 333 1234567."}
        except errors.PhoneNumberBannedError:
            await client.disconnect()
            return {"error": "Telegram ha bloccato questo numero."}
        except errors.FloodWaitError as e:
            await client.disconnect()
            return {"error": f"Troppi tentativi: Telegram fa aspettare {e.seconds // 60 + 1} minuti."}
        except Exception as e:  # noqa: BLE001
            await client.disconnect()
            log.warning("Invio codice non riuscito: %s", e)
            return {"error": f"Codice non inviato: {e}"}
        self.client, self.phone, self.code_hash, self.step = client, phone, sent.phone_code_hash, "code"
        return {"step": "code"}

    async def sign_in_code(self, code: str):
        """Restituisce l'utente se l'accesso è completo, altrimenti un dict con step o error."""
        from telethon import errors

        if self.step != "code" or not self.client:
            return {"error": "Richiedi prima il codice."}
        code = re.sub(r"\D", "", code or "")
        if not code:
            return {"error": "Scrivi il codice arrivato su Telegram."}
        try:
            return await self.client.sign_in(phone=self.phone, code=code, phone_code_hash=self.code_hash)
        except errors.SessionPasswordNeededError:
            self.step = "password"
            return {"step": "password"}
        except errors.PhoneCodeInvalidError:
            return {"error": "Codice sbagliato, ricontrollalo."}
        except errors.PhoneCodeExpiredError:
            await self.cancel()
            return {"error": "Codice scaduto: richiedine uno nuovo."}
        except errors.FloodWaitError as e:
            return {"error": f"Troppi tentativi: Telegram fa aspettare {e.seconds // 60 + 1} minuti."}

    async def sign_in_password(self, password: str):
        from telethon import errors

        if self.step != "password" or not self.client:
            return {"error": "Richiedi prima il codice."}
        if not password:
            return {"error": "Scrivi la password della verifica in due passaggi."}
        try:
            return await self.client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            return {"error": "Password sbagliata."}
        except errors.FloodWaitError as e:
            return {"error": f"Troppi tentativi: Telegram fa aspettare {e.seconds // 60 + 1} minuti."}

    async def take_session(self) -> None:
        """Chiude il client nuovo (salva la sessione) e la mette al posto di quella in uso."""
        if self.client:
            await self.client.disconnect()
        self.client, self.step, self.code_hash = None, "", ""
        src = str(NEW_SESSION) + ".session"
        dst = str(config.SESSION_PATH) + ".session"
        os.replace(src, dst)
        try:
            os.remove(str(NEW_SESSION) + ".session-journal")
        except OSError:
            pass
