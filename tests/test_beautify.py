import json
import unittest

from beautify import (
    Channel,
    PlanError,
    build_planner_prompt,
    parse_rename_plan,
    parse_structure_plan,
)


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
        self.assertIn('"creates"', prompt)
        self.assertIn("不要删除频道", prompt)

    def test_parse_complete_structure_plan(self):
        payload = {
            "renames": [{"channel_id": "text", "new_name": "💬・旧频道"}],
            "creates": [
                {"temp_id": "community", "name": "『 NEW COMMUNITY 』", "kind": "category"},
                {
                    "temp_id": "lobby",
                    "name": "💬・新大厅",
                    "kind": "text",
                    "parent_ref": "community",
                },
                {
                    "temp_id": "voice_lobby",
                    "name": "🎧・语音大厅",
                    "kind": "voice",
                    "parent_ref": "community",
                    "limit_amount": 25,
                    "voice_quality": "3",
                },
            ],
        }
        renames, creates = parse_structure_plan(
            json.dumps(payload, ensure_ascii=False), CHANNELS
        )
        self.assertEqual(len(renames), 1)
        self.assertEqual([item.kind for item in creates], ["category", "text", "voice"])
        self.assertEqual(creates[2].limit_amount, 25)

    def test_create_parent_must_be_category(self):
        with self.assertRaisesRegex(PlanError, "parent_ref 未引用有效分组"):
            parse_structure_plan(
                '{"renames":[],"creates":[{"temp_id":"new",'
                '"name":"新频道","kind":"text","parent_ref":"text"}]}',
                CHANNELS,
            )

    def test_category_parent_and_invalid_voice_settings_are_rejected(self):
        with self.assertRaisesRegex(PlanError, "不能设置 parent_ref"):
            parse_structure_plan(
                '{"renames":[],"creates":[{"temp_id":"cat2",'
                '"name":"新分组","kind":"category","parent_ref":"cat"}]}',
                CHANNELS,
            )
        with self.assertRaisesRegex(PlanError, "limit_amount"):
            parse_structure_plan(
                '{"renames":[],"creates":[{"temp_id":"voice2",'
                '"name":"新语音","kind":"voice","limit_amount":100}]}',
                CHANNELS,
            )

    def test_total_operation_limit_includes_creates(self):
        with self.assertRaisesRegex(PlanError, "超过单次上限"):
            parse_structure_plan(
                '{"renames":[{"channel_id":"text","new_name":"改名"}],'
                '"creates":[{"temp_id":"cat2","name":"新分组","kind":"category"}]}',
                CHANNELS,
                max_changes=1,
            )


if __name__ == "__main__":
    unittest.main()
