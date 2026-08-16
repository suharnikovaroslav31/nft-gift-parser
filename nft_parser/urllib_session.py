from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator
from enum import Enum
from typing import Any, cast

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods.base import TelegramType


class UrllibSession(BaseSession):
    """Синхронный urllib в отдельном потоке — проходит через VPN, где aiohttp часто падает."""

    def __init__(self, timeout: float = 90.0, **kwargs: Any) -> None:
        super().__init__(timeout=timeout, **kwargs)

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        data = await asyncio.to_thread(self._download, url, headers or {}, timeout)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def _download(self, url: str, headers: dict[str, Any], timeout: int) -> bytes:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    async def make_request(self, bot, method, timeout: int | None = None) -> TelegramType:
        try:
            status, raw = await asyncio.to_thread(self._post, bot, method, timeout)
        except Exception as exc:
            raise TelegramNetworkError(method=method, message=f"{type(exc).__name__}: {exc}") from exc
        response = self.check_response(bot=bot, method=method, status_code=status, content=raw)
        return cast(TelegramType, response.result)

    def _form_value(self, key: str, prepared: Any) -> str:
        if isinstance(prepared, Enum):
            prepared = prepared.value
        if key == "parse_mode":
            raw = str(prepared).replace("ParseMode.", "").strip().strip('"')
            if raw.lower() == "html":
                return "HTML"
            if raw.lower() in {"markdown", "markdownv2"}:
                return raw
            return "HTML"
        if isinstance(prepared, str):
            return prepared
        return json.dumps(prepared, ensure_ascii=False)

    def _post(self, bot, method, timeout: int | None) -> tuple[int, str]:
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        files: dict[str, Any] = {}
        payload: dict[str, Any] = {}
        for key, value in method.model_dump(warnings=False).items():
            prepared = self.prepare_value(value, bot=bot, files=files)
            if prepared in (None, "", [], {}):
                continue
            payload[key] = self._form_value(key, prepared)
        wait = timeout or self.timeout
        if method.__api_method__ == "getUpdates":
            payload["timeout"] = "0"
            wait = 20
        body = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=wait) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
