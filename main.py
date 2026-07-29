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
        DeleteChange,
        PlanError,
        PlanStore,
        RenamePlan,
        build_planner_prompt,
        extract_explicit_channel_ids,
        format_plan_preview,
        instruction_requires_creation,
        instruction_requires_deletion,
        instruction_requires_full_replacement,
        instruction_requires_grouped_template,
        parse_complete_plan,
        parse_structure_plan,
    )
    from .kook_api import KookApiClient, KookApiError
except ImportError:  # Allow direct local imports during standalone development.
    from beautify import (
        PLANNER_SYSTEM_PROMPT,
        Channel,
        CreatedChannel,
        DeleteChange,
        PlanError,
        PlanStore,
        RenamePlan,
        build_planner_prompt,
        extract_explicit_channel_ids,
        format_plan_preview,
        instruction_requires_creation,
        instruction_requires_deletion,
        instruction_requires_full_replacement,
        instruction_requires_grouped_template,
        parse_complete_plan,
        parse_structure_plan,
    )
    from kook_api import KookApiClient, KookApiError


__version__ = "0.5.0"


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

    async def _verify_created_channel_parents(
        self,
        client: KookApiClient,
        guild_id: str,
        created: list[CreatedChannel],
    ) -> None:
        expected = {
            item.channel_id: item
            for item in created
            if item.kind != "category" and item.parent_id
        }
        if not expected:
            return
        channels = await client.list_channels(guild_id)
        actual_parents = {channel.id: channel.parent_id for channel in channels}
        mismatches = [
            item
            for channel_id, item in expected.items()
            if str(actual_parents.get(channel_id, "") or "") != item.parent_id
        ]
        if not mismatches:
            self._debug(
                "[KOOK Beautify] parent verification success guild=%s channels=%s",
                guild_id,
                len(expected),
            )
            return
        for item in mismatches:
            logger.error(
                "[KOOK Beautify] parent verification failed guild=%s channel=%s name=%r expected=%s actual=%s",
                guild_id,
                item.channel_id,
                item.name,
                item.parent_id,
                actual_parents.get(item.channel_id, "missing") or "root",
            )
        raise KookApiError(
            "新频道未进入计划分组：" + "、".join(item.name for item in mismatches[:8])
        )

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

    @staticmethod
    def _find_channel_id(value: Any, depth: int = 0) -> str:
        if depth > 5:
            return ""
        if isinstance(value, dict):
            for key in ("channel_id", "target_id"):
                result = value.get(key)
                if isinstance(result, (str, int)) and str(result).strip():
                    return str(result).strip()
            for child in value.values():
                result = KookNameBeautifyPlugin._find_channel_id(child, depth + 1)
                if result:
                    return result
        elif isinstance(value, (list, tuple)):
            for child in value:
                result = KookNameBeautifyPlugin._find_channel_id(child, depth + 1)
                if result:
                    return result
        return ""

    def _resolve_current_channel_id(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                value = str(getter() or "").strip()
                if value:
                    return value
            except Exception:
                pass
        for source in (event, getattr(event, "message_obj", None)):
            for attr in ("channel_id", "group_id", "target_id"):
                value = getattr(source, attr, "")
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
        message_obj = getattr(event, "message_obj", None)
        for attr in ("raw_message", "raw_data", "message", "extra"):
            result = self._find_channel_id(getattr(message_obj, attr, None))
            if result:
                return result
        return ""

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
        protected_channel_ids: tuple[str, ...] = (),
    ) -> str:
        provider_id = await self._provider_id(event)
        system_prompt = PLANNER_SYSTEM_PROMPT
        if self.custom_planner_prompt:
            system_prompt += "\n\n管理员补充规范：\n" + self.custom_planner_prompt
        prompt = build_planner_prompt(instruction, channels, protected_channel_ids)
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
        require_full_replacement = instruction_requires_full_replacement(instruction)
        require_grouped_template = instruction_requires_grouped_template(instruction)
        if require_full_replacement:
            require_creates = True
        require_deletes = instruction_requires_deletion(instruction)
        explicit_parent_refs = extract_explicit_channel_ids(instruction)
        current_channel_id = self._resolve_current_channel_id(event)
        if require_deletes and not current_channel_id:
            raise PlanError("无法识别当前操作频道，为避免批量替换时删掉回复通道，已拒绝生成方案。")
        protected_channel_ids = (current_channel_id,) if require_deletes else ()
        for attempt in range(2):
            async with self._api_client(token) as client:
                channels = await client.list_channels(resolved_guild_id)
            if not channels:
                raise KookApiError("这个服务器没有返回可美化的频道。")
            if require_deletes and current_channel_id not in {channel.id for channel in channels}:
                raise PlanError("当前操作频道未出现在 KOOK 实时频道列表中，为避免误删已拒绝批量替换。")
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
                protected_channel_ids=protected_channel_ids,
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
                changes, creates, deletes = parse_complete_plan(
                    ai_output,
                    channels,
                    max_name_length=self.max_name_length,
                    max_changes=self.max_changes,
                    require_creates=require_creates,
                    require_deletes=require_deletes,
                    require_grouped_template=require_grouped_template,
                    require_full_replacement=require_full_replacement,
                    allowed_parent_refs=explicit_parent_refs,
                    protected_channel_ids=protected_channel_ids,
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
            deletes=deletes,
            protected_channel_ids=protected_channel_ids,
        )

    async def _create_deletion_plan(
        self,
        event: AstrMessageEvent,
        channel_id: str = "",
        channel_name: str = "",
    ) -> RenamePlan:
        self._check_kook_event(event)
        self._check_allowlist(event)
        channel_id = str(channel_id or "").strip()
        channel_name = str(channel_name or "").strip()
        if not channel_id and not channel_name:
            raise PlanError("请提供要永久删除的频道 ID 或完整频道名称。")
        token = self._resolve_token(event)
        guild_id = self._resolve_guild_id(event)
        async with self._api_client(token) as client:
            channels = await client.list_channels(guild_id)
        matches = [
            channel
            for channel in channels
            if (channel_id and channel.id == channel_id)
            or (not channel_id and channel.name == channel_name)
        ]
        if not matches:
            target = channel_id or channel_name
            raise PlanError(f"当前服务器找不到频道：{target}。请使用完整名称或最新频道 ID。")
        if len(matches) > 1:
            raise PlanError("存在多个同名频道，请改用频道 ID 指定删除目标。")
        target = matches[0]
        if channel_id and channel_name and target.name != channel_name:
            raise PlanError(
                f"频道 ID {channel_id} 当前名称是“{target.name}”，与指定名称不一致。"
            )
        if target.kind == "category":
            children = [channel.name for channel in channels if channel.parent_id == target.id]
            if children:
                raise PlanError(
                    "不能删除非空分组“"
                    + target.name
                    + "”，请先单独处理其中频道："
                    + "、".join(children[:8])
                )
        return self.plans.create(
            guild_id=guild_id,
            user_id=self._sender_id(event),
            instruction=f"永久删除频道 {target.name}",
            changes=[],
            creates=[],
            deletes=[DeleteChange(target.id, target.name, target.kind, "管理员明确要求永久删除")],
        )

    async def _delete_plan(self, event: AstrMessageEvent, plan_id: str) -> str:
        self._check_kook_event(event)
        self._check_allowlist(event)
        plan = self.plans.get(plan_id, user_id=self._sender_id(event))
        if plan.applied:
            raise PlanError("这个永久删除方案已经执行过了。")
        if len(plan.deletes) != 1 or plan.changes or plan.creates:
            raise PlanError("该方案不是独立的单频道删除方案，已拒绝执行。")
        target = plan.deletes[0]
        token = self._resolve_token(event)
        async with self._mutation_lock:
            async with self._api_client(token) as client:
                channels = await client.list_channels(plan.guild_id)
                current = {channel.id: channel for channel in channels}.get(target.channel_id)
                if current is None:
                    raise PlanError("目标频道已经不存在，未重复删除。")
                if current.name != target.old_name or current.kind != target.kind:
                    raise PlanError(
                        f"目标频道已发生变化，未删除：预览为“{target.old_name}”，"
                        f"当前为“{current.name}”。"
                    )
                if current.kind == "category":
                    children = [
                        channel.name for channel in channels if channel.parent_id == current.id
                    ]
                    if children:
                        raise PlanError(
                            "删除前发现分组中已有频道，未执行永久删除："
                            + "、".join(children[:8])
                        )
                self._debug(
                    "[KOOK Beautify] permanent delete start plan=%s channel=%s kind=%s name=%r",
                    plan.id,
                    target.channel_id,
                    target.kind,
                    target.old_name,
                )
                await client.delete_channel(target.channel_id)
        plan.applied = True
        logger.info(
            "[KOOK Beautify] permanently deleted guild=%s plan=%s channel=%s name=%r",
            plan.guild_id,
            plan.id,
            target.channel_id,
            target.old_name,
        )
        return (
            f"频道“{target.old_name}”已永久删除。"
            "此操作无法通过 /kook美化撤销 恢复频道内容、消息或权限。"
        )

    async def _replace_plan(self, event: AstrMessageEvent, plan_id: str) -> str:
        self._check_kook_event(event)
        self._check_allowlist(event)
        plan = self.plans.get(plan_id, user_id=self._sender_id(event))
        if plan.applied:
            raise PlanError("这个永久替换方案已经执行过了。")
        if not plan.deletes or not (plan.creates or plan.changes or len(plan.deletes) > 1):
            raise PlanError("该方案不是批量永久替换方案。")
        token = self._resolve_token(event)
        created: list[CreatedChannel] = []
        applied = []
        permanently_deleted = []
        moved_channels: list[tuple[str, str, str]] = []
        full_replacement = instruction_requires_full_replacement(plan.instruction)
        protected_ids = set(plan.protected_channel_ids)
        async with self._mutation_lock:
            async with self._api_client(token) as client:
                current_channels = await client.list_channels(plan.guild_id)
                current_map = {channel.id: channel for channel in current_channels}
                delete_ids = {item.channel_id for item in plan.deletes}
                conflicts = [
                    change.old_name
                    for change in plan.changes
                    if change.channel_id not in current_map
                    or current_map[change.channel_id].name != change.old_name
                ]
                conflicts.extend(
                    item.old_name
                    for item in plan.deletes
                    if item.channel_id not in current_map
                    or current_map[item.channel_id].name != item.old_name
                    or current_map[item.channel_id].kind != item.kind
                )
                if conflicts:
                    raise PlanError(
                        "永久替换前发现频道已变化，方案未执行：" + "、".join(conflicts[:8])
                    )
                for item in plan.deletes:
                    if item.kind != "category":
                        continue
                    outside_children = [
                        channel.name
                        for channel in current_channels
                        if channel.parent_id == item.channel_id
                        and channel.id not in delete_ids
                        and not (full_replacement and channel.id in protected_ids)
                    ]
                    if outside_children:
                        raise PlanError(
                            f"待删除分组“{item.old_name}”仍包含必须保留的频道："
                            + "、".join(outside_children[:8])
                        )
                rename_ids = {change.channel_id for change in plan.changes}
                occupied_names = {
                    channel.name
                    for channel in current_channels
                    if channel.id not in rename_ids and channel.id not in delete_ids
                }
                planned_names = [
                    *(change.new_name for change in plan.changes),
                    *(item.name for item in plan.creates),
                ]
                duplicate_names = [name for name in planned_names if name in occupied_names]
                if duplicate_names:
                    raise PlanError(
                        "永久替换前发现新名称已被保留频道占用："
                        + "、".join(duplicate_names[:8])
                    )
                temp_id_map: dict[str, str] = {}
                try:
                    for index, item in enumerate(plan.creates, start=1):
                        parent_id = temp_id_map.get(item.parent_ref, item.parent_ref)
                        self._debug(
                            "[KOOK Beautify] replacement create plan=%s item=%s/%s temp=%s name=%r",
                            plan.id,
                            index,
                            len(plan.creates),
                            item.temp_id,
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
                            item.temp_id,
                            channel.id,
                            item.name,
                            item.kind,
                            parent_id,
                        ))
                    await self._verify_created_channel_parents(
                        client, plan.guild_id, created
                    )
                    if full_replacement:
                        new_category = next(
                            (item for item in created if item.kind == "category"),
                            None,
                        )
                        if new_category is None:
                            raise PlanError("全部替换方案没有可接收当前操作频道的新分组。")
                        for channel in current_channels:
                            if channel.id not in protected_ids:
                                continue
                            self._debug(
                                "[KOOK Beautify] replacement move protected channel=%s old_parent=%s new_parent=%s",
                                channel.id,
                                channel.parent_id or "root",
                                new_category.channel_id,
                            )
                            moved_channels.append(
                                (channel.id, channel.parent_id, new_category.channel_id)
                            )
                            await client.update_channel_parent(
                                channel.id, new_category.channel_id
                            )
                        if moved_channels:
                            refreshed = await client.list_channels(plan.guild_id)
                            refreshed_map = {channel.id: channel for channel in refreshed}
                            misplaced = [
                                channel_id
                                for channel_id, _, new_parent in moved_channels
                                if channel_id not in refreshed_map
                                or refreshed_map[channel_id].parent_id != new_parent
                            ]
                            if misplaced:
                                raise KookApiError(
                                    "受保护的当前操作频道未能迁入新分组："
                                    + "、".join(misplaced)
                                )
                    for change in plan.changes:
                        await client.update_channel_name(change.channel_id, change.new_name)
                        applied.append(change)
                    for item in plan.deletes:
                        self._debug(
                            "[KOOK Beautify] replacement permanent delete plan=%s channel=%s kind=%s name=%r",
                            plan.id,
                            item.channel_id,
                            item.kind,
                            item.old_name,
                        )
                        await client.delete_channel(item.channel_id)
                        permanently_deleted.append(item)
                except Exception as exc:
                    if permanently_deleted:
                        plan.applied = True
                        plan.created_channels = created
                        plan.applied_channel_ids = [change.channel_id for change in applied]
                        raise KookApiError(
                            f"永久替换已进入不可逆删除阶段后失败：{exc}。"
                            f"已永久删除 {len(permanently_deleted)} 个旧频道，"
                            "为避免进一步破坏，未自动撤回新结构；请根据日志人工检查。"
                        ) from exc
                    rollback_failed = []
                    for change in reversed(applied):
                        try:
                            await client.update_channel_name(change.channel_id, change.old_name)
                        except Exception:
                            rollback_failed.append(change.new_name)
                    for channel_id, old_parent, _ in reversed(moved_channels):
                        try:
                            await client.update_channel_parent(channel_id, old_parent)
                        except Exception:
                            rollback_failed.append(channel_id)
                    for item in reversed(created):
                        try:
                            await client.delete_channel(item.channel_id)
                        except Exception:
                            rollback_failed.append(item.name)
                    detail = f"永久替换在删除旧频道前失败：{exc}。已尝试恢复改名并清理新建频道。"
                    if rollback_failed:
                        detail += " 自动恢复失败：" + "、".join(rollback_failed)
                    raise KookApiError(detail) from exc
        plan.applied = True
        plan.created_channels = created
        plan.applied_channel_ids = [change.channel_id for change in applied]
        logger.info(
            "[KOOK Beautify] replacement applied guild=%s plan=%s created=%s renamed=%s deleted=%s",
            plan.guild_id,
            plan.id,
            len(created),
            len(applied),
            len(permanently_deleted),
        )
        return (
            f"永久替换方案 {plan.id} 已完成：新建 {len(created)} 个，"
            f"改名 {len(applied)} 个，永久删除 {len(permanently_deleted)} 个旧频道。"
            "被删除频道的消息和权限无法恢复。"
        )

    async def _apply_plan(self, event: AstrMessageEvent, plan_id: str) -> str:
        self._check_kook_event(event)
        self._check_allowlist(event)
        plan = self.plans.get(plan_id, user_id=self._sender_id(event))
        if plan.applied:
            raise PlanError("这个方案已经执行过了。")
        if plan.deletes:
            command = "/kook替换确认" if (plan.creates or plan.changes or len(plan.deletes) > 1) else "/kook删除确认"
            raise PlanError(f"永久删除方案不能用普通确认执行，请发送：{command} {plan.id}")
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
                    await self._verify_created_channel_parents(
                        client, plan.guild_id, created
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
        if plan.deletes:
            raise PlanError("永久删除的频道内容、消息和权限无法撤销恢复。")
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
        若管理员只删除一个频道，应调用 kook_plan_channel_deletion；若要求生成新模板并替换、批量删除旧频道，则使用本工具生成永久替换预览。
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

    @filter.llm_tool(name="kook_apply_replacement_plan")
    async def kook_apply_replacement_plan(
        self,
        event: AstrMessageEvent,
        plan_id: str,
        confirm: bool = False,
    ) -> str:
        """执行管理员在当前消息中明确确认的 KOOK 批量永久替换方案。

        只有当前消息包含同一方案编号及“确认永久替换方案”、/kook替换确认 或
        /kook_replace_confirm 时才能调用，并且 confirm 必须为 true。该操作会先创建新模板，
        再永久删除预览中的旧频道，删除内容不可撤销。
        Args:
            plan_id(string): 永久替换预览中的八位方案编号。
            confirm(boolean): 管理员当前消息明确确认永久替换时传 true，否则传 false。
        """
        plan_id = str(plan_id or "").strip().lower()
        if not bool(confirm):
            return "未替换：confirm 必须为 true，请先让管理员明确确认永久替换。"
        if not self._has_explicit_plan_action(
            event,
            plan_id,
            (
                "/kook替换确认",
                "/kook_replace_confirm",
                "确认永久替换方案",
                "确认永久替换",
            ),
        ):
            return (
                "未替换：当前消息没有明确确认永久替换这个方案。"
                f"请让管理员回复“确认永久替换方案 {plan_id}”。"
            )
        try:
            return await self._replace_plan(event, plan_id)
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify Tool] replacement rejected plan=%s error=%s", plan_id, exc)
            return f"永久替换频道结构失败：{exc}"
        except Exception as exc:
            logger.exception("KOOK replacement tool failed")
            return f"永久替换频道结构失败：{exc.__class__.__name__}"

    @filter.llm_tool(name="kook_plan_channel_deletion")
    async def kook_plan_channel_deletion(
        self,
        event: AstrMessageEvent,
        channel_id: str = "",
        channel_name: str = "",
    ) -> str:
        """为管理员明确指定的一个 KOOK 频道生成永久删除预览。

        用户明确要求删除、移除某一个频道或空分组时调用。优先传 channel_id；只有用户仅提供名称时
        才传完整 channel_name。本工具只生成预览，不直接删除。频道删除会永久丢失消息和权限，
        必须让管理员随后明确回复“确认永久删除方案 <编号>”。
        Args:
            channel_id(string): 要删除的 KOOK 频道 ID；未知时传空字符串。
            channel_name(string): 要删除的完整频道名称；已提供 ID 时可传空字符串。
        """
        try:
            plan = await self._create_deletion_plan(event, channel_id, channel_name)
            return format_plan_preview(plan)
        except (PlanError, KookApiError) as exc:
            logger.warning("[KOOK Beautify Tool] delete planning failed: %s", exc)
            return f"KOOK 频道永久删除预览生成失败：{exc}"
        except Exception as exc:
            logger.exception("KOOK delete planning tool failed")
            return f"KOOK 频道永久删除预览生成失败：{exc.__class__.__name__}"

    @filter.llm_tool(name="kook_apply_channel_deletion_plan")
    async def kook_apply_channel_deletion_plan(
        self,
        event: AstrMessageEvent,
        plan_id: str,
        confirm: bool = False,
    ) -> str:
        """执行管理员在当前消息中明确确认的单频道永久删除方案。

        只有当前消息包含同一方案编号及“确认永久删除方案”、/kook删除确认 或
        /kook_delete_confirm 时才能调用，并且 confirm 必须为 true。不能根据历史消息代替确认。
        Args:
            plan_id(string): 永久删除预览中的八位方案编号。
            confirm(boolean): 管理员当前消息明确确认永久删除时传 true，否则传 false。
        """
        plan_id = str(plan_id or "").strip().lower()
        if not bool(confirm):
            return "未删除：confirm 必须为 true，请先让管理员明确确认永久删除。"
        if not self._has_explicit_plan_action(
            event,
            plan_id,
            (
                "/kook删除确认",
                "/kook_delete_confirm",
                "确认永久删除方案",
                "确认永久删除",
            ),
        ):
            return (
                "未删除：当前消息没有明确确认永久删除这个方案。"
                f"请让管理员回复“确认永久删除方案 {plan_id}”。"
            )
        try:
            return await self._delete_plan(event, plan_id)
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify Tool] permanent delete rejected plan=%s error=%s", plan_id, exc)
            return f"永久删除频道失败：{exc}"
        except Exception as exc:
            logger.exception("KOOK permanent delete tool failed")
            return f"永久删除频道失败：{exc.__class__.__name__}"

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
    @filter.command("kook替换确认")
    async def replacement_confirm_command(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook替换确认").strip().lower()
        try:
            yield event.plain_result(await self._replace_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify] replacement rejected plan=%s error=%s", plan_id, exc)
            yield event.plain_result(f"永久替换频道结构失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK replacement command failed")
            yield event.plain_result(f"永久替换频道结构失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook_replace_confirm")
    async def replacement_confirm_command_alias(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook_replace_confirm").strip().lower()
        try:
            yield event.plain_result(await self._replace_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify] replacement alias rejected plan=%s error=%s", plan_id, exc)
            yield event.plain_result(f"永久替换频道结构失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK replacement alias failed")
            yield event.plain_result(f"永久替换频道结构失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook删除确认")
    async def delete_confirm_command(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook删除确认").strip().lower()
        try:
            yield event.plain_result(await self._delete_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify] permanent delete rejected plan=%s error=%s", plan_id, exc)
            yield event.plain_result(f"永久删除频道失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK permanent delete command failed")
            yield event.plain_result(f"永久删除频道失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook_delete_confirm")
    async def delete_confirm_command_alias(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook_delete_confirm").strip().lower()
        try:
            yield event.plain_result(await self._delete_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            logger.error("[KOOK Beautify] permanent delete alias rejected plan=%s error=%s", plan_id, exc)
            yield event.plain_result(f"永久删除频道失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK permanent delete alias failed")
            yield event.plain_result(f"永久删除频道失败：{exc.__class__.__name__}")

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
            "/kook删除确认 <方案编号>  永久删除预览中的单个频道\n"
            "/kook替换确认 <方案编号>  先建新模板再永久删除旧频道\n"
            "/kook_replace_confirm <方案编号>  英文永久替换确认命令\n"
            "/kook_delete_confirm <方案编号>  英文永久删除确认命令\n"
            "/kook_beautify_confirm <方案编号>  英文确认命令\n"
            "/kook_beautify_rollback <方案编号>  英文撤销命令\n"
            "/kook频道列表  查看当前频道和 ID\n\n"
            "也可以直接对 AI 说“设计一套二次元社团频道并一键应用”，生成预览后回复“确认执行方案 编号”。"
        )
