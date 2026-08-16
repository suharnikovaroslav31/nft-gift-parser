from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from nft_parser.models import GiftInfo

log = logging.getLogger(__name__)

PORTALS_API = "https://portal-market.com/api/"
TONNEL_SALE = "https://gifts2.tonnel.network/api/saleHistory"

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _username(data: dict[str, Any]) -> str | None:
    for key in ("username", "user_name", "telegram_username"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lstrip("@")
    for nested_key in ("user", "buyer", "owner", "seller"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            val = nested.get("username") or nested.get("user_name")
            if isinstance(val, str) and val.strip():
                return val.strip().lstrip("@")
        elif isinstance(nested, str) and nested.startswith("@"):
            return nested[1:]
    return None


@dataclass
class MarketLead:
    user_id: int | None
    username: str | None
    gift: GiftInfo | None
    price: float
    key: str


def gift_from_portals_nft(nft: dict[str, Any]) -> GiftInfo:
    model = backdrop = symbol = None
    for attr in nft.get("attributes") or []:
        kind = attr.get("type")
        if kind == "model":
            model = attr.get("value")
        elif kind == "backdrop":
            backdrop = attr.get("value")
        elif kind == "symbol":
            symbol = attr.get("value")
    name = nft.get("name") or "Gift"
    num = int(nft.get("external_collection_number") or nft.get("tg_id") or 0)
    short = name.replace(" ", "").replace("'", "")
    slug = f"{short}-{num}" if num else short
    return GiftInfo(title=name, slug=slug, num=num, model=model, backdrop=backdrop, symbol=symbol)


class Marketplace:
    def __init__(self, portals_auth: str = "", tonnel_auth: str = "") -> None:
        self.portals_auth = portals_auth
        self.tonnel_auth = tonnel_auth
        self._http = httpx.AsyncClient(timeout=25.0, headers=BROWSER_HEADERS)

    async def close(self) -> None:
        await self._http.aclose()

    async def portals_actions(self, action_type: str = "buy", limit: int = 20) -> list[dict[str, Any]]:
        if not self.portals_auth:
            return []
        params = {
            "offset": 0,
            "limit": limit,
            "sort_by": "listed_at desc",
            "action_types": action_type,
        }
        try:
            response = await self._http.get(
                PORTALS_API + "market/actions/",
                params=params,
                headers={**BROWSER_HEADERS, "Authorization": self.portals_auth, "Origin": "https://portal-market.com", "Referer": "https://portal-market.com/"},
            )
            if response.status_code == 401:
                log.warning("Portals auth просрочен")
                return []
            response.raise_for_status()
            return list(response.json().get("actions") or [])
        except Exception as exc:
            log.warning("Portals actions: %s", exc)
            return []

    async def portals_listed(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.portals_auth:
            return []
        params = {"offset": 0, "limit": limit, "sort_by": "listed_at desc", "status": "listed"}
        try:
            response = await self._http.get(
                PORTALS_API + "nfts/search",
                params=params,
                headers={**BROWSER_HEADERS, "Authorization": self.portals_auth, "Origin": "https://portal-market.com", "Referer": "https://portal-market.com/"},
            )
            if response.status_code == 401:
                log.warning("Portals auth просрочен")
                return []
            response.raise_for_status()
            return list(response.json().get("results") or [])
        except Exception as exc:
            log.warning("Portals listings: %s", exc)
            return []

    async def tonnel_sales(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.tonnel_auth:
            return []
        payload = {
            "page": 1,
            "limit": min(limit, 50),
            "type": "SALE",
            "filter": "{}",
            "sort": '{"createdAt":-1}',
            "user_auth": self.tonnel_auth,
        }
        try:
            response = await self._http.post(
                TONNEL_SALE,
                json=payload,
                headers={
                    **BROWSER_HEADERS,
                    "Origin": "https://market.tonnel.network",
                    "Referer": "https://market.tonnel.network/",
                    "Content-Type": "application/json",
                },
            )
            if response.status_code in {401, 403}:
                log.warning("Tonnel недоступен (%s)", response.status_code)
                return []
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data
            return list(data.get("gifts") or data.get("data") or [])
        except Exception as exc:
            log.warning("Tonnel sales: %s", exc)
            return []

    def parse_portals_action(self, action: dict[str, Any]) -> MarketLead:
        nft = action.get("nft") or {}
        gift = gift_from_portals_nft(nft) if nft else None
        price = _as_float(action.get("amount") or nft.get("price"))
        user_id = None
        for key in ("user_id", "buyer_id", "target_user_id", "owner_id"):
            user_id = _as_int(action.get(key))
            if user_id:
                break
        if user_id is None:
            user_id = _as_int(nft.get("owner_id"))
        key = str(action.get("id") or f"{user_id}:{gift.slug if gift else ''}:{action.get('created_at')}")
        return MarketLead(user_id, _username(action) or _username(nft), gift, price, key)

    def parse_portals_listing(self, nft: dict[str, Any]) -> MarketLead:
        gift = gift_from_portals_nft(nft)
        price = _as_float(nft.get("price"))
        user_id = _as_int(nft.get("owner_id"))
        key = f"list:{nft.get('id') or gift.slug}"
        return MarketLead(user_id, _username(nft), gift, price, key)

    def parse_tonnel_sale(self, item: dict[str, Any]) -> MarketLead:
        name = item.get("name") or "Gift"
        num = int(item.get("gift_num") or 0)
        short = str(name).replace(" ", "")
        gift = GiftInfo(
            title=str(name),
            slug=f"{short}-{num}" if num else short,
            num=num,
            model=item.get("model"),
            backdrop=item.get("backdrop"),
            symbol=item.get("symbol"),
        )
        price = _as_float(item.get("price"))
        user_id = None
        for key in ("buyer", "buyerId", "buyer_id", "userId", "user_id", "ownerId"):
            value = item.get(key)
            if isinstance(value, dict):
                user_id = _as_int(value.get("id") or value.get("telegram_id"))
            else:
                user_id = _as_int(value)
            if user_id:
                break
        ident = item.get("gift_id") or item.get("_id") or f"{gift.slug}:{item.get('export_at')}"
        return MarketLead(user_id, _username(item), gift, price, f"tonnel:{ident}")
