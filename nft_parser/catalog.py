from __future__ import annotations

# Чаты и каналы, где люди светят t.me/nft и свежие гифты.
# join_catalog сам отсеет мёртвые юзернеймы.

CATALOG: list[dict[str, str]] = [
    {"username": "Trade_Gifts_Chat", "title": "Торговля Подарками", "kind": "chat"},
    {"username": "giftsotc", "title": "Gifts OTC", "kind": "chat"},
    {"username": "nftgifts", "title": "NFT Gifts", "kind": "chat"},
    {"username": "otc_gift_chat", "title": "NFT OTC", "kind": "chat"},
    {"username": "giftstradinghub", "title": "Gifts Trading Hub", "kind": "chat"},
    {"username": "giftsdevschat", "title": "Gifts Devs Chat", "kind": "chat"},
    {"username": "nft_chat", "title": "NFT Chat", "kind": "chat"},
    {"username": "GiftChat", "title": "Gift Chat", "kind": "chat"},
    {"username": "GiftTrade", "title": "Gift Trade", "kind": "chat"},
    {"username": "gifts_p2p", "title": "Gifts P2P", "kind": "chat"},
    {"username": "tggifts", "title": "TG Gifts", "kind": "chat"},
    {"username": "tonnftmarketplace", "title": "TON NFT Marketplace", "kind": "chat"},
    {"username": "RareOTC", "title": "Rare OTC", "kind": "chat"},
    {"username": "gifts_chat", "title": "Gifts Chat", "kind": "chat"},
    {"username": "GiftOTC", "title": "Gift OTC", "kind": "chat"},
    {"username": "gifts_ton", "title": "Gifts TON", "kind": "chat"},
    {"username": "TONGifts", "title": "TON Gifts", "kind": "chat"},
    {"username": "TelegramGifts", "title": "Telegram Gifts", "kind": "chat"},
    {"username": "collectiblegifts", "title": "Collectible Gifts", "kind": "chat"},
    {"username": "GiftMarketChat", "title": "Gift Market Chat", "kind": "chat"},
    {"username": "gifts_ru", "title": "Gifts RU", "kind": "chat"},
    {"username": "tgifts", "title": "TGifts", "kind": "chat"},
    {"username": "newgifts", "title": "New Gifts", "kind": "chat"},
    {"username": "giftsdevs", "title": "Gifts Devs", "kind": "news"},
    {"username": "portals_community", "title": "Portals Community", "kind": "news"},
    {"username": "tonnel_ru", "title": "Tonnel RU", "kind": "news"},
    {"username": "tonnel_en", "title": "Tonnel EN", "kind": "news"},
    {"username": "unique_gifts", "title": "Unique Gifts", "kind": "news"},
    {"username": "getgems", "title": "Getgems", "kind": "news"},
    {"username": "GetgemsNews", "title": "Getgems News", "kind": "news"},
    {"username": "ShowNFT_EN", "title": "ShowNFT EN", "kind": "news"},
    {"username": "ShowNFT", "title": "ShowNFT", "kind": "news"},
    {"username": "official_mrkt", "title": "MRKT официальный", "kind": "news"},
    {"username": "mrkt_pulse", "title": "MRKT Pulse", "kind": "news"},
    {"username": "mrktnotification", "title": "MRKT — продажи", "kind": "market"},
    {"username": "mrkt_stickers_notify", "title": "MRKT — стикеры", "kind": "market"},
    {"username": "mrkt_channels_notify", "title": "MRKT — каналы", "kind": "market"},
    {"username": "mrkt_goods_notify", "title": "MRKT — товары", "kind": "market"},
    {"username": "mrkt_collections_notify", "title": "MRKT — коллекции", "kind": "market"},
    {"username": "TonnelGift", "title": "Tonnel Gifts", "kind": "market"},
    {"username": "GiftsNotify", "title": "Gifts Notify", "kind": "market"},
    {"username": "gift_otc_alert", "title": "Gift OTC Alert", "kind": "market"},
    {"username": "PortalsNotify", "title": "Portals Notify", "kind": "market"},
    {"username": "portals_listings", "title": "Portals Listings", "kind": "market"},
    {"username": "floorgifts", "title": "Floor Gifts", "kind": "market"},
    {"username": "giftalerts", "title": "Gift Alerts", "kind": "market"},
    {"username": "giftsdrop", "title": "Gifts Drop", "kind": "market"},
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
        "name": "MRKT",
        "bot": "@mrkt",
        "link": "https://t.me/mrkt",
        "channel": "@mrktnotification",
        "note": "Листинги и продажи подарков.",
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
    """Публичные ленты t.me/s: маркет + жирные чаты со ссылками t.me/nft."""
    keep = {
        item["username"].lower()
        for item in CATALOG
        if item.get("kind") in {"market", "news", "chat"}
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
        key=lambda name: (KIND_RANK.get(catalog_kind(name), 9), name.lower()),
    )
