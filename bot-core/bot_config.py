import os
from dotenv import load_dotenv

load_dotenv()

def get_int(key, default=0):
    val = os.getenv(key)
    if not val or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default

class Config:
    DISCORD_TOKEN       = os.getenv("DISCORD_TOKEN")

    GOOGLE_DRIVE_FOLDER_ID      = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    GOFILE_TOKEN                = os.getenv("GOFILE_TOKEN")
    PROXY                       = os.getenv("PROXY")

    GUILD_ID          = get_int("DISCORD_GUILD_ID")
    ERROR_CHANNEL_ID  = get_int("DISCORD_ERROR_CHANNEL_ID")

    ALLOWED_USER_IDS = [
        int(uid.strip())
        for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if uid.strip() and uid.strip().isdigit()
    ]

    WEB_PANEL_SECRET = os.getenv("WEB_PANEL_SECRET")

    # --- HF Worker Config ---
    HF_WORKER_URL = os.getenv("HF_WORKER_URL")
    HF_WORKER_KEY = os.getenv("HF_WORKER_KEY") or WEB_PANEL_SECRET

    SUWAYOMI_URL = os.getenv("SUWAYOMI_URL") or "http://localhost:4567"

    # --- Inpainting Space Config ---
    INPAINTING_SPACE_URL = os.getenv("INPAINTING_SPACE_URL")  # e.g. https://mmo9-inpainting-bot.hf.space
    INPAINTING_SPACE_KEY = os.getenv("INPAINTING_SPACE_KEY")  # must match INPAINTING_API_KEY in the Space
    HF_TOKEN             = os.getenv("HF_TOKEN", "")

    @classmethod
    def is_allowed(cls, user_id):
        return user_id in cls.ALLOWED_USER_IDS
