import sys
import types
import unittest


class Decorators:
    class PermissionType:
        ADMIN = object()

    class PlatformAdapterType:
        KOOK = object()

    def __getattr__(self, name):
        return lambda *args, **kwargs: (lambda func: func)


if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_module.filter = Decorators()
    star = types.ModuleType("astrbot.api.star")
    star.Context = type("Context", (), {})
    star.Star = type("Star", (), {"__init__": lambda self, context: None})
    star.register = lambda *args, **kwargs: (lambda cls: cls)
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star,
    })

from beautify import Channel, CreateChange, DeleteChange, RenameChange
from kook_api import KookApiError
from main import KookNameBeautifyPlugin


class FakeEvent:
    message_str = ""

    def is_admin(self):
        return True

    def get_sender_id(self):
        return "admin"

    def get_platform_name(self):
        return "kook"

    def get_group_id(self):
        return "current"


class FakeClient:
    def __init__(self, channels, fail_create_name=""):
        self.channels = {item.id: item for item in channels}
        self.fail_create_name = fail_create_name
        self.next_id = 1
        self.deleted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def list_channels(self, guild_id):
        return list(self.channels.values())

    async def create_channel(self, guild_id, name, kind, **kwargs):
        if name == self.fail_create_name:
            raise KookApiError("simulated create failure")
        channel_id = f"new-{self.next_id}"
        self.next_id += 1
        channel = Channel(
            id=channel_id,
            name=name,
            type=0 if kind == "category" else (2 if kind == "voice" else 1),
            parent_id=kwargs.get("parent_id", ""),
            is_category=kind == "category",
        )
        self.channels[channel_id] = channel
        return channel

    async def update_channel_name(self, channel_id, name):
        old = self.channels[channel_id]
        self.channels[channel_id] = Channel(
            id=old.id,
            name=name,
            type=old.type,
            parent_id=old.parent_id,
            is_category=old.is_category,
        )

    async def delete_channel(self, channel_id):
        self.deleted.append(channel_id)
        self.channels.pop(channel_id)


class NonAdminEvent(FakeEvent):
    def is_admin(self):
        return False


class MainFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, client):
        plugin = KookNameBeautifyPlugin(object(), {"bot_token": "token", "guild_id": "guild"})
        plugin._api_client = lambda token: client
        return plugin

    def make_plan(self, plugin, creates):
        return plugin.plans.create(
            guild_id="guild",
            user_id="admin",
            instruction="test",
            changes=[RenameChange("old", "旧名称", "新名称", "text")],
            creates=creates,
        )

    async def test_apply_and_rollback_complete_structure(self):
        client = FakeClient([Channel("old", "旧名称", 1)])
        plugin = self.make_plugin(client)
        plan = self.make_plan(plugin, [
            CreateChange("cat", "新分组", "category"),
            CreateChange("chat", "新聊天", "text", parent_ref="cat"),
        ])

        result = await plugin._apply_plan(FakeEvent(), plan.id)
        self.assertIn("新建 2", result)
        self.assertEqual(client.channels["old"].name, "新名称")
        self.assertEqual(len(plan.created_channels), 2)
        self.assertEqual(plan.created_channels[1].parent_id, plan.created_channels[0].channel_id)

        result = await plugin._rollback_plan(FakeEvent(), plan.id)
        self.assertIn("删除 2", result)
        self.assertEqual(client.channels["old"].name, "旧名称")
        self.assertEqual(client.deleted, ["new-2", "new-1"])

    async def test_apply_failure_deletes_channels_created_by_plan(self):
        client = FakeClient(
            [Channel("old", "旧名称", 1)], fail_create_name="失败频道"
        )
        plugin = self.make_plugin(client)
        plan = self.make_plan(plugin, [
            CreateChange("cat", "新分组", "category"),
            CreateChange("bad", "失败频道", "text", parent_ref="cat"),
        ])

        with self.assertRaisesRegex(KookApiError, "simulated create failure"):
            await plugin._apply_plan(FakeEvent(), plan.id)
        self.assertEqual(set(client.channels), {"old"})
        self.assertEqual(client.channels["old"].name, "旧名称")
        self.assertFalse(plan.applied)

    async def test_rollback_refuses_category_with_external_child(self):
        client = FakeClient([Channel("old", "旧名称", 1)])
        plugin = self.make_plugin(client)
        plan = self.make_plan(plugin, [CreateChange("cat", "新分组", "category")])
        await plugin._apply_plan(FakeEvent(), plan.id)
        category_id = plan.created_channels[0].channel_id
        client.channels["manual"] = Channel(
            "manual", "人工频道", 1, parent_id=category_id
        )

        with self.assertRaisesRegex(Exception, "后来人工添加"):
            await plugin._rollback_plan(FakeEvent(), plan.id)
        self.assertIn(category_id, client.channels)
        self.assertEqual(client.channels["old"].name, "新名称")

    async def test_replacement_creates_before_permanently_deleting_old_channel(self):
        client = FakeClient([
            Channel("current", "机器人操作台", 1),
            Channel("old", "旧娱乐频道", 1),
        ])
        plugin = self.make_plugin(client)
        plan = plugin.plans.create(
            guild_id="guild",
            user_id="admin",
            instruction="套上新模板，旧频道都不要",
            changes=[],
            creates=[CreateChange("newcat", "赛博娱乐区", "category")],
            deletes=[DeleteChange("old", "旧娱乐频道", "text")],
        )

        result = await plugin._replace_plan(FakeEvent(), plan.id)
        self.assertIn("永久删除 1", result)
        self.assertIn("current", client.channels)
        self.assertNotIn("old", client.channels)
        self.assertTrue(any(channel.name == "赛博娱乐区" for channel in client.channels.values()))

    async def test_non_admin_cannot_plan_permanent_delete(self):
        client = FakeClient([Channel("old", "待删除频道", 1)])
        plugin = self.make_plugin(client)
        with self.assertRaisesRegex(Exception, "只有 AstrBot 管理员"):
            await plugin._create_deletion_plan(
                NonAdminEvent(), channel_name="待删除频道"
            )
        self.assertIn("old", client.channels)

    async def test_permanent_delete_by_exact_name(self):
        client = FakeClient([Channel("old", "待删除频道", 1)])
        plugin = self.make_plugin(client)
        plan = await plugin._create_deletion_plan(
            FakeEvent(), channel_name="待删除频道"
        )
        self.assertEqual(plan.deletes[0], DeleteChange("old", "待删除频道", "text", "管理员明确要求永久删除"))

        with self.assertRaisesRegex(Exception, "不能用普通确认"):
            await plugin._apply_plan(FakeEvent(), plan.id)
        result = await plugin._delete_plan(FakeEvent(), plan.id)
        self.assertIn("永久删除", result)
        self.assertNotIn("old", client.channels)
        with self.assertRaisesRegex(Exception, "无法撤销"):
            await plugin._rollback_plan(FakeEvent(), plan.id)

    async def test_nonempty_category_cannot_be_deleted(self):
        client = FakeClient([
            Channel("cat", "管理分组", 0, is_category=True),
            Channel("child", "管理频道", 1, parent_id="cat"),
        ])
        plugin = self.make_plugin(client)
        with self.assertRaisesRegex(Exception, "不能删除非空分组"):
            await plugin._create_deletion_plan(FakeEvent(), channel_id="cat")

    async def test_planning_refreshes_and_retries_stale_channel_id(self):
        client = FakeClient([Channel("old", "旧名称", 1)])
        plugin = self.make_plugin(client)
        outputs = iter([
            '{"renames":[{"channel_id":"deleted", "new_name":"失效"}],"creates":[]}',
            '{"renames":[{"channel_id":"old", "new_name":"新名称"}],"creates":[]}',
        ])
        validation_errors = []

        async def generate(event, instruction, channels, validation_error="", protected_channel_ids=()):
            validation_errors.append(validation_error)
            return next(outputs)

        plugin._generate_ai_plan = generate
        plan = await plugin._create_plan(FakeEvent(), "全部重新美化", "guild")

        self.assertEqual(plan.changes[0].channel_id, "old")
        self.assertEqual(validation_errors[0], "")
        self.assertIn("不存在的频道 ID", validation_errors[1])


if __name__ == "__main__":
    unittest.main()
