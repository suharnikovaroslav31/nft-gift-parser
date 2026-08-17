from __future__ import annotations

import logging
import re
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.payments import GetSavedStarGiftsRequest, GetUniqueStarGiftRequest, GetUniqueStarGiftValueInfoRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    MessageEntityTextUrl,
    MessageEntityUrl,
    PeerUser,
    StarGiftUnique,
    User,
)

from nft_parser.models import GiftInfo, ProfileGifts

log = logging.getLogger(__name__)


def public_username(user: Any) -> str | None:
    """Обычный @nick или активный из user.usernames (коллекционные ники)."""
    if user is None:
        return None
    name = getattr(user, "username", None)
    if name:
        return str(name).lstrip("@") or None
    picked = None
    for item in getattr(user, "usernames", None) or []:
        uname = str(getattr(item, "username", None) or "").lstrip("@")
        if not uname:
            continue
        if getattr(item, "active", None) is True or getattr(item, "editable", None) is True:
            return uname
        if picked is None:
            picked = uname
    return picked

NFT_SLUG_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:nft/|gift/)([A-Za-z0-9_+\-]+)",
    re.IGNORECASE,
)
FRAGMENT_SLUG_RE = re.compile(
    r"(?:https?://)?nft\.fragment\.com/gift/([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)


def extract_slugs(text: str | None, entities: list[Any] | None = None) -> list[str]:
    slugs: list[str] = []
    blobs = [text or ""]
    if entities:
        for ent in entities:
            if isinstance(ent, (MessageEntityUrl, MessageEntityTextUrl)):
                url = getattr(ent, "url", None) or ""
                blobs.append(url)
    for blob in blobs:
        for regex in (NFT_SLUG_RE, FRAGMENT_SLUG_RE):
            slugs.extend(regex.findall(blob))
    seen: set[str] = set()
    out: list[str] = []
    for slug in slugs:
        key = slug.strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            out.append(key)
    return out


def _attr_name(gift: Any) -> tuple[str | None, str | None, str | None]:
    model = backdrop = symbol = None
    for attr in getattr(gift, "attributes", None) or []:
        cls = type(attr).__name__
        name = getattr(attr, "name", None)
        if "Model" in cls:
            model = name
        elif "Backdrop" in cls:
            backdrop = name
        elif "Pattern" in cls:
            symbol = name
    return model, backdrop, symbol


def to_unix(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        stamp = int(value)
        if stamp > 10_000_000_000:
            stamp //= 1000
        return stamp if stamp > 0 else None
    if hasattr(value, "timestamp"):
        try:
            return int(value.timestamp())
        except (OSError, OverflowError, TypeError, ValueError):
            return None
    return None


def _as_usd(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value >= 100:
        return value / 100.0
    return value


def _as_ton(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value >= 1_000_000:
        return value / 1_000_000_000
    return float(value)


def _listed_ton(resell_amount: Any) -> float | None:
    if not resell_amount:
        return None
    items = resell_amount if isinstance(resell_amount, (list, tuple)) else [resell_amount]
    listed: list[float] = []
    for item in items:
        name = type(item).__name__
        raw = getattr(item, "amount", None)
        if raw is None:
            continue
        if name == "StarsTonAmount" or int(raw) >= 1_000_000:
            ton = _as_ton(raw)
        else:
            continue
        if ton:
            listed.append(ton)
    return min(listed) if listed else None


def unique_to_info(gift: Any, received_at: Any = None) -> GiftInfo:
    model, backdrop, symbol = _attr_name(gift)
    value = getattr(gift, "value_amount", None)
    currency = (getattr(gift, "value_currency", None) or "").upper()
    usd = _as_usd(getattr(gift, "value_usd_amount", None))
    ton = None
    if currency in {"TON", "TONCOIN"}:
        ton = _as_ton(value)
    return GiftInfo(
        title=gift.title,
        slug=gift.slug,
        num=int(gift.num),
        model=model,
        backdrop=backdrop,
        symbol=symbol,
        value_stars=int(value) if value is not None and currency in {"XTR", "STARS", ""} else None,
        value_usd=usd,
        value_ton=ton,
        listed_ton=_listed_ton(getattr(gift, "resell_amount", None)),
        received_at=to_unix(received_at),
    )


def owner_id_from_gift(gift: StarGiftUnique) -> int | None:
    owner = gift.owner_id
    if isinstance(owner, PeerUser):
        return int(owner.user_id)
    if owner is not None and getattr(owner, "user_id", None):
        return int(owner.user_id)
    return None


class GiftService:
    def __init__(self, client: TelegramClient, max_gifts: int = 80) -> None:
        self.client = client
        self.max_gifts = max_gifts
        self._value_cache: dict[str, tuple[float | None, float | None]] = {}

    async def user_level(self, user: User) -> int | None:
        try:
            full = await self.client(GetFullUserRequest(user))
        except (FloodWaitError, RPCError, ValueError, TypeError):
            return None
        rating = getattr(getattr(full, "full_user", None), "stars_rating", None)
        if rating is None:
            return 0
        try:
            return int(rating.level)
        except (TypeError, ValueError):
            return 0

    async def fill_gift_value(self, info: GiftInfo) -> GiftInfo:
        if (info.value_usd is not None or info.value_ton is not None) or not info.slug:
            return info
        if info.slug in self._value_cache:
            usd, ton = self._value_cache[info.slug]
            info.value_usd = info.value_usd or usd
            info.value_ton = info.value_ton or ton
            return info
        try:
            data = await self.client(GetUniqueStarGiftValueInfoRequest(info.slug))
        except (FloodWaitError, RPCError, ValueError, TypeError):
            self._value_cache[info.slug] = (None, None)
            return info
        currency = (getattr(data, "currency", None) or "").upper()
        usd = ton = None
        if currency in {"USD", "USDT"}:
            usd = _as_usd(getattr(data, "value", None) or getattr(data, "floor_price", None))
        elif currency in {"TON", "TONCOIN"}:
            ton = _as_ton(getattr(data, "floor_price", None) or getattr(data, "value", None))
        else:
            usd = _as_usd(getattr(data, "value", None))
            ton = _as_ton(getattr(data, "floor_price", None))
        self._value_cache[info.slug] = (usd, ton)
        info.value_usd = usd
        info.value_ton = ton
        return info

    async def get_unique_by_slug(self, slug: str) -> tuple[GiftInfo | None, int | None, User | None]:
        try:
            result = await self.client(GetUniqueStarGiftRequest(slug=slug))
        except FloodWaitError as exc:
            log.warning("FloodWait %ss on getUniqueStarGift", exc.seconds)
            raise
        except RPCError as exc:
            log.debug("Gift slug %s failed: %s", slug, exc)
            return None, None, None

        gift = result.gift
        if not isinstance(gift, StarGiftUnique):
            return None, None, None
        info = unique_to_info(gift)
        uid = owner_id_from_gift(gift)
        owner_user = None
        for user in result.users or []:
            if isinstance(user, User) and uid and user.id == uid:
                owner_user = user
                break
        if owner_user is None and result.users:
            maybe = result.users[0]
            if isinstance(maybe, User) and not maybe.bot:
                owner_user = maybe
                uid = uid or maybe.id
        return info, uid, owner_user

    async def profile_gifts(self, entity: Any) -> ProfileGifts | None:
        try:
            user = await self.client.get_entity(entity)
        except (RPCError, ValueError) as exc:
            log.debug("Cannot resolve %s: %s", entity, exc)
            return None
        if not isinstance(user, User) or user.bot or user.deleted:
            return None
        if not public_username(user):
            try:
                full = await self.client(GetFullUserRequest(user))
                for item in getattr(full, "users", None) or []:
                    if isinstance(item, User) and item.id == user.id:
                        user = item
                        break
            except (FloodWaitError, RPCError, ValueError, TypeError):
                pass

        unique: list[GiftInfo] = []
        offset = ""
        total = 0
        hidden = False
        try:
            while len(unique) < self.max_gifts:
                result = await self.client(
                    GetSavedStarGiftsRequest(
                        peer=user,
                        offset=offset,
                        limit=50,
                        exclude_unlimited=True,
                    )
                )
                total = int(result.count or 0)
                if not result.gifts:
                    break
                for saved in result.gifts:
                    gift = getattr(saved, "gift", None)
                    if isinstance(gift, StarGiftUnique):
                        unique.append(unique_to_info(gift, getattr(saved, "date", None)))
                next_offset = getattr(result, "next_offset", None)
                if not next_offset or next_offset == offset:
                    break
                offset = next_offset
        except FloodWaitError:
            raise
        except RPCError as exc:
            log.debug("getSavedStarGifts %s: %s", user.id, exc)
            hidden = True

        profile = ProfileGifts(
            user_id=int(user.id),
            username=public_username(user),
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            unique=unique,
            total_unique=max(total, len(unique)) if unique else total,
            hidden=hidden and not unique,
            tg_level=None,
        )
        count = profile.total_unique or len(unique)
        if unique and count <= 6:
            missing = [g for g in unique if g.value_usd is None and g.value_ton is None][:2]
            for gift in missing:
                await self.fill_gift_value(gift)
            profile.tg_level = await self.user_level(user)
        return profile
