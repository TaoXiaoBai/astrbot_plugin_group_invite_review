import asyncio
import copy
import time
import types
import unittest
from unittest.mock import AsyncMock

import test_invite_flow as fixtures
from group_relations import RELATIONS_KEY
from main import ActionResult, GroupInviteGuardPlugin


class Config(dict):
    def save_config(self):
        if getattr(self, "fail", False):
            raise OSError("disk unavailable")


class BlacklistSafetyTests(unittest.IsolatedAsyncioTestCase):
    make_plugin = fixtures.InviteFlowTests.make_plugin

    def setup_direct(self, *, inviter=False):
        p = self.make_plugin()
        p.config["mute_revenge"] = {"profile_mute_direct_blacklist": True,
                                     "mute_ban_mode": "astrbot_ban"}
        p._get_profile_plugin_instance = lambda: None
        config = Config(ban_list=[])
        p._get_ban_config = lambda: (config, "")
        bot = fixtures.ApiOnly()
        p._mute_recovery_bots = {"10000": bot}
        now = int(time.time() * 1000)
        p._kv["mute_event_jobs"] = {
            "old": {"payload": {"self_id": "10000", "group_id": "30000", "created_at_ms": now - 10000,
                                   "event_time": now // 1000 - 10},
                    "direct_targets": [{"qq": "20000", "attribution": "associated_inviter" if inviter else "operator",
                                        "state": "pending", "notice_done": False}], "profile_targets": []}
        }
        return p, config, bot

    def event(self, bot=None, **raw):
        return fixtures.FakeEvent({"self_id": 10000, **raw}, bot or fixtures.ApiOnly())

    async def test_manual_group_uses_current_scope_not_latest_rejected_history(self):
        p = self.make_plugin()
        p._kv["invite_records"] = {"30000": [{"inviter": "99999", "execution_state": "REJECTED", "time": 9999999999}]}
        fixtures.seed_relation(p, inviter="20000")
        fixtures.seed_relation(p, inviter="88888", self_id="10001")
        p._manual_blacklist = AsyncMock(return_value=ActionResult("done", persisted=True))
        bot = fixtures.ApiOnly()
        result = await p._manual_ban_text(self.event(bot), ["群", "30000"])
        p._manual_blacklist.assert_awaited_once_with("20000", bot, group_id="30000")
        self.assertIn("已退出群 30000", result)
        self.assertNotIn("dealt", p._kv["invite_records"]["30000"][0])

    async def test_manual_qq_only_leaves_current_in_relations_and_all_matching_groups(self):
        p = self.make_plugin()
        p._manual_blacklist = AsyncMock(return_value=ActionResult("done", persisted=True))
        for index in range(8):
            fixtures.seed_relation(p, group=str(30000 + index), inviter="20000")
        fixtures.seed_relation(p, group="40000", inviter="20000", membership="OUT")
        fixtures.seed_relation(p, group="40001", inviter="20000", self_id="10001")
        fixtures.seed_relation(p, group="40002", inviter="20000", platform="other")
        p._kv["invite_records"] = {"50000": [{"inviter": "20000"}]}
        bot = fixtures.ApiOnly()
        await p._manual_ban_text(self.event(bot), ["QQ", "20000"])
        left = [args["group_id"] for action, args in bot.calls if action == "set_group_leave"]
        self.assertEqual(left, list(range(30000, 30008)))

    async def test_unknown_group_and_numeric_ambiguity_refuse_guessing(self):
        p = self.make_plugin()
        p._kv["invite_records"] = {"30000": [{"inviter": "99999"}]}
        p._manual_blacklist = AsyncMock()
        bot = fixtures.ApiOnly()
        self.assertIn("不猜测", await p._manual_ban_text(self.event(bot), ["30000"]))
        self.assertIn("没有当前", await p._manual_ban_text(self.event(bot), ["群", "30000"]))
        fixtures.seed_relation(p, inviter="20000")
        fixtures.seed_relation(p, group="40000", inviter="30000")
        self.assertIn("歧义", await p._manual_ban_text(self.event(bot), ["30000"]))
        self.assertFalse(bot.calls)
        p._manual_blacklist.assert_not_awaited()

    async def test_explicit_qq_and_legacy_double_numbers_do_not_require_history(self):
        for args in (["QQ", "20000"], ["20000"], ["20000", "30000"]):
            p = self.make_plugin()
            p._get_profile_plugin_instance = lambda: None
            p._get_ban_config = lambda: (None, "qq_tools unavailable")
            bot = fixtures.ApiOnly()
            result = await p._manual_ban_text(self.event(bot), args)
            self.assertIn("已删除并拉黑好友", result)
            self.assertEqual(sum(action == "delete_friend" for action, _ in bot.calls), 1)
            self.assertEqual(sum(action == "set_group_leave" for action, _ in bot.calls), int(len(args) == 2 and args[0].isdigit()))

    async def test_kick_snapshots_inviter_before_out_and_never_uses_rejected_history(self):
        for known in (False, True):
            p = self.make_plugin()
            p.config["kick_revenge"] = {"revenge_mode": "delete_friend", "revenge_notify": True}
            p._kv["invite_records"] = {"30000": [{"inviter": "99999", "execution_state": "REJECTED"}]}
            if known:
                fixtures.seed_relation(p, inviter="20000")
            p._take_revenge = AsyncMock(return_value=ActionResult("done", protocol_deleted=True))
            event = self.event(post_type="notice", notice_type="group_decrease", sub_type="kick_me",
                               group_id=30000, user_id=10000, operator_id=40000, time=int(time.time()))
            await p.on_group_kick(event)
            self.assertEqual(p._take_revenge.await_count, int(known))
            if known:
                self.assertEqual(p._take_revenge.await_args.args[0], "20000")
                relation = await p.get_bot_group_relations(platform_id="test", self_id="10000", group_id="30000")
                self.assertEqual(relation[0]["membership"], "OUT")
            self.assertNotIn("dealt", p._kv["invite_records"]["30000"][0])

    async def test_rejected_and_undecided_admissions_do_not_bind_new_join(self):
        for state in ("REJECTED", "DECIDED", "REJECT_IN_FLIGHT", "UNEXPECTED_JOIN_KEPT"):
            p = self.make_plugin()
            p._kv["invite_records"] = {"30000": [{"inviter": "99999", "target_state": "VERIFIED",
                "self_id": "10000", "platform_id": "test", "time": int(time.time()), "execution_state": state}]}
            self.assertIsNone(await p._find_admission_for_join("30000", "10000", "test"))

    async def test_all_failed_actions_never_write_dealt(self):
        p = self.make_plugin()
        p._get_profile_plugin_instance = lambda: None
        p._get_ban_config = lambda: (None, "qq_tools unavailable")
        p._kv["invite_records"] = {"30000": [{"inviter": "20000"}]}
        bot = types.SimpleNamespace(delete_friend=AsyncMock(side_effect=OSError("failed")))
        result = await p._manual_ban_text(self.event(bot), ["QQ", "20000"])
        self.assertIn("失败", result)
        self.assertNotIn("dealt", p._kv["invite_records"]["30000"][0])
        self.assertFalse((await p._is_user_banned("20000"))[0])
        self.assertFalse(p._ban_result_ok("已拉黑"))

    async def test_protocol_only_success_is_structured_partial_success(self):
        p = self.make_plugin()
        p._get_profile_plugin_instance = lambda: None
        p._get_ban_config = lambda: (None, "qq_tools unavailable")
        result = await p._manual_blacklist("20000", fixtures.ApiOnly())
        self.assertIsInstance(result, str)
        self.assertTrue(result.protocol_deleted)
        self.assertFalse(result.persisted)
        self.assertTrue(result.success)

    async def test_disabled_basic_or_direct_pauses_pending_until_enabled(self):
        for section, key in (("basic", "enable"), ("mute_revenge", "profile_mute_direct_blacklist")):
            p, config, bot = self.setup_direct()
            p.config[section][key] = False
            await p._recover_mute_jobs()
            self.assertEqual(config["ban_list"], [])
            self.assertEqual(p._kv["mute_event_jobs"]["old"]["direct_targets"][0]["state"], "pending")
            p.config[section][key] = True
            await p._recover_mute_jobs()
            self.assertEqual(config["ban_list"][0]["user_id"], "20000")

    async def test_unban_without_qqtools_or_entry_clears_dealt_and_cancels_old_pending(self):
        for available in (False, True):
            p, config, bot = self.setup_direct()
            p._kv["invite_records"] = {"30000": [{"inviter": "20000", "dealt": True}]}
            p._kv["mute_event_jobs"]["done"] = copy.deepcopy(p._kv["mute_event_jobs"]["old"])
            p._kv["mute_event_jobs"]["done"]["direct_targets"][0]["state"] = "completed"
            if not available:
                p._get_ban_config = lambda: (None, "qq_tools unavailable")
            result = await p._unban_text("20000")
            self.assertIn("已取消 1", result)
            self.assertFalse((await p._is_user_banned("20000"))[0])
            self.assertEqual(p._kv["mute_event_jobs"]["old"]["direct_targets"][0]["state"], "cancelled")
            self.assertEqual(p._kv["mute_event_jobs"]["done"]["direct_targets"][0]["state"], "completed")
            await p._recover_mute_jobs()
            self.assertEqual(config["ban_list"], [])

    async def test_inflight_direct_finishes_before_unban_and_cannot_revive_after_restart(self):
        p, config, bot = self.setup_direct()
        entered, release = asyncio.Event(), asyncio.Event()
        original = p._apply_mute_ban
        async def blocked(qq, client):
            entered.set()
            await release.wait()
            return await original(qq, client)
        p._apply_mute_ban = blocked
        direct = asyncio.create_task(p._run_direct_job("old"))
        await asyncio.wait_for(entered.wait(), 1)
        unban = asyncio.create_task(p._unban_text("20000"))
        await asyncio.sleep(0)
        self.assertFalse(unban.done())
        release.set()
        await asyncio.wait_for(asyncio.gather(direct, unban), 2)
        self.assertEqual(config["ban_list"], [])
        restarted, config2, _ = self.setup_direct()
        restarted._kv = copy.deepcopy(p._kv)
        await restarted._recover_mute_jobs()
        self.assertEqual(config2["ban_list"], [])
        new = copy.deepcopy(restarted._kv["mute_event_jobs"]["old"])
        watermark = restarted._kv["direct_unban_watermarks"]["20000"]
        new["payload"].update(created_at_ms=watermark + 1000, event_time=watermark // 1000 + 1)
        new["direct_targets"][0]["state"] = "pending"
        restarted._kv["mute_event_jobs"]["new"] = new
        await restarted._run_direct_job("new")
        self.assertEqual(config2["ban_list"][0]["user_id"], "20000")

    async def test_unban_watermark_blocks_old_job_late_delivery(self):
        p, config, _ = self.setup_direct()
        old = copy.deepcopy(p._kv["mute_event_jobs"]["old"])
        await p._unban_text("20000")
        p._kv["mute_event_jobs"]["late"] = old
        await p._run_direct_job("late")
        self.assertEqual(config["ban_list"], [])
        self.assertEqual(p._kv["mute_event_jobs"]["late"]["direct_targets"][0]["state"], "cancelled")

    async def test_failed_notice_attempt_does_not_block_ban_or_repeat(self):
        p, config, bot = self.setup_direct(inviter=True)
        p.config["kick_revenge"] = {"ban_notice_message": "notice"}
        p._send_ban_notice = AsyncMock(return_value="通知失败：not a friend")
        config.fail = True
        await p._run_direct_job("old")
        self.assertTrue(p._kv["mute_event_jobs"]["old"]["direct_targets"][0]["notice_done"])
        config.fail = False
        await p._run_direct_job("old")
        self.assertEqual(config["ban_list"][0]["user_id"], "20000")
        p._send_ban_notice.assert_awaited_once()

    async def test_no_client_waits_for_notice_and_never_borrows_another_bot(self):
        p, config, _ = self.setup_direct(inviter=True)
        p.config["kick_revenge"] = {"ban_notice_message": "notice"}
        p._mute_recovery_bots = {"10001": fixtures.ApiOnly()}
        await p._run_direct_job("old")
        self.assertFalse(config["ban_list"])
        self.assertFalse(p._kv["mute_event_jobs"]["old"]["direct_targets"][0]["notice_done"])

    async def test_invite_kv_read_error_or_corruption_never_writes(self):
        for value in (None, [], {"30000": 123}, {"30000": ["invalid"]}, OSError("read failed")):
            p = self.make_plugin()
            original = p.get_kv_data
            async def load(key, default):
                if key == "invite_records":
                    if isinstance(value, Exception):
                        raise value
                    return value
                return await original(key, default)
            p.get_kv_data = load
            p.put_kv_data = AsyncMock()
            self.assertIn("失败", await p._record_invite_text("30000", "20000"))
            p.put_kv_data.assert_not_awaited()

    async def test_qqtools_ban_and_unban_save_failure_rolls_back_memory(self):
        p = self.make_plugin()
        original = [{"user_id": "20000", "duration": -1, "ban_time": 1}]
        config = Config(ban_list=copy.deepcopy(original))
        config.fail = True
        p._get_ban_config = lambda: (config, "")
        result = await p._ban_inviter("30000")
        self.assertFalse(result.persisted)
        self.assertEqual(config["ban_list"], original)
        self.assertIn("解封失败", await p._unban_text("20000"))
        self.assertEqual(config["ban_list"], original)

    async def test_expired_ban_does_not_intercept(self):
        p = self.make_plugin()
        config = Config(ban_list=[{"user_id": "20000", "duration": 10, "ban_time": 1},
                                  {"user_id": "21000", "duration": -1, "ban_time": 1}])
        p._get_ban_config = lambda: (config, "")
        self.assertIsNone(p._find_ban_entry("20000"))
        self.assertFalse((await p._is_user_banned("20000"))[0])
        self.assertTrue((await p._is_user_banned("21000"))[0])

    async def test_default_inquiry_json_and_custom_prompt_reach_model(self):
        p = self.make_plugin()
        p.config["decision"]["llm_provider_id"] = "fixture"
        variables = {key: "fixture" for key in ("sender_id", "text", "is_banned", "ban_reason", "ban_time",
                    "invite_summary", "persona_prompt", "tone", "show_reason", "max_length")}
        p._build_blacklist_inquiry_variables = AsyncMock(return_value=variables)
        p.context.llm_generate = AsyncMock(return_value=types.SimpleNamespace(completion_text='{"is_inquiry":true,"reply":"ok","reason":"fixture"}'))
        result = await p._ask_blacklist_inquiry("20000", "why")
        self.assertTrue(result["is_inquiry"])
        self.assertIn('"is_inquiry"', p.context.llm_generate.await_args.kwargs["prompt"])
        p.config["private_intent"] = {"blacklist_inquiry_user_prompt": '{"literal":1} {sender_id}'}
        await p._ask_blacklist_inquiry("20000", "why")
        self.assertEqual(p.context.llm_generate.await_args.kwargs["prompt"], '{"literal":1} fixture')

    async def test_switch_disabled_during_notice_prevents_following_action(self):
        p, config, _ = self.setup_direct(inviter=True)
        p.config["kick_revenge"] = {"ban_notice_message": "notice"}
        async def disable(*args):
            p.config["basic"]["enable"] = False
            return "attempted"
        p._send_ban_notice = AsyncMock(side_effect=disable)
        await p._run_direct_job("old")
        self.assertEqual(config["ban_list"], [])
        self.assertEqual(p._kv["mute_event_jobs"]["old"]["direct_targets"][0]["state"], "pending")
        p.config["basic"]["enable"] = True
        await p._run_direct_job("old")
        self.assertEqual(config["ban_list"][0]["user_id"], "20000")
        p._send_ban_notice.assert_awaited_once()

    async def test_relation_known_but_unknown_inviter_refuses_bare_number(self):
        p = self.make_plugin()
        fixtures.seed_relation(p, inviter="")
        p._manual_blacklist = AsyncMock()
        result = await p._manual_ban_text(self.event(), ["30000"])
        self.assertIn("不猜测", result)
        p._manual_blacklist.assert_not_awaited()

    async def test_invalid_expiries_are_inactive_and_context_omits_expired(self):
        p = self.make_plugin()
        for duration in (True, "nan", "inf", -2, 0, None):
            self.assertFalse(p._ban_entry_active({"user_id": "20000", "duration": duration, "ban_time": 1}))
        p._get_ban_config = lambda: (Config(ban_list=[{"user_id": "20000", "duration": 1, "ban_time": 1}]), "")
        self.assertEqual(p._read_ban_entries(), [])

    async def test_relation_read_failure_stops_manual_and_does_not_write(self):
        p = self.make_plugin()
        p._kv[RELATIONS_KEY] = "invalid"
        p._manual_blacklist = AsyncMock()
        p.put_kv_data = AsyncMock()
        self.assertIn("停止", await p._manual_ban_text(self.event(), ["QQ", "20000"]))
        p._manual_blacklist.assert_not_awaited()
        p.put_kv_data.assert_not_awaited()

    async def test_kick_all_failed_does_not_mark_dealt(self):
        p = self.make_plugin()
        fixtures.seed_relation(p, inviter="20000")
        p._kv["invite_records"] = {"30000": [{"inviter": "20000"}]}
        p.config["kick_revenge"] = {"revenge_mode": "delete_and_ban"}
        p._get_ban_config = lambda: (None, "qq_tools unavailable")
        bot = types.SimpleNamespace(delete_friend=AsyncMock(side_effect=OSError("failed")))
        await p.on_group_kick(self.event(bot, post_type="notice", notice_type="group_decrease",
                             sub_type="kick_me", group_id=30000, user_id=10000, operator_id=40000))
        self.assertNotIn("dealt", p._kv["invite_records"]["30000"][0])
        self.assertFalse((await p._is_user_banned("20000"))[0])

    async def test_cross_group_ignores_global_history_and_other_scopes(self):
        p = self.make_plugin()
        p.config["kick_revenge"] = {"cross_group_retaliation": True}
        bot = fixtures.ApiOnly()
        p._remember_relation_scope(bot, "test", "10000")
        fixtures.seed_relation(p, group="30001", inviter="20000")
        fixtures.seed_relation(p, group="30002", inviter="20000", self_id="10001")
        fixtures.seed_relation(p, group="30003", inviter="20000", membership="OUT")
        fixtures.seed_relation(p, group="30004", inviter="99999")
        history = {"30005": [{"inviter": "20000", "execution_state": "REJECTED"}]}
        await p._cross_group_leave("20000", "30000", history, bot)
        self.assertEqual([args["group_id"] for action, args in bot.calls if action == "set_group_leave"], [30001])
        bot.calls.clear()
        p._kv[RELATIONS_KEY] = "corrupt"
        self.assertIn("读取失败", await p._cross_group_leave("20000", "30000", history, bot))
        self.assertEqual(bot.calls, [])
