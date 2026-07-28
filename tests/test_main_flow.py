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

from beautify import Channel, CreateChange, RenameChange
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


class MainFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, client):
        plugin = KookNameBeautifyPlugin(object(), {"bot_token": "token"})
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


if __name__ == "__main__":
    unittest.main()
