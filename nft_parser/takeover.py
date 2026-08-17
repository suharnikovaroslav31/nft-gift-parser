from __future__ import annotations

import logging
import os
import sys

from nft_parser.config import Settings, session_file_path
from nft_parser.notifier import Notifier

log = logging.getLogger(__name__)

_password_future = None


def submit_cloud_password(password: str) -> bool:
    return False


def persist_session(session: str) -> None:
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session.strip(), encoding="utf-8")
    os.environ["SESSION_STRING"] = session.strip()


def restart_process() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])


async def takeover_by_qr(settings: Settings, notifier: Notifier) -> str | None:
    log.warning("QR-вход выключен, ссылку не шлю")
    return "QR выключен"
