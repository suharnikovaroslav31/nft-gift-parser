from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    bot_token: str
    api_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("API_ID", "TELEGRAM_API_ID", "api_id"),
    )
    api_hash: str = Field(
        default="",
        validation_alias=AliasChoices("API_HASH", "TELEGRAM_API_HASH", "api_hash"),
    )
    phone: str = ""
    session_string: str = Field(
        default="",
        validation_alias=AliasChoices("SESSION_STRING", "STRING_SESSION", "session_string"),
    )
    admin_ids: str = ""
    portals_auth: str = ""
    tonnel_auth: str = ""
    proxy_type: str = ""
    proxy_host: str = ""
    proxy_port: int = 0

    db_path: str = ""
    session_path: str = ""

    check_delay_sec: float = 1.0
    user_cooldown_hours: int = 6
    notify_cooldown_hours: int = 24
    market_poll_sec: int = 25
    max_gifts_fetch: int = 80

    def admin_id_list(self) -> list[int]:
        ids: list[int] = []
        for part in self.admin_ids.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids

    @field_validator("api_id", mode="before")
    @classmethod
    def empty_api_id(cls, value: object) -> object:
        if value in ("", None, 0, "0"):
            return None
        return value

    @field_validator("proxy_port", mode="before")
    @classmethod
    def empty_proxy_port(cls, value: object) -> object:
        if value in ("", None):
            return 0
        return value

    @model_validator(mode="after")
    def fill_data_paths(self) -> Settings:
        root = Path(os.getenv("DATA_DIR", "data"))
        root.mkdir(parents=True, exist_ok=True)
        if not self.db_path:
            self.db_path = str(root / "parser.db")
        if not self.session_path:
            self.session_path = str(root / "userbot")
        return self

    @property
    def has_userbot(self) -> bool:
        return bool(self.api_id and self.api_hash and (self.phone or self.session_string))

    def telethon_session(self):
        if self.session_string.strip():
            from telethon.sessions import StringSession

            return StringSession(self.session_string.strip())
        return self.session_path

    def telethon_proxy(self) -> dict | None:
        host = (self.proxy_host or "").strip()
        if not host or not self.proxy_port:
            return None
        if host in {"127.0.0.1", "localhost"} and os.getenv("DATA_DIR"):
            return None
        kind = (self.proxy_type or "socks5").strip().lower()
        return {"proxy_type": kind, "addr": host, "port": int(self.proxy_port)}

    def portals_header(self) -> str:
        token = (self.portals_auth or "").strip()
        if not token:
            return ""
        if token.lower().startswith("tma "):
            return token
        return f"tma {token}"
