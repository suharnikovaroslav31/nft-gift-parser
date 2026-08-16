"""Один раз локально: python gen_session.py

Пишет String Session в data/session_string.txt — её надо вставить
в переменные окружения Bothost как SESSION_STRING. В Git не класть.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

SRC = Path("data/userbot.session")
TMP = Path("data/userbot_export")
OUT = Path("data/session_string.txt")


async def main() -> None:
    if not SRC.exists():
        raise SystemExit("Нет data/userbot.session — сначала залогинь юзербота локально.")
    shutil.copyfile(SRC, TMP.with_suffix(".session"))
    client = TelegramClient(str(TMP), int(os.environ["API_ID"]), os.environ["API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("Сессия не авторизована.")
    OUT.write_text(StringSession.save(client.session), encoding="utf-8")
    await client.disconnect()
    TMP.with_suffix(".session").unlink(missing_ok=True)
    TMP.with_suffix(".session-journal").unlink(missing_ok=True)
    print(f"Готово: {OUT} ({OUT.stat().st_size} байт). Вставь содержимое в SESSION_STRING на Bothost.")


if __name__ == "__main__":
    asyncio.run(main())
