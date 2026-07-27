"""Small asynchronous client for the KOOK v3 channel API."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from beautify import Channel


class KookApiError(RuntimeError):
    pass


class KookApiClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://www.kookapp.cn/api/v3",
        timeout_seconds: int = 20,
        request_interval_ms: int = 350,
        max_rate_limit_retries: int = 2,
    ):
        self.token = str(token).strip()
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = max(5, int(timeout_seconds))
        self.request_interval = max(0, int(request_interval_ms)) / 1000
        self.max_rate_limit_retries = max(0, int(max_rate_limit_retries))
        self._session: aiohttp.ClientSession | None = None
        self._last_request_at = 0.0

    async def __aenter__(self) -> "KookApiClient":
        if not self.token:
            raise KookApiError("KOOK Bot Token 未配置，也无法从 AstrBot KOOK 适配器读取。")
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "Authorization": f"Bot {self.token}",
                "Accept-Language": "zh-CN",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_interval:
            await asyncio.sleep(self.request_interval - elapsed)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("KookApiClient must be used with 'async with'.")
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(self.max_rate_limit_retries + 1):
            await self._throttle()
            try:
                async with self._session.request(method, url, params=params, json=json_body) as response:
                    self._last_request_at = time.monotonic()
                    if response.status == 429:
                        await response.read()
                        if attempt >= self.max_rate_limit_retries:
                            raise KookApiError("KOOK API 触发限流，重试后仍未恢复。")
                        reset = response.headers.get("X-Rate-Limit-Reset", "1")
                        try:
                            wait_seconds = max(0.5, min(float(reset), 30.0))
                        except ValueError:
                            wait_seconds = 1.0
                        await asyncio.sleep(wait_seconds)
                        continue
                    try:
                        payload = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        detail = (await response.text())[:300]
                        raise KookApiError(
                            f"KOOK API 返回了无法解析的响应（HTTP {response.status}）：{detail}"
                        ) from exc
                    if response.status >= 400:
                        message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
                        raise KookApiError(f"KOOK API 请求失败（HTTP {response.status}）：{message or '未知错误'}")
                    if not isinstance(payload, dict):
                        raise KookApiError("KOOK API 返回格式不正确。")
                    if int(payload.get("code", -1)) != 0:
                        raise KookApiError(
                            f"KOOK API 错误 {payload.get('code')}：{payload.get('message', '未知错误')}"
                        )
                    return payload.get("data")
            except asyncio.TimeoutError as exc:
                raise KookApiError("KOOK API 请求超时。") from exc
            except aiohttp.ClientError as exc:
                raise KookApiError(f"KOOK API 网络请求失败：{exc.__class__.__name__}") from exc
        raise KookApiError("KOOK API 请求失败。")

    async def list_channels(self, guild_id: str) -> list[Channel]:
        guild_id = str(guild_id).strip()
        if not guild_id:
            raise KookApiError("缺少 KOOK 服务器 ID。")
        channels: dict[str, Channel] = {}
        for channel_type in (1, 2):
            page = 1
            while True:
                data = await self._request(
                    "GET",
                    "channel/list",
                    params={
                        "guild_id": guild_id,
                        "type": channel_type,
                        "page": page,
                        "page_size": 50,
                    },
                )
                if not isinstance(data, dict):
                    raise KookApiError("KOOK 频道列表返回格式不正确。")
                items = data.get("items", [])
                if not isinstance(items, list):
                    raise KookApiError("KOOK 频道列表缺少 items。")
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    channel = Channel.from_api(item)
                    if channel.id:
                        channels[channel.id] = channel
                meta = data.get("meta", {})
                page_total = int(meta.get("page_total", page) or page) if isinstance(meta, dict) else page
                if page >= page_total:
                    break
                page += 1
        return sorted(channels.values(), key=lambda channel: (channel.level, channel.kind, channel.id))

    async def update_channel_name(self, channel_id: str, name: str) -> None:
        await self._request(
            "POST",
            "channel/update",
            json_body={"channel_id": str(channel_id), "name": str(name)},
        )
