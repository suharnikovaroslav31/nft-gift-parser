from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, Chat, MessageActionStarGiftUnique, User
from telethon.utils import get_peer_id

from nft_parser.db import Database
from nft_parser.gifts import GiftService, extract_slugs, to_unix, unique_to_info
from nft_parser.models import GiftInfo, Hit, ProfileGifts
from nft_parser.web_feed import is_whale_gift, looks_like_trader

log = logging.getLogger(__name__)

SKIP_USERNAMES = {
    "mrktbank",
    "giftrelayer",
    "thermos_vault",
    "giftsatellite_cashier",
    "giftsatellite",
    "giftbackpack",
    "rolls_transfer",
    "starscasegifts",
    "portals",
    "tonnel",
}
SKIP_NAME_PARTS = ("vault", "cashier", "relayer", "escrow", "deposit", "backpack")


def _username_of(entity: Any) -> str:
    if isinstance(entity, str):
        return entity.lstrip("@").lower()
    return (getattr(entity, "username", None) or "").lstrip("@").lower()


def is_market_account(entity: Any) -> bool:
    uname = _username_of(entity)
    if not uname:
        return False
    if uname in SKIP_USERNAMES:
        return True
    return any(part in uname for part in SKIP_NAME_PARTS)


def _entity_id(entity: Any) -> int | None:
    if isinstance(entity, int):
        return int(entity)
    uid = getattr(entity, "id", None)
    return int(uid) if uid else None


@dataclass
class CheckJob:
    entity: Any
    source: str
    source_label: str
    force: bool = False
    extra_gifts: list[GiftInfo] = field(default_factory=list)
    reason: str = ""


class CheckQueue:
    def __init__(self, delay_sec: float) -> None:
        self.delay_sec = delay_sec
        self.queue: asyncio.Queue[CheckJob] = asyncio.Queue(maxsize=800)
        self._pending: set[str] = set()

    def _key(self, job: CheckJob) -> str:
        ent = job.entity
        if isinstance(ent, int):
            return f"u:{ent}"
        if isinstance(ent, str):
            return f"u:{ent.strip().lstrip('@').lower()}"
        uid = getattr(ent, "id", None)
        if uid:
            return f"u:{int(uid)}"
        return f"u:{ent}"

    async def put(self, job: CheckJob) -> None:
        key = self._key(job)
        if key in self._pending:
            return
        if self.queue.full():
            log.warning("Очередка проверки переполнена, пропускаю %s", job.entity)
            return
        self._pending.add(key)
        await self.queue.put(job)

    async def get(self) -> CheckJob:
        job = await self.queue.get()
        self._pending.discard(self._key(job))
        return job


def passes_filters(
    profile: ProfileGifts,
    filters: dict[str, Any],
    extra_gifts: list[GiftInfo] | None = None,
) -> tuple[bool, bool, bool]:
    gifts = list(profile.unique or extra_gifts or [])
    count = profile.total_unique or len(gifts)
    if profile.hidden and not gifts:
        return False, False, False
    if filters.get("require_username") and not profile.username:
        return False, False, False
    if looks_like_trader(profile.username) or looks_like_trader(profile.first_name):
        return False, False, False
    min_unique = int(filters.get("min_unique") or 1)
    max_unique = int(filters.get("max_unique") or 0)
    if count < min_unique:
        return False, False, False
    if max_unique and count > max_unique:
        return False, False, False
    newbie_max = int(filters.get("newbie_max") or 2)
    is_newbie = 0 < count <= newbie_max
    if filters.get("newbie_only") and not is_newbie:
        return False, False, False

    max_level = int(filters.get("max_tg_level") or 2)
    if profile.tg_level is not None and profile.tg_level > max_level:
        return False, is_newbie, False

    if any(is_whale_gift(g.slug, g.title) for g in gifts):
        return False, is_newbie, False

    max_usd = float(filters.get("max_gift_usd") or 12)
    max_ton = float(filters.get("max_gift_ton") or 6)
    cheap_list = float(filters.get("cheap_list_ton") or 4)
    usd_vals = [g.value_usd for g in gifts if g.value_usd]
    ton_vals = [g.value_ton for g in gifts if g.value_ton]
    listed_vals = [g.listed_ton for g in gifts if g.listed_ton]
    if usd_vals and max(usd_vals) > max_usd:
        return False, is_newbie, False
    if ton_vals and max(ton_vals) > max_ton:
        return False, is_newbie, False
    if listed_vals and min(listed_vals) > cheap_list:
        return False, is_newbie, False

    recent_hours = int(filters.get("recent_hours") or 48)
    just_bought = bool(extra_gifts)
    now = int(time.time())
    stamps = [stamp for stamp in (to_unix(g.received_at) or 0 for g in gifts) if stamp]
    if recent_hours > 0:
        cutoff = now - recent_hours * 3600
        if stamps:
            just_bought = any(stamp >= cutoff for stamp in stamps)
            if not just_bought:
                return False, is_newbie, False
        elif not extra_gifts:
            return False, is_newbie, False
    elif stamps:
        just_bought = just_bought or any(stamp >= now - 24 * 3600 for stamp in stamps)
    return True, is_newbie, just_bought


class ChatScanner:
    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        gifts: GiftService,
        queue: CheckQueue,
        on_hit: Callable[[Hit], Awaitable[None]],
        cooldown_hours: int,
        notify_hours: int = 24,
    ) -> None:
        self.client = client
        self.db = db
        self.gifts = gifts
        self.queue = queue
        self.on_hit = on_hit
        self.cooldown_hours = cooldown_hours
        self.notify_hours = notify_hours
        self._me_id: int | None = None

    async def start(self) -> None:
        me = await self.client.get_me()
        self._me_id = int(me.id)
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        log.info("Сканер чатов запущен")

    async def resolve_and_add(self, raw: str) -> tuple[int, str, str | None]:
        raw = raw.strip()
        entity = None
        if "t.me/+" in raw or "joinchat/" in raw:
            invite = raw.rsplit("/", 1)[-1].replace("+", "")
            try:
                info = await self.client(CheckChatInviteRequest(invite))
                chat = getattr(info, "chat", None)
                if chat is None:
                    imported = await self.client(ImportChatInviteRequest(invite))
                    chats = getattr(imported, "chats", None) or []
                    chat = chats[0] if chats else None
                entity = chat
            except RPCError as exc:
                raise ValueError(f"Не удалось вступить по ссылке: {exc}") from exc
            if entity is None:
                raise ValueError("Не удалось получить чат по инвайту")
        else:
            username = raw.replace("https://t.me/", "").replace("t.me/", "").lstrip("@")
            try:
                entity = await self.client.get_entity(username)
            except (RPCError, ValueError) as exc:
                raise ValueError(f"Чат не найден: {exc}") from exc
            if isinstance(entity, Channel):
                try:
                    await self.client(JoinChannelRequest(entity))
                except RPCError:
                    pass

        if not isinstance(entity, (Channel, Chat)):
            raise ValueError("Нужен канал или чат, не пользователь")
        chat_id = int(get_peer_id(entity))
        title = getattr(entity, "title", None) or str(chat_id)
        username = getattr(entity, "username", None)
        await self.db.add_chat(chat_id, title, username)
        return chat_id, title, username

    async def join_catalog(self, items: list[dict[str, str]]) -> tuple[list[str], list[str]]:
        ok: list[str] = []
        fail: list[str] = []
        for item in items:
            username = item["username"]
            try:
                _, title, _ = await self.resolve_and_add(username)
                ok.append(f"{title} (@{username})")
                await asyncio.sleep(0.6)
            except Exception as exc:
                fail.append(f"@{username} — {exc}")
                log.warning("Каталог: не вступил в @%s: %s", username, exc)
        return ok, fail

    async def watch_all_dialogs(self, limit: int = 180) -> int:
        added = 0
        async for dialog in self.client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if not isinstance(entity, (Channel, Chat)):
                continue
            chat_id = int(get_peer_id(entity))
            title = getattr(entity, "title", None) or str(chat_id)
            username = getattr(entity, "username", None)
            await self.db.add_chat(chat_id, title, username)
            added += 1
        return added

    async def process_job(self, job: CheckJob) -> None:
        if is_market_account(job.entity):
            log.info("Пропуск депозита маркета %s", _username_of(job.entity) or job.entity)
            return
        if not await self.db.is_running() and not job.force:
            return
        filters = await self.db.get_filters()
        if job.source.startswith("chat") and not filters.get("chats_enabled", True):
            return
        if job.source.startswith("market") and not filters.get("market_enabled", True):
            return

        newbie_max = int(filters.get("newbie_max") or 2)
        cached = await self.db.cached_profile(_entity_id(job.entity), _username_of(job.entity))
        if cached:
            known = int(cached.get("unique_count") or 0)
            if known > newbie_max and filters.get("newbie_only", True):
                log.info("Кэш кит %s: %s NFT", cached.get("username") or cached.get("user_id"), known)
                return
            if await self.db.already_notified(
                int(cached["user_id"]),
                cached.get("username"),
                self.notify_hours,
            ) and job.source != "manual":
                return

        try:
            profile = await self.gifts.profile_gifts(job.entity)
        except FloodWaitError as exc:
            log.warning("FloodWait %ss, пауза", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            await self.queue.put(job)
            return
        if profile is None:
            uid = job.entity if isinstance(job.entity, int) else None
            uname = job.entity[1:] if isinstance(job.entity, str) and job.entity.startswith("@") else (
                job.entity if isinstance(job.entity, str) else None
            )
            if (uid or uname) and job.extra_gifts:
                profile = ProfileGifts(
                    user_id=int(uid or 0),
                    username=uname if isinstance(uname, str) else None,
                    first_name=uname or (f"id{uid}" if uid else "unknown"),
                    last_name="",
                    unique=list(job.extra_gifts),
                    total_unique=len(job.extra_gifts),
                    hidden=True,
                )
            else:
                return

        if is_market_account(profile.username) or is_market_account(profile.first_name):
            log.info("Пропуск депозита маркета %s", profile.username or profile.first_name)
            return

        if job.extra_gifts:
            known = {g.slug for g in profile.unique}
            for gift in job.extra_gifts:
                if gift.slug not in known:
                    profile.unique.insert(0, gift)
                    known.add(gift.slug)
            profile.total_unique = max(profile.total_unique, len(profile.unique))

        if profile.tg_level is None and (profile.total_unique or 0) <= 6:
            try:
                user = await self.client.get_entity(job.entity)
                if isinstance(user, User):
                    profile.tg_level = await self.gifts.user_level(user)
                    for gift in profile.unique[:2]:
                        await self.gifts.fill_gift_value(gift)
            except (FloodWaitError, RPCError, ValueError, TypeError):
                pass

        await self.db.touch_user(
            profile.user_id,
            profile.username,
            profile.first_name,
            profile.total_unique,
            profile.gift_fingerprint(),
        )

        ok, is_newbie, just_bought = passes_filters(profile, filters, job.extra_gifts)
        if not ok:
            log.info(
                "Фильтр отсёк %s: unique=%s lvl=%s usd=%s ton=%s listed=%s",
                profile.username or profile.user_id,
                profile.total_unique,
                profile.tg_level,
                max((g.value_usd or 0) for g in profile.unique) if profile.unique else 0,
                max((g.value_ton or 0) for g in profile.unique) if profile.unique else 0,
                min((g.listed_ton for g in profile.unique if g.listed_ton), default=0),
            )
            return
        if job.source != "manual" and not await self.db.should_notify(
            profile.user_id,
            profile.gift_fingerprint(),
            self.notify_hours,
            username=profile.username,
        ):
            log.info("Уже кидали @%s", profile.username or profile.user_id)
            return

        hit = Hit(
            profile=profile,
            source=job.source,
            source_label=job.source_label,
            reason=job.reason,
            is_newbie=is_newbie,
            just_bought=just_bought or job.source in {"market_buy", "gift_link", "gift_action"},
        )
        await self.on_hit(hit)

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        if not await self.db.is_running():
            return
        chat = await event.get_chat()
        chat_id = int(event.chat_id)
        watched = await self.db.enabled_chat_ids()
        watched_names = await self.db.enabled_chat_usernames()
        chat_username = (getattr(chat, "username", None) or "").lstrip("@").lower()
        if chat_id not in watched and chat_username not in watched_names:
            return

        filters = await self.db.get_filters()
        if not filters.get("chats_enabled", True):
            return
        title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)
        source_label = f"{title}"

        if filters.get("check_gift_links", True):
            await self._handle_gift_links(event, source_label)
            action = getattr(event.message, "action", None)
            if isinstance(action, MessageActionStarGiftUnique):
                gift = getattr(action, "gift", None)
                if gift is not None:
                    try:
                        info = unique_to_info(gift)
                    except Exception:
                        info = None
                    sender = await event.get_sender()
                    if info and isinstance(sender, User) and not sender.bot:
                        await self._enqueue_user(
                            sender,
                            source="gift_action",
                            source_label=source_label,
                            extra=[info],
                            reason="получил уникальный подарок",
                        )

        if not filters.get("check_senders", True):
            return
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot or sender.id == self._me_id:
            return
        if not await self.db.should_check_user(int(sender.id), self.cooldown_hours):
            return
        await self._enqueue_user(sender, source="chat", source_label=source_label)

    async def _handle_gift_links(self, event: events.NewMessage.Event, source_label: str) -> None:
        slugs = extract_slugs(event.raw_text, event.message.entities)
        for slug in slugs[:8]:
            try:
                info, owner_id, owner_user = await self.gifts.get_unique_by_slug(slug)
            except FloodWaitError as exc:
                await asyncio.sleep(exc.seconds + 1)
                continue
            if not info:
                continue
            entity = owner_user or owner_id
            if entity is None:
                continue
            await self._enqueue_user(
                entity,
                source="gift_link",
                source_label=source_label,
                extra=[info],
                reason=f"ссылка на {info.label}",
            )

    async def _enqueue_user(
        self,
        entity: Any,
        source: str,
        source_label: str,
        extra: list[GiftInfo] | None = None,
        reason: str = "",
        force: bool = False,
    ) -> None:
        if is_market_account(entity):
            return
        await self.queue.put(
            CheckJob(
                entity=entity,
                source=source,
                source_label=source_label,
                extra_gifts=extra or [],
                reason=reason,
                force=force,
            )
        )

    async def scan_recent(self, chat_id: int, limit: int = 80) -> int:
        queued = 0
        async for message in self.client.iter_messages(chat_id, limit=limit):
            sender = await message.get_sender()
            if isinstance(sender, User) and not sender.bot:
                if await self.db.should_check_user(int(sender.id), self.cooldown_hours):
                    chat = await message.get_chat()
                    title = getattr(chat, "title", None) or str(chat_id)
                    await self._enqueue_user(sender, source="chat_scan", source_label=title)
                    queued += 1
            slugs = extract_slugs(message.raw_text, message.entities)
            for slug in slugs[:3]:
                try:
                    info, owner_id, owner_user = await self.gifts.get_unique_by_slug(slug)
                except FloodWaitError:
                    break
                if info and (owner_user or owner_id):
                    await self._enqueue_user(
                        owner_user or owner_id,
                        source="gift_link",
                        source_label="история чата",
                        extra=[info],
                    )
                    queued += 1
        return queued
