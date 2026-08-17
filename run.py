from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from nft_parser.app import run
from nft_parser.config import Settings, clean_session_value, session_file_path

log = logging.getLogger(__name__)

_CANON = {
    "BOT_TOKEN": ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "TOKEN"),
    "API_ID": ("API_ID", "TELEGRAM_API_ID", "TG_API_ID", "APP_ID"),
    "API_HASH": ("API_HASH", "TELEGRAM_API_HASH", "TG_API_HASH", "APP_HASH"),
    "SESSION_STRING": (
        "SESSION_STRING",
        "STRING_SESSION",
        "TELEGRAM_SESSION",
        "TELEGRAM_SESSION_STRING",
        "USERBOT_SESSION",
    ),
    "PHONE": ("PHONE", "TELEGRAM_PHONE", "TG_PHONE"),
    "ADMIN_IDS": ("ADMIN_IDS", "ADMIN_ID", "ADMINS"),
}


def _norm(key: str) -> str:
    return key.strip().upper().replace("-", "_").replace(" ", "_")


def _clean(value: str) -> str:
    return value.strip().strip('"').strip("'").replace("\r", "").replace("\n", "")


def hydrate_env() -> None:
    try:
        from dotenv import load_dotenv

        for path in (
            ".env",
            "/app/.env",
            "/usr/src/app/.env",
            str(Path(os.getenv("DATA_DIR", "data")) / ".env"),
        ):
            load_dotenv(path, override=False)
    except ImportError:
        pass

    by_norm: dict[str, str] = {}
    for key, raw in os.environ.items():
        if raw is None:
            continue
        value = _clean(str(raw))
        if value:
            by_norm[_norm(key)] = value

    for dest, aliases in _CANON.items():
        current = _clean(os.environ.get(dest, ""))
        if current:
            os.environ[dest] = current
            continue
        found = ""
        for alias in aliases:
            found = by_norm.get(alias, "")
            if found:
                break
        if not found:
            for name, value in by_norm.items():
                if dest in name:
                    found = value
                    break
        if found:
            os.environ[dest] = found

    chunks: list[str] = []
    for index in range(1, 6):
        piece = clean_session_value(os.environ.get(f"SESSION_STRING_{index}", "") or by_norm.get(f"SESSION_STRING_{index}", ""))
        if piece:
            chunks.append(piece)
    joined = "".join(chunks)
    env_session = clean_session_value(os.environ.get("SESSION_STRING", ""))
    file_session = ""
    for path in (
        session_file_path(),
        Path("/app/data/session_string.txt"),
        Path("/usr/src/app/data/session_string.txt"),
        Path("session_string.txt"),
    ):
        if path.is_file():
            file_session = clean_session_value(path.read_text(encoding="utf-8"))
            if file_session:
                break
    best = max((item for item in (env_session, joined, file_session) if item), key=len, default="")
    if best:
        os.environ["SESSION_STRING"] = best


def setup_logging() -> None:
    root = Path(os.getenv("DATA_DIR", "data"))
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(root / "parser.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


def main() -> None:
    print("NFT Gift Hunter boot 17aug-e", flush=True)
    hydrate_env()
    setup_logging()
    settings = Settings()
    env_session = os.environ.get("SESSION_STRING", "").strip()
    if env_session and len(env_session) > len(settings.session_string.strip()):
        settings.session_string = env_session
    interesting = sorted(
        key
        for key in os.environ
        if any(
            part in key.upper()
            for part in ("API", "SESSION", "PHONE", "ADMIN", "BOT_TOKEN", "DATA_DIR", "PORT", "TELEGRAM")
        )
    )
    log.info("env keys: %s", ", ".join(interesting) or "—")
    log.info(
        "boot token=%s api_id=%s hash=%s session_len=%s phone=%s userbot=%s data_dir=%s port=%s",
        bool(settings.bot_token),
        bool(settings.api_id),
        bool(settings.api_hash),
        len(settings.session_string.strip()),
        bool(settings.phone),
        settings.has_userbot,
        os.getenv("DATA_DIR"),
        os.getenv("PORT"),
    )
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
