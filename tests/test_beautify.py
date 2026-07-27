import json
import unittest

from beautify import Channel, PlanError, build_planner_prompt, parse_rename_plan


CHANNELS = [
    Channel(id="cat", name="社区交流", type=0, level=1, is_category=True),
    Channel(id="text", name="闲聊大厅", type=1, level=2, parent_id="cat"),
    Channel(id="voice", name="组队开黑", type=2, level=3, parent_id="cat"),
]


class RenamePlanTests(unittest.TestCase):
    def test_parse_fenced_json_plan(self):
        payload = {
            "renames": [
                {"channel_id": "cat", "new_name": "『 COMMUNITY 』", "reason": "统一分组"},
                {"channel_id": "text", "new_name": "💬・闲聊大厅"},
            ]
        }
        changes = parse_rename_plan(f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```", CHANNELS)
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0].old_name, "社区交流")
        self.assertEqual(changes[0].new_name, "『 COMMUNITY 』")
        self.assertEqual(changes[1].kind, "text")

    def test_unknown_channel_is_rejected(self):
        with self.assertRaisesRegex(PlanError, "不存在的频道"):
            parse_rename_plan(
                '{"renames":[{"channel_id":"missing","new_name":"新名称"}]}',
                CHANNELS,
            )

    def test_duplicate_channel_is_rejected(self):
        with self.assertRaisesRegex(PlanError, "重复出现"):
            parse_rename_plan(
                '{"renames":['
                '{"channel_id":"text","new_name":"聊天"},'
                '{"channel_id":"text","new_name":"闲聊"}'
                "]}",
                CHANNELS,
            )

    def test_duplicate_result_name_is_rejected(self):
        with self.assertRaisesRegex(PlanError, "重名"):
            parse_rename_plan(
                '{"renames":[{"channel_id":"text","new_name":"组队开黑"}]}',
                CHANNELS,
            )

    def test_two_channel_name_swap_is_order_independent(self):
        changes = parse_rename_plan(
            '{"renames":['
            '{"channel_id":"text","new_name":"组队开黑"},'
            '{"channel_id":"voice","new_name":"闲聊大厅"}'
            "]}",
            CHANNELS,
        )
        self.assertEqual([item.new_name for item in changes], ["组队开黑", "闲聊大厅"])

    def test_control_character_and_length_are_rejected(self):
        with self.assertRaisesRegex(PlanError, "控制字符"):
            parse_rename_plan(
                '{"renames":[{"channel_id":"text","new_name":"坏\\n名称"}]}',
                CHANNELS,
            )
        with self.assertRaisesRegex(PlanError, "超过上限"):
            parse_rename_plan(
                '{"renames":[{"channel_id":"text","new_name":"123456789"}]}',
                CHANNELS,
                max_name_length=8,
            )

    def test_prompt_treats_channel_data_as_data(self):
        prompt = build_planner_prompt("改成科技风", CHANNELS)
        self.assertIn("<channels_json>", prompt)
        self.assertIn('"channel_id":"voice"', prompt)
        self.assertIn("不要创建、删除或移动频道", prompt)


if __name__ == "__main__":
    unittest.main()
