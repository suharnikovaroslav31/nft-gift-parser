from __future__ import annotations

# Рынок первый: там Gift Sold и ссылки t.me/nft. Человека берём только с карточки Owner.

CATALOG: list[dict[str, str]] = [
    {"username": "giftsotc", "title": "Gifts OTC", "kind": "chat"},
    {"username": "nftgifts", "title": "NFT Gifts", "kind": "chat"},
    {"username": "giftsdevschat", "title": "Gifts Devs Chat", "kind": "chat"},
    {"username": "giftstradinghub", "title": "Gifts Trading Hub", "kind": "chat"},
    {"username": "tonnftmarketplace", "title": "TON NFT Marketplace", "kind": "chat"},
    {"username": "nft_chat", "title": "NFT Chat", "kind": "chat"},
    {"username": "otc_gift_chat", "title": "OTC Gift Chat", "kind": "chat"},
    {"username": "GiftTrade", "title": "Gift Trade", "kind": "chat"},
    {"username": "gifts_p2p", "title": "Gifts P2P", "kind": "chat"},
    {"username": "GiftChat", "title": "Gift Chat", "kind": "chat"},
    {"username": "tggifts", "title": "TG Gifts", "kind": "chat"},
    {"username": "giftsdevs", "title": "Gifts Devs", "kind": "news"},
    {"username": "portals_community", "title": "Portals Community", "kind": "news"},
    {"username": "tonnel_ru", "title": "Tonnel RU", "kind": "news"},
    {"username": "tonnel_en", "title": "Tonnel EN", "kind": "news"},
    {"username": "unique_gifts", "title": "Unique Gifts", "kind": "news"},
    {"username": "getgems", "title": "Getgems", "kind": "news"},
    {"username": "GetgemsNews", "title": "Getgems News", "kind": "news"},
    {"username": "mrktnotification", "title": "MRKT — продажи", "kind": "market"},
    {"username": "mrkt_stickers_notify", "title": "MRKT — стикеры", "kind": "market"},
    {"username": "mrkt_channels_notify", "title": "MRKT — каналы", "kind": "market"},
    {"username": "mrkt_goods_notify", "title": "MRKT — товары", "kind": "market"},
    {"username": "mrkt_collections_notify", "title": "MRKT — коллекции", "kind": "market"},
    {"username": "TonnelGift", "title": "Tonnel Gifts", "kind": "market"},
    {"username": "GiftsNotify", "title": "Gifts Notify", "kind": "market"},
    {"username": "official_mrkt", "title": "MRKT официальный", "kind": "market"},
    {"username": "mrkt_pulse", "title": "MRKT Pulse", "kind": "market"},
]

MARKETPLACES: list[dict[str, str]] = [
    {
        "name": "Portals",
        "bot": "@portals",
        "link": "https://t.me/portals",
        "channel": "@portals_community",
        "note": "Сообщество Portals, не лента продаж.",
    },
    {
        "name": "Tonnel",
        "bot": "@Tonnel_Network_bot",
        "link": "https://t.me/Tonnel_Network_bot",
        "channel": "@tonnel_ru",
        "note": "Новости Tonnel и люди вокруг подарков.",
    },
    {
        "name": "Fragment",
        "bot": "fragment.com",
        "link": "https://fragment.com/gifts",
        "channel": "",
        "note": "Официальные коллекционные подарки, смотрим владельцев.",
    },
    {
        "name": "Getgems",
        "bot": "getgems.io",
        "link": "https://getgems.io",
        "channel": "",
        "note": "On-chain подарки в TON.",
    },
]

KIND_RANK = {"market": 0, "news": 1, "community": 1, "chat": 2}


def tracker_usernames() -> list[str]:
    """Каналы с реальными Gift Sold / New Gift Listed, не пустые чаты."""
    keep = {
        "mrktnotification",
        "tonnelgift",
        "mrkt_stickers_notify",
        "nftgifts",
        "giftsotc",
        "giftsdevs",
    }
    return [item["username"] for item in CATALOG if item["username"].lower() in keep]


def catalog_usernames() -> list[str]:
    return [item["username"] for item in CATALOG]


def catalog_kind(username: str) -> str:
    key = username.lstrip("@").lower()
    for item in CATALOG:
        if item["username"].lower() == key:
            return item.get("kind") or "chat"
    return "chat"


def people_scan_order(usernames: list[str]) -> list[str]:
    return sorted(
        usernames,
        key=lambda name: (KIND_RANK.get(catalog_kind(name), 0), name.lower()),
    )
