import sys
import types
import unittest

try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    aiohttp = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    class ContentTypeError(ClientError):
        pass

    class ClientTimeout:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class ClientSession:
        pass

    aiohttp.ClientError = ClientError
    aiohttp.ContentTypeError = ContentTypeError
    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp

from kook_api import KookApiClient


class FakeKookApiClient(KookApiClient):
    def __init__(self):
        super().__init__("test-token", request_interval_ms=0)
        self.calls = []

    async def _request(self, method, path, *, params=None, json_body=None):
        self.calls.append((method, path, params, json_body))
        if path == "channel/update":
            return {}
        if path == "channel/create":
            return {
                "id": "created-id",
                "name": json_body["name"],
                "type": json_body.get("type", 0),
                "is_category": bool(json_body.get("is_category")),
                "parent_id": json_body.get("parent_id", ""),
            }
        if path == "channel/delete":
            return {}
        channel_type = params["type"]
        page = params["page"]
        if channel_type == 1 and page == 1:
            return {
                "items": [
                    {"id": "cat", "name": "社区", "type": 0, "is_category": True, "level": 1},
                    {"id": "text1", "name": "聊天", "type": 1, "level": 2},
                ],
                "meta": {"page_total": 2},
            }
        if channel_type == 1 and page == 2:
            return {
                "items": [{"id": "text2", "name": "分享", "type": 1, "level": 3}],
                "meta": {"page_total": 2},
            }
        return {
            "items": [
                {"id": "cat", "name": "社区", "type": 0, "is_category": True, "level": 1},
                {"id": "voice", "name": "语音", "type": 2, "level": 4},
            ],
            "meta": {"page_total": 1},
        }


class KookApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_debug_callback_is_opt_in(self):
        messages = []
        client = KookApiClient(
            "test-token",
            debug=True,
            debug_log=lambda message, *args: messages.append(message % args),
        )
        client._debug("request path=%s", "/channel/update")
        self.assertEqual(messages, ["request path=/channel/update"])

        quiet_messages = []
        quiet_client = KookApiClient(
            "test-token",
            debug=False,
            debug_log=lambda message, *args: quiet_messages.append(message % args),
        )
        quiet_client._debug("should not be logged")
        self.assertEqual(quiet_messages, [])

    async def test_list_channels_paginates_both_types_and_deduplicates_categories(self):
        client = FakeKookApiClient()
        channels = await client.list_channels("guild")
        self.assertEqual({channel.id for channel in channels}, {"cat", "text1", "text2", "voice"})
        self.assertEqual(len(client.calls), 3)

    async def test_update_channel_uses_official_endpoint_and_json_body(self):
        client = FakeKookApiClient()
        await client.update_channel_name("123", "💬・聊天")
        self.assertEqual(
            client.calls,
            [("POST", "channel/update", None, {"channel_id": "123", "name": "💬・聊天"})],
        )

    async def test_update_channel_parent_uses_official_parent_id_parameter(self):
        client = FakeKookApiClient()
        await client.update_channel_parent("123", "category-456")
        await client.update_channel_parent("123", "")
        self.assertEqual(
            client.calls,
            [
                ("POST", "channel/update", None, {
                    "channel_id": "123", "parent_id": "category-456"
                }),
                ("POST", "channel/update", None, {
                    "channel_id": "123", "parent_id": "0"
                }),
            ],
        )

    async def test_create_category_text_and_voice_use_official_payloads(self):
        client = FakeKookApiClient()
        category = await client.create_channel("guild", "COMMUNITY", "category")
        text_channel = await client.create_channel(
            "guild", "闲聊", "text", parent_id=category.id
        )
        await client.create_channel(
            "guild",
            "语音",
            "voice",
            parent_id=category.id,
            limit_amount=25,
            voice_quality="3",
        )
        self.assertEqual(category.id, "created-id")
        self.assertEqual(text_channel.parent_id, "created-id")
        self.assertEqual(client.calls[0][3], {
            "guild_id": "guild", "name": "COMMUNITY", "is_category": 1
        })
        self.assertEqual(client.calls[1][3]["type"], 1)
        self.assertEqual(client.calls[2][3]["type"], 2)
        self.assertEqual(client.calls[2][3]["limit_amount"], 25)
        self.assertEqual(client.calls[2][3]["voice_quality"], "3")

    async def test_delete_channel_uses_official_endpoint(self):
        client = FakeKookApiClient()
        await client.delete_channel("123")
        self.assertEqual(
            client.calls,
            [("POST", "channel/delete", None, {"channel_id": "123"})],
        )


if __name__ == "__main__":
    unittest.main()
