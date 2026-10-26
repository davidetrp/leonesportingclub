"""Primo accesso a Telegram: chiede numero, codice e (se c'è) la password 2FA.

Esegui una volta sola:  python login.py
La sessione viene salvata in data/telegram.session e il server la riusa.
"""
import asyncio

from telethon import TelegramClient

import config


async def main() -> None:
    if not config.TG_API_ID or not config.TG_API_HASH:
        raise SystemExit("Imposta TG_API_ID e TG_API_HASH nel file .env (da https://my.telegram.org).")
    client = TelegramClient(str(config.SESSION_PATH), config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"\nAccesso fatto come {me.first_name} (@{me.username or '-'}).")
    print("Ora avvia il server con:  python server.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
