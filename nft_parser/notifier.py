from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from nft_parser.db import Database
from nft_parser.emoji import icon, pe
from nft_parser.gifts import to_unix
from nft_parser.models import Hit, TrackEvent

log = logging.getLogger(__name__)


def _esc(text: str | None) -> str:
    return html.escape(text or "")


def hit_keyboard(hit: Hit) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    profile = hit.profile
    if profile.username:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Профиль",
                    url=f"https://t.me/{profile.username}",
                    icon_custom_emoji_id=icon("user"),
                )
            ]
        )
    gift = next((g for g in profile.unique if g.link), None)
    if gift and gift.link:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(gift.label[:34] + "…") if len(gift.label) > 34 else gift.label,
                    url=gift.link,
                    icon_custom_emoji_id=icon("gift"),
                )
            ]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _user_block(profile) -> str:
    if profile.username:
        line = (
            f'{pe("user")} <a href="https://t.me/{profile.username}">'
            f"<b>@{_esc(profile.username)}</b></a>"
        )
        name = profile.display_name
        if name and name.lower() not in {profile.username.lower(), f"@{profile.username.lower()}"}:
            if name not in {"юзер не указан", "на маркете"}:
                line += f" · {_esc(name)}"
        if profile.user_id and profile.user_id < 1_000_000_000:
            line += f'\n{pe("pin")} <code>{profile.user_id}</code>'
        return line
    if profile.user_id and profile.user_id < 1_000_000_000:
        return f"{pe('user')} {profile.mention}\n{pe('pin')} <code>{profile.user_id}</code>"
    name = (profile.first_name or "").strip()
    if name and name.lower() not in {"юзер не указан", "на маркете"}:
        return f"{pe('user')} {_esc(name)} — @username нет"
    return f"{pe('user')} @username скрыт"


def format_hit(hit: Hit) -> str:
    profile = hit.profile
    badges: list[str] = []
    if hit.is_newbie or profile.total_unique <= 4:
        badges.append(f'{pe("teddy")} новичок')
    if profile.tg_level is not None:
        badges.append(f'{pe("star")} lvl {profile.tg_level}')
    if profile.total_unique:
        badges.append(f'{pe("top")} {profile.total_unique} NFT')
    listed = [g.listed_ton for g in profile.unique if g.listed_ton]
    if listed:
        badges.append(f'{pe("money")} листинг {min(listed):g} TON')
    if hit.just_bought:
        badges.append(f'{pe("new")} свежий')

    gift_lines: list[str] = []
    for gift in profile.unique[:8]:
        link = f'<a href="{gift.link}"><b>{_esc(gift.label)}</b></a>' if gift.link else f"<b>{_esc(gift.label)}</b>"
        bits: list[str] = []
        if gift.backdrop:
            bits.append(_esc(gift.backdrop))
        if gift.listed_ton:
            bits.append(f"{gift.listed_ton:g} TON")
        elif gift.value_ton:
            bits.append(f"~{gift.value_ton:g} TON")
        elif gift.value_usd:
            bits.append(f"${gift.value_usd:g}")
        stamp = to_unix(gift.received_at)
        if stamp:
            bits.append(datetime.fromtimestamp(stamp).strftime("%d.%m %H:%M"))
        extra = f" · <i>{', '.join(bits)}</i>" if bits else ""
        gift_lines.append(f'{pe("gift")} {link}{extra}')
    if profile.total_unique > len(profile.unique[:8]):
        gift_lines.append(f'{pe("plus")} ещё {profile.total_unique - len(profile.unique[:8])}')

    gifts_block = "\n".join(gift_lines) if gift_lines else f'{pe("warn")} коллекция скрыта'
    reason = f'\n{pe("info")} <i>{_esc(hit.reason)}</i>' if hit.reason else ""
    badge_line = "  ·  ".join(badges)

    return (
        f'{pe("fire")} <b>NFT Gift Hunter</b>\n'
        f"{badge_line}\n\n"
        f"{_user_block(profile)}\n"
        f'{pe("stars")} уникальных: <b>{profile.total_unique}</b> · lvl <b>{profile.tg_level if profile.tg_level is not None else "—"}</b>\n'
        f'{pe("place")} {_esc(hit.source_label)}'
        f"{reason}\n\n"
        f"<blockquote expandable>{gifts_block}</blockquote>"
    )


def format_track(event: TrackEvent) -> str:
    gift = event.gift
    header = f'{pe("cross")} <b>Продажа</b>' if event.kind == "sold" else f'{pe("check")} <b>Листинг</b>'
    title = _esc(gift.title)
    if gift.link:
        title_line = f'<a href="{gift.link}"><b>{title} #{gift.num}</b></a>'
    else:
        title_line = f"<b>{title} #{gift.num}</b>"
    lines = [header, "", f'{pe("gift")} {title_line}']
    if gift.model:
        lines.append(f'{pe("teddy")} {_esc(gift.model)}')
    if gift.backdrop:
        lines.append(f'{pe("star")} {_esc(gift.backdrop)}')
    if event.price:
        lines.append(f'{pe("money")} {event.price:g} {_esc(event.asset or "TON")}')
    if event.source:
        lines.append(f'{pe("place")} @{_esc(event.source)}')
    return "\n".join(lines)


@dataclass
class _Outgoing:
    text: str
    preview: bool
    markup: InlineKeyboardMarkup | None = None


class Notifier:
    MAX_PENDING = 80

    def __init__(self, bot: Bot, db: Database, fallback_admins: list[int]) -> None:
        self.bot = bot
        self.db = db
        self.fallback_admins = fallback_admins
        self.skip_ids: set[int] = set()
        self._out: asyncio.Queue[_Outgoing] = asyncio.Queue()
        self._last_send = 0.0

    def pending(self) -> int:
        return self._out.qsize()

    async def notify_delay(self) -> float:
        filters = await self.db.get_filters()
        try:
            return max(0.0, float(filters.get("notify_every_sec") or 0))
        except (TypeError, ValueError):
            return 0.0

    async def pace_loop(self) -> None:
        while True:
            item = await self._out.get()
            delay = await self.notify_delay()
            if delay > 0 and self._last_send:
                wait = delay - (time.monotonic() - self._last_send)
                if wait > 0:
                    await asyncio.sleep(wait)
            await self._broadcast(item.text, item.preview, item.markup)
            self._last_send = time.monotonic()

    async def _enqueue(
        self,
        text: str,
        preview: bool,
        markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        delay = await self.notify_delay()
        if delay <= 0 and self._out.empty():
            await self._broadcast(text, preview, markup)
            self._last_send = time.monotonic()
            return
        dropped = 0
        while self._out.qsize() >= self.MAX_PENDING:
            try:
                self._out.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            log.warning("Очередь карточек переполнена, сбросил старые: %s", dropped)
        await self._out.put(_Outgoing(text, preview, markup))

    async def recipients(self) -> list[int]:
        admins = await self.db.list_admins()
        ids = admins or [uid for uid in self.fallback_admins if uid not in self.skip_ids]
        owner = await self.db.get_setting("owner_id")
        if owner and owner.isdigit() and int(owner) not in self.skip_ids:
            ids = [int(owner), *[uid for uid in ids if uid != int(owner)]]
        seen: set[int] = set()
        out: list[int] = []
        for uid in ids:
            if uid in self.skip_ids or uid in seen:
                continue
            seen.add(uid)
            out.append(uid)
        return out

    async def send_hit(self, hit: Hit) -> None:
        text = format_hit(hit)
        gifts_payload = [
            {
                "title": g.title,
                "slug": g.slug,
                "num": g.num,
                "model": g.model,
            }
            for g in hit.profile.unique[:20]
        ]
        await self.db.save_find(
            hit.profile.user_id,
            hit.profile.username,
            hit.source,
            hit.profile.total_unique,
            gifts_payload,
        )
        await self.db.mark_notified(hit.profile.user_id)
        log.info(
            "Карточка @%s · %s NFT · %s",
            hit.profile.username or hit.profile.user_id,
            hit.profile.total_unique,
            hit.source_label,
        )
        await self._enqueue(text, preview=True, markup=hit_keyboard(hit))

    async def send_track(self, event: TrackEvent) -> None:
        text = format_track(event)
        gift = event.gift
        await self.db.save_find(
            0,
            None,
            event.kind,
            1,
            [{"title": gift.title, "slug": gift.slug, "num": gift.num, "model": gift.model}],
        )
        await self._enqueue(text, preview=True)

    async def _broadcast(
        self,
        text: str,
        preview: bool,
        markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        for admin_id in await self.recipients():
            try:
                await self.bot.send_message(
                    admin_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=not preview,
                    reply_markup=markup,
                )
            except Exception as exc:
                if markup is not None:
                    try:
                        await self.bot.send_message(
                            admin_id,
                            text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=not preview,
                        )
                        continue
                    except Exception:
                        pass
                err = str(exc).lower()
                if "can't parse entities" in err or "unsupported" in err or "tg-emoji" in err:
                    plain = re.sub(r'<tg-emoji emoji-id="\d+">', "", text).replace("</tg-emoji>", "")
                    plain = plain.replace("<blockquote expandable>", "").replace("</blockquote>", "")
                    try:
                        await self.bot.send_message(
                            admin_id,
                            plain,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=not preview,
                        )
                        continue
                    except Exception:
                        pass
                log.warning("Не смог отправить %s: %s", admin_id, exc)

    async def send_text(self, text: str) -> None:
        await self._broadcast(text, preview=False)
