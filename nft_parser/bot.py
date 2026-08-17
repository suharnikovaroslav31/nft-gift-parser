from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from nft_parser.catalog import CATALOG, MARKETPLACES
from nft_parser.config import clean_session_value, session_file_path
from nft_parser.db import Database
from nft_parser.takeover import submit_cloud_password
from nft_parser.emoji import icon, pe
from nft_parser.web_feed import web_chat_id

if TYPE_CHECKING:
    from nft_parser.app import App

log = logging.getLogger(__name__)
router = Router()
BUILD = "17aug-l"

FILTER_HINTS = {
    "newbie_max": "Сколько unique NFT максимум (лох: 1–2).",
    "max_tg_level": "Максимальный уровень Telegram. Выше — уже не лох.",
    "max_gift_ton": "Максимальная цена подарка в TON.",
    "max_gift_usd": "Максимальная цена подарка в $.",
    "cheap_list_ton": "Если листинг дороже — это уже не лох.",
    "min_price_ton": "Минимальная цена с маркета (0 = без лимита).",
}

FREQ_PRESETS: list[tuple[int, str]] = [
    (0, "сразу"),
    (10, "10 сек"),
    (30, "30 сек"),
    (60, "1 мин"),
    (120, "2 мин"),
    (300, "5 мин"),
]


def freq_seconds(filters: dict[str, Any]) -> int:
    try:
        return max(0, int(float(filters.get("notify_every_sec") or 0)))
    except (TypeError, ValueError):
        return 0


def freq_label(sec: int) -> str:
    labels = {
        0: "сразу",
        10: "каждые 10 сек",
        30: "каждые 30 сек",
        60: "каждую минуту",
        120: "каждые 2 мин",
        300: "каждые 5 мин",
    }
    if sec in labels:
        return labels[sec]
    if sec < 60:
        return f"каждые {sec} сек"
    mins, rem = divmod(sec, 60)
    if rem == 0:
        return "каждую минуту" if mins == 1 else f"каждые {mins} мин"
    return f"каждые {sec} сек"


class AppMiddleware(BaseMiddleware):
    def __init__(self, app: App) -> None:
        super().__init__()
        self.app = app

    async def __call__(self, handler, event, data):  # type: ignore[no-untyped-def]
        data["app"] = self.app
        user = getattr(event, "from_user", None)
        log.info("Входящее: %s от %s", type(event).__name__, getattr(user, "id", None))
        if not user:
            return None
        if isinstance(event, Message):
            cmd = (event.text or "").split()[0].split("@")[0]
            if cmd == "/start":
                await self.app.db.add_admin(user.id)
                self.app.notifier.skip_ids.discard(user.id)
                if not await self.app.db.get_setting("owner_id"):
                    await self.app.db.set_setting("owner_id", str(user.id))
        if not await is_admin(user.id, self.app.db, self.app.settings.admin_id_list()):
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Нет доступа.")
            return None
        return await handler(event, data)


class Form(StatesGroup):
    add_chat = State()
    check_user = State()
    set_number = State()
    set_session = State()


def btn(text: str, data: str | None = None, *, url: str | None = None, key: str | None = None) -> InlineKeyboardButton:
    kwargs: dict[str, Any] = {"text": text}
    if data:
        kwargs["callback_data"] = data
    if url:
        kwargs["url"] = url
    if key and icon(key):
        kwargs["icon_custom_emoji_id"] = icon(key)
    return InlineKeyboardButton(**kwargs)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def main_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("Профиль", "menu:profile", key="user")],
        [btn("Админ", "menu:admin", key="star")],
        [btn("Фильтры", "menu:filters", key="search"), btn("Настройки", "menu:settings", key="settings")],
    )


def back_kb(to: str = "menu:home") -> InlineKeyboardMarkup:
    return kb([btn("Назад", to, key="back")])


async def is_admin(message_user_id: int, db: Database, env_admins: list[int]) -> bool:
    owner = await db.get_setting("owner_id")
    if owner and str(message_user_id) == str(owner):
        await db.add_admin(message_user_id)
        return True
    db_admins = await db.list_admins()
    scanner = await db.get_setting("scanner_id")
    env_ok = [uid for uid in env_admins if not scanner or str(uid) != str(scanner)]
    if not db_admins and not env_ok:
        await db.add_admin(message_user_id)
        await db.set_setting("owner_id", str(message_user_id))
        return True
    return message_user_id in db_admins or message_user_id in env_ok


async def safe_edit(target: CallbackQuery | Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        else:
            await target.answer(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        log.exception("Не отправил экран")
        if isinstance(target, CallbackQuery):
            await target.answer("Обновляю…", show_alert=False)


def env_report(app: App) -> str:
    settings = app.settings
    session_len = len(settings.session_string.strip())
    api = bool(settings.api_id)
    digest = bool(settings.api_hash)
    session = f"есть ({session_len} симв.)" if session_len else "нет"
    return (
        f"API_ID: <b>{'есть' if api else 'нет'}</b>\n"
        f"API_HASH: <b>{'есть' if digest else 'нет'}</b>\n"
        f"SESSION_STRING: <b>{session}</b>\n"
        f"PHONE: <b>{'есть' if settings.phone else 'нет'}</b>"
    )


def onoff(value: bool) -> str:
    return "вкл" if value else "выкл"


async def scanner_label(app: App) -> str:
    uname = (await app.db.get_setting("scanner_username") or "").strip().lstrip("@")
    sid = (await app.db.get_setting("scanner_id") or "").strip()
    if uname:
        return f"@{uname}"
    if sid:
        return sid
    return "не залогинен"


async def home_text(app: App, user: Any) -> str:
    running = await app.db.is_running()
    stats = await app.db.stats()
    name = html.escape((getattr(user, "first_name", None) or "Admin").strip())
    status = "в работе" if running else "пауза"
    mark = pe("check") if running else pe("pause")
    scanner = await scanner_label(app)
    warn = ""
    if scanner == "не залогинен":
        if app.settings.session_string.strip() and app.settings.api_id:
            warn = (
                f'\n\n{pe("warn")} сессия в env есть, юзербот ещё не подключился к Telegram.\n'
                f"{env_report(app)}"
            )
        else:
            warn = (
                f'\n\n{pe("warn")} <b>карточки не идут</b>: нет сессии юзербота.\n'
                f"{env_report(app)}\n"
                "Пришли боту <code>/setsession</code> и следом строку из "
                "<code>session_string.txt</code>."
            )
    return (
        f'{pe("fire")} <b>Gift Hunter</b>\n\n'
        f'{pe("user")} привет, <b>{name}</b>\n'
        f"{mark} парсер: <b>{status}</b>\n"
        f'{pe("teddy")} юзербот: <b>{html.escape(scanner)}</b>\n'
        f'{pe("star")} находок: <b>{stats["finds"]}</b> · чатов: <b>{stats["chats"]}</b>'
        f"{warn}\n\n"
        f'{pe("pin")} разделы: профиль · <b>админ</b> · фильтры · настройки\n'
        f"<i>сборка {BUILD}</i>"
    )


async def profile_text(app: App, user: Any) -> str:
    stats = await app.db.stats()
    owner = await app.db.get_setting("owner_id")
    role = "владелец" if owner and str(getattr(user, "id", "")) == str(owner) else "админ"
    uname = f"@{user.username}" if getattr(user, "username", None) else "без username"
    scanner = await scanner_label(app)
    return (
        f'{pe("user")} <b>Профиль</b>\n\n'
        f'{pe("pin")} {html.escape(user.full_name or "Admin")}\n'
        f'{pe("link")} {html.escape(uname)}\n'
        f'{pe("info")} id <code>{user.id}</code>\n'
        f'{pe("star")} роль: <b>{role}</b>\n\n'
        f'{pe("teddy")} юзербот-парсер: <b>{html.escape(scanner)}</b>\n'
        f'{pe("bell")} бот-панель: этот чат\n'
        f'{pe("graph")} находок: <b>{stats["finds"]}</b> · чатов: <b>{stats["chats"]}</b>'
    )


async def admin_text(app: App) -> str:
    running = await app.db.is_running()
    stats = await app.db.stats()
    filters = await app.db.get_filters()
    admins = await app.db.list_admins()
    owner = await app.db.get_setting("owner_id") or "—"
    mark = pe("check") if running else pe("pause")
    status = "в работе" if running else "на паузе"
    scanner = await scanner_label(app)
    return (
        f'{pe("star")} <b>Админ</b>\n'
        f"<i>управление парсером</i>\n\n"
        f"{mark} статус: <b>{status}</b>\n"
        f'{pe("user")} юзербот: <b>{html.escape(scanner)}</b>\n'
        f'{pe("pin")} owner id: <code>{html.escape(str(owner))}</code>\n'
        f'{pe("info")} админов: <b>{len(admins)}</b>\n\n'
        f'{pe("graph")} находок: <b>{stats["finds"]}</b> · чатов: <b>{stats["chats"]}</b>\n'
        f'{pe("teddy")} фильтр: ≤{filters.get("newbie_max", 2)} NFT · lvl ≤{filters.get("max_tg_level", 6)} · до {filters.get("max_gift_ton") or 18:g} TON'
    )


def admin_kb(running: bool) -> InlineKeyboardMarkup:
    run_row = (
        [btn("Стоп парсер", "run:off", key="pause")]
        if running
        else [btn("Старт парсер", "run:on", key="play")]
    )
    return kb(
        run_row,
        [btn("Находки", "menu:finds", key="list"), btn("Пробить", "menu:check", key="search")],
        [btn("Чаты", "menu:chats", key="chat"), btn("Маркет", "menu:market", key="store")],
        [btn("Назад в меню", "menu:home", key="back")],
    )


async def filters_text(filters: dict[str, Any]) -> str:
    return (
        f'{pe("search")} <b>Фильтры</b>\n'
        f"<i>лохи: 1–2 NFT, недорого, не трейдеры и не киты</i>\n\n"
        f'{pe("teddy")} unique NFT: <b>≤ {filters.get("newbie_max", 2)}</b>\n'
        f'{pe("star")} Telegram lvl: <b>≤ {filters.get("max_tg_level", 6)}</b>\n'
        f'{pe("money")} цена подарка: <b>≤ {filters.get("max_gift_ton") or 18:g} TON</b>\n'
        f'{pe("graph")} цена в $: <b>≤ {filters.get("max_gift_usd") or 40:g}</b>\n'
        f'{pe("new")} дешёвый листинг: <b>≤ {filters.get("cheap_list_ton") or 12:g} TON</b>\n\n'
        f'{pe("check") if filters.get("newbie_only", True) else pe("cross")} только новички: <b>{onoff(bool(filters.get("newbie_only", True)))}</b>\n'
        f'{pe("check") if filters.get("require_username", True) else pe("cross")} нужен @username: <b>{onoff(bool(filters.get("require_username", True)))}</b>'
    )


def filters_kb(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    return kb(
        [btn(f"Макс. NFT · {filters.get('newbie_max', 2)}", "flt:num:newbie_max", key="teddy")],
        [btn(f"Макс. lvl · {filters.get('max_tg_level', 6)}", "flt:num:max_tg_level", key="star")],
        [btn(f"Макс. TON · {filters.get('max_gift_ton') or 18}", "flt:num:max_gift_ton", key="money")],
        [btn(f"Макс. $ · {filters.get('max_gift_usd') or 40}", "flt:num:max_gift_usd", key="graph")],
        [btn(f"Листинг · {filters.get('cheap_list_ton') or 12} TON", "flt:num:cheap_list_ton", key="new")],
        [
            btn(
                "Новички " + ("✓" if filters.get("newbie_only", True) else "✗"),
                "flt:toggle:newbie_only",
                key="check" if filters.get("newbie_only", True) else "cross",
            )
        ],
        [
            btn(
                "@username " + ("✓" if filters.get("require_username", True) else "✗"),
                "flt:toggle:require_username",
                key="user",
            )
        ],
        [btn("Назад", "menu:home", key="back")],
    )


async def settings_text(app: App) -> str:
    filters = await app.db.get_filters()
    stats = await app.db.stats()
    pending = app.notifier.pending()
    queue_line = f'\n{pe("list")} в очереди: <b>{pending}</b>' if pending else ""
    return (
        f'{pe("settings")} <b>Настройки</b>\n'
        f"<i>как работает парсер</i>\n\n"
        f'{pe("chat")} чаты юзербота: <b>{stats["chats"]}</b>\n'
        f'{pe("store")} API маркета: <b>{onoff(bool(filters.get("market_enabled", True)))}</b>\n'
        f'{pe("user")} читать отправителей: <b>{onoff(bool(filters.get("check_senders", True)))}</b>\n'
        f'{pe("link")} читать NFT-ссылки: <b>{onoff(bool(filters.get("check_gift_links", True)))}</b>\n'
        f'{pe("bell")} карточки: <b>{freq_label(freq_seconds(filters))}</b>'
        f"{queue_line}\n"
        f'{pe("info")} один человек — не чаще раза в 24ч'
    )


def settings_kb(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    return kb(
        [btn("Чаты и каналы", "menu:chats", key="chat")],
        [btn("Маркет", "menu:market", key="store")],
        [btn(f"Частота карточек · {freq_label(freq_seconds(filters))}", "menu:freq", key="bell")],
        [
            btn(
                "Маркет API " + ("✓" if filters.get("market_enabled", True) else "✗"),
                "market:toggle",
                key="store",
            )
        ],
        [
            btn(
                "Отправители " + ("✓" if filters.get("check_senders", True) else "✗"),
                "flt:toggle:check_senders",
                key="user",
            )
        ],
        [
            btn(
                "NFT-ссылки " + ("✓" if filters.get("check_gift_links", True) else "✗"),
                "flt:toggle:check_gift_links",
                key="link",
            )
        ],
        [btn("Назад", "menu:home", key="back")],
    )


def freq_text(filters: dict[str, Any], pending: int = 0) -> str:
    sec = freq_seconds(filters)
    extra = f'\n{pe("list")} сейчас в очереди: <b>{pending}</b>' if pending else ""
    return (
        f'{pe("bell")} <b>Частота карточек</b>\n'
        f"<i>как часто слать находки тебе в бот</i>\n\n"
        f"сейчас: <b>{freq_label(sec)}</b>\n"
        f"{pe('info')} парсер копит карточки и шлёт по одной "
        f"с выбранным интервалом. «сразу» — как только нашёл."
        f"{extra}"
    )


def freq_kb(filters: dict[str, Any]) -> InlineKeyboardMarkup:
    current = freq_seconds(filters)
    rows: list[list[InlineKeyboardButton]] = []
    chunk: list[InlineKeyboardButton] = []
    for sec, name in FREQ_PRESETS:
        mark = "● " if sec == current else ""
        chunk.append(btn(f"{mark}{name}", f"freq:set:{sec}", key="bell" if sec == current else "play"))
        if len(chunk) == 3:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    rows.append([btn("Назад", "menu:settings", key="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chats_kb(chats: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat in chats[:18]:
        flag = "●" if chat["enabled"] else "○"
        name = chat["title"] or str(chat["chat_id"])
        rows.append(
            [
                btn(f"{flag} {name[:26]}", f"chat:toggle:{chat['chat_id']}", key="chat"),
                btn("×", f"chat:del:{chat['chat_id']}", key="trash"),
            ]
        )
    rows.append([btn("Готовый список", "chat:catalog", key="list")])
    rows.append([btn("Добавить чат", "chat:add", key="plus")])
    rows.append([btn("Назад", "menu:settings", key="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def save_session_and_restart(message: Message, raw: str) -> None:
    session = clean_session_value(raw)
    if len(session) < 80:
        await message.answer("Слишком коротко. Нужна вся строка из session_string.txt одним сообщением.")
        return
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session, encoding="utf-8")
    os.environ["SESSION_STRING"] = session
    await message.answer(
        f"Сессия сохранена ({len(session)} симв.). Перезапускаюсь — через минуту жми /start."
    )
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable, *sys.argv])


@router.message(Command("2fa"))
async def cmd_2fa(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Напиши: /2fa пароль_облака юзербота")
        return
    if submit_cloud_password(parts[1]):
        await message.answer("Пароль принял, логинюсь…")
        try:
            await message.delete()
        except Exception:
            pass
        return
    await message.answer("Сейчас облачный пароль не спрашивают.")


@router.message(Command("setsession"))
async def cmd_setsession(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await state.clear()
        await save_session_and_restart(message, parts[1])
        return
    await state.set_state(Form.set_session)
    await message.answer("Пришли одним сообщением строку из session_string.txt.")


@router.message(Form.set_session)
async def got_session(message: Message, state: FSMContext) -> None:
    await state.clear()
    await save_session_and_restart(message, message.text or "")


@router.message(CommandStart())
async def cmd_start(message: Message, app: App) -> None:
    if not message.from_user or not await is_admin(message.from_user.id, app.db, app.settings.admin_id_list()):
        await message.answer("Нет доступа.")
        return
    await app.db.add_admin(message.from_user.id)
    await safe_edit(message, await home_text(app, message.from_user), main_kb())


@router.callback_query(F.data == "menu:home")
async def cb_home(call: CallbackQuery, app: App) -> None:
    await call.answer()
    await safe_edit(call, await home_text(app, call.from_user), main_kb())


@router.callback_query(F.data.startswith("claim:"))
async def cb_claim(call: CallbackQuery, app: App) -> None:
    if not call.from_user or not call.data:
        await call.answer()
        return
    try:
        target_id = int(call.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await call.answer("Карточка сломалась", show_alert=True)
        return
    if not target_id:
        await call.answer("Этого человека нельзя занять", show_alert=True)
        return
    user = call.from_user
    name = f"@{user.username}" if user.username else (user.first_name or "админ").strip()[:32]
    existing = await app.db.get_claim(target_id)
    if existing and int(existing["by_id"]) != user.id:
        await call.answer(f"Уже пишет {existing['by_name']}", show_alert=True)
        return
    if existing and int(existing["by_id"]) == user.id:
        await app.db.clear_claim(target_id)
        await app.notifier.refresh_claim_cards(target_id)
        await call.answer("Снял, можно писать другим")
        return
    await app.db.set_claim(target_id, user.id, name)
    await app.notifier.refresh_claim_cards(target_id)
    await call.answer("Занял. Пиши ему, остальные видят что занято")


@router.callback_query(F.data == "menu:profile")
async def cb_profile(call: CallbackQuery, app: App) -> None:
    await call.answer()
    await safe_edit(call, await profile_text(app, call.from_user), back_kb())


@router.callback_query(F.data.in_({"menu:admin", "menu:panel"}))
async def cb_admin(call: CallbackQuery, app: App) -> None:
    await call.answer()
    running = await app.db.is_running()
    await safe_edit(call, await admin_text(app), admin_kb(running))


@router.callback_query(F.data == "run:on")
async def cb_on(call: CallbackQuery, app: App) -> None:
    await app.db.set_running(True)
    await call.answer("Парсер включён")
    await safe_edit(call, await admin_text(app), admin_kb(True))


@router.callback_query(F.data == "run:off")
async def cb_off(call: CallbackQuery, app: App) -> None:
    await app.db.set_running(False)
    await call.answer("Парсер на паузе")
    await safe_edit(call, await admin_text(app), admin_kb(False))


@router.callback_query(F.data == "menu:chats")
async def cb_chats(call: CallbackQuery, app: App) -> None:
    chats = await app.db.list_chats()
    text = (
        f'{pe("chat")} <b>Чаты</b>\n'
        f"<i>что читает юзербот</i>\n\n"
        f"Включено: <b>{sum(1 for c in chats if c['enabled'])}</b> из {len(chats)}"
    )
    if not chats:
        text = (
            f'{pe("warn")} <b>Чаты</b>\n\n'
            "Список пуст. Нажми «Готовый список» — юзербот сам вступит."
        )
    await safe_edit(call, text, chats_kb(chats))
    await call.answer()


@router.callback_query(F.data == "chat:catalog")
async def cb_chat_catalog(call: CallbackQuery, app: App) -> None:
    await call.answer("Подписываюсь…")
    await safe_edit(call, f'{pe("list")} Добавляю готовые каналы…')
    if app.settings.has_userbot and app.scanner is not None:
        ok, fail = await app.scanner.join_catalog(CATALOG)
    else:
        ok, fail = [], []
        for item in CATALOG:
            await app.db.add_chat(web_chat_id(item["username"]), item["title"], item["username"])
            ok.append(f"{item['title']} (@{item['username']})")
    lines = [f'{pe("list")} <b>Каталог</b>\n']
    if ok:
        lines.append("В работе:\n" + "\n".join(f"{pe('check')} {html.escape(x)}" for x in ok[:15]))
    if fail:
        lines.append("\nНедоступны:\n" + "\n".join(f"{pe('cross')} {html.escape(x)}" for x in fail[:8]))
    chats = await app.db.list_chats()
    await safe_edit(call, "\n".join(lines), chats_kb(chats))


@router.callback_query(F.data == "chat:add")
async def cb_chat_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.add_chat)
    await safe_edit(
        call,
        f'{pe("plus")} <b>Новый чат</b>\n\nПришли @канал, например <code>@mrktnotification</code>',
        back_kb("menu:chats"),
    )
    await call.answer()


@router.message(Form.add_chat)
async def on_add_chat(message: Message, state: FSMContext, app: App) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пришлите ссылку или @username")
        return
    try:
        if app.settings.has_userbot and app.scanner is not None:
            chat_id, title, username = await app.scanner.resolve_and_add(raw)
        else:
            username = raw.replace("https://t.me/", "").replace("t.me/", "").replace("/s/", "").lstrip("@").split("/")[0]
            if not username:
                raise ValueError("Нужен @username публичного канала")
            title = username
            chat_id = web_chat_id(username)
            await app.db.add_chat(chat_id, title, username)
    except ValueError as exc:
        await message.answer(f"{pe('cross')} {html.escape(str(exc))}")
        return
    await state.clear()
    extra = f" @{username}" if username else ""
    await message.answer(
        f'{pe("check")} Добавлен: <b>{html.escape(title)}</b>{html.escape(extra)}\nID: <code>{chat_id}</code>',
        reply_markup=chats_kb(await app.db.list_chats()),
    )


@router.callback_query(F.data.startswith("chat:toggle:"))
async def cb_chat_toggle(call: CallbackQuery, app: App) -> None:
    chat_id = int(call.data.split(":")[-1])
    await app.db.toggle_chat(chat_id)
    chats = await app.db.list_chats()
    await call.message.edit_reply_markup(reply_markup=chats_kb(chats))
    await call.answer()


@router.callback_query(F.data.startswith("chat:del:"))
async def cb_chat_del(call: CallbackQuery, app: App) -> None:
    chat_id = int(call.data.split(":")[-1])
    await app.db.remove_chat(chat_id)
    chats = await app.db.list_chats()
    await safe_edit(
        call,
        f'{pe("chat")} <b>Чаты</b>\n\nВключено: <b>{sum(1 for c in chats if c["enabled"])}</b> из {len(chats)}',
        chats_kb(chats),
    )
    await call.answer("Удалён")


@router.callback_query(F.data == "menu:filters")
async def cb_filters(call: CallbackQuery, app: App) -> None:
    filters = await app.db.get_filters()
    await safe_edit(call, await filters_text(filters), filters_kb(filters))
    await call.answer()


@router.callback_query(F.data == "menu:settings")
async def cb_settings(call: CallbackQuery, app: App) -> None:
    filters = await app.db.get_filters()
    await safe_edit(call, await settings_text(app), settings_kb(filters))
    await call.answer()


@router.callback_query(F.data == "menu:freq")
async def cb_freq(call: CallbackQuery, app: App) -> None:
    filters = await app.db.get_filters()
    await safe_edit(call, freq_text(filters, app.notifier.pending()), freq_kb(filters))
    await call.answer()


@router.callback_query(F.data.startswith("freq:set:"))
async def cb_freq_set(call: CallbackQuery, app: App) -> None:
    try:
        sec = max(0, int(call.data.split(":")[-1]))
    except ValueError:
        await call.answer("Не вышло", show_alert=True)
        return
    filters = await app.db.update_filters(notify_every_sec=sec)
    await safe_edit(call, freq_text(filters, app.notifier.pending()), freq_kb(filters))
    await call.answer(freq_label(sec))


@router.callback_query(F.data.startswith("flt:toggle:"))
async def cb_flt_toggle(call: CallbackQuery, app: App) -> None:
    key = call.data.split(":")[-1]
    filters = await app.db.get_filters()
    filters[key] = not bool(filters.get(key))
    await app.db.set_filters(filters)
    await call.answer("Сохранил")
    if key in {"newbie_only", "require_username"}:
        await safe_edit(call, await filters_text(filters), filters_kb(filters))
    else:
        await safe_edit(call, await settings_text(app), settings_kb(filters))


@router.callback_query(F.data.startswith("flt:num:"))
async def cb_flt_num(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":")[-1]
    await state.set_state(Form.set_number)
    await state.update_data(filter_key=key)
    hint = FILTER_HINTS.get(key, "Пришли число.")
    await call.message.answer(f'{pe("pin")} <b>{html.escape(key)}</b>\n{hint}\n\nНапиши число.')
    await call.answer()


@router.message(Form.set_number)
async def on_set_number(message: Message, state: FSMContext, app: App) -> None:
    data = await state.get_data()
    key = data.get("filter_key")
    raw = (message.text or "").replace(",", ".").strip()
    await state.clear()
    try:
        value: Any = (
            float(raw)
            if key in {"min_price_ton", "max_gift_ton", "max_gift_usd", "cheap_list_ton"}
            else int(float(raw))
        )
    except ValueError:
        await message.answer(f"{pe('cross')} Нужно число", reply_markup=main_kb())
        return
    filters = await app.db.update_filters(**{key: value})
    await message.answer(await filters_text(filters), reply_markup=filters_kb(filters))


@router.callback_query(F.data == "menu:market")
async def cb_market(call: CallbackQuery, app: App) -> None:
    filters = await app.db.get_filters()
    lines = [
        f'{pe("store")} <b>Маркет</b>\n',
        "С лент беру ссылки t.me/nft, человека — Owner на карточке.\n",
    ]
    for item in MARKETPLACES:
        channel = f" · {item['channel']}" if item["channel"] else ""
        lines.append(
            f'{pe("link")} <b>{html.escape(item["name"])}</b> — '
            f'<a href="{item["link"]}">{html.escape(item["bot"])}</a>{html.escape(channel)}'
        )
    lines.append(
        f'\n{pe("check") if filters.get("market_enabled") else pe("cross")} API: '
        f"<b>{onoff(bool(filters.get('market_enabled')))}</b>"
    )
    await safe_edit(
        call,
        "\n".join(lines),
        kb(
            [
                btn(
                    "API маркета " + ("выкл" if filters.get("market_enabled") else "вкл"),
                    "market:toggle",
                    key="store",
                )
            ],
            [btn("Подписать каналы", "chat:catalog", key="list")],
            [btn("Назад", "menu:settings", key="back")],
        ),
    )
    await call.answer()


@router.callback_query(F.data == "market:toggle")
async def cb_market_toggle(call: CallbackQuery, app: App) -> None:
    filters = await app.db.get_filters()
    filters["market_enabled"] = not bool(filters.get("market_enabled"))
    await app.db.set_filters(filters)
    await call.answer("Маркет обновлён")
    await cb_market(call, app)


@router.callback_query(F.data == "menu:check")
async def cb_check(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.check_user)
    await safe_edit(
        call,
        f'{pe("search")} <b>Пробить</b>\n\nПришли @username или numeric ID — юзербот снимет профиль.',
        back_kb("menu:admin"),
    )
    await call.answer()


@router.message(Form.check_user)
async def on_check_user(message: Message, state: FSMContext, app: App) -> None:
    raw = (message.text or "").strip()
    await state.clear()
    if not raw:
        await message.answer("Пусто", reply_markup=main_kb())
        return
    if not app.settings.has_userbot or app.queue is None:
        await message.answer("Пробивка нужна с юзерботом.", reply_markup=main_kb())
        return
    from nft_parser.scanner import CheckJob

    entity: Any = int(raw) if raw.lstrip("-").isdigit() else raw
    await app.queue.put(
        CheckJob(entity=entity, source="manual", source_label="ручная проверка", force=True)
    )
    await message.answer(f"{pe('check')} В очереди. Если пройдёт фильтр — придёт карточка.", reply_markup=main_kb())


@router.callback_query(F.data == "menu:finds")
async def cb_finds(call: CallbackQuery, app: App) -> None:
    rows = await app.db.recent_finds(12)
    if not rows:
        await safe_edit(call, f'{pe("list")} <b>Находки</b>\n\nПока пусто — парсер ещё ищет.', back_kb("menu:admin"))
        await call.answer()
        return
    lines = [f'{pe("list")} <b>Находки</b>\n']
    for row in rows:
        try:
            gifts = json.loads(row["gifts_json"] or "[]")
        except json.JSONDecodeError:
            gifts = []
        uname = f"@{row['username']}" if row["username"] else "без @"
        gift = gifts[0] if gifts else {}
        label = gift.get("title") or ""
        extra = f" — {html.escape(str(label))}" if label else ""
        lines.append(f'{pe("gift")} {html.escape(uname)}{extra}')
    await safe_edit(call, "\n".join(lines), back_kb("menu:admin"))
    await call.answer()


@router.message(Command("scan"))
async def cmd_scan(message: Message, app: App) -> None:
    if not message.from_user or not await is_admin(message.from_user.id, app.db, app.settings.admin_id_list()):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /scan @username")
        return
    if not app.settings.has_userbot or app.queue is None:
        await message.answer("Нужен юзербот.")
        return
    from nft_parser.scanner import CheckJob

    await app.queue.put(
        CheckJob(entity=parts[1].strip(), source="manual", source_label="команда /scan", force=True)
    )
    await message.answer("Проверяю…")


@router.message(Command("backfill"))
async def cmd_backfill(message: Message, app: App) -> None:
    if not message.from_user or not await is_admin(message.from_user.id, app.db, app.settings.admin_id_list()):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /backfill @chat [лимит сообщений]")
        return
    if not app.settings.has_userbot or app.userbot is None or app.scanner is None:
        await message.answer("Нужен юзербот.")
        return
    limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 80
    try:
        entity = await app.userbot.get_entity(parts[1])
        queued = await app.scanner.scan_recent(entity, limit=min(limit, 300))
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")
        return
    await message.answer(f"В очередь поставлено {queued} проверок из истории.")
