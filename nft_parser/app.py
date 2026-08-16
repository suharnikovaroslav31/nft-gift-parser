from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from nft_parser.bot import AppMiddleware, router
from nft_parser.emoji import pe
from nft_parser.catalog import CATALOG, tracker_usernames
from nft_parser.config import Settings
from nft_parser.db import Database
from nft_parser.models import Hit, ProfileGifts
from nft_parser.notifier import Notifier
from nft_parser.urllib_session import UrllibSession
from nft_parser.web_feed import PublicFeed, is_deposit_owner, web_chat_id, web_user_id, why_not_noob

log = logging.getLogger(__name__)


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

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=UrllibSession(),
    )
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
    log.info("Запуск без api_id: читаю публичные ленты t.me/s/…")
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
        "🎁 <b>Люди, которые в NFT не шарят</b>\n"
        "Только свежий подарок, сам владелец, не маркет и не киты.\n"
        "Напишите /start."
    )
    try:
        await asyncio.gather(
            app.dp.start_polling(app.bot, polling_timeout=0, handle_signals=False),
            web_loop(app),
            app.notifier.pace_loop(),
        )
    finally:
        await app.feed.close()
        await app.bot.session.close()
        await app.db.close()


async def run_userbot(settings: Settings) -> None:
    from telethon import TelegramClient

    from nft_parser.gifts import GiftService
    from nft_parser.marketplace import Marketplace
    from nft_parser.scanner import ChatScanner, CheckQueue

    db = Database(settings.db_path)
    await db.connect()
    for admin_id in settings.admin_id_list():
        await db.add_admin(admin_id)

    userbot = TelegramClient(
        settings.telethon_session(),
        settings.api_id,
        settings.api_hash,
        proxy=settings.telethon_proxy(),
        device_model="Desktop",
        system_version="Windows 10",
        app_version="5.0",
    )
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=UrllibSession(),
    )
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

    log.info("Логин юзербота…")
    if settings.session_string.strip():
        await userbot.connect()
        if not await userbot.is_user_authorized():
            log.error("SESSION_STRING недействительна. Сгенерируй её локально и вставь в env Bothost.")
            await userbot.disconnect()
            return
        await userbot.start()
    else:
        await userbot.start(phone=settings.phone or None)
    await apply_userbot_mode(db)
    await scanner.start()
    me = await userbot.get_me()
    scanner_id = int(me.id)
    await db.set_setting("scanner_id", str(scanner_id))
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

    async def _bootstrap() -> None:
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

    try:
        await asyncio.gather(
            dp.start_polling(bot),
            _worker(app),
            _market(app),
            notifier.pace_loop(),
            _bootstrap(),
        )
    finally:
        await app.feed.close()
        await market.close()
        await bot.session.close()
        await userbot.disconnect()
        await db.close()


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
