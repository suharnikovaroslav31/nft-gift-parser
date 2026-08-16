from __future__ import annotations

# Пакеты:
#   Gifts — t.me/addemoji/DMJGiftsEmoji  (NFT-подарки Telegram)
#   News  — t.me/addemoji/NewsEmoji      (анимированные иконки)
#   UI    — t.me/addemoji/TgAndroidIcons (минималистичные иконки)
#
# Бот рисует <tg-emoji>, если у владельца бота есть Telegram Premium (Bot API 9.4).
# Иначе клиент показывает обычный unicode из fallback.

IDS: dict[str, str] = {
    "gift": "6003312957713293120",  # 🎁 Gifts
    "teddy": "5956471060835605059",  # 🧸 Gifts
    "fire": "5424972470023104089",  # 🔥 News
    "star": "5438496463044752972",  # ⭐ News
    "new": "5382357040008021292",  # 🆕 News
    "pin": "5397782960512444700",  # 📌 News
    "place": "5391032818111363540",  # 📍 News
    "link": "5271604874419647061",  # 🔗 News
    "check": "5206607081334906820",  # ✅ News
    "cross": "5210952531676504517",  # ❌ News
    "bell": "5458603043203327669",  # 🔔 News
    "money": "5409048419211682843",  # 💵 News
    "graph": "5231200819986047254",  # 📊 News
    "info": "5323442290708985472",  # ℹ️ News
    "chat": "5443038326535759644",  # 💬 News
    "play": "5348125953090403204",  # ▶️ News
    "pause": "5359543311897998264",  # ⏸ News
    "plus": "5397916757333654639",  # ➕ News
    "search": "5874960879434338403",  # 🔍 UI
    "user": "5879770735999717115",  # 👤 UI
    "settings": "5877260593903177342",  # ⚙️ UI
    "list": "5877597667231534929",  # 📋 UI
    "store": "5983399041197675256",  # 🛒 UI
    "back": "5967355281057779430",  # ⬅️ UI
    "trash": "5879896690210639947",  # 🗑 UI
    "stars": "5172484558305625218",  # ⭐ Telegram Stars
    "warn": "5274099962655816924",  # ❗ News
    "top": "5415655814079723871",  # 🔝 News
}

FALLBACK: dict[str, str] = {
    "gift": "🎁",
    "teddy": "🧸",
    "fire": "🔥",
    "star": "⭐",
    "new": "✨",
    "pin": "📌",
    "place": "📍",
    "link": "🔗",
    "check": "✅",
    "cross": "❌",
    "bell": "🔔",
    "money": "💰",
    "graph": "📊",
    "info": "ℹ️",
    "chat": "💬",
    "play": "▶️",
    "pause": "⏸",
    "plus": "➕",
    "search": "🔍",
    "user": "👤",
    "settings": "⚙️",
    "list": "📋",
    "store": "🛒",
    "back": "⬅️",
    "trash": "🗑",
    "stars": "⭐",
    "warn": "❗",
    "top": "💎",
}


def pe(key: str, fallback: str | None = None) -> str:
    eid = IDS.get(key)
    text = fallback or FALLBACK.get(key, "•")
    if not eid:
        return text
    return f'<tg-emoji emoji-id="{eid}">{text}</tg-emoji>'


def icon(key: str) -> str | None:
    return IDS.get(key)
