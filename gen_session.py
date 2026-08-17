"""Один раз локально: python gen_session.py

Пишет String Session в data/session_string.txt — её надо вставить
в переменные окружения Bothost как SESSION_STRING. В Git не класть.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from telethon.sessions import SQLiteSession, StringSession

SRC = Path("data/userbot.session")
TMP = Path("data/userbot_export")
OUT = Path("data/session_string.txt")


def main() -> None:
    if not SRC.exists():
        raise SystemExit("Нет data/userbot.session — сначала залогинь юзербота локально.")
    shutil.copyfile(SRC, TMP.with_suffix(".session"))
    session = SQLiteSession(str(TMP))
    raw = StringSession.save(session)
    session.close()
    TMP.with_suffix(".session").unlink(missing_ok=True)
    TMP.with_suffix(".session-journal").unlink(missing_ok=True)
    if not raw:
        raise SystemExit("В session нет ключа авторизации.")
    OUT.write_text(raw, encoding="utf-8")
    print(f"Готово: {OUT} ({len(raw)} символов). Вставь содержимое файла в SESSION_STRING на Bothost.")


if __name__ == "__main__":
    main()
