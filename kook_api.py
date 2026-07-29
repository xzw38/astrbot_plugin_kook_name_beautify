"""Small asynchronous client for the KOOK v3 channel API."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import aiohttp

try:
    from .beautify import Channel
except ImportError:  # Allow direct local imports during standalone development.
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
        debug: bool = False,
        debug_log: Callable[..., None] | None = None,
    ):
        self.token = str(token).strip()
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = max(5, int(timeout_seconds))
        self.request_interval = max(0, int(request_interval_ms)) / 1000
        self.max_rate_limit_retries = max(0, int(max_rate_limit_retries))
        self.debug = bool(debug)
        self.debug_log = debug_log
        self._session: aiohttp.ClientSession | None = None
        self._last_request_at = 0.0

    def _debug(self, message: str, *args: Any) -> None:
        if self.debug and self.debug_log is not None:
            self.debug_log(message, *args)

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
            self._debug(
                "[KOOK API] request method=%s path=/%s attempt=%s params=%s body=%s",
                method,
                path.lstrip("/"),
                attempt + 1,
                params or {},
                json_body or {},
            )
            try:
                async with self._session.request(method, url, params=params, json=json_body) as response:
                    self._last_request_at = time.monotonic()
                    self._debug(
                        "[KOOK API] response path=/%s http=%s remaining=%s reset=%s bucket=%s",
                        path.lstrip("/"),
                        response.status,
                        response.headers.get("X-Rate-Limit-Remaining", "-"),
                        response.headers.get("X-Rate-Limit-Reset", "-"),
                        response.headers.get("X-Rate-Limit-Bucket", "-"),
                    )
                    if response.status == 429:
                        await response.read()
                        if attempt >= self.max_rate_limit_retries:
                            raise KookApiError("KOOK API 触发限流，重试后仍未恢复。")
                        reset = response.headers.get("X-Rate-Limit-Reset", "1")
                        try:
                            wait_seconds = max(0.5, min(float(reset), 30.0))
                        except ValueError:
                            wait_seconds = 1.0
                        self._debug(
                            "[KOOK API] rate limited path=/%s wait=%.2fs retry=%s/%s",
                            path.lstrip("/"),
                            wait_seconds,
                            attempt + 1,
                            self.max_rate_limit_retries,
                        )
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
                    self._debug(
                        "[KOOK API] payload path=/%s code=%s message=%s",
                        path.lstrip("/"),
                        payload.get("code"),
                        payload.get("message", ""),
                    )
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
        guild_data = await self._request(
            "GET",
            "guild/view",
            params={"guild_id": guild_id},
        )
        if not isinstance(guild_data, dict):
            raise KookApiError("KOOK 服务器详情返回格式不正确。")
        guild_channels = guild_data.get("channels", [])
        if not isinstance(guild_channels, list):
            raise KookApiError("KOOK 服务器详情缺少 channels。")
        for item in guild_channels:
            if not isinstance(item, dict):
                continue
            channel = Channel.from_api(item)
            if channel.id:
                channels[channel.id] = channel
        self._debug(
            "guild/view channels=%s categories=%s",
            len(guild_channels),
            sum(channel.kind == "category" for channel in channels.values()),
        )
        # KOOK categories are returned by the unfiltered/default channel list on
        # some guilds, but disappear when type=1 is supplied explicitly.
        for channel_type in (None, 2):
            page = 1
            while True:
                params: dict[str, Any] = {
                    "guild_id": guild_id,
                    "page": page,
                    "page_size": 50,
                }
                if channel_type is not None:
                    params["type"] = channel_type
                data = await self._request(
                    "GET",
                    "channel/list",
                    params=params,
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
        missing_parent_ids = {
            channel.parent_id
            for channel in channels.values()
            if channel.parent_id and channel.parent_id not in channels
        }
        for parent_id in sorted(missing_parent_ids):
            try:
                data = await self._request(
                    "GET",
                    "channel/view",
                    params={"target_id": parent_id, "need_children": False},
                )
            except KookApiError as exc:
                self._debug(
                    "unable to resolve parent category id=%s error=%s",
                    parent_id,
                    exc,
                )
                continue
            if not isinstance(data, dict):
                continue
            category = Channel.from_api(data)
            if category.id and category.kind == "category":
                channels[category.id] = category
                self._debug(
                    "resolved missing parent category id=%s name=%r",
                    category.id,
                    category.name,
                )
        self._debug(
            "merged channel inventory guild=%s total=%s categories=%s text=%s voice=%s",
            guild_id,
            len(channels),
            sum(channel.kind == "category" for channel in channels.values()),
            sum(channel.kind == "text" for channel in channels.values()),
            sum(channel.kind == "voice" for channel in channels.values()),
        )
        return sorted(channels.values(), key=lambda channel: (channel.level, channel.kind, channel.id))

    async def update_channel_name(self, channel_id: str, name: str) -> None:
        await self._request(
            "POST",
            "channel/update",
            json_body={"channel_id": str(channel_id), "name": str(name)},
        )

    async def update_channel_parent(self, channel_id: str, parent_id: str = "") -> None:
        await self._request(
            "POST",
            "channel/update",
            json_body={
                "channel_id": str(channel_id),
                "parent_id": str(parent_id).strip() or "0",
            },
        )

    async def create_channel(
        self,
        guild_id: str,
        name: str,
        kind: str,
        *,
        parent_id: str = "",
        limit_amount: int = 0,
        voice_quality: str = "2",
    ) -> Channel:
        body: dict[str, Any] = {
            "guild_id": str(guild_id),
            "name": str(name),
        }
        if kind == "category":
            body["is_category"] = 1
        elif kind in {"text", "voice"}:
            body["type"] = 1 if kind == "text" else 2
            if parent_id:
                body["parent_id"] = str(parent_id)
            if kind == "voice":
                body["limit_amount"] = max(0, min(int(limit_amount), 99))
                body["voice_quality"] = str(voice_quality)
        else:
            raise KookApiError(f"不支持创建的频道类型：{kind}")
        data = await self._request("POST", "channel/create", json_body=body)
        if not isinstance(data, dict):
            raise KookApiError("KOOK 创建频道返回格式不正确。")
        channel = Channel.from_api(data)
        if not channel.id:
            raise KookApiError("KOOK 创建频道成功但未返回频道 ID。")
        return Channel(
            id=channel.id,
            name=channel.name or str(name),
            type=0 if kind == "category" else (2 if kind == "voice" else 1),
            level=channel.level,
            parent_id=channel.parent_id or str(parent_id),
            is_category=kind == "category",
        )

    async def delete_channel(self, channel_id: str) -> None:
        await self._request(
            "POST",
            "channel/delete",
            json_body={"channel_id": str(channel_id)},
        )
