"""AstrBot plugin for AI-assisted KOOK channel name beautification."""

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
        PlanError,
        PlanStore,
        RenamePlan,
        build_planner_prompt,
        format_plan_preview,
        parse_rename_plan,
    )
    from .kook_api import KookApiClient, KookApiError
except ImportError:  # Allow direct local imports during standalone development.
    from beautify import (
        PLANNER_SYSTEM_PROMPT,
        Channel,
        PlanError,
        PlanStore,
        RenamePlan,
        build_planner_prompt,
        format_plan_preview,
        parse_rename_plan,
    )
    from kook_api import KookApiClient, KookApiError


__version__ = "0.1.1"


@register(
    "astrbot_plugin_kook_name_beautify",
    "xzw38",
    "用自然语言预览并安全执行 KOOK 频道名称美化",
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
            return self.bot_token
        client = getattr(event, "client", None)
        for attr in ("token", "bot_token"):
            value = getattr(client, attr, "")
            if isinstance(value, str) and value.strip():
                return value.strip()
        client_config = getattr(client, "config", None)
        if isinstance(client_config, dict):
            for key in ("token", "bot_token"):
                value = client_config.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        else:
            for attr in ("token", "bot_token"):
                value = getattr(client_config, attr, "")
                if isinstance(value, str) and value.strip():
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
            return str(requested).strip()
        if self.default_guild_id:
            return self.default_guild_id
        for source in (event, getattr(event, "message_obj", None)):
            for attr in ("guild_id", "server_id"):
                value = getattr(source, attr, "")
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
        message_obj = getattr(event, "message_obj", None)
        for attr in ("raw_message", "raw_data", "message", "extra"):
            result = self._find_guild_id(getattr(message_obj, attr, None))
            if result:
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
    ) -> str:
        provider_id = await self._provider_id(event)
        system_prompt = PLANNER_SYSTEM_PROMPT
        if self.custom_planner_prompt:
            system_prompt += "\n\n管理员补充规范：\n" + self.custom_planner_prompt
        result = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=build_planner_prompt(instruction, channels),
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
        async with self._api_client(token) as client:
            channels = await client.list_channels(resolved_guild_id)
        if not channels:
            raise KookApiError("这个服务器没有返回可美化的频道。")
        logger.info(
            "[KOOK Beautify] planning guild=%s channels=%s user=%s",
            resolved_guild_id,
            len(channels),
            self._sender_id(event),
        )
        ai_output = await self._generate_ai_plan(event, instruction, channels)
        changes = parse_rename_plan(
            ai_output,
            channels,
            max_name_length=self.max_name_length,
            max_changes=self.max_changes,
        )
        return self.plans.create(
            guild_id=resolved_guild_id,
            user_id=self._sender_id(event),
            instruction=instruction,
            changes=changes,
        )

    async def _apply_plan(self, event: AstrMessageEvent, plan_id: str) -> str:
        self._check_kook_event(event)
        self._check_allowlist(event)
        plan = self.plans.get(plan_id, user_id=self._sender_id(event))
        if plan.applied:
            raise PlanError("这个方案已经执行过了。")
        token = self._resolve_token(event)
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
                    raise PlanError(
                        "执行前检查发现频道已被修改，方案未执行：" + "、".join(conflicts[:8])
                    )
                applied = []
                try:
                    for change in plan.changes:
                        await client.update_channel_name(change.channel_id, change.new_name)
                        applied.append(change)
                except Exception as exc:
                    rollback_failed = []
                    for change in reversed(applied):
                        try:
                            await client.update_channel_name(change.channel_id, change.old_name)
                        except Exception:
                            rollback_failed.append(change.new_name)
                    detail = f"执行到第 {len(applied) + 1} 项时失败，已尝试恢复之前的名称。"
                    if rollback_failed:
                        detail += " 以下频道自动恢复失败：" + "、".join(rollback_failed)
                    raise KookApiError(detail) from exc
        plan.applied = True
        plan.applied_channel_ids = [change.channel_id for change in plan.changes]
        logger.info("[KOOK Beautify] applied guild=%s plan=%s changes=%s", plan.guild_id, plan.id, len(plan.changes))
        return f"方案 {plan.id} 已执行，共更新 {len(plan.changes)} 个频道名称。\n需要恢复时发送：/kook美化撤销 {plan.id}"

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
        async with self._mutation_lock:
            async with self._api_client(token) as client:
                current_channels = await client.list_channels(plan.guild_id)
                current_names = {channel.id: channel.name for channel in current_channels}
                conflicts = [
                    change.new_name
                    for change in plan.changes
                    if current_names.get(change.channel_id) != change.new_name
                ]
                if conflicts:
                    raise PlanError(
                        "撤销前检查发现频道名称又被修改，未自动覆盖：" + "、".join(conflicts[:8])
                    )
                restored_changes = []
                try:
                    for change in reversed(plan.changes):
                        await client.update_channel_name(change.channel_id, change.old_name)
                        restored_changes.append(change)
                except Exception as exc:
                    compensation_failed = []
                    for change in reversed(restored_changes):
                        try:
                            await client.update_channel_name(change.channel_id, change.new_name)
                        except Exception:
                            compensation_failed.append(change.old_name)
                    detail = "撤销过程中发生错误，已尝试恢复到撤销前状态。"
                    if compensation_failed:
                        detail += " 以下频道恢复失败：" + "、".join(compensation_failed)
                    raise KookApiError(detail) from exc
        plan.rolled_back = True
        restored = len(restored_changes)
        logger.info("[KOOK Beautify] rolled back guild=%s plan=%s changes=%s", plan.guild_id, plan.id, restored)
        return f"方案 {plan.id} 已撤销，共恢复 {restored} 个频道名称。"

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
        """为当前 KOOK 服务器生成频道名称美化预览。

        当管理员用自然语言要求整理、美化、统一或重命名 KOOK 的分组、文字频道、语音频道时调用。
        instruction 必须完整保留管理员指定的主题、风格、语言和例外要求。
        本工具只生成预览，不会直接修改频道。返回结果中会提供确认命令，必须让管理员自行发送该命令。
        不要声称已经修改完成，也不要代替管理员确认。

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
            yield event.plain_result(f"执行美化方案失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK beautify confirmation failed")
            yield event.plain_result(f"执行美化方案失败：{exc.__class__.__name__}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    @filter.command("kook美化撤销")
    async def rollback_command(self, event: AstrMessageEvent):
        plan_id = str(event.message_str or "").removeprefix("/kook美化撤销").strip().lower()
        try:
            yield event.plain_result(await self._rollback_plan(event, plan_id))
        except (PlanError, KookApiError) as exc:
            yield event.plain_result(f"撤销美化方案失败：{exc}")
        except Exception as exc:
            logger.exception("KOOK beautify rollback failed")
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
            "/kook美化确认 <方案编号>  执行改名\n"
            "/kook美化撤销 <方案编号>  恢复原名称\n"
            "/kook频道列表  查看当前频道和 ID\n\n"
            "也可以直接对 AI 说“把这个服务器的频道统一成简约科技风”，AI 会调用本插件生成预览。"
        )
