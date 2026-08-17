from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from typing import Any

from nft_parser.config import Settings, session_file_path
from nft_parser.notifier import Notifier

log = logging.getLogger(__name__)

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


def render_qr_png(url: str) -> bytes | None:
    try:
        import qrcode

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        log.exception("Не собрал картинку QR")
        return None


async def send_login_qr(notifier: Notifier, url: str) -> None:
    from aiogram.types import BufferedInputFile

    caption = (
        "Это QR входа юзербота. Бери его здесь, в этом чате.\n\n"
        "1. На телефоне открой Telegram аккаунта ЮЗЕРБОТА (не этот).\n"
        "2. Камера или Настройки → устройства → сканировать QR.\n"
        "3. Подтверди вход.\n\n"
        "Если QR не сканируется — открой ссылку следующим сообщением с того же аккаунта."
    )
    png = render_qr_png(url)
    if png:
        for admin_id in await notifier.recipients():
            photo = BufferedInputFile(png, filename="userbot-qr.png")
            try:
                await notifier.bot.send_photo(admin_id, photo, caption=caption)
            except Exception:
                log.exception("Не отправил QR-фото %s", admin_id)
                try:
                    file = BufferedInputFile(png, filename="userbot-qr.png")
                    await notifier.bot.send_document(admin_id, file, caption=caption)
                except Exception:
                    log.exception("Не отправил QR-файл %s", admin_id)
    await notifier.send_direct(url)


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


async def takeover_by_qr(settings: Settings, notifier: Notifier) -> str | None:
    """Новый вход по QR. Старый ключ не используем — Telegram его не отдаёт второму коннекту."""
    global _password_future
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    if not (settings.api_id and settings.api_hash):
        await notifier.send_direct("Нет API_ID / API_HASH, ссылку входа сделать не могу.")
        return "Нет API_ID / API_HASH для нового входа."

    await notifier.send_direct("Подключаюсь к Telegram, сейчас пришлю QR-картинку в этот чат.")

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
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        qr = await client.qr_login()
        for _ in range(8):
            await send_login_qr(notifier, qr.url)
            try:
                await qr.wait()
                break
            except errors.SessionPasswordNeededError:
                _password_future = asyncio.get_running_loop().create_future()
                await notifier.send_direct("Нужен облачный пароль. Напиши: /2fa пароль")
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
            return "Не подтвердили ссылку вовремя."

        if not await client.is_user_authorized():
            return "QR-вход не завершился."

        session = client.session.save()
        if not session:
            return "Не получил новую SESSION_STRING."
        persist_session(session)
        killed = await kick_other_sessions(client)
        me = await client.get_me()
        await notifier.send_direct(
            f"Юзербот @{me.username or me.id} вошёл заново. "
            f"Отключил старых сессий парсера: {killed}. Перезапускаюсь."
        )
        return None
    except Exception as exc:
        log.exception("QR-вход не удался")
        await notifier.send_direct(f"Не смог сделать ссылку входа: {type(exc).__name__}: {exc}")
        return f"{type(exc).__name__}: {exc}"
    finally:
        _password_future = None
        try:
            await client.disconnect()
        except Exception:
            pass
