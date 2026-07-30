import json
import unittest

from beautify import (
    Channel,
    PlanError,
    build_channel_inventory,
    build_planner_prompt,
    extract_explicit_channel_ids,
    instruction_requires_creation,
    instruction_requires_deletion,
    instruction_requires_full_replacement,
    instruction_requires_grouped_template,
    instruction_preserved_kinds,
    instruction_preserves_text_scope,
    parse_complete_plan,
    parse_rename_plan,
    parse_structure_plan,
    protected_scope_channel_ids,
    protected_text_scope_channel_ids,
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
        self.assertIn('"deletes"', prompt)
        self.assertIn("现有分组也可以直接改名、添加或调整 Emoji", prompt)

    def test_inventory_removes_deleted_category_parent_reference(self):
        inventory = json.loads(build_channel_inventory([
            Channel(id="text", name="娱乐聊天", type=1, parent_id="deleted-category"),
            Channel(id="cat", name="当前分组", type=0, is_category=True),
            Channel(id="voice", name="当前语音", type=2, parent_id="cat"),
        ]))
        records = {item["channel_id"]: item for item in inventory}
        self.assertEqual(records["text"]["parent_id"], "")
        self.assertEqual(records["voice"]["parent_id"], "cat")
        self.assertNotIn("deleted-category", json.dumps(inventory))

    def test_explicit_creation_instruction_requires_nonempty_creates(self):
        self.assertTrue(instruction_requires_creation("帮我新建几个游戏频道"))
        self.assertTrue(instruction_requires_creation("从零设计服务器"))
        self.assertFalse(instruction_requires_creation("只改名，不要新建频道"))
        with self.assertRaisesRegex(PlanError, "creates 为空"):
            parse_structure_plan(
                '{"renames":[],"creates":[]}',
                CHANNELS,
                require_creates=True,
            )

    def test_replacement_wording_requires_new_structure(self):
        self.assertTrue(
            instruction_requires_creation("替换掉所有语音频道，不替换文字频道")
        )
        self.assertFalse(instruction_requires_creation("不要替换，只改名"))

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

    def test_replacement_plan_protects_current_channel_and_orders_deletes(self):
        channels = CHANNELS + [Channel("current", "机器人操作台", 1)]
        payload = {
            "renames": [],
            "creates": [{"temp_id": "newcat", "name": "新分组", "kind": "category"}],
            "deletes": [
                {"channel_id": "cat"},
                {"channel_id": "text"},
                {"channel_id": "voice"},
            ],
        }
        self.assertTrue(instruction_requires_creation("生成一套赛博朋克新模板"))
        self.assertTrue(instruction_requires_deletion("套上新模板，之前频道都不要"))
        changes, creates, deletes = parse_complete_plan(
            json.dumps(payload, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            protected_channel_ids=("current",),
        )
        self.assertEqual(changes, [])
        self.assertEqual(creates[0].name, "新分组")
        self.assertEqual([item.channel_id for item in deletes], ["text", "voice", "cat"])

        payload["deletes"].append({"channel_id": "current"})
        _, _, filtered_deletes = parse_complete_plan(
            json.dumps(payload, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            protected_channel_ids=("current",),
        )
        self.assertNotIn("current", {item.channel_id for item in filtered_deletes})

    def test_full_replacement_requires_grouped_template_and_all_old_items(self):
        self.assertTrue(instruction_requires_full_replacement("全部替换成赛博朋克风"))
        self.assertTrue(instruction_requires_grouped_template("给我新建一个赛博朋克模板"))
        channels = CHANNELS + [Channel("current", "机器人操作台", 1, parent_id="cat")]
        incomplete = {
            "renames": [],
            "creates": [
                {"temp_id": "newcat", "name": "赛博都市", "kind": "category"},
                {
                    "temp_id": "newchat",
                    "name": "霓虹广场",
                    "kind": "text",
                    "parent_ref": "newcat",
                },
            ],
            "deletes": [{"channel_id": "text"}, {"channel_id": "voice"}],
        }
        _, creates, deletes = parse_complete_plan(
            json.dumps(incomplete, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            require_grouped_template=True,
            require_full_replacement=True,
            protected_channel_ids=("current",),
        )
        self.assertEqual([item.kind for item in creates], ["category", "text"])
        self.assertEqual([item.kind for item in deletes], ["text", "voice", "category"])

        incomplete["deletes"] = []
        _, _, automatic_deletes = parse_complete_plan(
            json.dumps(incomplete, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            require_grouped_template=True,
            require_full_replacement=True,
            protected_channel_ids=("current",),
        )
        self.assertEqual(
            {item.channel_id for item in automatic_deletes},
            {"cat", "text", "voice"},
        )

    def test_full_replacement_preserves_text_channels_and_their_categories(self):
        instruction = (
            "再来一套冬天感觉的频道美化，所有文字频道和包含文字频道的分组保持原样，"
            "其他全部替换"
        )
        self.assertTrue(instruction_preserves_text_scope(instruction))
        channels = [
            Channel("textcat", "文字·帖子", 0, is_category=True),
            Channel("text", "日常聊天", 1, parent_id="textcat"),
            Channel("voicecat", "旧语音区", 0, is_category=True),
            Channel("voice", "旧语音", 2, parent_id="voicecat"),
        ]
        protected = protected_text_scope_channel_ids(instruction, channels)
        self.assertEqual(protected, {"textcat", "text"})
        payload = {
            "renames": [
                {"channel_id": "textcat", "new_name": "❄️・文字·帖子"},
                {"channel_id": "text", "new_name": "❄️・日常聊天"},
            ],
            "creates": [
                {"temp_id": "winter", "name": "❄️・冬日语音", "kind": "category"},
                {
                    "temp_id": "snow_voice",
                    "name": "☃️・雪夜围炉",
                    "kind": "voice",
                    "parent_ref": "winter",
                },
            ],
            "deletes": [
                {"channel_id": "textcat"},
                {"channel_id": "text"},
                {"channel_id": "voicecat"},
                {"channel_id": "voice"},
            ],
        }
        changes, _, deletes = parse_complete_plan(
            json.dumps(payload, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            require_grouped_template=True,
            require_full_replacement=True,
            protected_channel_ids=protected,
        )
        self.assertEqual(changes, [])
        self.assertEqual(
            [item.channel_id for item in deletes],
            ["voice", "voicecat"],
        )

        payload["creates"].append({
            "temp_id": "winter_text",
            "name": "冬日文字",
            "kind": "text",
            "parent_ref": "winter",
        })
        _, filtered_creates, _ = parse_complete_plan(
            json.dumps(payload, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            require_grouped_template=True,
            require_full_replacement=True,
            protected_kinds=("text",),
            protected_channel_ids=protected,
        )
        self.assertNotIn("text", {item.kind for item in filtered_creates})

    def test_selective_protection_supports_voice_and_category_scopes(self):
        channels = [
            Channel("textcat", "文字区", 0, is_category=True),
            Channel("text", "聊天", 1, parent_id="textcat"),
            Channel("voicecat", "语音区", 0, is_category=True),
            Channel("voice", "开黑", 2, parent_id="voicecat"),
        ]
        self.assertEqual(
            instruction_preserved_kinds("语音频道保持原样，其他全部替换"),
            {"voice"},
        )
        self.assertEqual(
            protected_scope_channel_ids("不碰语音，其他全部替换", channels),
            {"voice", "voicecat"},
        )
        self.assertEqual(
            instruction_preserved_kinds("不动分组，只替换频道"),
            {"category"},
        )
        self.assertEqual(
            protected_scope_channel_ids("不动分组，只替换频道", channels),
            {"textcat", "voicecat"},
        )

    def test_preservation_is_clause_aware_and_explicit_replacement_wins(self):
        self.assertEqual(
            instruction_preserved_kinds("文字不动，语音全部替换"),
            {"text"},
        )
        self.assertEqual(
            instruction_preserved_kinds("替换掉所有语音频道，不替换文字"),
            {"text"},
        )
        self.assertEqual(
            instruction_preserved_kinds("不动文字和语音频道，其他全部替换"),
            {"text", "voice"},
        )
        self.assertEqual(
            instruction_preserved_kinds("除了语音频道以外，其他全部替换"),
            {"voice"},
        )
        self.assertNotIn(
            "text", instruction_preserved_kinds("文字频道也替换，不保留文字")
        )

    def test_voice_protection_discards_ai_mutations_and_rejects_new_voice(self):
        channels = [
            Channel("textcat", "文字区", 0, is_category=True),
            Channel("text", "聊天", 1, parent_id="textcat"),
            Channel("voicecat", "语音区", 0, is_category=True),
            Channel("voice", "开黑", 2, parent_id="voicecat"),
        ]
        instruction = "不动语音频道，其他全部替换"
        protected = protected_scope_channel_ids(instruction, channels)
        payload = {
            "renames": [{"channel_id": "voice", "new_name": "错误改名"}],
            "creates": [
                {"temp_id": "newcat", "name": "新文字区", "kind": "category"},
                {
                    "temp_id": "newtext", "name": "新聊天", "kind": "text",
                    "parent_ref": "newcat",
                },
            ],
            "deletes": [{"channel_id": item.id} for item in channels],
        }
        changes, creates, deletes = parse_complete_plan(
            json.dumps(payload, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            require_grouped_template=True,
            require_full_replacement=True,
            protected_kinds=("voice",),
            protected_channel_ids=protected,
        )
        self.assertEqual(changes, [])
        self.assertEqual([item.kind for item in creates], ["category", "text"])
        self.assertEqual([item.channel_id for item in deletes], ["text", "textcat"])

        payload["creates"].append({
            "temp_id": "newvoice", "name": "新语音", "kind": "voice",
            "parent_ref": "newcat",
        })
        _, filtered_creates, _ = parse_complete_plan(
            json.dumps(payload, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            require_grouped_template=True,
            require_full_replacement=True,
            protected_kinds=("voice",),
            protected_channel_ids=protected,
        )
        self.assertNotIn("voice", {item.kind for item in filtered_creates})

    def test_scoped_replacement_filters_protected_items_without_retry(self):
        channels = [
            Channel("textcat", "文字区", 0, is_category=True),
            Channel("text", "聊天", 1, parent_id="textcat"),
            Channel("voicecat", "语音区", 0, is_category=True),
            Channel("voice", "开黑", 2, parent_id="voicecat"),
        ]
        payload = {
            "renames": [{"channel_id": "text", "new_name": "错误文字改名"}],
            "creates": [
                {
                    "temp_id": "newvoice", "name": "新语音", "kind": "voice",
                    "parent_ref": "voicecat",
                },
                {
                    "temp_id": "wrongtext", "name": "错误新文字", "kind": "text",
                    "parent_ref": "textcat",
                },
            ],
            "deletes": [{"channel_id": "text"}, {"channel_id": "voice"}],
        }
        changes, creates, deletes = parse_complete_plan(
            json.dumps(payload, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            protected_kinds=("text",),
            protected_channel_ids=("textcat", "text"),
        )
        self.assertEqual(changes, [])
        self.assertEqual([item.temp_id for item in creates], ["newvoice"])
        self.assertEqual([item.channel_id for item in deletes], ["voice"])

    def test_protected_categories_can_host_replacement_template(self):
        channels = [
            Channel("cat", "保留分组", 0, is_category=True),
            Channel("old", "旧文字", 1, parent_id="cat"),
        ]
        payload = {
            "renames": [],
            "creates": [{
                "temp_id": "newtext", "name": "新文字", "kind": "text",
                "parent_ref": "cat",
            }],
            "deletes": [{"channel_id": "old"}],
        }
        _, creates, deletes = parse_complete_plan(
            json.dumps(payload, ensure_ascii=False),
            channels,
            require_creates=True,
            require_deletes=True,
            require_grouped_template=True,
            require_full_replacement=True,
            protected_kinds=("category",),
            protected_channel_ids=("cat",),
        )
        self.assertEqual(creates[0].parent_ref, "cat")
        self.assertEqual([item.channel_id for item in deletes], ["old"])

    def test_template_requires_category_without_explicit_group_wording(self):
        with self.assertRaisesRegex(PlanError, "没有创建任何分组"):
            parse_structure_plan(
                '{"renames":[],"creates":['
                '{"temp_id":"chat","name":"霓虹广场","kind":"text"}]}',
                CHANNELS,
                require_grouped_template=True,
            )

    def test_explicit_numeric_parent_is_allowed_for_kook_validation(self):
        instruction = "在分组 1305474374831940 下新建语音频道"
        self.assertEqual(extract_explicit_channel_ids(instruction), ["1305474374831940"])
        prompt = build_planner_prompt(instruction, CHANNELS)
        self.assertIn('<explicit_parent_refs_json>\n["1305474374831940"]', prompt)
        _, creates = parse_structure_plan(
            '{"renames":[],"creates":[{"temp_id":"room","name":"歌房二号",'
            '"kind":"voice","parent_ref":"1305474374831940"}]}',
            CHANNELS,
            require_creates=True,
            allowed_parent_refs=extract_explicit_channel_ids(instruction),
        )
        self.assertEqual(creates[0].parent_ref, "1305474374831940")

    def test_create_parent_must_be_category(self):
        with self.assertRaisesRegex(PlanError, "parent_ref 未引用有效分组"):
            parse_structure_plan(
                '{"renames":[],"creates":[{"temp_id":"new",'
                '"name":"新频道","kind":"text","parent_ref":"text"}]}',
                CHANNELS,
            )

    def test_new_categories_require_every_new_child_to_have_parent_ref(self):
        with self.assertRaisesRegex(PlanError, "没有 parent_ref"):
            parse_structure_plan(
                '{"renames":[],"creates":['
                '{"temp_id":"cat2","name":"新分组","kind":"category"},'
                '{"temp_id":"chat","name":"新聊天","kind":"text"}]}',
                CHANNELS,
            )

    def test_root_channel_is_allowed_when_plan_creates_no_category(self):
        _, creates = parse_structure_plan(
            '{"renames":[],"creates":['
            '{"temp_id":"chat","name":"根频道","kind":"text"}]}',
            CHANNELS,
        )
        self.assertEqual(creates[0].parent_ref, "")

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
