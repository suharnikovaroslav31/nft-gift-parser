from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GiftInfo:
    title: str
    slug: str
    num: int
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None
    value_stars: int | None = None
    value_usd: float | None = None
    value_ton: float | None = None
    listed_ton: float | None = None
    received_at: int | None = None

    @property
    def link(self) -> str:
        return f"https://t.me/nft/{self.slug}" if self.slug else ""

    @property
    def label(self) -> str:
        extra = f" · {self.model}" if self.model else ""
        return f"{self.title} #{self.num}{extra}"

    def fingerprint(self) -> str:
        return self.slug or f"{self.title}:{self.num}"


@dataclass
class TrackEvent:
    kind: str  # sold / listed
    gift: GiftInfo
    price: float = 0.0
    asset: str = ""
    source: str = ""
    key: str = ""


@dataclass
class ProfileGifts:
    user_id: int
    username: str | None
    first_name: str
    last_name: str
    unique: list[GiftInfo] = field(default_factory=list)
    total_unique: int = 0
    hidden: bool = False
    tg_level: int | None = None

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return name or (f"@{self.username}" if self.username else str(self.user_id))

    @property
    def mention(self) -> str:
        if self.username:
            return f"@{self.username}"
        if self.user_id:
            return f'<a href="tg://user?id={self.user_id}">{self.display_name}</a>'
        return self.display_name

    def gift_fingerprint(self) -> str:
        return "|".join(sorted(g.fingerprint() for g in self.unique))


@dataclass
class Hit:
    profile: ProfileGifts
    source: str
    source_label: str
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    is_newbie: bool = False
    just_bought: bool = False
