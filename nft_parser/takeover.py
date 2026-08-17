from __future__ import annotations

import asyncio
import html
import logging
import os
import sys
from typing import Any

from nft_parser.config import Settings, session_file_path
from nft_parser.emoji import pe
from nft_parser.notifier import Notifier

log = logging.getLogger(__name__)

# Официальные приложения Telegram — их не сбрасываем.
OFFICIAL_API_IDS = {
    1,
    4,
    5,
    6,
    7,
    8,
    2040,
    2496,
    2834,
    9452,
    10840,
    21724,
}

_password_future: asyncio.Future[str] | None = None


def submit_cloud_password(password: str) -> bool:
    future = _password_future
    if future is None or future.done():
        return False
    future.set_result(password.strip())
    return True


def persist_session(session: str) -> None:
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session.strip(), encoding="utf-8")
    os.environ["SESSION_STRING"] = session.strip()


def restart_process() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])


async def kick_other_sessions(client: Any) -> int:
    from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest

    result = await client(GetAuthorizationsRequest())
    killed = 0
    for auth in result.authorizations:
        if getattr(auth, "current", False):
            continue
        api_id = int(getattr(auth, "api_id", 0) or 0)
        if api_id in OFFICIAL_API_IDS:
            continue
        try:
            await client(ResetAuthorizationRequest(hash=auth.hash))
            killed += 1
            log.info("Сбросил чужую сессию api_id=%s %s", api_id, getattr(auth, "app_name", ""))
        except Exception:
            log.exception("Не сбросил сессию api_id=%s", api_id)
    return killed


async def _edit_or_send(notifier: Notifier, posts: list[tuple[int, int]], text: str) -> list[tuple[int, int]]:
    if posts:
        for chat_id, message_id in posts:
            try:
                await notifier.bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("Не обновил QR-сообщение")
        return posts
    return await notifier.send_direct(text)


async def takeover_by_qr(settings: Settings, notifier: Notifier) -> str | None:
    """Новый вход по QR. Старый ключ не используем — Telegram его не отдаёт второму коннекту."""
    global _password_future
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    if not (settings.api_id and settings.api_hash):
        return "Нет API_ID / API_HASH для нового входа."

    client = TelegramClient(
        StringSession(),
        settings.api_id,
        settings.api_hash,
        proxy=settings.telethon_proxy(),
        device_model="GiftHunter",
        system_version="Linux",
        app_version="1.0",
        connection_retries=5,
        timeout=30,
        use_ipv6=False,
        flood_sleep_threshold=24 * 60 * 60,
    )
    posts: list[tuple[int, int]] = []
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        qr = await client.qr_login()
        for _ in range(8):
            text = (
                f'{pe("warn")} <b>Нужно подтвердить юзербота</b>\n\n'
                "Старый ключ занят, поэтому делаю <b>новый вход</b> и потом отключу "
                "старые сессии парсера. Телефон и Telegram на компе не трогаю.\n\n"
                "1. На телефоне переключись на аккаунт юзербота (не этот чат).\n"
                "2. Открой ссылку:\n"
                f"<code>{html.escape(qr.url)}</code>\n\n"
                "Если попросит облачный пароль — напиши сюда <code>/2fa пароль</code>."
            )
            posts = await _edit_or_send(notifier, posts, text)
            try:
                await qr.wait()
                break
            except errors.SessionPasswordNeededError:
                _password_future = asyncio.get_running_loop().create_future()
                await notifier.send_text(
                    f'{pe("warn")} Нужен облачный пароль 2FA юзербота.\n'
                    "Напиши: <code>/2fa пароль</code>"
                )
                try:
                    password = await asyncio.wait_for(_password_future, timeout=180)
                except asyncio.TimeoutError:
                    return "Не дождался облачного пароля."
                finally:
                    _password_future = None
                await client.sign_in(password=password)
                break
            except asyncio.TimeoutError:
                await qr.recreate()
        else:
            return "Не подтвердили QR вовремя."

        if not await client.is_user_authorized():
            return "QR-вход не завершился."

        session = client.session.save()
        if not session:
            return "Не получил новую SESSION_STRING."
        persist_session(session)
        killed = await kick_other_sessions(client)
        me = await client.get_me()
        await notifier.send_text(
            f'{pe("check")} Юзербот @{html.escape(me.username or str(me.id))} вошёл заново.\n'
            f"Отключил старых сессий парсера: <b>{killed}</b>.\n"
            "Перезапускаюсь…"
        )
        return None
    except Exception as exc:
        log.exception("QR-вход не удался")
        return f"{type(exc).__name__}: {exc}"
    finally:
        _password_future = None
        try:
            await client.disconnect()
        except Exception:
            pass
