"""AstrBot plugin for AI-assisted KOOK channel structure beautification."""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .beautify import (
        PLANNER_SYSTEM_PROMPT,
        Channel,
        CreatedChannel,
        PlanError,
        PlanStore,
        RenamePlan,
        build_planner_prompt,
        extract_explicit_channel_ids,
        format_plan_preview,
        instruction_requires_creation,
        parse_structure_plan,
    )
    from .kook_api import KookApiClient, KookApiError
except ImportError:  # Allow direct local imports during standalone development.
    from beautify import (
        PLANNER_SYSTEM_PROMPT,
        Channel,
        CreatedChannel,
        PlanError,
        PlanStore,
        RenamePlan,
        build_planner_prompt,
        extract_explicit_channel_ids,
        format_plan_preview,
        instruction_requires_creation,
        parse_structure_plan,
    )
    from kook_api import KookApiClient, KookApiError


__version__ = "0.2.4"


@register(
    "astrbot_plugin_kook_name_beautify",
    "xzw38",
    "用自然语言预览并一键应用 KOOK 频道结构美化",
    __version__,
)
class KookNameBeautifyPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.bot_token = str(self.config.get("bot_token", "")).strip()
        self.default_guild_id = str(self.config.get("guild_id", "")).strip()
        self.api_base_url = str(
            self.config.get("api_base_url", "https://www.kookapp.cn/api/v3")
        ).rstrip("/")
        self.request_timeout = max(5, int(self.config.get("request_timeout", 20)))
        self.request_interval_ms = max(0, int(self.config.get("request_interval_ms", 350)))
        self.max_rate_limit_retries = max(0, int(self.config.get("max_rate_limit_retries", 2)))
        self.debug_logging = bool(self.config.get("debug_logging", True))
        self.max_changes = min(200, max(1, int(self.config.get("max_changes", 100))))
        self.max_name_length = min(100, max(8, int(self.config.get("max_name_length", 50))))
        self.llm_timeout = max(10, int(self.config.get("llm_timeout", 60)))
        self.llm_temperature = min(1.0, max(0.0, float(self.config.get("llm_temperature", 0.3))))
        self.custom_planner_prompt = str(self.config.get("custom_planner_prompt", "")).strip()
        self.allowed_user_ids = {
            str(user_id).strip()
            for user_id in self.config.get("allowed_user_ids", [])
            if str(user_id).strip()
        }
        self.plans = PlanStore(int(self.config.get("plan_ttl_seconds", 600)))
        self._mutation_lock = asyncio.Lock()

    def _api_client(self, token: str) -> KookApiClient:
        return KookApiClient(
            token,
            base_url=self.api_base_url,
            timeout_seconds=self.request_timeout,
            request_interval_ms=self.request_interval_ms,
            max_rate_limit_retries=self.max_rate_limit_retries,
            debug=self.debug_logging,
            debug_log=logger.info,
        )

    def _debug(self, message: str, *args: Any) -> None:
        if self.debug_logging:
            logger.info(message, *args)

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                return str(getter() or "").strip()
            except Exception:
                pass
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        return str(getattr(sender, "user_id", "") or getattr(sender, "id", "")).strip()

    def _check_allowlist(self, event: AstrMessageEvent) -> None:
        is_admin = getattr(event, "is_admin", None)
        if not callable(is_admin) or not bool(is_admin()):
            raise PlanError("只有 AstrBot 管理员可以使用 KOOK 频道美化插件。")
        if not self.allowed_user_ids:
            return
        sender_id = self._sender_id(event)
        if sender_id not in self.allowed_user_ids:
            raise PlanError("你的用户 ID 不在插件 allowed_user_ids 白名单中。")

    @staticmethod
    def _has_explicit_plan_action(
        event: AstrMessageEvent,
        plan_id: str,
        markers: tuple[str, ...],
    ) -> bool:
        message = str(getattr(event, "message_str", "") or "").strip().lower()
        normalized_plan_id = str(plan_id or "").strip().lower()
        return bool(
            normalized_plan_id
            and normalized_plan_id in message
            and any(marker.lower() in message for marker in markers)
        )

    @staticmethod
    def _check_kook_event(event: AstrMessageEvent) -> None:
        platform_name = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                platform_name = str(getter() or "").lower()
            except Exception:
                platform_name = ""
        if not platform_name:
            platform_meta = getattr(event, "platform_meta", None)
            platform_name = str(
                getattr(platform_meta, "name", "") or getattr(platform_meta, "id", "")
            ).lower()
        if "kook" not in platform_name:
            raise PlanError("此工具只能在 KOOK 会话中使用。")

    def _resolve_token(self, event: AstrMessageEvent) -> str:
        if self.bot_token:
            self._debug("[KOOK Beautify] token source=plugin_config")
            return self.bot_token
        client = getattr(event, "client", None)
        for attr in ("token", "bot_token"):
            value = getattr(client, attr, "")
            if isinstance(value, str) and value.strip():
                self._debug("[KOOK Beautify] token source=event.client.%s", attr)
                return value.strip()
        client_config = getattr(client, "config", None)
        if isinstance(client_config, dict):
            for key in ("token", "bot_token"):
                value = client_config.get(key)
                if isinstance(value, str) and value.strip():
                    self._debug("[KOOK Beautify] token source=event.client.config[%s]", key)
                    return value.strip()
        else:
            for attr in ("token", "bot_token"):
                value = getattr(client_config, attr, "")
                if isinstance(value, str) and value.strip():
                    self._debug("[KOOK Beautify] token source=event.client.config.%s", attr)
                    return value.strip()
        raise KookApiError("无法读取 KOOK Bot Token，请在插件配置中填写 bot_token。")

    @staticmethod
    def _find_guild_id(value: Any, depth: int = 0) -> str:
        if depth > 5:
            return ""
        if isinstance(value, dict):
            for key in ("guild_id", "server_id"):
                result = value.get(key)
                if isinstance(result, (str, int)) and str(result).strip():
                    return str(result).strip()
            for child in value.values():
                result = KookNameBeautifyPlugin._find_guild_id(child, depth + 1)
                if result:
                    return result
        elif isinstance(value, (list, tuple)):
            for child in value:
                result = KookNameBeautifyPlugin._find_guild_id(child, depth + 1)
                if result:
                    return result
        return ""

    def _resolve_guild_id(self, event: AstrMessageEvent, requested: str = "") -> str:
        if str(requested).strip():
            self._debug("[KOOK Beautify] guild source=tool_argument guild=%s", str(requested).strip())
            return str(requested).strip()
        if self.default_guild_id:
            self._debug("[KOOK Beautify] guild source=plugin_config guild=%s", self.default_guild_id)
            return self.default_guild_id
        for source in (event, getattr(event, "message_obj", None)):
            for attr in ("guild_id", "server_id"):
                value = getattr(source, attr, "")
                if isinstance(value, (str, int)) and str(value).strip():
                    self._debug("[KOOK Beautify] guild source=event.%s guild=%s", attr, str(value).strip())
                    return str(value).strip()
        message_obj = getattr(event, "message_obj", None)
        for attr in ("raw_message", "raw_data", "message", "extra"):
            result = self._find_guild_id(getattr(message_obj, attr, None))
            if result:
                self._debug("[KOOK Beautify] guild source=message_obj.%s guild=%s", attr, result)
                return result
        raise KookApiError("无法从当前消息识别服务器 ID，请在插件配置中填写 guild_id。")

    async def _provider_id(self, event: AstrMessageEvent) -> str:
        current_id_getter = getattr(self.context, "get_current_chat_provider_id", None)
        unified_origin = getattr(event, "unified_msg_origin", None)
        if callable(current_id_getter) and unified_origin:
            provider_id = current_id_getter(unified_origin)
            if hasattr(provider_id, "__await__"):
                provider_id = await provider_id
            if str(provider_id or "").strip():
                return str(provider_id).strip()
        getter = getattr(self.context, "get_using_provider", None)
        if not callable(getter):
            raise PlanError("当前 AstrBot 版本没有可用的 LLM Provider 接口。")
        provider = None
        if unified_origin:
            try:
                provider = getter(unified_origin)
            except TypeError:
                provider = None
        if provider is None:
            provider = getter()
        if hasattr(provider, "__await__"):
            provider = await provider
        if provider is None:
            raise PlanError("AstrBot 当前没有启用的文本 LLM Provider，无法理解自然语言要求。")
        meta = provider.meta()
        provider_id = str(getattr(meta, "id", "")).strip()
        if not provider_id:
            raise PlanError("无法识别当前 AstrBot LLM Provider。")
        return provider_id

    async def _generate_ai_plan(
        self,
        event: AstrMessageEvent,
        instruction: str,
        channels: list[Channel],
        validation_error: str = "",
    ) -> str:
        provider_id = await self._provider_id(event)
        system_prompt = PLANNER_SYSTEM_PROMPT
        if self.custom_planner_prompt:
            system_prompt += "\n\n管理员补充规范：\n" + self.custom_planner_prompt
        prompt = build_planner_prompt(instruction, channels)
        if validation_error:
            prompt += (
                "\n\n上一次方案校验失败："
                + validation_error
                + "\n请完全丢弃上一次输出并重新生成完整 JSON。"
                + "现有频道 ID 只根据本次 channels_json；但 parent_ref 可以使用 explicit_parent_refs_json 中管理员明确给出的 ID。"
                + "本次重试必须至少返回一个有效操作；若用户要求新建频道，creates 必须非空，禁止同时返回两个空数组。"
            )
        result = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=4000,
                temperature=self.llm_temperature,
            ),
            timeout=self.llm_timeout,
        )
        text = str(getattr(result, "completion_text", "") or "").strip()
        if not text:
            raise PlanError("LLM 返回了空方案。")
        return text

    async def _create_plan(
        self,
        event: AstrMessageEvent,
        instruction: str,
        guild_id: str = "",
    ) -> RenamePlan:
        self._check_kook_event(event)
        self._check_allowlist(event)
        instruction = str(instruction or "").strip()
        if not instruction:
            raise PlanError("请描述希望使用的风格，例如“统一成简约黑白风，保留中文语义”。")
        token = self._resolve_token(event)
        resolved_guild_id = self._resolve_guild_id(event, guild_id)
        validation_error = ""
        require_creates = instruction_requires_creation(instruction)
        explicit_parent_refs = extract_explicit_channel_ids(instruction)
        for attempt in range(2):
            async with self._api_client(token) as client:
                channels = await client.list_channels(resolved_guild_id)
            if not channels:
                raise KookApiError("这个服务器没有返回可美化的频道。")
            logger.info(
                "[KOOK Beautify] planning guild=%s channels=%s user=%s attempt=%s/2",
                resolved_guild_id,
                len(channels),
                self._sender_id(event),
                attempt + 1,
            )
            ai_output = await self._generate_ai_plan(
                event,
                instruction,
                channels,
                validation_error=validation_error,
            )
            self._debug(
                "[KOOK Beautify] AI plan output guild=%s attempt=%s/2 require_creates=%s explicit_parents=%s output=%r",
                resolved_guild_id,
                attempt + 1,
                require_creates,
                explicit_parent_refs,
                ai_output[:1500],
            )
            try:
                changes, creates = parse_structure_plan(
                    ai_output,
                    channels,
                    max_name_length=self.max_name_length,
                    max_changes=self.max_changes,
                    require_creates=require_creates,
                    allowed_parent_refs=explicit_parent_refs,
                )
                break
            except PlanError as exc:
                validation_error = str(exc)
                logger.warning(
                    "[KOOK Beautify] plan validation failed guild=%s attempt=%s/2 error=%s",
                    resolved_guild_id,
                    attempt + 1,
                    exc,
                )
                if attempt == 1:
                    raise PlanError(f"AI 刷新频道列表并重试后仍未生成有效方案：{exc}") from exc
        return self.plans.create(
            guild_id=resolved_guild_id,
            user_id=self._sender_id(event),
            instruction=instruction,
            changes=changes,
            creates=creates,
        )

    async def _apply_plan(self, event: AstrMessageEvent, plan_id: str) -> str:
        self._check_kook_event(event)
        self._check_allowlist(event)
        plan = self.plans.get(plan_id, user_id=self._sender_id(event))
        if plan.applied:
            raise PlanError("这个方案已经执行过了。")
        token = self._resolve_token(event)
        self._debug(
            "[KOOK Beautify] apply start plan=%s guild=%s creates=%s renames=%s user=%s",
            plan.id,
            plan.guild_id,
            len(plan.creates),
            len(plan.changes),
            self._sender_id(event),
        )
        async with self._mutation_lock:
            async with self._api_client(token) as client:
                current_channels = await client.list_channels(plan.guild_id)
                current_names = {channel.id: channel.name for channel in current_channels}
                conflicts = [
                    change.old_name
                    for change in plan.changes
                    if current_names.get(change.channel_id) != change.old_name
                ]
                if conflicts:
                    logger.error(
                        "[KOOK Beautify] apply conflict plan=%s channels=%s",
                        plan.id,
                        conflicts[:8],
                    )
                    raise PlanError(
                        "执行前检查发现频道已被修改，方案未执行：" + "、".join(conflicts[:8])
                    )
                rename_ids = {change.channel_id for change in plan.changes}
                occupied_names = {
                    channel.name for channel in current_channels if channel.id not in rename_ids
                }
                duplicate_names = [
                    name
                    for name in [
                        *(change.new_name for change in plan.changes),
                        *(item.name for item in plan.creates),
                    ]
                    if name in occupied_names
                ]
                if duplicate_names:
                    raise PlanError(
                        "执行前发现新名称已被其他频道占用：" + "、".join(duplicate_names[:8])
                    )

                applied = []
                created: list[CreatedChannel] = []
                temp_id_map: dict[str, str] = {}
                try:
                    for index, item in enumerate(plan.creates, start=1):
                        parent_id = temp_id_map.get(item.parent_ref, item.parent_ref)
                        self._debug(
                            "[KOOK Beautify] create start plan=%s item=%s/%s temp=%s kind=%s parent=%s name=%r",
                            plan.id,
                            index,
                            len(plan.creates),
                            item.temp_id,
                            item.kind,
                            parent_id or "root",
                            item.name,
                        )
                        channel = await client.create_channel(
                            plan.guild_id,
                            item.name,
                            item.kind,
                            parent_id=parent_id,
                            limit_amount=item.limit_amount,
                            voice_quality=item.voice_quality,
                        )
                        temp_id_map[item.temp_id] = channel.id
                        created.append(CreatedChannel(
                            temp_id=item.temp_id,
                            channel_id=channel.id,
                            name=item.name,
                            kind=item.kind,
                            parent_id=parent_id,
                        ))
                        self._debug(
                            "[KOOK Beautify] create success plan=%s temp=%s channel=%s",
                            plan.id,
                            item.temp_id,
                            channel.id,
                        )
                    for index, change in enumerate(plan.changes, start=1):
                        self._debug(
                            "[KOOK Beautify] update start plan=%s item=%s/%s channel=%s old=%r new=%r",
                            plan.id,
                            index,
                            len(plan.changes),
                            change.channel_id,
                            change.old_name,
                            change.new_name,
                        )
                        await client.update_channel_name(change.channel_id, change.new_name)
                        applied.append(change)
                        self._debug(
                            "[KOOK Beautify] update success plan=%s item=%s/%s channel=%s",
                            plan.id,
                            index,
                            len(plan.changes),
                            change.channel_id,
                        )
                except Exception as exc:
                    logger.error(
                        "[KOOK Beautify] apply failed plan=%s created=%s/%s renamed=%s/%s error=%s",
                        plan.id,
                        len(created),
                        len(plan.creates),
                        len(applied),
                        len(plan.changes),
                        exc,
                    )
                    rollback_failed = []
                    for change in reversed(applied):
                        try:
                            self._debug(
                                "[KOOK Beautify] auto-rollback start plan=%s channel=%s restore=%r",
                                plan.id,
                                change.channel_id,
                                change.old_name,
                            )
                            await client.update_channel_name(change.channel_id, change.old_name)
                            self._debug(
                                "[KOOK Beautify] auto-rollback success plan=%s channel=%s",
                                plan.id,
                                change.channel_id,
                            )
                        except Exception:
                            rollback_failed.append(change.new_name)
                            logger.exception(
                                "[KOOK Beautify] auto-rollback failed plan=%s channel=%s",
                                plan.id,
                                change.channel_id,
                            )
                    for item in reversed(created):
                        try:
                            self._debug(
                                "[KOOK Beautify] auto-delete start plan=%s channel=%s name=%r",
                                plan.id,
                                item.channel_id,
                                item.name,
                            )
                            await client.delete_channel(item.channel_id)
                        except Exception:
                            rollback_failed.append(item.name)
                            logger.exception(
                                "[KOOK Beautify] auto-delete failed plan=%s channel=%s",
                                plan.id,
                                item.channel_id,
                            )
                    detail = f"应用频道结构时失败：{exc}。已尝试恢复改名并清理本次新建频道。"
                    if rollback_failed:
                        detail += " 以下项目自动恢复失败：" + "、".join(rollback_failed)
                    raise KookApiError(detail) from exc
        plan.applied = True
        plan.applied_channel_ids = [change.channel_id for change in plan.changes]
        plan.created_channels = created
        logger.info(
            "[KOOK Beautify] applied guild=%s plan=%s created=%s renamed=%s",
            plan.guild_id,
            plan.id,
            len(created),
            len(plan.changes),
        )
        return (
            f"方案 {plan.id} 已一键应用：新建 {len(created)} 个频道，改名 {len(plan.changes)} 个频道。"
            f"\n需要恢复时发送：/kook美化撤销 {plan.id}"
        )

    async def _rollback_plan(self, event: AstrMessageEvent, plan_id: str) -> str:
        self._check_kook_event(event)
        self._check_allowlist(event)
        plan = self.plans.get(
            plan_id,
            user_id=self._sender_id(event),
            allow_expired_applied=True,
        )
        if not plan.applied:
            raise PlanError("这个方案尚未执行，不需要撤销。")
        if plan.rolled_back:
            raise PlanError("这个方案已经撤销过了。")
        token = self._resolve_token(event)
        self._debug(
            "[KOOK Beautify] rollback start plan=%s guild=%s created=%s renames=%s user=%s",
            plan.id,
            plan.guild_id,
            len(plan.created_channels),
            len(plan.changes),
            self._sender_id(event),
        )
        async with self._mutation_lock:
            async with self._api_client(token) as client:
                current_channels = await client.list_channels(plan.guild_id)
                current_map = {channel.id: channel for channel in current_channels}
                current_names = {channel.id: channel.name for channel in current_channels}
                conflicts = [
                    change.new_name
                    for change in plan.changes
                    if current_names.get(change.channel_id) != change.new_name
                ]
                if conflicts:
                    logger.error(
                        "[KOOK Beautify] rollback conflict plan=%s channels=%s",
                        plan.id,
                        conflicts[:8],
                    )
                    raise PlanError(
                        "撤销前检查发现频道名称又被修改，未自动覆盖：" + "、".join(conflicts[:8])
                    )
                created_ids = {item.channel_id for item in plan.created_channels}
                created_conflicts = [
                    item.name
                    for item in plan.created_channels
                    if item.channel_id not in current_map
                    or current_map[item.channel_id].name != item.name
                ]
                if created_conflicts:
                    raise PlanError(
                        "撤销前发现本方案创建的频道已被删除或改名，未继续操作："
                        + "、".join(created_conflicts[:8])
                    )
                unsafe_categories = []
                for item in plan.created_channels:
                    if item.kind != "category":
                        continue
                    outside_children = [
                        channel.name
                        for channel in current_channels
                        if channel.parent_id == item.channel_id and channel.id not in created_ids
                    ]
                    if outside_children:
                        unsafe_categories.append(f"{item.name}（含：{'、'.join(outside_children[:4])}）")
                if unsafe_categories:
                    raise PlanError(
                        "为避免删除后来人工添加的频道，以下新建分组不会自动撤销："
                        + "；".join(unsafe_categories)
                    )
                restored_changes = []
                deleted_channels = []
                try:
                    for item in reversed(plan.created_channels):
                        self._debug(
                            "[KOOK Beautify] rollback delete start plan=%s channel=%s kind=%s name=%r",
                            plan.id,
                            item.channel_id,
                            item.kind,
                            item.name,
                        )
                        await client.delete_channel(item.channel_id)
                        deleted_channels.append(item)
                    for index, change in enumerate(reversed(plan.changes), start=1):
                        self._debug(
                            "[KOOK Beautify] rollback update start plan=%s item=%s/%s channel=%s current=%r restore=%r",
                            plan.id,
                            index,
                            len(plan.changes),
                            change.channel_id,
                            change.new_name,
                            change.old_name,
                        )
                        await client.update_channel_name(change.channel_id, change.old_name)
                        restored_changes.append(change)
                        self._debug(
                            "[KOOK Beautify] rollback update success plan=%s channel=%s",
                            plan.id,
                            change.channel_id,
                        )
                except Exception as exc:
                    logger.error(
                        "[KOOK Beautify] rollback failed plan=%s deleted=%s/%s restored=%s/%s error=%s",
                        plan.id,
                        len(deleted_channels),
                        len(plan.created_channels),
                        len(restored_changes),
                        len(plan.changes),
                        exc,
                    )
                    compensation_failed = []
                    for change in reversed(restored_changes):
                        try:
                            await client.update_channel_name(change.channel_id, change.new_name)
                        except Exception:
                            compensation_failed.append(change.old_name)
                    detail = f"撤销过程中发生错误：{exc}。已尝试恢复已改回的名称。"
                    if deleted_channels:
                        detail += " 已删除的本方案频道无法自动重建：" + "、".join(
                            item.name for item in deleted_channels
                        ) + "。"
                    if compensation_failed:
                        detail += " 以下频道恢复失败：" + "、".join(compensation_failed)
                    raise KookApiError(detail) from exc
        plan.rolled_back = True
        restored = len(restored_changes)
        logger.info(
            "[KOOK Beautify] rolled back guild=%s plan=%s deleted=%s restored=%s",
            plan.guild_id,
            plan.id,
            len(deleted_channels),
            restored,
        )
        return f"方案 {plan.id} 已撤销：删除 {len(deleted_channels)} 个本方案频道，恢复 {restored} 个原频道名称。"

    async def _channel_list(self, event: AstrMessageEvent) -> str:
        self._check_kook_event(event)
        self._check_allowlist(event)
        token = self._resolve_token(event)
        guild_id = self._resolve_guild_id(event)
        async with self._api_client(token) as client:
            channels = await client.list_channels(guild_id)
        labels = {"category": "分组", "text": "文字", "voice": "语音"}
        lines = [f"KOOK 频道列表（{len(channels)} 项）"]
        for channel in channels:
            lines.append(f"[{labels[channel.kind]}] {channel.name}  ({channel.id})")
        return "\n".join(lines)

    @filter.llm_tool(name="kook_beautify_channels")
    async def kook_beautify_channels(
        self,
        event: AstrMessageEvent,
        instruction: str,
    ) -> str:
        """为当前 KOOK 服务器生成完整频道结构与名称美化预览。

        当管理员要求设计、创建、整理、美化、统一或重命名 KOOK 分组、文字频道、语音频道时调用。
        instruction 必须完整保留管理员指定的主题、风格、语言和例外要求。
        本工具只生成预览，不会直接修改频道。返回结果中会提供确认命令，必须让管理员自行发送该命令。
        用户明确回复“确认执行方案 <编号>”或发送确认命令后，应调用 kook_apply_beautify_plan。
        不要在用户尚未明确确认时调用执行工具，也不要声称已经修改完成。

        Args:
            instruction(string): 管理员对主题、风格、语言、分隔符和例外频道的完整自然语言要求。
        """
        try:
            plan = await self._create_plan(event, instruction)
            return format_plan_preview(plan)
        except (PlanError, KookApiError, asyncio.TimeoutError) as exc:
            logger.warning("[KOOK Beautify Tool] planning failed: %s", exc)
            return f"KOOK 频道美化预览生成失败：{exc}"
        except Exception as exc:
            logger.exception("KOOK beautify tool failed")
            return f"KOOK 频道美化预览生成失败：{exc.__class__.__name__}"

    @filter.llm_tool(name="kook_apply_beautify_plan")
    async def kook_apply_beautify_plan(
        self,
        event: AstrMessageEvent,
        plan_id: str,
        confirm: bool = False,
    ) -> str:
        """执行管理员已经明确确认的 KOOK 频道美化方案。

        只有当前用户消息明确包含同一个方案编号，并且说“确认执行方案”、发送 /kook美化确认
        或 /kook_beautify_confirm 时才能调用。必须把 confirm 设为 true。不能根据之前的对话、默认同意
        或模型自己的判断代替用户确认。此工具会实际创建 KOOK 频道并修改现有频道名称。

        Args:
            plan_id(string): 美化预览中显示的八位方案编号。
            confirm(boolean): 用户已在当前消息中明确确认执行时传 true，否则传 false。
        """
        plan_id = str(plan_id or "").strip().lower()
        if not bool(confirm):
            return "未执行：confirm 必须为 true，请先让管理员明确确认这个方案。"
        if not self._has_explicit_plan_action(
            event,
            plan_id,
            (
                "/kook美化确认",
                "/kook_beautify_confirm",
                "确认执行方案",
                "确认执行",
                "执行方案",
            ),
        ):
            return (
                "未执行：当前用户消息没有明确确认这个方案编号。"
                f"请让管理员回复“确认执行方案 {plan_id}”。"
            )
        try:
            return await self._apply_plan(event, plan_id)
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify Tool] apply rejected plan=%s error=%s", plan_id, exc)
            return f"执行美化方案失败：{exc}"
        except Exception as exc:
            logger.exception("KOOK beautify apply tool failed")
            return f"执行美化方案失败：{exc.__class__.__name__}"

    @filter.llm_tool(name="kook_rollback_beautify_plan")
    async def kook_rollback_beautify_plan(
        self,
        event: AstrMessageEvent,
        plan_id: str,
        confirm: bool = False,
    ) -> str:
        """撤销管理员已经明确确认撤销的 KOOK 频道美化方案。

        只有当前用户消息明确包含同一个方案编号，并且说“确认撤销方案”、发送 /kook美化撤销
        或 /kook_beautify_rollback 时才能调用。必须把 confirm 设为 true。此工具会实际恢复原频道名称。

        Args:
            plan_id(string): 已执行美化方案的八位方案编号。
            confirm(boolean): 用户已在当前消息中明确确认撤销时传 true，否则传 false。
        """
        plan_id = str(plan_id or "").strip().lower()
        if not bool(confirm):
            return "未撤销：confirm 必须为 true，请先让管理员明确确认撤销。"
        if not self._has_explicit_plan_action(
            event,
            plan_id,
            (
                "/kook美化撤销",
                "/kook_beautify_rollback",
                "确认撤销方案",
                "确认撤销",
                "撤销方案",
            ),
        ):
            return (
                "未撤销：当前用户消息没有明确确认撤销这个方案编号。"
                f"请让管理员回复“确认撤销方案 {plan_id}”。"
            )
        try:
            return await self._rollback_plan(event, plan_id)
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify Tool] rollback rejected plan=%s error=%s", plan_id, exc)
            return f"撤销美化方案失败：{exc}"
        except Exception as exc:
            logger.exception("KOOK beautify rollback tool failed")
            return f"撤销美化方案失败：{exc.__class__.__name__}"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook美化")
    async def beautify_command(self, event: AstrMessageEvent):
        instruction = str(event.message_str or "").strip()
        if instruction.startswith("/kook美化"):
            instruction = instruction[len("/kook美化") :].strip()
        try:
            plan = await self._create_plan(event, instruction)
            yield event.plain_result(format_plan_preview(plan))
        except (PlanError, KookApiError, asyncio.TimeoutError) as exc:
            yield event.plain_result(f"生成美化预览失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK beautify command failed")
            yield event.plain_result(f"生成美化预览失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook美化确认")
    async def confirm_command(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook美化确认").strip().lower()
        try:
            yield event.plain_result(await self._apply_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify] confirmation rejected plan=%s error=%s", plan_id, exc)
            yield event.plain_result(f"执行美化方案失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK beautify confirmation failed")
            yield event.plain_result(f"执行美化方案失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook_beautify_confirm")
    async def confirm_command_alias(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook_beautify_confirm").strip().lower()
        try:
            yield event.plain_result(await self._apply_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify] confirmation alias rejected plan=%s error=%s", plan_id, exc)
            yield event.plain_result(f"执行美化方案失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK beautify confirmation alias failed")
            yield event.plain_result(f"执行美化方案失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook美化撤销")
    async def rollback_command(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook美化撤销").strip().lower()
        try:
            yield event.plain_result(await self._rollback_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify] rollback rejected plan=%s error=%s", plan_id, exc)
            yield event.plain_result(f"撤销美化方案失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK beautify rollback failed")
            yield event.plain_result(f"撤销美化方案失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook_beautify_rollback")
    async def rollback_command_alias(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook_beautify_rollback").strip().lower()
        try:
            yield event.plain_result(await self._rollback_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify] rollback alias rejected plan=%s error=%s", plan_id, exc)
            yield event.plain_result(f"撤销美化方案失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK beautify rollback alias failed")
            yield event.plain_result(f"撤销美化方案失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook频道列表")
    async def list_command(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._channel_list(event))
        except (PlanError, KookApiError) as exc:
            yield event.plain_result(f"读取频道列表失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK channel list failed")
            yield event.plain_result(f"读取频道列表失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook美化帮助")
    async def help_command(self, event: AstrMessageEvent):
        yield event.plain_result(
            "KOOK 频道美化命令\n"
            "/kook美化 <自然语言要求>  生成预览\n"
            "/kook美化确认 <方案编号>  一键应用结构\n"
            "/kook美化撤销 <方案编号>  删除本方案新建频道并恢复原名称\n"
            "/kook_beautify_confirm <方案编号>  英文确认命令\n"
            "/kook_beautify_rollback <方案编号>  英文撤销命令\n"
            "/kook频道列表  查看当前频道和 ID\n\n"
            "也可以直接对 AI 说“设计一套二次元社团频道并一键应用”，生成预览后回复“确认执行方案 编号”。"
        )
