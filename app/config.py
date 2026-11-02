"""Project-wide constants for the training app."""

import os
from pathlib import Path

from dotenv import load_dotenv

from garmin_auth import DEFAULT_TOKEN_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_DIR = DEFAULT_TOKEN_DIR
DEFAULT_SYNC_WINDOW_DAYS = 35

load_dotenv(PROJECT_ROOT / ".env")

# APP_DB_PATH lets tests and throwaway servers use their own database file.
DB_PATH = Path(os.environ.get("APP_DB_PATH") or PROJECT_ROOT / "data" / "garmin.db")

APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

if not APP_USERNAME or not APP_PASSWORD:
    raise RuntimeError(
        "APP_USERNAME and APP_PASSWORD must be set (in .env or the environment) -- "
        "this app binds to the LAN and has no other protection."
    )

# Optional: DeepSeek-backed conversational coach. When unset, the coach falls
# back to the fixed templates in app/coach/responder.py so the app still
# works without a key.
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or None
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# Look of the web app: the name in the greeting, and an optional personal
# background photo (kept in data/, never in git). BACKGROUND_PHOTO is a file,
# or a folder whose newest picture is used -- drop in a new photo to change it.
ATHLETE_NAME = os.environ.get("ATHLETE_NAME", "Matas")
BACKGROUND_PHOTO = Path(os.environ.get("BACKGROUND_PHOTO") or PROJECT_ROOT / "data" / "background")
PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def background_photo() -> Path | None:
    p = BACKGROUND_PHOTO
    if p.is_dir():
        pics = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in PHOTO_TYPES]
        return max(pics, key=lambda f: f.stat().st_mtime) if pics else None
    return p if p.is_file() and p.suffix.lower() in PHOTO_TYPES else None
