"""无需启动 AstrBot 的规则及 OneBot 调用测试。"""

import asyncio
import importlib.util
import json
import logging
import random
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_rules import (
    BEIJING,
    MODE_MISSING,
    MODE_PRESENT,
    choose_reminder_targets,
    invalid_cards,
    next_check,
    parse_check_time,
    parse_check_times,
    parse_excluded_qq_ids,
    parse_group_ids,
    parse_keywords,
    parse_match_mode,
    reminder_messages,
)
from daily_state import DailyState


class Star:
    def __init__(self, context):
        self.context = context


def load_plugin():
    modules = {
        name: types.ModuleType(name)
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star")
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("group-card-test")
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=lambda *args, **kwargs: lambda function: function
    )
    star = modules["astrbot.api.star"]
    star.Context = object
    star.Star = Star
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: "/tmp")
    star.register = lambda *args, **kwargs: lambda cls: cls
    spec = importlib.util.spec_from_file_location("group_card_plugin_under_test", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    modules["group_card_plugin_under_test"] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


plugin_module = load_plugin()


class FakeClient:
    def __init__(self, bot_id, members=None, *, query_error=False, send_error=False, fail_on_send=None):
        self.bot_id = bot_id
        self.members = members
        self.query_error = query_error
        self.send_error = send_error
        self.fail_on_send = fail_on_send
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_login_info":
            return {"user_id": self.bot_id}
        if action == "get_group_member_list":
            if self.query_error:
                raise RuntimeError("群不存在")
            return self.members
        if action == "send_group_msg":
            if self.send_error or (
                self.fail_on_send is not None and len(self.sent()) == self.fail_on_send
            ):
                raise RuntimeError("发送失败")
            return {"message_id": 1}
        raise AssertionError(action)

    def sent(self):
        return [params for action, params in self.calls if action == "send_group_msg"]


class FakePlatform:
    def __init__(self, client, name="aiocqhttp"):
        self.client = client
        self.name = name

    def meta(self):
        return types.SimpleNamespace(name=self.name)

    def get_client(self):
        return self.client


def make_member(user_id, card):
    return {"user_id": user_id, "card": card, "nickname": "QQ 昵称"}


class RuleTests(unittest.TestCase):
    def test_group_keywords_and_time_config(self):
        self.assertEqual(parse_group_ids("123, 456，123"), [123, 456])
        self.assertEqual(
            parse_group_ids(["123", "aiocqhttp:GroupMessage:456", "123", ""]),
            [123, 456],
        )
        with self.assertRaises(ValueError):
            parse_group_ids(["123", "bad"])
        self.assertEqual(parse_keywords([" 部门 ", "学生", "部门", ""]), ["部门", "学生"])
        self.assertEqual(parse_match_mode(MODE_MISSING), MODE_MISSING)
        self.assertEqual(parse_match_mode(MODE_PRESENT), MODE_PRESENT)
        self.assertEqual(parse_check_times(["18:30", "09:00", "18:30", ""]), ["09:00", "18:30"])
        self.assertEqual(parse_check_time("09:05"), (9, 5))
        self.assertEqual(
            next_check(datetime(2026, 9, 22, 9, 1, tzinfo=BEIJING), ["09:00", "18:30"]),
            datetime(2026, 9, 22, 18, 30, tzinfo=BEIJING),
        )
        self.assertEqual(
            next_check(datetime(2026, 9, 22, 19, 0, tzinfo=BEIJING), ["09:00", "18:30"]),
            datetime(2026, 9, 23, 9, 0, tzinfo=BEIJING),
        )
        with self.assertRaisesRegex(ValueError, "第 2 个"):
            parse_check_times(["09:00", "25:00"])
        self.assertEqual(parse_excluded_qq_ids([" 100 ", "200", "100", ""]), {100, 200})
        with self.assertRaisesRegex(ValueError, "第 2 个排除的 QQ 号"):
            parse_excluded_qq_ids(["100", "not-a-qq"])

    def test_positive_negative_contains_empty_card_and_qq_exclusion(self):
        keywords = ["部门", "学生"]
        members = [
            make_member(9, ""),
            make_member(3, "部门-张三"),
            make_member(4, "部门-李四-尾巴"),
            make_member(5, "前缀部门-王五"),
            make_member(6, ""),
            make_member(7, None),
            make_member(8, "学生-赵六"),
            make_member(11, "游客"),
        ]
        self.assertEqual(
            [member["user_id"] for member in invalid_cards(members, keywords, MODE_MISSING, {6})],
            [7, 9, 11],
        )
        self.assertEqual(
            [member["user_id"] for member in invalid_cards(members, keywords, MODE_PRESENT, {6})],
            [3, 4, 5, 8],
        )
        with self.assertRaises(ValueError):
            invalid_cards([{"user_id": 1}], keywords, MODE_MISSING, set())
        with self.assertRaises(ValueError):
            invalid_cards(members, [], MODE_MISSING, set())

    def test_keyword_metacharacters_are_literal_text(self):
        members = [make_member(1, "abc"), make_member(2, "abc.*")]
        self.assertEqual(
            [item["user_id"] for item in invalid_cards(members, [".*"], MODE_PRESENT, set())],
            [2],
        )

    def test_robot_flag_does_not_affect_either_match_mode(self):
        members = [
            {**make_member(1, ""), "is_robot": True},
            {**make_member(2, "部门"), "is_robot": True},
            {**make_member(3, ""), "is_robot": False},
            {**make_member(4, "部门"), "is_robot": False},
        ]
        self.assertEqual(
            [item["user_id"] for item in invalid_cards(members, ["部门"], MODE_MISSING, set())],
            [1, 3],
        )
        self.assertEqual(
            [item["user_id"] for item in invalid_cards(members, ["部门"], MODE_PRESENT, set())],
            [2, 4],
        )

    def test_schema_uses_individually_editable_lists(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("group_ids", "excluded_qq_ids", "keywords", "check_times"):
            self.assertEqual(schema[key]["type"], "list")
            self.assertEqual(schema[key]["item_type"], "string")
        self.assertEqual(schema["group_ids"]["default"], [])
        self.assertEqual(schema["excluded_qq_ids"]["default"], [])
        self.assertTrue(schema["excluded_bot_ids"]["invisible"])
        self.assertEqual(schema["keywords"]["default"], [])
        self.assertEqual(schema["check_times"]["default"], ["09:00"])
        self.assertEqual(schema["daily_mention_limit"]["default"], 0)
        self.assertEqual(schema["match_mode"]["options"], [MODE_MISSING, MODE_PRESENT])
        self.assertTrue(schema["check_time"]["invisible"])
        self.assertNotIn("card_patterns", schema)

    def test_45_people_become_three_real_at_messages(self):
        invalid = [make_member(user_id, "") for user_id in range(1, 46)]
        messages = reminder_messages(invalid, "请改名片")
        self.assertEqual(len(messages), 3)
        self.assertEqual(
            [sum(part["type"] == "at" for part in message) for message in messages],
            [20, 20, 5],
        )
        self.assertEqual(messages[0][1], {"type": "at", "data": {"qq": "1"}})
        self.assertIn("请改名片", messages[0][0]["data"]["text"])

    def test_selection_prefers_never_mentioned_then_decays_repeat_probability(self):
        candidates = [make_member(user_id, "") for user_id in (1, 2, 3, 4)]
        self.assertEqual(
            [item["user_id"] for item in choose_reminder_targets(candidates, 2, {1: 4, 2: 1})],
            [3, 4],
        )
        rng = random.Random(7)
        picks = {1: 0, 2: 0}
        for _ in range(2000):
            picked = choose_reminder_targets(candidates[:2], 1, {1: 1, 2: 4}, rng)
            picks[picked[0]["user_id"]] += 1
        self.assertGreater(picks[1], picks[2] * 3)

    def test_weighted_selection_is_unique_and_handles_large_history(self):
        candidates = [make_member(user_id, "") for user_id in range(1, 101)]
        counts = {user_id: 10_000 + user_id for user_id in range(1, 101)}
        selected = choose_reminder_targets(candidates, 80, counts, random.Random(7))
        self.assertEqual(len(selected), 80)
        self.assertEqual(len({item["user_id"] for item in selected}), 80)

    def test_daily_state_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily_state.json"
            state = DailyState(path)
            self.assertTrue(state.claim(123, "2026-09-22", "09:00", limit=3, member_ids=[11, 12]))
            state.record_sent_members(123, [11, 12])
            self.assertFalse(DailyState(path).claim(123, "2026-09-22", "09:00"))
            self.assertEqual(state.remaining(123, "2026-09-22", 3), 1)
            self.assertTrue(state.claim(123, "2026-09-22", "18:30", limit=3, member_ids=[11]))
            state.record_sent_members(123, [11])
            snapshot = state.member_mention_counts(123)
            snapshot[11] = 999
            self.assertEqual(state.member_mention_counts(123)[11], 2)
            self.assertEqual(state.remaining(123, "2026-09-22", 3), 0)
            self.assertEqual(DailyState(path).member_mention_counts(123), {11: 2, 12: 1})
            with self.assertRaises(ValueError):
                state.claim(123, "2026-09-22", "20:00", mentions=1, limit=3)
            self.assertEqual(
                json.loads(path.read_text()),
                {
                    "123@09:00": "2026-09-22",
                    "123@18:30": "2026-09-22",
                    "@count:123:2026-09-22": "3",
                    "@member:123:11": "2",
                    "@member:123:12": "1",
                },
            )

    def test_failed_state_write_does_not_advance_cached_history(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DailyState(Path(directory) / "daily_state.json")
            state.record_sent_members(123, [11])
            with patch.object(state, "_save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    state.record_sent_members(123, [11, 12])
            self.assertEqual(state.member_mention_counts(123), {11: 1})

    def test_old_state_blocks_repeat_on_upgrade_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily_state.json"
            path.write_text('{"123": "2026-09-22"}', encoding="utf-8")
            state = DailyState(path)
            self.assertEqual(state.remaining(123, "2026-09-22", 5), 0)
            self.assertFalse(state.claim(123, "2026-09-22", "09:00"))
            self.assertFalse(state.claim(123, "2026-09-22", "18:30"))
            self.assertTrue(state.claim(123, "2026-09-23", "09:00"))


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "daily_state.json"
        self.config = {
            "group_ids": ["123"],
            "keywords": ["部门"],
            "match_mode": MODE_MISSING,
            "check_times": ["09:00", "18:30"],
            "reminder_text": "请改名片",
        }
        self.day = datetime(2026, 9, 22, 9, 0, tzinfo=BEIJING)

    def make_plugin(self, *clients):
        platforms = [FakePlatform(client) for client in clients]
        context = types.SimpleNamespace(
            platform_manager=types.SimpleNamespace(get_insts=lambda: platforms)
        )
        plugin = plugin_module.GroupCardReminder(context, dict(self.config))
        plugin._state = DailyState(self.path)
        return plugin

    async def test_two_accounts_one_group_one_sender_and_daily_dedupe(self):
        members = [
            make_member(10, ""),
            make_member(20, "部门-张三"),
            make_member(100, ""),
            make_member(200, ""),
        ]
        first = FakeClient(100, members)
        second = FakeClient(200, members)
        plugin = self.make_plugin(second, first)  # 即使平台顺序相反，也先选 QQ 号较小的账号。
        plugin.config["excluded_qq_ids"] = ["100", "200"]
        await plugin._run_at("09:00", self.day)
        await plugin._run_at("09:00", self.day)
        self.assertEqual(len(first.sent()), 1)
        self.assertEqual(second.sent(), [])
        self.assertEqual(first.sent()[0]["group_id"], 123)
        self.assertEqual(
            [part["data"]["qq"] for part in first.sent()[0]["message"] if part["type"] == "at"],
            ["10"],
        )

    async def test_sender_is_included_unless_qq_is_excluded(self):
        first = FakeClient(100, [{**make_member(100, ""), "is_robot": True}])
        plugin = self.make_plugin(first)
        await plugin._run_at("09:00", self.day)
        self.assertEqual(
            [part["data"]["qq"] for part in first.sent()[0]["message"] if part["type"] == "at"],
            ["100"],
        )

    async def test_qq_exclusions_apply_before_mention_and_daily_limit(self):
        members = [
            make_member(1, ""),
            {**make_member(2, ""), "is_robot": True},
            make_member(3, ""),
            make_member(100, ""),
        ]
        first = FakeClient(100, members)
        plugin = self.make_plugin(first)
        plugin.config["excluded_qq_ids"] = ["3", "100"]
        plugin.config["daily_mention_limit"] = 2
        await plugin._run_at("09:00", self.day)
        self.assertEqual(
            [part["data"]["qq"] for part in first.sent()[0]["message"] if part["type"] == "at"],
            ["1", "2"],
        )
        self.assertEqual(DailyState(self.path).member_mention_counts(123), {1: 1, 2: 1})

    async def test_qq_exclusions_apply_to_manual_preview(self):
        members = [
            make_member(1, ""),
            {**make_member(2, ""), "is_robot": True},
            make_member(3, ""),
        ]
        first = FakeClient(100, members)
        plugin = self.make_plugin(first)
        plugin.config["excluded_qq_ids"] = ["3"]
        event = types.SimpleNamespace(
            is_admin=lambda: True,
            get_group_id=lambda: "123",
            plain_result=lambda result: result,
        )
        replies = [reply async for reply in plugin.check_cards(event)]
        self.assertIn("1：", replies[0])
        self.assertIn("2：", replies[0])
        self.assertNotIn("3：", replies[0])

    async def test_logins_are_queried_concurrently_and_sorted(self):
        started = 0
        both_started = asyncio.Event()

        class WaitingClient(FakeClient):
            async def call_action(self, action, **kwargs):
                nonlocal started
                if action == "get_login_info":
                    started += 1
                    if started == 2:
                        both_started.set()
                    await both_started.wait()
                return await super().call_action(action, **kwargs)

        plugin = self.make_plugin(WaitingClient(200), WaitingClient(100))
        accounts = await asyncio.wait_for(plugin._accounts(), timeout=0.5)
        self.assertEqual([account[0] for account in accounts], [100, 200])

    async def test_query_failure_falls_back_to_second_account(self):
        first = FakeClient(100, query_error=True)
        second = FakeClient(200, [make_member(10, "")])
        plugin = self.make_plugin(first, second)
        await plugin._run_at("09:00", self.day)
        self.assertEqual(first.sent(), [])
        self.assertEqual(len(second.sent()), 1)

    async def test_all_queries_fail_do_not_claim_or_send(self):
        first = FakeClient(100, query_error=True)
        plugin = self.make_plugin(first)
        await plugin._run_at("09:00", self.day)
        self.assertFalse(self.path.exists())
        self.assertEqual(first.sent(), [])

    async def test_blank_config_stays_silent(self):
        first = FakeClient(100, [make_member(10, "")])
        plugin = self.make_plugin(first)
        plugin.config["keywords"] = []
        await plugin._run_at("09:00", self.day)
        self.assertEqual(first.calls, [])

    async def test_each_group_list_item_is_checked_once(self):
        first = FakeClient(100, [make_member(10, "")])
        plugin = self.make_plugin(first)
        plugin.config["group_ids"] = ["123", "onebot:GroupMessage:456", "123"]
        await plugin._run_at("09:00", self.day)
        self.assertEqual([message["group_id"] for message in first.sent()], [123, 456])
        self.assertEqual(
            DailyState(self.path).days,
            {
                "123@09:00": "2026-09-22",
                "456@09:00": "2026-09-22",
                "@count:123:2026-09-22": "1",
                "@count:456:2026-09-22": "1",
                "@member:123:10": "1",
                "@member:456:10": "1",
            },
        )

    async def test_legacy_config_is_migrated_to_lists(self):
        first = FakeClient(100, [make_member(10, "")])
        plugin = self.make_plugin(first)
        class SaveableConfig(dict):
            saves = 0

            def save_config(self):
                self.saves += 1

        plugin.config = SaveableConfig({
            "group_ids": "123,456",
            "card_patterns": [r"部门-.+"],
            "keywords": [],
            "check_time": "18:30",
            "check_times": ["09:00"],
            "excluded_bot_ids": ["200", "100", "200"],
            "excluded_qq_ids": [],
        })
        await plugin._migrate_config()
        self.assertEqual(plugin.config["group_ids"], ["123", "456"])
        self.assertEqual(plugin.config["check_times"], ["18:30"])
        self.assertEqual(plugin.config["check_time"], "")
        self.assertEqual(plugin.config["keywords"], [])
        self.assertEqual(plugin.config["excluded_qq_ids"], ["100", "200"])
        self.assertEqual(plugin.config["excluded_bot_ids"], [])
        self.assertEqual(plugin.config.saves, 1)

    async def test_new_exclusion_list_overrides_old_one(self):
        plugin = self.make_plugin(FakeClient(100, [make_member(1, "")]))
        plugin.config["excluded_bot_ids"] = ["1"]
        plugin.config["excluded_qq_ids"] = ["2"]
        await plugin._migrate_config()
        self.assertEqual(plugin.config["excluded_bot_ids"], [])
        self.assertEqual(plugin._settings().excluded_qq_ids, {2})

    async def test_invalid_old_exclusion_list_prevents_scan(self):
        first = FakeClient(100, [make_member(1, "")])
        plugin = self.make_plugin(first)
        plugin.config["excluded_bot_ids"] = ["invalid"]
        plugin.config["excluded_qq_ids"] = []
        await plugin._migrate_config()
        await plugin._run_at("09:00", self.day)
        self.assertEqual(first.calls, [])
        self.assertFalse(self.path.exists())

    async def test_unlimited_scan_skips_history_lookup(self):
        first = FakeClient(100, [make_member(1, "")])
        plugin = self.make_plugin(first)
        prior_counts = plugin._state.member_mention_counts
        with patch.object(plugin._state, "member_mention_counts", wraps=prior_counts) as lookup:
            await plugin._run_at("09:00", self.day)
        # 成功发送后的落盘仍需读一次，筛选阶段不应额外读取。
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(len(first.sent()), 1)

    async def test_bad_time_prevents_sending(self):
        first = FakeClient(100, [make_member(10, "")])
        plugin = self.make_plugin(first)
        plugin.config["check_times"].append("25:00")
        await plugin._run_at("09:00", self.day)
        self.assertEqual(first.calls, [])
        self.assertFalse(self.path.exists())

    async def test_two_times_same_day_each_send_once(self):
        first = FakeClient(100, [make_member(10, "")])
        plugin = self.make_plugin(first)
        await plugin._run_at("09:00", self.day)
        evening = datetime(2026, 9, 22, 18, 30, tzinfo=BEIJING)
        await plugin._run_at("18:30", evening)
        await plugin._run_at("18:30", evening)
        self.assertEqual(len(first.sent()), 2)
        self.assertEqual(
            DailyState(self.path).days,
            {
                "123@09:00": "2026-09-22",
                "123@18:30": "2026-09-22",
                "@count:123:2026-09-22": "2",
                "@member:123:10": "2",
            },
        )

    async def test_daily_limit_truncates_and_stops_later_slots(self):
        first = FakeClient(100, [make_member(user_id, "") for user_id in range(1, 6)])
        plugin = self.make_plugin(first)
        plugin.config["daily_mention_limit"] = 3
        await plugin._run_at("09:00", self.day)
        evening = datetime(2026, 9, 22, 18, 30, tzinfo=BEIJING)
        await plugin._run_at("18:30", evening)
        at_ids = [
            part["data"]["qq"]
            for message in first.sent()
            for part in message["message"]
            if part["type"] == "at"
        ]
        self.assertEqual(at_ids, ["1", "2", "3"])
        self.assertEqual(DailyState(self.path).remaining(123, "2026-09-22", 3), 0)

    async def test_daily_limit_prefers_new_members_across_times(self):
        first = FakeClient(100, [make_member(1, ""), make_member(2, "")])
        plugin = self.make_plugin(first)
        plugin.config["daily_mention_limit"] = 3
        await plugin._run_at("09:00", self.day)
        first.members.append(make_member(3, ""))
        evening = datetime(2026, 9, 22, 18, 30, tzinfo=BEIJING)
        await plugin._run_at("18:30", evening)
        at_ids = [
            part["data"]["qq"]
            for message in first.sent()
            for part in message["message"]
            if part["type"] == "at"
        ]
        self.assertEqual(at_ids, ["1", "2", "3"])
        self.assertEqual(DailyState(self.path).mentioned_count(123, "2026-09-22"), 3)

    async def test_daily_limit_prefers_never_mentioned_across_days_and_reload(self):
        first = FakeClient(100, [make_member(user_id, "") for user_id in (1, 2, 3)])
        self.config["daily_mention_limit"] = 1
        for offset in range(3):
            plugin = self.make_plugin(first)
            await plugin._run_at(
                "09:00", datetime(2026, 9, 22 + offset, 9, 0, tzinfo=BEIJING)
            )
        at_ids = [
            part["data"]["qq"]
            for message in first.sent()
            for part in message["message"]
            if part["type"] == "at"
        ]
        self.assertEqual(at_ids, ["1", "2", "3"])
        self.assertEqual(DailyState(self.path).member_mention_counts(123), {1: 1, 2: 1, 3: 1})

    async def test_daily_limit_is_separate_per_group_and_resets_next_day(self):
        first = FakeClient(100, [make_member(1, ""), make_member(2, "")])
        plugin = self.make_plugin(first)
        plugin.config["group_ids"] = ["123", "456"]
        plugin.config["daily_mention_limit"] = 1
        await plugin._run_at("09:00", self.day)
        tomorrow = datetime(2026, 9, 23, 9, 0, tzinfo=BEIJING)
        await plugin._run_at("09:00", tomorrow)
        self.assertEqual([message["group_id"] for message in first.sent()], [123, 456, 123, 456])
        state = DailyState(self.path)
        self.assertEqual(state.mentioned_count(123, "2026-09-23"), 1)
        self.assertEqual(state.mentioned_count(456, "2026-09-23"), 1)
        self.assertNotIn("@count:123:2026-09-22", state.days)

    async def test_send_failure_is_not_retried_after_reload(self):
        first = FakeClient(100, [make_member(10, "")], send_error=True)
        plugin = self.make_plugin(first)
        await plugin._run_at("09:00", self.day)
        reloaded = self.make_plugin(first)
        await reloaded._run_at("09:00", self.day)
        self.assertEqual(len(first.sent()), 1)
        self.assertEqual(
            DailyState(self.path).days,
            {"123@09:00": "2026-09-22", "@count:123:2026-09-22": "1"},
        )

    async def test_send_failure_reserves_daily_limit(self):
        first = FakeClient(100, [make_member(10, "")], send_error=True)
        plugin = self.make_plugin(first)
        plugin.config["daily_mention_limit"] = 1
        await plugin._run_at("09:00", self.day)
        first.send_error = False
        evening = datetime(2026, 9, 22, 18, 30, tzinfo=BEIJING)
        await plugin._run_at("18:30", evening)
        self.assertEqual(len(first.sent()), 1)
        self.assertEqual(DailyState(self.path).remaining(123, "2026-09-22", 1), 0)
        self.assertEqual(DailyState(self.path).member_mention_counts(123), {})

    async def test_partial_send_records_only_successful_batch(self):
        first = FakeClient(
            100, [make_member(user_id, "") for user_id in range(1, 22)], fail_on_send=2
        )
        plugin = self.make_plugin(first)
        plugin.config["daily_mention_limit"] = 21
        with patch.object(plugin_module.asyncio, "sleep", new=AsyncMock()):
            await plugin._run_at("09:00", self.day)
        state = DailyState(self.path)
        self.assertEqual(state.mentioned_count(123, "2026-09-22"), 21)
        self.assertEqual(state.member_mention_counts(123), {user_id: 1 for user_id in range(1, 21)})

    async def test_old_slot_state_with_unknown_mentions_uses_no_limit_today(self):
        self.path.write_text('{"123@09:00": "2026-09-22"}', encoding="utf-8")
        first = FakeClient(100, [make_member(10, "")])
        plugin = self.make_plugin(first)
        plugin.config["daily_mention_limit"] = 5
        evening = datetime(2026, 9, 22, 18, 30, tzinfo=BEIJING)
        await plugin._run_at("18:30", evening)
        self.assertEqual(first.sent(), [])
        self.assertEqual(DailyState(self.path).mentioned_count(123, "2026-09-22"), None)

    async def test_manual_preview_has_no_at_or_daily_claim(self):
        first = FakeClient(100, [make_member(10, "")])
        plugin = self.make_plugin(first)
        event = types.SimpleNamespace(
            is_admin=lambda: True,
            get_group_id=lambda: "123",
            plain_result=lambda text: text,
        )
        replies = [reply async for reply in plugin.check_cards(event)]
        self.assertEqual(len(replies), 1)
        self.assertIn("10：", replies[0])
        self.assertEqual(first.sent(), [])
        self.assertFalse(self.path.exists())

    async def test_non_admin_cannot_preview(self):
        first = FakeClient(100, [make_member(10, "")])
        plugin = self.make_plugin(first)
        event = types.SimpleNamespace(
            is_admin=lambda: False,
            plain_result=lambda text: text,
        )
        replies = [reply async for reply in plugin.check_cards(event)]
        self.assertIn("只有 AstrBot 管理员", replies[0])
        self.assertEqual(first.calls, [])

    async def test_compliant_group_records_day_without_sending(self):
        first = FakeClient(100, [make_member(100, ""), make_member(10, "部门-张三")])
        plugin = self.make_plugin(first)
        plugin.config["excluded_qq_ids"] = ["100"]
        await plugin._run_at("09:00", self.day)
        self.assertEqual(first.sent(), [])
        self.assertEqual(
            DailyState(self.path).days,
            {"123@09:00": "2026-09-22", "@count:123:2026-09-22": "0"},
        )


if __name__ == "__main__":
    unittest.main()
