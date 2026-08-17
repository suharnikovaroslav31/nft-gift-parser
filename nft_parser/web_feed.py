from __future__ import annotations

import asyncio
import logging
import re
import ssl
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape

from nft_parser.catalog import catalog_usernames
from nft_parser.models import GiftInfo

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.9",
}

SOLD_RE = re.compile(
    r"Gift Sold\s+(.+?)\s+#(\d+)",
    re.I,
)
LISTED_RE = re.compile(
    r"New Gift Listed\s+(.+?)\s+#(\d+)",
    re.I,
)
NAME_NUM_RE = re.compile(r"([A-Za-z][A-Za-z0-9' \-]{1,40})\s+#(\d{1,8})")
MODEL_RE = re.compile(r"Model:\s*([^\n\-(]+)", re.I)
BACKDROP_RE = re.compile(r"Backdrop:\s*([^\n\-(]+)", re.I)
SYMBOL_RE = re.compile(r"Symbol:\s*([^\n\-(]+)", re.I)
PRICE_RE = re.compile(
    r"(?:Price|цена)[:\s]*([\d.,]+)\s*(\$TON|TON|GRAM|USDT)?",
    re.I,
)
POST_ID_RE = re.compile(r'data-post="([^"]+)"')
MORE_BEFORE_RE = re.compile(
    r'tme_messages_more[^>]*data-before="(\d+)"|data-before="(\d+)"[^>]*tme_messages_more|[?&]before=(\d+)',
    re.I,
)
TEXT_RE = re.compile(
    r'class="tgme_widget_message_text[^"]*" dir="auto">(.*?)</div>',
    re.I | re.S,
)
AUTHOR_RE = re.compile(
    r'class="tgme_widget_message_owner_name"[^>]*href="https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,32})"',
    re.I,
)
AUTHOR_RE_ALT = re.compile(
    r'href="https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,32})"[^>]*class="tgme_widget_message_owner_name"',
    re.I,
)
AUTHOR_LABEL_RE = re.compile(
    r'class="tgme_widget_message_owner_name"[^>]*>(.*?)</a>',
    re.I | re.S,
)
TAG_RE = re.compile(r"<[^>]+>")
OWNER_TD_RE = re.compile(
    r"<th>\s*(?:Owner|Владелец)\s*</th>\s*<td[^>]*>(.*?)</td>",
    re.I | re.S,
)
GIFTED_TD_RE = re.compile(
    r"<th>\s*Gifted(?:\s+to)?\s*</th>\s*<td[^>]*>(.*?)</td>",
    re.I | re.S,
)
OWNER_RE = re.compile(r"Owner</td>\s*<td[^>]*>\s*([^<]+)", re.I)
OWNER_MD_RE = re.compile(r"Owner\s*\|\s*(.+)", re.I)
NFT_SLUG_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:nft/|gift/)([A-Za-z0-9_+\-]+)",
    re.I,
)
TG_LINK_RE = re.compile(
    r"https?://t\.me/(?!nft/|s/|joinchat/|share/|addstickers/|iv\?|c/)([A-Za-z][A-Za-z0-9_]{3,32})(?!/)",
    re.I,
)
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{3,32})")
GIFTED_TO_RE = re.compile(r"Gifted to\s+(@?[^\n,<]+?)(?:\s+on\s+|\s*$)", re.I)
GIFTED_DATE_RE = re.compile(
    r"\bon\s+(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})",
    re.I,
)
SOLD_TO_RE = re.compile(
    r"(?:sold to|bought by|buyer|покупатель|купил[аи]?)[:\s]+@([A-Za-z][A-Za-z0-9_]{3,32})",
    re.I,
)
OWNER_HREF_RE = re.compile(
    r'href="https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,32})"',
    re.I,
)
FORWARD_RE = re.compile(
    r'tgme_widget_message_forwarded_from[^>]*>.*?href="https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,32})"',
    re.I | re.S,
)
USER_ID_RE = re.compile(r"tg://user\?id=(\d{5,15})")
GIFTISH_RE = re.compile(
    r"(?:t\.me/nft|telegram\.me/nft|gift sold|gift listed|подар|collectible|\bnft\b|уникальн)",
    re.I,
)

SKIP_USERS = {
    "mrkt",
    "portals",
    "giftrelayer",
    "giftstoportals",
    "tonnel_network_bot",
    "tonnelgift",
    "telegram",
    "premiumbot",
    "giftdirector",
    "mrkt_help_service",
    "portals_help_bot",
    "fragment",
    "getgems",
    "durov",
    "wallet",
    "tons",
}
SKIP_USERS.update(u.lower() for u in catalog_usernames())

DEPOSIT_OWNERS = {
    "gift deposit",
    "mrkt bank",
    "mrkt",
    "portals",
    "tonnel",
    "gift relayer",
    "giftrelayer",
    "telegram",
    "unknown",
    "*",
}

TRADER_NICK = (
    "nft",
    "mrkt",
    "portal",
    "tonnel",
    "fragment",
    "getgem",
    "trade",
    "trader",
    "p2p",
    "otc",
    "floor",
    "whale",
    "listing",
    "market",
)

WHALE_SLUGS = (
    "plushpepe",
    "durovscap",
    "durovsglasses",
    "heartlocket",
    "preciouspeach",
    "signetring",
    "swisswatch",
    "scaredcat",
    "lootbag",
    "astralshard",
    "gemsignet",
    "nailbracelet",
    "vintagecigar",
    "heroichelmet",
    "minioscar",
    "bondedring",
)


def _clean_user(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lstrip("@")
    value = re.sub(r"[^A-Za-z0-9_]", "", value)
    if len(value) < 5:
        return None
    low = value.lower()
    if low in SKIP_USERS:
        return None
    if low.endswith("bot"):
        return None
    if low.endswith("notify") or low.endswith("notification"):
        return None
    if "official" in low or low.endswith("_channel"):
        return None
    return value


def extract_people(html: str, text: str, author: str | None = None, author_name: str | None = None) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def add(uname: str | None, name: str | None = None) -> None:
        clean = _clean_user(uname)
        if not clean:
            return
        key = clean.lower()
        if key in seen:
            return
        seen.add(key)
        found.append((clean, name))

    add(author, author_name)
    for raw in MENTION_RE.findall(text or "") + MENTION_RE.findall(html or ""):
        add(raw)
    for raw in TG_LINK_RE.findall(html or "") + TG_LINK_RE.findall(text or ""):
        add(raw)
    for raw in FORWARD_RE.findall(html or ""):
        add(raw)
    sold = SOLD_TO_RE.search(text or "") or SOLD_TO_RE.search(html or "")
    if sold:
        add(sold.group(1))
    return found


def is_deposit_owner(name: str | None) -> bool:
    if not name:
        return False
    return name.strip().lower() in DEPOSIT_OWNERS


def _person_from_cell(cell: str) -> tuple[str | None, str | None]:
    href = OWNER_HREF_RE.search(cell or "")
    name = TAG_RE.sub("", cell or "").strip() or None
    if href:
        uname = _clean_user(href.group(1))
        if uname:
            return uname, name
    if name and name.startswith("@"):
        uname = _clean_user(name)
        if uname:
            return uname, None
    if name and not is_deposit_owner(name):
        return None, name[:64]
    return None, None


def owner_from_nft_card(html: str) -> tuple[str | None, str | None]:
    """Только ячейка Owner на карточке NFT. Никаких упоминаний из поста."""
    td = OWNER_TD_RE.search(html or "")
    if not td:
        return None, None
    cell = td.group(1)
    name = TAG_RE.sub("", cell).strip() or None
    if name and is_deposit_owner(name):
        return None, None
    href = OWNER_HREF_RE.search(cell)
    if href:
        uname = _clean_user(href.group(1))
        if uname:
            return uname, name
    mention = MENTION_RE.search(name or "")
    if mention:
        uname = _clean_user(mention.group(1))
        if uname:
            return uname, name
    return None, None


def looks_like_trader(username: str | None) -> bool:
    low = (username or "").lower()
    return any(part in low for part in TRADER_NICK)


def is_whale_gift(slug: str | None, title: str | None = None) -> bool:
    key = re.sub(r"[^a-z0-9]", "", f"{slug or ''}{title or ''}".lower())
    return any(w in key for w in WHALE_SLUGS)


def parse_gifted_at(blob: str | None) -> int | None:
    match = GIFTED_DATE_RE.search(blob or "")
    if not match:
        return None
    raw = f"{match.group(1)} {match.group(2)[:3].title()} {match.group(3)}"
    try:
        dt = datetime.strptime(raw, "%d %b %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


@dataclass
class NftCard:
    owner: str | None = None
    owner_name: str | None = None
    gifted_user: str | None = None
    gifted_at: int | None = None


def parse_nft_card(html: str) -> NftCard:
    owner, owner_name = owner_from_nft_card(html)
    gifted_user = None
    gifted_td = GIFTED_TD_RE.search(html or "")
    if gifted_td:
        gifted_user, _ = _person_from_cell(gifted_td.group(1))
    if not gifted_user:
        gifted = GIFTED_TO_RE.search(TAG_RE.sub(" ", html or ""))
        if gifted:
            gifted_user = _clean_user(gifted.group(1))
    return NftCard(
        owner=owner,
        owner_name=owner_name,
        gifted_user=gifted_user,
        gifted_at=parse_gifted_at(html),
    )


def why_not_noob(
    uname: str,
    card: NftCard,
    *,
    slug: str,
    title: str | None,
    price: float,
    asset: str,
    deal_kind: str,
    max_age_days: int = 2,
) -> str | None:
    """Почему это не лох. None = похож на лёгкую цель."""
    if looks_like_trader(uname):
        return "ник как у трейдера"
    if is_whale_gift(slug, title):
        return "дорогой/прошаренный подарок"
    if deal_kind == "sold":
        return "купил на маркете — уже шарит"
    asset_u = (asset or "").upper()
    if price and asset_u in {"TON", "$TON"} and price >= 8:
        return f"цена {price:g} TON"
    if price and asset_u == "GRAM" and price >= 6:
        return f"цена {price:g} GRAM"
    if card.gifted_at is None:
        return "нет даты Gifted to"
    age_days = (datetime.now(timezone.utc).timestamp() - card.gifted_at) / 86400
    if age_days > max_age_days:
        return f"подарок получен {age_days:.0f} дн. назад"
    if age_days < 0:
        return "кривая дата"
    if card.gifted_user and card.owner and card.gifted_user.lower() != card.owner.lower():
        return "уже перепродавали, не первый владелец"
    return None


def web_chat_id(username: str) -> int:
    return -1_000_000_000 - (zlib.crc32(username.lower().encode("utf-8")) % 900_000_000)


def web_user_id(username: str) -> int:
    return 1_000_000_000 + (zlib.crc32(username.lower().encode("utf-8")) % 800_000_000)


def name_to_slug(name: str, num: int) -> str:
    cleaned = name.replace("'", "").replace("'", "").replace("'", "").replace(" ", "")
    cleaned = re.sub(r"[^A-Za-z0-9\-]", "", cleaned)
    return f"{cleaned}-{num}" if cleaned else str(num)


def _strip_html(raw: str) -> str:
    text = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("</a>", " ")
    text = TAG_RE.sub("", text)
    return unescape(re.sub(r"\n{2,}", "\n", text)).strip()


@dataclass
class WebPost:
    key: str
    text: str
    html: str
    author: str | None = None
    author_name: str | None = None


@dataclass
class WebDeal:
    key: str
    channel: str
    gift: GiftInfo
    price: float
    asset: str
    kind: str  # buy / listing
    text: str
    owner: str | None = None
    username: str | None = None


class PublicFeed:
    def __init__(self) -> None:
        self._http = None

    async def close(self) -> None:
        return None

    def _fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers=HEADERS)
        contexts = [None, ssl._create_unverified_context()]
        last_error: Exception | None = None
        for ctx in contexts:
            try:
                with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                    if resp.status != 200:
                        continue
                    return resp.read().decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return ""

    def _posts_from_html(self, username: str, html: str) -> list[WebPost]:
        posts: list[WebPost] = []
        markers = list(POST_ID_RE.finditer(html or ""))
        if not markers:
            text = _strip_html(html or "")
            if text:
                posts.append(WebPost(key=f"{username}:page", text=text, html=html))
            return posts

        for i, match in enumerate(markers):
            start = match.start()
            end = markers[i + 1].start() if i + 1 < len(markers) else len(html)
            block = html[start:end]
            text_m = TEXT_RE.search(block)
            if not text_m:
                continue
            text = _strip_html(text_m.group(1))
            if not text:
                continue
            author_m = AUTHOR_RE.search(block) or AUTHOR_RE_ALT.search(block)
            raw_author = author_m.group(1) if author_m else None
            label_m = AUTHOR_LABEL_RE.search(block)
            author_name = _strip_html(label_m.group(1)) if label_m else None
            posts.append(
                WebPost(
                    key=match.group(1),
                    text=text,
                    html=block,
                    author=_clean_user(raw_author),
                    author_name=author_name,
                )
            )
        return posts

    def _next_before(self, html: str, posts: list[WebPost]) -> str | None:
        match = MORE_BEFORE_RE.search(html or "")
        if match:
            return next((g for g in match.groups() if g), None)
        ids: list[int] = []
        for post in posts:
            if "/" in (post.key or ""):
                tail = post.key.rsplit("/", 1)[-1]
                if tail.isdigit():
                    ids.append(int(tail))
        return str(min(ids)) if ids else None

    async def channel_posts(self, username: str, pages: int = 1) -> list[WebPost]:
        slug = username.lstrip("@")
        collected: list[WebPost] = []
        seen_keys: set[str] = set()
        before: str | None = None
        last_error: Exception | None = None
        pages = max(1, min(int(pages or 1), 6))

        for _ in range(pages):
            html = ""
            urls = (
                [f"https://t.me/s/{slug}", f"https://telegram.me/s/{slug}"]
                if not before
                else [f"https://t.me/s/{slug}?before={before}"]
            )
            for url in urls:
                try:
                    html = await asyncio.to_thread(self._fetch, url)
                except Exception as exc:
                    last_error = exc
                    html = ""
                if html:
                    break
            if not html:
                if not collected:
                    log.warning("Не прочитал @%s: %s", username, last_error or "пусто")
                break
            page_posts = self._posts_from_html(username, html)
            new = 0
            for post in page_posts:
                if post.key in seen_keys:
                    continue
                seen_keys.add(post.key)
                collected.append(post)
                new += 1
            if new == 0:
                break
            next_before = self._next_before(html, page_posts)
            if not next_before or next_before == before:
                break
            before = next_before
            await asyncio.sleep(0.2)
        return collected

    def post_people(self, post: WebPost) -> list[tuple[str, str | None]]:
        return extract_people(post.html, post.text, post.author, post.author_name)

    def is_giftish(self, post: WebPost) -> bool:
        blob = f"{post.text}\n{post.html}"
        return bool(GIFTISH_RE.search(blob) or NFT_SLUG_RE.search(blob))

    async def gift_card(self, slug: str) -> NftCard:
        for url in (f"https://telegram.me/nft/{slug}", f"https://t.me/nft/{slug}"):
            try:
                html = await asyncio.to_thread(self._fetch, url)
            except Exception:
                continue
            if not html:
                continue
            card = parse_nft_card(html)
            if card.owner or card.gifted_at:
                return card
        return NftCard()

    async def gift_owner(self, slug: str) -> tuple[str | None, str | None]:
        card = await self.gift_card(slug)
        return card.owner, card.owner_name

    def is_channel(self, username: str) -> bool:
        """Публичный канал/группа, не личный аккаунт."""
        uname = (username or "").lstrip("@").lower()
        if not uname:
            return True
        cached = getattr(self, "_channel_cache", None)
        if cached is None:
            self._channel_cache = {}
            cached = self._channel_cache
        if uname in cached:
            return cached[uname]
        html = ""
        try:
            html = self._fetch(f"https://t.me/s/{uname}")
        except Exception:
            html = ""
        blob = (html or "").lower()
        channel = any(
            mark in blob
            for mark in (
                "subscribers",
                "подписчик",
                "tgme_channel_info",
                "tgme_page_extra",
            )
        ) and ("tgme_widget_message" in blob or "subscribers" in blob or "подписчик" in blob)
        # Личный профиль на t.me/s обычно без ленты постов
        if "you can contact @" in blob and "tgme_widget_message" not in blob:
            channel = False
        cached[uname] = bool(channel)
        return cached[uname]

    async def username_is_channel(self, username: str) -> bool:
        return await asyncio.to_thread(self.is_channel, username)

    async def fragment_people(self) -> list[tuple[str, str | None]]:
        found: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for url in (
            "https://fragment.com/gifts",
            "https://fragment.com/gifts?sort=ending",
        ):
            try:
                html = await asyncio.to_thread(self._fetch, url)
            except Exception as exc:
                log.warning("Fragment %s: %s", url, exc)
                continue
            if not html:
                continue
            for uname, name in extract_people(html, _strip_html(html)):
                key = uname.lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append((uname, name))
        return found

    def parse_deals(self, channel: str, post: WebPost) -> list[WebDeal]:
        text, html = post.text, post.html
        sold = SOLD_RE.search(text)
        listed = LISTED_RE.search(text)
        if sold:
            kind = "sold"
            name, num_s = sold.group(1).strip(), sold.group(2)
        elif listed:
            kind = "listed"
            name, num_s = listed.group(1).strip(), listed.group(2)
        else:
            return []
        if "bundle" in text.lower():
            return []

        price_m = PRICE_RE.search(text)
        price = 0.0
        if price_m:
            raw_price = price_m.group(1).replace(" ", "").replace(",", ".")
            if any(ch.isdigit() for ch in raw_price):
                try:
                    price = float(raw_price)
                except ValueError:
                    price = 0.0
        asset = (price_m.group(2) or "").replace("$", "") if price_m else ""
        if price and not asset:
            asset = "TON"
        model = MODEL_RE.search(text).group(1).strip() if MODEL_RE.search(text) else None
        backdrop = BACKDROP_RE.search(text).group(1).strip() if BACKDROP_RE.search(text) else None
        symbol = SYMBOL_RE.search(text).group(1).strip() if SYMBOL_RE.search(text) else None

        slugs = NFT_SLUG_RE.findall(text) + NFT_SLUG_RE.findall(html)
        slug = slugs[0] if slugs else name_to_slug(name, int(num_s))
        num = int(num_s)
        if "-" in slug:
            tail = slug.rsplit("-", 1)[-1]
            if tail.isdigit():
                num = int(tail)
        gift = GiftInfo(
            title=name,
            slug=slug,
            num=num,
            model=model,
            backdrop=backdrop,
            symbol=symbol,
        )
        return [
            WebDeal(
                key=f"{kind}:{slug.lower()}",
                channel=channel,
                gift=gift,
                price=price,
                asset=asset,
                kind=kind,
                text=text[:400],
            )
        ]
