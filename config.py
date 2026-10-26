"""Impostazioni lette da .env (o dalle variabili d'ambiente)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")

TG_API_ID = int(os.environ.get("TG_API_ID", "0") or 0)
TG_API_HASH = os.environ.get("TG_API_HASH", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
STREAM_QUALITY = int(os.environ.get("STREAM_QUALITY", "2"))   # 0 bassa, 1 media, 2 alta
DEMO = os.environ.get("DEMO", "0") in ("1", "true", "yes", "si", "sì")
DOMAIN = os.environ.get("DOMAIN", "")              # impostato sul server online (Caddy + HTTPS)
PUBLIC = bool(DOMAIN)
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_PATH = DATA_DIR / "telegram"
