from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

DEFAULT_FILTERS: dict[str, Any] = {
    "min_unique": 1,
    "max_unique": 0,
    "recent_hours": 0,
    "newbie_max": 4,
    "newbie_only": True,
    "min_price_ton": 0.0,
    "require_username": False,
    "skip_sold": False,
    "skip_listed": False,
    "show_sold": True,
    "show_listed": True,
    "chats_enabled": True,
    "market_enabled": True,
    "check_senders": True,
    "check_gift_links": True,
    "notify_every_sec": 0,
}


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                username TEXT,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS users_cache (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                unique_count INTEGER NOT NULL DEFAULT 0,
                fingerprint TEXT NOT NULL DEFAULT '',
                last_checked INTEGER NOT NULL DEFAULT 0,
                last_notified INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS finds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                source TEXT NOT NULL,
                unique_count INTEGER NOT NULL,
                gifts_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_market (
                key TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS pending_owners (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                num INTEGER NOT NULL DEFAULT 0,
                channel TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'buy',
                price REAL NOT NULL DEFAULT 0,
                asset TEXT NOT NULL DEFAULT '',
                tries INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claims (
                target_id INTEGER PRIMARY KEY,
                by_id INTEGER NOT NULL,
                by_name TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS card_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                gift_url TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_card_target ON card_messages(target_id);
            """
        )
        await self._db.commit()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database is not connected")
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        cur = await self.db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def get_filters(self) -> dict[str, Any]:
        raw = await self.get_setting("filters")
        data = dict(DEFAULT_FILTERS)
        if raw:
            try:
                data.update(json.loads(raw))
            except json.JSONDecodeError:
                pass
        return data

    async def set_filters(self, filters: dict[str, Any]) -> None:
        await self.set_setting("filters", json.dumps(filters, ensure_ascii=False))

    async def update_filters(self, **kwargs: Any) -> dict[str, Any]:
        filters = await self.get_filters()
        filters.update(kwargs)
        await self.set_filters(filters)
        return filters

    async def is_running(self) -> bool:
        return (await self.get_setting("running", "1")) == "1"

    async def set_running(self, value: bool) -> None:
        await self.set_setting("running", "1" if value else "0")

    async def list_admins(self) -> list[int]:
        cur = await self.db.execute("SELECT user_id FROM admins")
        rows = await cur.fetchall()
        return [int(row["user_id"]) for row in rows]

    async def add_admin(self, user_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (user_id,)
        )
        await self.db.commit()

    async def remove_admin(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await self.db.commit()

    async def add_chat(self, chat_id: int, title: str, username: str | None) -> None:
        if username:
            await self.db.execute(
                "DELETE FROM chats WHERE lower(username) = lower(?) AND chat_id != ?",
                (username, chat_id),
            )
        await self.db.execute(
            """
            INSERT INTO chats(chat_id, title, username, enabled)
            VALUES(?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, username = excluded.username, enabled = 1
            """,
            (chat_id, title, username),
        )
        await self.db.commit()

    async def purge_synthetic_chats(self) -> int:
        cur = await self.db.execute(
            "DELETE FROM chats WHERE chat_id BETWEEN ? AND ?",
            (-1_900_000_000, -1_000_000_000),
        )
        await self.db.commit()
        return int(cur.rowcount or 0)

    async def enabled_chat_usernames(self) -> set[str]:
        cur = await self.db.execute(
            "SELECT username FROM chats WHERE enabled = 1 AND username IS NOT NULL AND username != ''"
        )
        return {(row["username"] or "").lstrip("@").lower() for row in await cur.fetchall()}

    async def remove_chat(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        await self.db.commit()

    async def list_chats(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT chat_id, title, username, enabled FROM chats ORDER BY title"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def enabled_chat_ids(self) -> set[int]:
        cur = await self.db.execute("SELECT chat_id FROM chats WHERE enabled = 1")
        return {int(row["chat_id"]) for row in await cur.fetchall()}

    async def toggle_chat(self, chat_id: int) -> bool | None:
        cur = await self.db.execute("SELECT enabled FROM chats WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        if not row:
            return None
        new_val = 0 if row["enabled"] else 1
        await self.db.execute("UPDATE chats SET enabled = ? WHERE chat_id = ?", (new_val, chat_id))
        await self.db.commit()
        return bool(new_val)

    async def should_check_user(self, user_id: int, cooldown_hours: int, force: bool = False) -> bool:
        if force:
            return True
        cur = await self.db.execute(
            "SELECT last_checked FROM users_cache WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            return True
        return time.time() - row["last_checked"] >= cooldown_hours * 3600

    async def touch_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str,
        unique_count: int,
        fingerprint: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO users_cache(user_id, username, first_name, unique_count, fingerprint, last_checked)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                unique_count = excluded.unique_count,
                fingerprint = excluded.fingerprint,
                last_checked = excluded.last_checked
            """,
            (user_id, username, first_name, unique_count, fingerprint, int(time.time())),
        )
        await self.db.commit()

    async def cached_profile(self, user_id: int | None, username: str | None) -> dict[str, Any] | None:
        if user_id:
            cur = await self.db.execute("SELECT * FROM users_cache WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if row:
                return dict(row)
        name = (username or "").lstrip("@").lower()
        if name:
            cur = await self.db.execute(
                "SELECT * FROM users_cache WHERE lower(username) = ? ORDER BY last_notified DESC LIMIT 1",
                (name,),
            )
            row = await cur.fetchone()
            if row:
                return dict(row)
        return None

    async def already_notified(self, user_id: int, username: str | None, hours: int) -> bool:
        if hours <= 0:
            return False
        cutoff = int(time.time() - hours * 3600)
        name = (username or "").lstrip("@").lower()
        if name:
            cur = await self.db.execute(
                """
                SELECT 1 FROM users_cache
                WHERE last_notified >= ?
                  AND (user_id = ? OR lower(username) = ?)
                LIMIT 1
                """,
                (cutoff, user_id, name),
            )
        else:
            cur = await self.db.execute(
                """
                SELECT 1 FROM users_cache
                WHERE last_notified >= ? AND user_id = ?
                LIMIT 1
                """,
                (cutoff, user_id),
            )
        return await cur.fetchone() is not None

    async def should_notify(
        self,
        user_id: int,
        fingerprint: str,
        cooldown_hours: int,
        force: bool = False,
        username: str | None = None,
    ) -> bool:
        if force:
            return True
        if await self.already_notified(user_id, username, cooldown_hours):
            return False
        name = (username or "").lstrip("@").lower()
        if name:
            cur = await self.db.execute(
                """
                SELECT fingerprint, last_notified FROM users_cache
                WHERE user_id = ? OR lower(username) = ?
                ORDER BY last_notified DESC
                LIMIT 1
                """,
                (user_id, name),
            )
        else:
            cur = await self.db.execute(
                "SELECT fingerprint, last_notified FROM users_cache WHERE user_id = ?",
                (user_id,),
            )
        row = await cur.fetchone()
        if not row:
            return True
        if row["fingerprint"] == fingerprint and row["last_notified"]:
            return False
        return True

    async def mark_notified(self, user_id: int) -> None:
        await self.db.execute(
            "UPDATE users_cache SET last_notified = ? WHERE user_id = ?",
            (int(time.time()), user_id),
        )
        await self.db.commit()

    async def save_find(
        self,
        user_id: int,
        username: str | None,
        source: str,
        unique_count: int,
        gifts: list[dict[str, Any]],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO finds(user_id, username, source, unique_count, gifts_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                source,
                unique_count,
                json.dumps(gifts, ensure_ascii=False),
                int(time.time()),
            ),
        )
        await self.db.commit()

    async def recent_finds(self, limit: int = 15) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM finds ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cur.fetchall()]

    async def stats(self) -> dict[str, int]:
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM finds")
        finds = int((await cur.fetchone())["c"])
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM chats WHERE enabled = 1")
        chats = int((await cur.fetchone())["c"])
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM users_cache")
        users = int((await cur.fetchone())["c"])
        return {"finds": finds, "chats": chats, "users": users}

    async def remember_user_gift(self, username: str, slug: str) -> int:
        name = username.strip().lstrip("@").lower()
        slug_key = (slug or "").strip().lower()
        if not name or not slug_key:
            return await self.username_gift_count(name)
        await self.db.execute(
            "INSERT OR IGNORE INTO user_gifts(username, slug, created_at) VALUES(?, ?, ?)",
            (name, slug_key, int(time.time())),
        )
        await self.db.commit()
        return await self.username_gift_count(name)

    async def username_gift_count(self, username: str) -> int:
        name = (username or "").strip().lstrip("@").lower()
        if not name:
            return 0
        cur = await self.db.execute(
            "SELECT COUNT(*) AS c FROM user_gifts WHERE username = ?", (name,)
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def seen_market(self, key: str) -> bool:
        cur = await self.db.execute("SELECT 1 FROM seen_market WHERE key = ?", (key,))
        return await cur.fetchone() is not None

    async def mark_market(self, key: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO seen_market(key, created_at) VALUES(?, ?)",
            (key, int(time.time())),
        )
        await self.db.commit()

    async def queue_pending_owner(
        self,
        slug: str,
        title: str,
        num: int,
        channel: str,
        kind: str,
        price: float,
        asset: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO pending_owners(slug, title, num, channel, kind, price, asset, tries, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(slug) DO NOTHING
            """,
            (slug, title, num, channel, kind, price, asset, int(time.time())),
        )
        await self.db.commit()

    async def list_pending_owners(self, limit: int = 12) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM pending_owners WHERE tries < 48 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def bump_pending_owner(self, slug: str) -> None:
        await self.db.execute(
            "UPDATE pending_owners SET tries = tries + 1 WHERE slug = ?",
            (slug,),
        )
        await self.db.commit()

    async def drop_pending_owner(self, slug: str) -> None:
        await self.db.execute("DELETE FROM pending_owners WHERE slug = ?", (slug,))
        await self.db.commit()

    async def get_claim(self, target_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT target_id, by_id, by_name, created_at FROM claims WHERE target_id = ?",
            (target_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_claim(self, target_id: int, by_id: int, by_name: str) -> None:
        await self.db.execute(
            """
            INSERT INTO claims(target_id, by_id, by_name, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                by_id = excluded.by_id,
                by_name = excluded.by_name,
                created_at = excluded.created_at
            """,
            (target_id, by_id, by_name[:48], int(time.time())),
        )
        await self.db.commit()

    async def clear_claim(self, target_id: int) -> None:
        await self.db.execute("DELETE FROM claims WHERE target_id = ?", (target_id,))
        await self.db.commit()

    async def save_card_message(
        self,
        target_id: int,
        chat_id: int,
        message_id: int,
        body: str,
        username: str,
        gift_url: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO card_messages(chat_id, message_id, target_id, body, username, gift_url)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                target_id = excluded.target_id,
                body = excluded.body,
                username = excluded.username,
                gift_url = excluded.gift_url
            """,
            (chat_id, message_id, target_id, body, username, gift_url),
        )
        await self.db.commit()

    async def list_card_messages(self, target_id: int) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            """
            SELECT chat_id, message_id, body, username, gift_url
            FROM card_messages
            WHERE target_id = ?
            ORDER BY message_id DESC
            LIMIT 40
            """,
            (target_id,),
        )
        return [dict(row) for row in await cur.fetchall()]
