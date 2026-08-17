from __future__ import annotations

import asyncio
import html
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from nft_parser.bot import AppMiddleware, router
from nft_parser.emoji import pe
from nft_parser.catalog import CATALOG, tracker_usernames
from nft_parser.config import Settings, load_live_session
from nft_parser.db import Database
from nft_parser.models import Hit, ProfileGifts
from nft_parser.notifier import Notifier
from nft_parser.takeover import persist_session
from nft_parser.urllib_session import UrllibSession
from nft_parser.web_feed import PublicFeed, is_deposit_owner, web_chat_id, web_user_id, why_not_noob

log = logging.getLogger(__name__)


def make_bot(token: str) -> Bot:
    kwargs: dict[str, Any] = {
        "default": DefaultBotProperties(parse_mode=ParseMode.HTML),
    }
    if not os.getenv("DATA_DIR") and not os.getenv("PORT"):
        kwargs["session"] = UrllibSession()
    return Bot(token, **kwargs)


async def bothost_health() -> None:
    raw = (os.getenv("PORT") or "").strip()
    if not raw.isdigit():
        log.info("PORT нет — health-сервер не поднимаю")
        await asyncio.Future()
        return
    port = int(raw)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(4096)
            body = b"ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 2\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("Bothost health 0.0.0.0:%s", port)
    async with server:
        await server.serve_forever()


@dataclass
class App:
    settings: Settings
    db: Database
    bot: Bot
    dp: Dispatcher
    notifier: Notifier
    feed: PublicFeed
    userbot: Any = None
    queue: Any = None
    scanner: Any = None
    market: Any = None
    gifts: Any = None


async def build_web_app(settings: Settings) -> App:
    db = Database(settings.db_path)
    await db.connect()
    for admin_id in settings.admin_id_list():
        await db.add_admin(admin_id)

    bot = make_bot(settings.bot_token)
    dp = Dispatcher()
    notifier = Notifier(bot, db, settings.admin_id_list())
    feed = PublicFeed()
    app = App(
        settings=settings,
        db=db,
        bot=bot,
        dp=dp,
        notifier=notifier,
        feed=feed,
    )
    middleware = AppMiddleware(app)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    dp.include_router(router)
    return app


async def ensure_catalog(db: Database) -> None:
    existing = {(c.get("username") or "").lower() for c in await db.list_chats()}
    added = 0
    for item in CATALOG:
        if item["username"].lower() in existing:
            continue
        await db.add_chat(web_chat_id(item["username"]), item["title"], item["username"])
        added += 1
    if added:
        log.info("Добавлены каналы из каталога: %s", added)


async def apply_people_mode(db: Database) -> None:
    if await db.get_setting("people_mode") == "10":
        return
    await db.update_filters(
        newbie_only=True,
        newbie_max=2,
        require_username=True,
        skip_sold=True,
        skip_listed=False,
        show_sold=False,
        show_listed=True,
        chats_enabled=False,
        market_enabled=True,
        min_price_ton=0.0,
        recent_hours=504,
    )
    await db.set_setting("people_mode", "10")
    await db.set_running(True)
    log.info("Режим: только кто недавно получил подарок и не шарит рынок")


async def apply_userbot_mode(db: Database) -> None:
    await db.update_filters(
        newbie_only=True,
        newbie_max=4,
        require_username=True,
        skip_sold=True,
        chats_enabled=True,
        market_enabled=True,
        min_price_ton=0.0,
        recent_hours=0,
        check_senders=True,
        check_gift_links=True,
        max_tg_level=3,
        max_gift_usd=30.0,
        max_gift_ton=15.0,
        cheap_list_ton=8.0,
    )
    await db.set_setting("people_mode", "13")
    await db.set_running(True)
    log.info("Новички: ≤4 NFT, lvl≤3, дешёвые подарки / дешёвый листинг")


def _min_price_ok(deal_price: float, min_price: float) -> bool:
    if not min_price:
        return True
    return deal_price >= min_price


def _reason_for(deal_kind: str, price: float, asset: str) -> str:
    price_s = f" · {price:g} {asset}".strip() if price else ""
    if deal_kind == "person":
        return f"живой человек в чате, светит NFT{price_s}"
    if deal_kind == "buy":
        return f"свежая сделка{price_s}"
    return f"выставили{price_s}"


async def emit_person(
    app: App,
    *,
    uname: str | None,
    display: str | None,
    gift,
    source: str,
    source_label: str,
    reason: str,
    just_bought: bool,
    newbie_max: int,
    newbie_only: bool,
) -> bool:
    from nft_parser.web_feed import _clean_user

    uname = _clean_user(uname)
    if display and is_deposit_owner(display):
        display = None
    if not uname or gift is None or not gift.slug:
        return False
    if await app.feed.username_is_channel(uname):
        log.info("Пропуск канала @%s", uname)
        return False
    count = await app.db.remember_user_gift(uname, gift.slug)
    if count > newbie_max:
        log.info("Пропуск @%s: уже %s NFT (лимит %s)", uname, count, newbie_max)
        return False
    is_newbie = count <= newbie_max
    uid = web_user_id(uname)
    fingerprint = f"p:{uname.lower()}"
    if not await app.db.should_notify(uid, fingerprint, 24):
        return False
    await app.db.touch_user(uid, uname, display or uname, count, fingerprint)
    await app.notifier.send_hit(
        Hit(
            profile=ProfileGifts(
                user_id=uid,
                username=uname,
                first_name=display or uname,
                last_name="",
                unique=[gift],
                total_unique=count,
            ),
            source=source,
            source_label=source_label,
            reason=reason,
            is_newbie=is_newbie,
            just_bought=just_bought,
        )
    )
    return True


async def web_loop(app: App) -> None:
    announced = False
    live_n = 8
    while True:
        try:
            if await app.db.is_running():
                filters = await app.db.get_filters()
                newbie_max = int(filters.get("newbie_max") or 5)
                newbie_only = bool(filters.get("newbie_only", True))
                sent = 0
                scanned = 0

                for row in await app.db.list_pending_owners(20):
                    if sent >= 15:
                        break
                    card = await app.feed.gift_card(row["slug"])
                    await app.db.bump_pending_owner(row["slug"])
                    uname, display = card.owner, card.owner_name
                    if uname:
                        skip = why_not_noob(
                            uname,
                            card,
                            slug=row["slug"],
                            title=row["title"],
                            price=float(row["price"] or 0),
                            asset=row["asset"] or "",
                            deal_kind=row["kind"] or "sold",
                        )
                        if skip:
                            log.info("Пропуск @%s: %s", uname, skip)
                            await app.db.drop_pending_owner(row["slug"])
                        else:
                            from nft_parser.models import GiftInfo

                            gift = GiftInfo(
                                title=row["title"] or row["slug"],
                                slug=row["slug"],
                                num=int(row["num"] or 0),
                                received_at=card.gifted_at,
                            )
                            ok = await emit_person(
                                app,
                                uname=uname,
                                display=display,
                                gift=gift,
                                source="nft_owner",
                                source_label="карточка NFT",
                                reason="недавно получил подарок, сам владелец",
                                just_bought=False,
                                newbie_max=newbie_max,
                                newbie_only=newbie_only,
                            )
                            if ok:
                                sent += 1
                            await app.db.drop_pending_owner(row["slug"])
                    await asyncio.sleep(0.25)

                for username in tracker_usernames():
                    if sent >= 15:
                        break
                    posts = await app.feed.channel_posts(username, pages=1)
                    scanned += 1
                    log.info("Скан @%s: постов %s", username, len(posts))
                    live = posts[-live_n:] if len(posts) > live_n else posts
                    live_keys = {post.key for post in live}
                    for post in posts:
                        deals = app.feed.parse_deals(username, post)
                        for deal in deals:
                            if await app.db.seen_market(deal.key):
                                continue
                            await app.db.mark_market(deal.key)
                            if post.key not in live_keys or not deal.gift.slug:
                                continue
                            if deal.kind == "sold":
                                continue
                            card = await app.feed.gift_card(deal.gift.slug)
                            uname, display = card.owner, card.owner_name
                            if not uname:
                                await asyncio.sleep(0.1)
                                continue
                            skip = why_not_noob(
                                uname,
                                card,
                                slug=deal.gift.slug,
                                title=deal.gift.title,
                                price=deal.price,
                                asset=deal.asset,
                                deal_kind=deal.kind,
                            )
                            if skip:
                                log.info("Пропуск @%s: %s", uname, skip)
                                await asyncio.sleep(0.1)
                                continue
                            deal.gift.received_at = card.gifted_at
                            ok = await emit_person(
                                app,
                                uname=uname,
                                display=display,
                                gift=deal.gift,
                                source="nft_owner",
                                source_label=f"@{username}",
                                reason="недавно получил подарок и сам его держит",
                                just_bought=False,
                                newbie_max=newbie_max,
                                newbie_only=newbie_only,
                            )
                            if ok:
                                sent += 1
                            await asyncio.sleep(0.1)
                    await asyncio.sleep(0.4)
                log.info("Цикл людей: каналов %s, карточек %s", scanned, sent)
                if not announced:
                    await app.notifier.send_text(
                        "🎁 Ищу тех, кто в NFT не шарит: свежий Gifted to, сам владелец, не киты и не маркет."
                    )
                    announced = True
        except Exception:
            log.exception("Ошибка поиска людей")
        await asyncio.sleep(max(15, app.settings.market_poll_sec))


async def run(settings: Settings) -> None:
    if settings.has_userbot:
        await run_userbot(settings)
        return
    log.warning(
        "Юзербот выключен: api_id=%s hash=%s session=%s phone=%s DATA_DIR=%s",
        bool(settings.api_id),
        bool(settings.api_hash),
        bool(settings.session_string.strip()),
        bool(settings.phone),
        bool(os.getenv("DATA_DIR")),
    )
    log.info("Запуск без юзербота: публичные ленты t.me/s/…")
    app = await build_web_app(settings)
    await ensure_catalog(app.db)
    await apply_people_mode(app.db)
    bot_me = None
    for attempt in range(5):
        try:
            bot_me = await app.bot.get_me()
            break
        except Exception as exc:
            log.warning("getMe попытка %s: %s", attempt + 1, exc)
            await asyncio.sleep(3)
    if bot_me is None:
        log.error("Не удалось достучаться до api.telegram.org. Включите VPN и перезапустите.")
        await app.feed.close()
        await app.bot.session.close()
        await app.db.close()
        return
    log.info("Бот: @%s", bot_me.username)
    await app.notifier.send_text(
        f'{pe("warn")} <b>Юзербот не залогинен</b>\n'
        "Админка работает, парсер нет.\n"
        f"API_ID: <b>{'есть' if settings.api_id else 'нет'}</b>\n"
        f"API_HASH: <b>{'есть' if settings.api_hash else 'нет'}</b>\n"
        f"SESSION_STRING: <b>{'есть (' + str(len(settings.session_string.strip())) + ' симв.)' if settings.session_string.strip() else 'нет'}</b>\n"
        "Если SESSION_STRING «нет» — пришли боту /setsession и строку из session_string.txt."
    )
    try:
        await asyncio.gather(
            app.dp.start_polling(app.bot, polling_timeout=0, handle_signals=False),
            web_loop(app),
            app.notifier.pace_loop(),
            bothost_health(),
        )
    finally:
        await app.feed.close()
        await app.bot.session.close()
        await app.db.close()


TG_DC = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}


async def connect_userbot(userbot: Any) -> None:
    try:
        await asyncio.wait_for(userbot.connect(), timeout=30)
        return
    except Exception:
        log.warning("Первый коннект юзербота не прошёл, пробую официальный DC")
    dc = int(getattr(userbot.session, "dc_id", 0) or 2)
    ip, port = TG_DC.get(dc, TG_DC[2])
    try:
        userbot.session.set_dc(dc, ip, port)
    except Exception:
        log.exception("Не сменил DC")
    await asyncio.wait_for(userbot.connect(), timeout=30)


def acquire_userbot_lock():
    path = Path(os.getenv("DATA_DIR", "data")) / "userbot.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
    except OSError:
        handle.close()
        return None


async def authorize_userbot(userbot: Any) -> str | None:
    from telethon import errors, functions

    await connect_userbot(userbot)
    try:
        await asyncio.wait_for(userbot(functions.updates.GetStateRequest()), timeout=30)
        return None
    except errors.AuthKeyDuplicatedError:
        log.warning("AuthKeyDuplicated — сразу новый вход")
        try:
            await userbot.disconnect()
        except Exception:
            pass
        return "AuthKeyDuplicated"
    except errors.AuthKeyUnregisteredError:
        try:
            await userbot.disconnect()
        except Exception:
            pass
        return "AuthKeyUnregistered"
    except errors.FloodWaitError as exc:
        wait = min(int(getattr(exc, "seconds", 5) or 5) + 1, 90)
        log.warning("GetState FloodWait %sс, жду", wait)
        await asyncio.sleep(wait)
        try:
            await asyncio.wait_for(userbot(functions.updates.GetStateRequest()), timeout=30)
            return None
        except Exception as retry_exc:
            return f"{type(retry_exc).__name__}: {retry_exc}"
    except errors.RPCError as exc:
        return f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        log.exception("GetState упал")
        return f"{type(exc).__name__}: {exc}"


async def run_userbot(settings: Settings) -> None:
    from telethon import TelegramClient

    from nft_parser.gifts import GiftService
    from nft_parser.marketplace import Marketplace
    from nft_parser.scanner import ChatScanner, CheckQueue

    db = Database(settings.db_path)
    await db.connect()
    for admin_id in settings.admin_id_list():
        await db.add_admin(admin_id)

    lock_fp = acquire_userbot_lock()
    if lock_fp is None:
        log.error("Юзербот уже запущен в другом процессе этого контейнера")

    live = load_live_session(settings.session_string)
    if live:
        settings.session_string = live

    userbot = TelegramClient(
        settings.telethon_session(),
        settings.api_id,
        settings.api_hash,
        proxy=settings.telethon_proxy(),
        device_model="Desktop",
        system_version="Windows 10",
        app_version="5.0",
        connection_retries=5,
        timeout=30,
        use_ipv6=False,
        flood_sleep_threshold=24 * 60 * 60,
    )
    bot = make_bot(settings.bot_token)
    dp = Dispatcher()
    queue = CheckQueue(settings.check_delay_sec)
    gifts = GiftService(userbot, max_gifts=settings.max_gifts_fetch)
    market = Marketplace(settings.portals_header(), settings.tonnel_auth)
    notifier = Notifier(bot, db, settings.admin_id_list())
    scanner = ChatScanner(
        client=userbot,
        db=db,
        gifts=gifts,
        queue=queue,
        on_hit=notifier.send_hit,
        cooldown_hours=settings.user_cooldown_hours,
        notify_hours=settings.notify_cooldown_hours,
    )
    app = App(
        settings=settings,
        db=db,
        bot=bot,
        dp=dp,
        notifier=notifier,
        feed=PublicFeed(),
        userbot=userbot,
        queue=queue,
        scanner=scanner,
        market=market,
        gifts=gifts,
    )
    middleware = AppMiddleware(app)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    dp.include_router(router)

    async def login_userbot() -> None:
        log.info("Логин юзербота…")
        try:
            if lock_fp is None:
                await notifier.send_text(
                    f'{pe("warn")} <b>Юзербот не залогинен</b>\n'
                    "В контейнере уже есть второй процесс с этой сессией. "
                    "На Bothost должен быть один бот, без своего Dockerfile если main.py запускается сам."
                )
                return
            if settings.session_string.strip():
                log.info("SESSION_STRING длина=%s", len(settings.session_string.strip()))
                noticed = False
                while True:
                    why = await authorize_userbot(userbot)
                    if not why:
                        break
                    log.error("Юзербот оффлайн: %s", why)
                    try:
                        await userbot.disconnect()
                    except Exception:
                        pass
                    if not noticed:
                        await notifier.send_direct(
                            "Юзербот оффлайн, жду сессию. QR не отправляю — карточки пойдут сами, как только коннект оживёт."
                        )
                        noticed = True
                    await asyncio.sleep(20)
                await asyncio.wait_for(userbot.start(), timeout=45)
                try:
                    saved = userbot.session.save()
                    if saved:
                        persist_session(saved)
                except Exception:
                    log.exception("Не сохранил живую сессию")
            else:
                await notifier.send_direct(
                    "Нет SESSION_STRING. Юзербот не парсит. QR больше не отправляю."
                )
                return
        except Exception as exc:
            log.exception("Не удалось залогинить юзербота")
            try:
                err = html.escape(f"{type(exc).__name__}: {str(exc)[:180]}")
                await notifier.send_text(
                    f'{pe("warn")} <b>Юзербот не залогинен</b>\n'
                    "Панель бота работает, парсер нет.\n"
                    f"ошибка: <code>{err}</code>"
                )
            except Exception:
                pass
            return

        await apply_userbot_mode(db)
        await scanner.start()
        me = await userbot.get_me()
        scanner_id = int(me.id)
        await db.set_setting("scanner_id", str(scanner_id))
        await db.set_setting("scanner_username", me.username or "")
        await db.remove_admin(scanner_id)
        notifier.skip_ids.add(scanner_id)
        owner = await db.get_setting("owner_id")
        if owner == str(scanner_id):
            await db.set_setting("owner_id", "")
            owner = ""
        panel = [uid for uid in await db.list_admins() if uid != scanner_id]
        if panel and not owner:
            await db.set_setting("owner_id", str(panel[0]))
        log.info(
            "Юзербот @%s парсит. Админка — бот @parsers_informain_bot, напиши /start с личного аккаунта",
            me.username or scanner_id,
        )
        await asyncio.gather(_worker(app), _market(app), _bootstrap(app, me, scanner, db, notifier))

    log.info("Стартую панель бота…")
    try:
        await asyncio.gather(
            dp.start_polling(bot),
            notifier.pace_loop(),
            login_userbot(),
            bothost_health(),
        )
    finally:
        await app.feed.close()
        await market.close()
        await bot.session.close()
        await userbot.disconnect()
        await db.close()
        if lock_fp is not None:
            try:
                lock_fp.close()
            except Exception:
                pass


async def _bootstrap(app: App, me: Any, scanner: Any, db: Database, notifier: Notifier) -> None:
    removed = await db.purge_synthetic_chats()
    if removed:
        log.info("Убрал фейковые web-чаты: %s", removed)
    ok, fail = await scanner.join_catalog(CATALOG)
    log.info("Каталог юзербота: ок=%s fail=%s", len(ok), len(fail))
    if fail:
        log.warning("Не вступил: %s", "; ".join(fail[:8]))

    extra = await scanner.watch_all_dialogs()
    log.info("Подхватил диалоги юзербота: %s чатов", extra)

    warmed = 0
    people = {
        item["username"].lower()
        for item in CATALOG
        if item.get("kind") in {"chat", "community"}
    }
    track = {name.lower() for name in tracker_usernames()} | people
    for chat in await db.list_chats():
        uname = (chat.get("username") or "").lower()
        if uname not in track or not chat.get("enabled"):
            continue
        limit = 40 if uname in people else 15
        try:
            warmed += await scanner.scan_recent(int(chat["chat_id"]), limit=limit)
        except Exception:
            log.exception("Прогрев @%s", uname)
    log.info("Прогрев истории: %s проверок в очереди", warmed)
    chats_n = len(await db.list_chats())
    panel = await db.list_admins()
    hello = (
        f'{pe("fire")} <b>Админ-панель</b>\n'
        f'{pe("user")} парсер: юзербот @{me.username or me.id}\n'
        f'{pe("chat")} каналов: <b>{chats_n}</b>\n'
        f'{pe("teddy")} ≤4 NFT · lvl ≤3 · дешёвые гифты'
    )
    if panel:
        await notifier.send_text(hello)
    else:
        log.warning("Админки ещё нет: открой @parsers_informain_bot с личного аккаунта и нажми /start")


async def _worker(app: App) -> None:
    while True:
        job = await app.queue.get()
        try:
            await app.scanner.process_job(job)
        except Exception:
            log.exception("Ошибка проверки %s", job.entity)
        await asyncio.sleep(app.settings.check_delay_sec)


async def _market(app: App) -> None:
    from nft_parser.scanner import CheckJob

    warm = True
    while True:
        try:
            if await app.db.is_running() and app.market is not None:
                filters = await app.db.get_filters()
                if filters.get("market_enabled", True):
                    min_price = float(filters.get("min_price_ton") or 0)
                    for action in await app.market.portals_actions("buy", limit=20):
                        lead = app.market.parse_portals_action(action)
                        await _enqueue_market(app, lead, min_price, "market_buy", "Portals · покупка", "купил на Portals", not warm)
                    warm = False
        except Exception:
            log.exception("Ошибка API маркета")
        await asyncio.sleep(app.settings.market_poll_sec)


async def _enqueue_market(app: App, lead, min_price, source, label, reason, notify) -> None:
    from nft_parser.scanner import CheckJob

    if not lead.user_id and not lead.username:
        return
    if min_price and lead.price and lead.price < min_price:
        return
    if await app.db.seen_market(lead.key):
        return
    await app.db.mark_market(lead.key)
    if not notify:
        return
    extra = [lead.gift] if lead.gift else []
    entity = f"@{lead.username}" if lead.username else int(lead.user_id)
    await app.queue.put(
        CheckJob(
            entity=entity,
            source=source,
            source_label=label,
            extra_gifts=extra,
            reason=reason if not lead.price else f"{reason} · {lead.price:g} TON",
            force=True,
        )
    )
