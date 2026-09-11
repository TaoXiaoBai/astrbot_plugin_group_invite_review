import asyncio
import copy
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


def _identity_decorator(*args, **kwargs):
    def decorate(obj):
        return obj
    return decorate


class _Filter:
    class CustomFilter:
        pass

    class EventMessageType:
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"

    custom_filter = staticmethod(_identity_decorator)
    event_message_type = staticmethod(_identity_decorator)
    on_llm_request = staticmethod(_identity_decorator)
    llm_tool = staticmethod(_identity_decorator)


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Star:
    def __init__(self, context=None):
        self.context = context


astrbot = types.ModuleType("astrbot")
astrbot.__path__ = []
api = types.ModuleType("astrbot.api")
event = types.ModuleType("astrbot.api.event")
components = types.ModuleType("astrbot.api.message_components")
star = types.ModuleType("astrbot.api.star")
api.logger = _Logger()
event.filter = _Filter
event.AstrMessageEvent = object
event.MessageChain = lambda chain: chain
components.Plain = lambda text: text
components.Image = lambda file: file
star.Context = object
star.Star = _Star
star.register = _identity_decorator
sys.modules.update({
    "astrbot": astrbot,
    "astrbot.api": api,
    "astrbot.api.event": event,
    "astrbot.api.message_components": components,
    "astrbot.api.star": star,
})

from main import GroupInviteGuardPlugin, _invite_status_label, _parse_json


class FakeBot:
    def __init__(self):
        self.calls = []
        self.fail_action = None
        self.fail_private = False
        self.fail_group_message = False

    async def set_group_add_request(self, **params):
        self.calls.append(("set_group_add_request", params))
        if self.fail_action == "set_group_add_request":
            raise RuntimeError("protocol failed")

    async def set_group_leave(self, **params):
        self.calls.append(("set_group_leave", params))
        if self.fail_action == "set_group_leave":
            raise RuntimeError("leave failed")

    async def send_private_msg(self, **params):
        self.calls.append(("send_private_msg", params))
        if self.fail_private:
            raise RuntimeError("private failed")

    async def send_group_msg(self, **params):
        self.calls.append(("send_group_msg", params))
        if self.fail_group_message:
            raise RuntimeError("message failed")


class ApiOnly:
    def __init__(self, login_id=10000):
        self.calls = []
        self.login_id = login_id

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": self.login_id}}
        return {"status": "ok", "retcode": 0, "data": {}}


class ApiWrapper:
    def __init__(self, login_id=10000):
        self.api = ApiOnly(login_id)


class FailedApprovalApi(ApiOnly):
    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": self.login_id}}
        if action == "set_group_add_request":
            return {
                "status": "failed", "retcode": 1404,
                "message": "approval failed", "wording": "request expired",
            }
        return {"status": "ok", "retcode": 0, "data": {}}


class TopLevelUnsupportedWrapper:
    def __init__(self):
        self.top_calls = 0
        self.api = ApiOnly()

    async def call_action(self, action, **params):
        self.top_calls += 1
        raise AttributeError("unsupported method")


class TopLevelBusinessErrorWrapper:
    def __init__(self):
        self.top_calls = 0
        self.api = ApiOnly()

    async def call_action(self, action, **params):
        self.top_calls += 1
        raise TimeoutError("network timeout")


class FakeEvent:
    def __init__(self, raw, bot):
        self.message_obj = types.SimpleNamespace(raw_message=raw)
        self.bot = bot
        self.stopped = False

    def get_platform_id(self):
        return "test"

    def stop_event(self):
        self.stopped = True


class InviteFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, decision="approve", mode="notify_only", membership=None):
        plugin = GroupInviteGuardPlugin.__new__(GroupInviteGuardPlugin)
        plugin.config = {
            "basic": {"enable": True},
            "decision": {
                "auto_approve": True,
                "auto_reject": True,
                "reply_inviter_on_decision": True,
            },
            "unexpected_join": {
                "mode": mode,
                "custom_leave_message": "审核未通过，先退出本群。",
            },
        }
        plugin._kv = {}
        plugin.get_calls = []
        plugin.put_calls = []
        plugin.fail_put_states = {}
        plugin.put_delay = 0

        async def get_kv(key, default):
            plugin.get_calls.append(key)
            return copy.deepcopy(plugin._kv.get(key, default))

        async def put_kv(key, value):
            plugin.put_calls.append(key)
            if plugin.put_delay:
                await asyncio.sleep(plugin.put_delay)
            if key == "invite_records":
                states = {
                    str(rec.get("execution_state") or "")
                    for recs in value.values()
                    for rec in recs
                }
                for state in states:
                    remaining = plugin.fail_put_states.get(state, 0)
                    if remaining:
                        plugin.fail_put_states[state] = remaining - 1
                        raise RuntimeError(f"simulated put failure: {state}")
            plugin._kv[key] = copy.deepcopy(value)

        plugin.get_kv_data = get_kv
        plugin.put_kv_data = put_kv
        plugin._find_onebot_client = lambda event: event.bot
        plugin._notify = AsyncMock()
        plugin._ask_llm = AsyncMock(return_value={
            "action": decision,
            "reason": "test reason",
            "reply": "test reply",
        })
        plugin._membership_state = AsyncMock(side_effect=membership or ["OUT", "OUT", "OUT"])
        return plugin

    @staticmethod
    def raw(**overrides):
        data = {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "invite",
            "self_id": 10000,
            "invited_id": 10000,
            "user_id": 20000,
            "group_id": 30000,
            "comment": "hello",
            "flag": "fixture-flag",
            "time": 123456,
        }
        data.update(overrides)
        return data

    @staticmethod
    def record(plugin):
        return next(iter(plugin._kv["invite_records"].values()))[0]

    def seed_inflight(self, plugin, state, decision):
        raw = self.raw()
        plugin._kv["invite_records"] = {
            "30000": [{
                "record_id": "seeded01",
                "request_key": plugin._make_request_key(raw, raw["flag"]),
                "inviter": "20000",
                "comment": "hello",
                "time": 123456,
                "decision": decision,
                "decision_reason": "seeded",
                "review_state": "DECIDED",
                "execution_state": state,
                "membership_before": "OUT",
                "action_attempted": True,
                "action_succeeded": False,
                "auto_executed": True,
                "target_state": "VERIFIED",
                "self_id": "10000",
            }]
        }
        return raw

    async def run_invite(self, plugin, bot=None, **raw):
        bot = bot or FakeBot()
        event = FakeEvent(self.raw(**raw), bot)
        await plugin.on_group_invite(event)
        return bot, event, self.record(plugin)

    async def test_normal_approve(self):
        plugin = self.make_plugin("approve")
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "APPROVED")
        self.assertTrue(rec["action_attempted"])
        self.assertTrue(rec["action_succeeded"])
        self.assertTrue(any(name == "set_group_add_request" and p["approve"] for name, p in bot.calls))
        self.assertNotIn("fixture-flag", repr(plugin._kv))

    async def test_normal_reject(self):
        plugin = self.make_plugin("reject")
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "REJECTED")
        self.assertTrue(any(name == "set_group_add_request" and not p["approve"] for name, p in bot.calls))

    async def test_invited_id_not_bot(self):
        plugin = self.make_plugin()
        bot, event, rec = await self.run_invite(plugin, invited_id=99999)
        self.assertEqual(rec["target_state"], "NOT_FOR_BOT")
        self.assertEqual(rec["execution_state"], "NOT_FOR_BOT")
        plugin._ask_llm.assert_not_awaited()
        self.assertFalse(event.stopped)
        self.assertEqual(bot.calls, [])

    async def test_already_in_group_approve_still_reviews(self):
        plugin = self.make_plugin("approve", membership=["IN", "IN"])
        bot, _, rec = await self.run_invite(plugin)
        plugin._ask_llm.assert_awaited_once()
        self.assertEqual(rec["execution_state"], "EXTERNAL_JOIN_APPROVED")
        self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.calls))

    async def test_in_group_reject_notify_only(self):
        plugin = self.make_plugin("reject", "notify_only", ["IN", "IN"])
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "UNEXPECTED_JOIN_NOTIFIED")
        self.assertFalse(any(name == "set_group_leave" for name, _ in bot.calls))

    async def test_in_group_reject_leave(self):
        plugin = self.make_plugin("reject", "leave", ["IN", "IN", "OUT"])
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "UNEXPECTED_JOIN_LEFT")
        self.assertTrue(any(name == "set_group_leave" for name, _ in bot.calls))

    async def test_in_group_reject_message_then_leave(self):
        plugin = self.make_plugin("reject", "message_then_leave", ["IN", "IN", "OUT"])
        bot, _, rec = await self.run_invite(plugin)
        names = [name for name, _ in bot.calls]
        self.assertLess(names.index("send_group_msg"), names.index("set_group_leave"))
        self.assertEqual(rec["execution_state"], "UNEXPECTED_JOIN_LEFT")

    async def test_group_message_failure_does_not_block_leave(self):
        plugin = self.make_plugin("reject", "message_then_leave", ["IN", "IN", "OUT"])
        bot = FakeBot()
        bot.fail_group_message = True
        bot, _, rec = await self.run_invite(plugin, bot)
        self.assertTrue(any(name == "set_group_leave" for name, _ in bot.calls))
        self.assertIn("群消息发送失败", rec["protocol_error"])

    async def test_join_during_review(self):
        plugin = self.make_plugin("approve", membership=["OUT", "IN"])
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "EXTERNAL_JOIN_APPROVED")
        self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.calls))

    async def test_unknown_membership_is_not_out(self):
        plugin = self.make_plugin("approve", membership=["UNKNOWN", "UNKNOWN"])
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "MEMBERSHIP_UNKNOWN")
        self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.calls))

    async def test_missing_flag_is_recorded_and_reviewed(self):
        plugin = self.make_plugin("reject", membership=["OUT", "OUT"])
        bot, _, rec = await self.run_invite(plugin, flag="")
        plugin._ask_llm.assert_awaited_once()
        self.assertEqual(rec["execution_state"], "MISSING_FLAG")
        self.assertIn("缺少 flag", rec["protocol_error"])
        self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.calls))

    async def test_unknown_inviter_is_recorded(self):
        plugin = self.make_plugin("approve")
        _, _, rec = await self.run_invite(plugin, user_id=None)
        self.assertEqual(rec["inviter"], "")
        self.assertIn("未识别邀请人", rec["reply_status"])

    async def test_duplicate_terminal_request(self):
        plugin = self.make_plugin("approve", membership=["OUT", "OUT", "OUT"])
        bot = FakeBot()
        event = FakeEvent(self.raw(), bot)
        await plugin.on_group_invite(event)
        await plugin.on_group_invite(event)
        self.assertEqual(plugin._ask_llm.await_count, 1)
        self.assertEqual(len(plugin._kv["invite_records"]["30000"]), 1)

    async def test_action_failure(self):
        plugin = self.make_plugin("approve", membership=["OUT", "OUT", "OUT"])
        bot = FakeBot()
        bot.fail_action = "set_group_add_request"
        _, _, rec = await self.run_invite(plugin, bot)
        self.assertEqual(rec["execution_state"], "ACTION_FAILED")
        self.assertTrue(rec["action_attempted"])
        self.assertFalse(rec["action_succeeded"])
        self.assertIn("protocol failed", rec["protocol_error"])

    async def test_private_reply_failure_does_not_change_action_success(self):
        plugin = self.make_plugin("approve")
        bot = FakeBot()
        bot.fail_private = True
        _, _, rec = await self.run_invite(plugin, bot)
        self.assertTrue(rec["action_succeeded"])
        self.assertIn("发送失败", rec["reply_status"])

    async def test_disabled_only_records(self):
        plugin = self.make_plugin("approve")
        plugin.config["basic"]["enable"] = False
        bot, event, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "DISABLED_RECORDED")
        self.assertFalse(event.stopped)
        self.assertEqual(bot.calls, [])
        plugin._ask_llm.assert_not_awaited()

    async def test_final_write_failure_replay_does_not_repeat_action(self):
        plugin = self.make_plugin(
            "approve", membership=["OUT", "OUT", "OUT", "IN"]
        )
        plugin.fail_put_states["APPROVED"] = 1
        bot = FakeBot()
        event = FakeEvent(self.raw(), bot)
        await plugin.on_group_invite(event)
        self.assertEqual(self.record(plugin)["execution_state"], "APPROVE_IN_FLIGHT")
        await plugin.on_group_invite(event)
        action_calls = [name for name, _ in bot.calls if name == "set_group_add_request"]
        self.assertEqual(len(action_calls), 1)
        self.assertEqual(self.record(plugin)["execution_state"], "APPROVED_RECONCILED")

    async def test_reject_inflight_replay_does_not_repeat_request_action(self):
        plugin = self.make_plugin("reject", membership=["OUT"])
        raw = self.seed_inflight(plugin, "REJECT_IN_FLIGHT", "reject")
        bot = FakeBot()
        await plugin.on_group_invite(FakeEvent(raw, bot))
        self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.calls))
        self.assertEqual(self.record(plugin)["execution_state"], "ACTION_OUTCOME_UNKNOWN")

    async def test_leave_inflight_replay_does_not_repeat_leave_action(self):
        plugin = self.make_plugin("reject", "leave", membership=["OUT"])
        raw = self.seed_inflight(plugin, "LEAVE_IN_FLIGHT", "reject")
        bot = FakeBot()
        await plugin.on_group_invite(FakeEvent(raw, bot))
        self.assertFalse(any(name == "set_group_leave" for name, _ in bot.calls))
        self.assertEqual(self.record(plugin)["execution_state"], "UNEXPECTED_JOIN_LEFT")

    async def test_pre_action_write_failure_prevents_action(self):
        plugin = self.make_plugin("approve", membership=["OUT", "OUT"])
        plugin.fail_put_states["APPROVE_IN_FLIGHT"] = 1
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "PRE_ACTION_PERSIST_FAILED")
        self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.calls))

    async def test_leave_returning_without_error_but_still_in_is_unconfirmed(self):
        plugin = self.make_plugin("reject", "leave", ["IN", "IN", "IN"])
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "LEAVE_UNCONFIRMED")
        self.assertFalse(rec["action_succeeded"])
        self.assertFalse(any(name == "send_private_msg" for name, _ in bot.calls))

    async def test_leave_returning_without_error_but_unknown_is_unknown(self):
        plugin = self.make_plugin("reject", "leave", ["IN", "IN", "UNKNOWN"])
        _, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "ACTION_OUTCOME_UNKNOWN")
        self.assertFalse(rec["action_succeeded"])

    async def test_self_id_missing_is_verified_from_api_login(self):
        plugin = self.make_plugin("approve")
        bot = ApiWrapper(10000)
        _, _, rec = await self.run_invite(plugin, bot, self_id=None)
        self.assertEqual(rec["target_state"], "VERIFIED")
        self.assertEqual(rec["self_id"], "10000")
        self.assertTrue(any(name == "set_group_add_request" for name, _ in bot.api.calls))

    async def test_self_id_missing_and_login_unavailable_blocks_action(self):
        plugin = self.make_plugin("approve", membership=["OUT", "OUT"])
        bot, _, rec = await self.run_invite(plugin, self_id=None, invited_id=None)
        self.assertEqual(rec["target_state"], "UNVERIFIED")
        self.assertEqual(rec["execution_state"], "TARGET_UNVERIFIED")
        self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.calls))

    async def test_raw_self_id_mismatch_with_login_blocks_review_and_action(self):
        plugin = self.make_plugin("approve")
        bot = ApiWrapper(99999)
        _, _, rec = await self.run_invite(plugin, bot)
        self.assertEqual(rec["execution_state"], "NOT_FOR_BOT")
        plugin._ask_llm.assert_not_awaited()
        self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.api.calls))

    async def test_missing_invited_id_with_self_id_remains_compatible(self):
        plugin = self.make_plugin("approve")
        _, _, rec = await self.run_invite(plugin, invited_id=None)
        self.assertEqual(rec["target_state"], "VERIFIED")
        self.assertEqual(rec["execution_state"], "APPROVED")

    async def test_api_call_action_wrapper_is_discovered_and_unwrapped(self):
        plugin = GroupInviteGuardPlugin.__new__(GroupInviteGuardPlugin)
        bot = ApiWrapper(54321)
        event = FakeEvent(self.raw(), bot)
        found = plugin._find_onebot_client(event)
        self.assertIs(found, bot)
        result = await plugin._call_action(found, "get_login_info")
        self.assertEqual(result, {"user_id": 54321})

    async def test_failed_wrapper_response_raises_and_approval_is_not_success(self):
        plugin = self.make_plugin("approve")
        bot = ApiWrapper()
        bot.api = FailedApprovalApi()
        _, _, rec = await self.run_invite(plugin, bot)
        self.assertEqual(rec["execution_state"], "ACTION_FAILED")
        self.assertFalse(rec["action_succeeded"])
        self.assertIn("retcode=1404", rec["protocol_error"])
        self.assertIn("approval failed", rec["protocol_error"])

    async def test_top_level_unsupported_falls_back_to_api(self):
        plugin = GroupInviteGuardPlugin.__new__(GroupInviteGuardPlugin)
        bot = TopLevelUnsupportedWrapper()
        result = await plugin._call_action(bot, "get_login_info")
        self.assertEqual(result, {"user_id": 10000})
        self.assertEqual(bot.top_calls, 1)
        self.assertEqual(len(bot.api.calls), 1)

    async def test_top_level_business_error_does_not_fallback(self):
        plugin = GroupInviteGuardPlugin.__new__(GroupInviteGuardPlugin)
        bot = TopLevelBusinessErrorWrapper()
        with self.assertRaises(TimeoutError):
            await plugin._call_action(bot, "set_group_add_request", approve=True)
        self.assertEqual(bot.top_calls, 1)
        self.assertEqual(bot.api.calls, [])

    async def test_plain_dict_with_data_is_not_unwrapped(self):
        plugin = GroupInviteGuardPlugin.__new__(GroupInviteGuardPlugin)
        plain = {"data": {"business": True}, "name": "plain"}
        self.assertIs(plugin._unwrap_onebot_response(plain), plain)

    async def test_same_request_concurrently_only_runs_once(self):
        plugin = self.make_plugin("approve", membership=["OUT", "OUT", "OUT"])
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_decision(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"action": "approve", "reason": "ok", "reply": "ok"}

        plugin._ask_llm = AsyncMock(side_effect=delayed_decision)
        bot = FakeBot()
        event1 = FakeEvent(self.raw(), bot)
        event2 = FakeEvent(self.raw(), bot)
        first = asyncio.create_task(plugin.on_group_invite(event1))
        await entered.wait()
        second = asyncio.create_task(plugin.on_group_invite(event2))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(plugin._ask_llm.await_count, 1)
        self.assertEqual(
            len([name for name, _ in bot.calls if name == "set_group_add_request"]), 1
        )

    async def test_different_requests_concurrently_keep_both_records(self):
        plugin = self.make_plugin(
            "approve", membership=["OUT", "OUT", "OUT", "OUT", "OUT", "OUT"]
        )
        plugin.put_delay = 0.005
        bot = FakeBot()
        first = FakeEvent(self.raw(flag="fixture-a", time=1), bot)
        second = FakeEvent(self.raw(flag="fixture-b", time=2), bot)
        await asyncio.gather(
            plugin.on_group_invite(first), plugin.on_group_invite(second)
        )
        self.assertEqual(len(plugin._kv["invite_records"]["30000"]), 2)

    async def test_invalid_unexpected_join_mode_falls_back_to_notify_only(self):
        plugin = self.make_plugin("reject", "invalid", ["IN", "IN"])
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "UNEXPECTED_JOIN_NOTIFIED")
        self.assertFalse(any(name == "set_group_leave" for name, _ in bot.calls))

    async def test_old_config_defaults_to_notify_only(self):
        plugin = self.make_plugin("reject", membership=["IN", "IN"])
        plugin.config.pop("unexpected_join")
        bot, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["execution_state"], "UNEXPECTED_JOIN_NOTIFIED")
        self.assertFalse(any(name == "set_group_leave" for name, _ in bot.calls))

    async def test_old_record_text_display_is_compatible(self):
        plugin = self.make_plugin()
        plugin._kv["invite_records"] = {"30000": "20000"}
        text = await plugin._list_invite_records_text()
        self.assertIn("旧记录", text)
        self.assertIn("20000", text)

    def test_status_label_does_not_misread_negative_agreement(self):
        self.assertEqual(_invite_status_label("不同意", False)[1], "rejected")
        self.assertEqual(_invite_status_label("未同意", False)[1], "rejected")

    def test_disabled_init_does_not_migrate_external_ban_list(self):
        config = {"enable": False}
        with patch.object(
            GroupInviteGuardPlugin, "_migrate_ban_list_sync"
        ) as migrate_ban:
            GroupInviteGuardPlugin(object(), config)
        self.assertFalse(config["basic"]["enable"])
        migrate_ban.assert_not_called()

    async def test_structured_profile_is_used_and_current_request_excluded(self):
        calls = []

        class Profile:
            async def get_decision_profile(self, qq, event=None, exclude_request_key=""):
                calls.append((qq, exclude_request_key))
                return {
                    "schema_version": 1,
                    "provider": "astrbot_plugin_user_profile",
                    "captured_at": 123,
                    "score": 72,
                    "level": "高",
                    "tags": [
                        {"tag": "scam_suspect", "confidence": 0.9, "source": "llm", "evidence": "测试证据"},
                        {"tag": "invite_rejected", "confidence": 0.9, "source": "guard", "evidence": "重复前科"},
                    ],
                    "activity": {"group_messages": 8, "private_messages": 2, "active_groups": 2},
                    "social_origin": {
                        "friend_add_time": 100,
                        "friend_request_comment": "来自测试",
                        "join_sources": [{"gid": "30000", "sub_type": "invite", "operator": "21000", "time": 110}],
                    },
                }

        plugin = self.make_plugin()
        plugin.context = types.SimpleNamespace(
            get_registered_star=lambda name: types.SimpleNamespace(star_cls=Profile())
        )
        plugin._build_member_section = AsyncMock(return_value="成员区块")
        plugin._build_impression_section = AsyncMock(return_value=("印象区块", 3))
        plugin._build_profile_section = AsyncMock(return_value="守卫前科区块")
        plugin._detect_alt_account = AsyncMock(return_value="")
        text, warning, snapshot = await plugin._build_invite_context(
            "20000", "30000", None, request_key="current-key"
        )
        self.assertEqual(calls, [("20000", "current-key")])
        self.assertEqual(snapshot["score"], 72)
        self.assertIn("scam_suspect", text)
        self.assertNotIn("invite_rejected", text)
        self.assertIn("守卫前科区块", text)
        self.assertIn("好友申请验证语", text)
        self.assertEqual(warning, "")

    async def test_old_profile_api_gracefully_falls_back(self):
        class OldProfile:
            async def get_profile_text(self, qq, event=None):
                return "旧版完整画像"

        plugin = self.make_plugin()
        plugin.context = types.SimpleNamespace(
            get_registered_star=lambda name: types.SimpleNamespace(star_cls=OldProfile())
        )
        section, snapshot = await plugin._fetch_external_decision_profile("20000")
        self.assertEqual(section, "旧版完整画像")
        self.assertEqual(snapshot["provider"], "astrbot_plugin_user_profile_legacy")

    async def test_profile_snapshot_is_persisted_on_decision_record(self):
        plugin = self.make_plugin("approve")
        plugin._ask_llm = AsyncMock(return_value={
            "action": "approve",
            "reason": "profile ok",
            "reply": "ok",
            "_profile_snapshot": {
                "schema_version": 1,
                "provider": "astrbot_plugin_user_profile",
                "captured_at": 123,
                "score": 20,
                "level": "低",
                "tags": [],
                "activity": {},
                "social_origin": {},
            },
        })
        _, _, rec = await self.run_invite(plugin)
        self.assertEqual(rec["profile_snapshot"]["score"], 20)
        self.assertEqual(rec["decision"], "approve")

    async def test_record_display_distinguishes_decision_and_result(self):
        plugin = self.make_plugin()
        plugin._kv["invite_records"] = {
            "30000": [{
                "inviter": "20000",
                "time": 123456,
                "comment": "hello",
                "decision": "reject",
                "decision_reason": "风险偏高",
                "action": "仅通知管理员（未自动处理）",
                "execution_state": "NO_ACTION",
                "membership_before": "OUT",
                "membership_after": "OUT",
                "profile_snapshot": {
                    "score": 75,
                    "level": "高",
                    "tags": [{"tag": "scam_suspect"}],
                    "social_origin": {},
                },
            }]
        }
        text = await plugin._list_invite_records_text()
        self.assertIn("LLM 建议拒绝", text)
        self.assertIn("实际", text)
        self.assertIn("画像：75/100", text)
        self.assertIn("理由：风险偏高", text)

    async def test_first_invite_empty_profile_does_not_fallback_and_reinclude_current(self):
        class Profile:
            def __init__(self):
                self.decision_calls = []
                self.score_calls = 0

            async def get_decision_profile(self, qq, event=None, exclude_request_key=""):
                self.decision_calls.append(exclude_request_key)
                return {}

            async def get_profile_tags_with_score(self, qq, event=None):
                self.score_calls += 1
                return {"score": 55, "level": "中", "tags": [{"tag": "inviter"}]}

        profile = Profile()
        plugin = self.make_plugin()
        plugin.context = types.SimpleNamespace(
            get_registered_star=lambda name: types.SimpleNamespace(star_cls=profile)
        )
        section, snapshot = await plugin._fetch_external_decision_profile(
            "20000", exclude_request_key="current-key"
        )
        self.assertEqual(profile.decision_calls, ["current-key"])
        self.assertEqual(profile.score_calls, 0)
        self.assertEqual(section, "")
        self.assertEqual(snapshot, {})

    async def test_real_ask_llm_carries_profile_snapshot(self):
        plugin = self.make_plugin()
        plugin._default_provider_id = lambda: "provider"
        plugin._resolve_persona_prompt = AsyncMock(return_value="")
        snapshot = {"provider": "astrbot_plugin_user_profile", "score": 18}
        plugin._build_invite_context = AsyncMock(
            return_value=("结构化画像", "", snapshot)
        )
        plugin.context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text='{"action":"approve","reason":"ok","reply":"hi"}'
                )
            )
        )
        decision = await GroupInviteGuardPlugin._ask_llm(
            plugin, "20000", "30000", "hello", request_key="current-key"
        )
        self.assertEqual(decision["_profile_snapshot"], snapshot)
        prompt = plugin.context.llm_generate.await_args.kwargs["prompt"]
        self.assertEqual(prompt.count("结构化画像"), 1)

    async def test_profile_snapshot_sanitizer_handles_bad_values(self):
        snapshot = GroupInviteGuardPlugin._sanitize_profile_snapshot({
            "provider": "test",
            "schema_version": "bad",
            "captured_at": "bad",
            "score": "bad",
            "social_origin": {
                "friend_add_time": "bad",
                "friend_request_time": [],
                "join_sources": [{"gid": "1", "time": "bad"}],
            },
        })
        self.assertEqual(snapshot["score"], 0)
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["social_origin"]["join_sources"][0]["time"], 0)
        self.assertEqual(
            GroupInviteGuardPlugin._sanitize_profile_snapshot({"unexpected": "value"}),
            {},
        )
        malformed = GroupInviteGuardPlugin._sanitize_profile_snapshot({
            "provider": "test", "tags": 1,
            "social_origin": {"join_sources": 1},
        })
        self.assertEqual(malformed["tags"], [])
        self.assertEqual(malformed["social_origin"]["join_sources"], [])
        detail = GroupInviteGuardPlugin._invite_record_detail({
            "profile_snapshot": {"tags": 1, "social_origin": {"join_sources": 1}}
        })
        self.assertIsInstance(detail["detail"], str)

    async def test_mute_count_uses_unique_invited_groups(self):
        plugin = self.make_plugin()
        plugin._kv["invite_records"] = {
            "30000": [
                {"inviter": "20000", "time": 1},
                {"inviter": "20000", "time": 2},
            ],
            "30001": [{"inviter": "20000", "time": 3}],
        }
        plugin._kv["mute_records"] = {"30000": 2, "30001": 1}
        data = await plugin._collect_profile_data("20000")
        self.assertEqual(data["mute_total"], 3)

    async def test_detail_switch_hides_llm_and_profile_detail(self):
        plugin = self.make_plugin()
        plugin.config["display"] = {
            "invite_records_hide_dealt": False,
            "invite_records_show_decision_detail": False,
        }
        plugin._kv["invite_records"] = {
            "30000": [{
                "inviter": "20000",
                "time": 1,
                "decision": "reject",
                "decision_reason": "secret reason",
                "action": "仅通知管理员",
                "execution_state": "NO_ACTION",
                "profile_snapshot": {"score": 99, "tags": [{"tag": "secret_tag"}]},
            }]
        }
        text = await plugin._list_invite_records_text()
        self.assertNotIn("LLM", text)
        self.assertNotIn("secret reason", text)
        self.assertNotIn("secret_tag", text)
        plugin.html_render = AsyncMock(return_value="record.png")
        await plugin._render_invite_records_image(FakeBot())
        items = plugin.html_render.await_args.args[1]["items"]
        self.assertEqual(items[0]["decision"], "")
        self.assertEqual(items[0]["reason"], "")
        self.assertEqual(items[0]["risk_text"], "")
        self.assertEqual(items[0]["tags"], [])
        self.assertEqual(items[0]["relation"], "")
        self.assertEqual(items[0]["no_snapshot"], "")

    def test_invite_image_template_uses_cards_not_wide_table(self):
        from main import _INVITE_RECORDS_TEMPLATE
        self.assertIn('class="records"', _INVITE_RECORDS_TEMPLATE)
        self.assertIn('class="card', _INVITE_RECORDS_TEMPLATE)
        self.assertNotIn("<table", _INVITE_RECORDS_TEMPLATE)
        self.assertIn("邀请附言", _INVITE_RECORDS_TEMPLATE)
        self.assertIn("LLM 判断理由", _INVITE_RECORDS_TEMPLATE)
        self.assertIn("决策时用户画像", _INVITE_RECORDS_TEMPLATE)
        self.assertIn("社交来源 / 关系", _INVITE_RECORDS_TEMPLATE)

    async def test_card_image_data_is_split_into_sections(self):
        plugin = self.make_plugin()
        plugin.config["display"] = {
            "invite_records_show_profile": False,
            "invite_records_show_group_profile": False,
            "invite_records_hide_dealt": False,
            "invite_records_show_decision_detail": True,
        }
        plugin._kv["invite_records"] = {
            "30000": [{
                "inviter": "20000",
                "time": 123456,
                "comment": "请让我加入这个群",
                "decision": "reject",
                "decision_reason": "长理由" * 200,
                "action": "仅通知管理员",
                "execution_state": "NO_ACTION",
                "membership_before": "OUT",
                "membership_after": "OUT",
                "profile_snapshot": {
                    "score": 76,
                    "level": "高",
                    "tags": [{"tag": f"tag_{i}"} for i in range(12)],
                    "social_origin": {
                        "join_sources": [{
                            "gid": "30001", "sub_type": "invite", "operator": "20001"
                        }]
                    },
                },
            }]
        }
        plugin.html_render = AsyncMock(return_value="card.png")
        path = await plugin._render_invite_records_image(FakeBot())
        self.assertEqual(path, "card.png")
        item = plugin.html_render.await_args.args[1]["items"][0]
        self.assertEqual(item["decision"], "建议拒绝")
        self.assertEqual(item["decision_class"], "reject")
        self.assertEqual(item["risk_text"], "76/100 · 高风险")
        self.assertEqual(len(item["tags"]), 8)
        self.assertLessEqual(len(item["reason"]), 240)
        self.assertIn("群 30001", item["relation"])
        self.assertNotIn("detail", item)

    async def test_image_profile_lookups_are_deduplicated(self):
        plugin = self.make_plugin()
        plugin.config["display"] = {
            "invite_records_show_profile": True,
            "invite_records_show_group_profile": True,
            "invite_records_hide_dealt": False,
            "invite_records_show_decision_detail": True,
        }
        plugin._kv["invite_records"] = {
            "30000": [
                {"inviter": "20000", "time": 2, "action": "自动同意进群"},
                {"inviter": "20000", "time": 1, "action": "自动拒绝"},
            ]
        }
        plugin._fetch_nickname = AsyncMock(return_value="昵称")
        plugin._fetch_group_name = AsyncMock(return_value="群名")
        plugin.html_render = AsyncMock(return_value="record.png")
        path = await plugin._render_invite_records_image(FakeBot())
        self.assertEqual(path, "record.png")
        plugin._fetch_nickname.assert_awaited_once_with(unittest.mock.ANY, "20000")
        plugin._fetch_group_name.assert_awaited_once_with(unittest.mock.ANY, "30000")

    async def test_inviter_history_reuses_query_without_expanding_impression_scope(self):
        plugin = self.make_plugin()
        conversations = [
            types.SimpleNamespace(
                history=json.dumps([{
                    "role": "user",
                    "content": f"昵称{i} (ID: 20000): 原话{i}",
                }], ensure_ascii=False)
            )
            for i in range(1, 11)
        ]
        manager = types.SimpleNamespace(
            get_filtered_conversations=AsyncMock(return_value=(conversations, 10))
        )
        plugin.context = types.SimpleNamespace(conversation_manager=manager)
        lines, quotes = await plugin._search_inviter_history("20000")
        self.assertEqual(manager.get_filtered_conversations.await_count, 1)
        self.assertEqual(
            manager.get_filtered_conversations.await_args.kwargs["page_size"], 10
        )
        self.assertTrue(any("原话5" in line for line in lines))
        self.assertFalse(any("原话6" in line for line in lines))
        self.assertFalse(any("原话10" in line for line in lines))
        self.assertEqual(quotes, [f"原话{i}" for i in range(1, 11)])

    async def test_speaker_history_stops_scanning_at_twenty_quotes(self):
        plugin = self.make_plugin()
        first_history = json.dumps([{
            "role": "user",
            "content": "\n".join(
                f"昵称 (ID: 20000): 原话{i}" for i in range(1, 21)
            ),
        }], ensure_ascii=False)

        class UnreadableConversation:
            @property
            def history(self):
                raise AssertionError("speaker scan continued after reaching 20")

        conversations = [types.SimpleNamespace(history=first_history)]
        conversations.extend(types.SimpleNamespace(history="[]") for _ in range(4))
        conversations.append(UnreadableConversation())
        plugin.context = types.SimpleNamespace(
            conversation_manager=types.SimpleNamespace(
                get_filtered_conversations=AsyncMock(
                    return_value=(conversations, len(conversations))
                )
            )
        )
        _, quotes = await plugin._search_inviter_history("20000")
        self.assertEqual(quotes, [f"原话{i}" for i in range(1, 21)])

    async def test_invite_snapshot_avoids_duplicate_invite_kv_read(self):
        plugin = self.make_plugin()
        snapshot = {"30000": [{"inviter": "20000", "request_key": "old"}]}
        data = await plugin._collect_profile_data(
            "20000", "current", invite_records=snapshot
        )
        self.assertEqual(data["invited"][0][0], "30000")
        self.assertNotIn("invite_records", plugin.get_calls)
        self.assertCountEqual(plugin.get_calls, ["join_records", "mute_records"])

    async def test_context_snapshot_sees_other_request_latest_terminal_state(self):
        plugin = self.make_plugin()
        plugin.config["decision"].update({
            "enable_member_context": False,
            "enable_impression_context": False,
            "enable_user_profile": True,
            "use_profile_plugin": False,
        })
        plugin.config["alt_detect"] = {"alt_account_detect": False}
        snapshot_started = asyncio.Event()
        release_snapshot = asyncio.Event()

        async def get_kv(key, default):
            plugin.get_calls.append(key)
            if key == "invite_records":
                snapshot_started.set()
                await release_snapshot.wait()
            return copy.deepcopy(plugin._kv.get(key, default))

        plugin.get_kv_data = get_kv
        plugin._kv["invite_records"] = {
            "30000": [{
                "inviter": "20000",
                "request_key": "current",
                "execution_state": "REVIEWING",
            }]
        }
        task = asyncio.create_task(plugin._build_invite_context(
            "20000", "30000", None, request_key="current"
        ))
        await snapshot_started.wait()
        plugin._kv["invite_records"]["30001"] = [{
            "inviter": "20000",
            "request_key": "other",
            "decision": "reject",
            "execution_state": "REJECTED",
            "action": "自动拒绝",
        }]
        release_snapshot.set()
        context, _, _ = await task
        self.assertIn("历史邀请被拒绝 1 次", context)
        self.assertIn("群：30001", context)
        self.assertEqual(plugin.get_calls.count("invite_records"), 1)

    async def test_context_gather_warns_for_noncritical_failure(self):
        plugin = self.make_plugin()
        plugin.config["decision"].update({
            "enable_member_context": True,
            "enable_impression_context": False,
            "enable_user_profile": False,
        })
        plugin.config["alt_detect"] = {"alt_account_detect": False}
        plugin._build_member_section = AsyncMock(
            side_effect=RuntimeError("member context broke")
        )
        with patch("main.logger.warning") as warning:
            context, alt_warning, snapshot = await plugin._build_invite_context(
                "20000", "30000", None
            )
        self.assertEqual((context, alt_warning, snapshot), ("", "", {}))
        self.assertTrue(any(
            "member context failed" in str(call.args[0])
            and "member context broke" in str(call.args[0])
            for call in warning.call_args_list
        ))

    async def test_alt_detection_failure_warns_and_aborts_decision_context(self):
        plugin = self.make_plugin()
        plugin.config["decision"].update({
            "enable_member_context": False,
            "enable_impression_context": False,
            "enable_user_profile": False,
        })
        plugin._detect_alt_account = AsyncMock(
            side_effect=RuntimeError("alt detection broke")
        )
        with patch("main.logger.warning") as warning:
            with self.assertRaisesRegex(RuntimeError, "alt detection broke"):
                await plugin._build_invite_context("20000", "30000", None)
        self.assertTrue(any(
            "alt detection failed" in str(call.args[0])
            for call in warning.call_args_list
        ))

    async def test_noop_invite_update_skips_write(self):
        plugin = self.make_plugin()
        plugin._kv["invite_records"] = {
            "30000": [{"record_id": "same", "execution_state": "DECIDED"}]
        }
        saved = await plugin._update_invite_record(
            "30000", "same", execution_state="DECIDED"
        )
        self.assertTrue(saved)
        self.assertEqual(plugin.put_calls, [])

    async def test_banned_inviter_index_is_cached_and_write_invalidates(self):
        plugin = self.make_plugin()
        plugin._find_ban_entry = Mock(return_value=None)
        plugin._kv["invite_records"] = {
            "30000": [{"record_id": "one", "inviter": "20000", "dealt": True}]
        }
        self.assertTrue((await plugin._is_user_banned("20000"))[0])
        self.assertFalse((await plugin._is_user_banned("20001"))[0])
        self.assertEqual(plugin.get_calls.count("invite_records"), 1)
        await plugin._record_invite("30001", "20001", action="手动记录")
        await plugin._is_user_banned("20001")
        self.assertEqual(plugin.get_calls.count("invite_records"), 3)

    async def test_banned_index_generation_blocks_stale_refill(self):
        plugin = self.make_plugin()
        plugin._find_ban_entry = Mock(return_value=None)
        read_started = asyncio.Event()
        release_read = asyncio.Event()
        calls = 0

        async def get_kv(key, default):
            nonlocal calls
            calls += 1
            value = copy.deepcopy(plugin._kv.get(key, default))
            if calls == 1:
                read_started.set()
                await release_read.wait()
            return value

        plugin.get_kv_data = get_kv
        plugin._kv["invite_records"] = {}
        stale_read = asyncio.create_task(plugin._is_user_banned("20000"))
        await read_started.wait()
        plugin._kv["invite_records"] = {
            "30000": [{"inviter": "20000", "dealt": True}]
        }
        plugin._invalidate_derived_cache("banned_inviters")
        release_read.set()
        self.assertFalse((await stale_read)[0])
        self.assertNotIn("banned_inviters", plugin._derived_cache)
        self.assertTrue((await plugin._is_user_banned("20000"))[0])
        self.assertEqual(calls, 2)

    async def test_ban_context_generation_blocks_stale_refill(self):
        plugin = self.make_plugin()
        invite_started = asyncio.Event()
        mute_started = asyncio.Event()
        release_reads = asyncio.Event()
        first_reads = {"invite_records": True, "mute_records": True}

        async def get_kv(key, default):
            value = copy.deepcopy(plugin._kv.get(key, default))
            if first_reads.get(key):
                first_reads[key] = False
                (invite_started if key == "invite_records" else mute_started).set()
                await release_reads.wait()
            return value

        plugin.get_kv_data = get_kv
        plugin._kv = {"invite_records": {}, "mute_records": {}}
        stale_read = asyncio.create_task(plugin._build_derived_ban_lines())
        await asyncio.gather(invite_started.wait(), mute_started.wait())
        plugin._kv = {
            "invite_records": {"30000": [{"inviter": "20000"}]},
            "mute_records": {"30000": 2},
        }
        plugin._invalidate_derived_cache("ban_context")
        release_reads.set()
        self.assertEqual(await stale_read, [])
        self.assertNotIn("ban_context", plugin._derived_cache)
        refreshed = await plugin._build_derived_ban_lines()
        self.assertTrue(any("20000" in line for line in refreshed))
        self.assertTrue(any("2 次" in line for line in refreshed))

    async def test_persona_and_context_load_concurrently(self):
        plugin = self.make_plugin()
        persona_entered = asyncio.Event()
        context_entered = asyncio.Event()

        async def persona(*args):
            persona_entered.set()
            await context_entered.wait()
            return "人格"

        async def context(*args, **kwargs):
            context_entered.set()
            await persona_entered.wait()
            return "背景", "", {}

        plugin._resolve_persona_prompt = persona
        plugin._build_invite_context = context
        plugin._default_provider_id = lambda: "provider"
        plugin.context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text='{"action":"approve","reason":"ok"}'
                )
            )
        )
        result = await plugin._ask_llm("20000", "30000", "hello")
        self.assertEqual(result["action"], "approve")

    async def test_same_impression_summary_is_singleflight(self):
        plugin = self.make_plugin()
        plugin._default_provider_id = lambda: "provider"
        entered = asyncio.Event()
        release = asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            await release.wait()
            return types.SimpleNamespace(completion_text="可靠用户")

        plugin.context = types.SimpleNamespace(llm_generate=AsyncMock(side_effect=generate))
        first = asyncio.create_task(plugin._summarize_impression("20000", ["你好"]))
        await entered.wait()
        second = asyncio.create_task(plugin._summarize_impression("20000", ["你好"]))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(first, second), ["可靠用户", "可靠用户"])
        self.assertEqual(plugin.context.llm_generate.await_count, 1)

    async def test_singleflight_cleans_after_all_waiters_cancel(self):
        plugin = self.make_plugin()
        loop = asyncio.get_running_loop()
        unhandled = []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            for should_fail in (False, True):
                entered = asyncio.Event()
                release = asyncio.Event()

                async def factory(fail=should_fail):
                    entered.set()
                    await release.wait()
                    if fail:
                        raise RuntimeError("factory failed")
                    return "ok"

                first = asyncio.create_task(plugin._singleflight("cancelled", factory))
                second = asyncio.create_task(plugin._singleflight("cancelled", factory))
                await entered.wait()
                first.cancel()
                second.cancel()
                await asyncio.gather(first, second, return_exceptions=True)
                release.set()
                for _ in range(5):
                    await asyncio.sleep(0)
                    if not getattr(plugin, "_llm_flights", {}):
                        break
                self.assertEqual(getattr(plugin, "_llm_flights", {}), {})
            await asyncio.sleep(0)
            self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(old_handler)

    async def test_inflight_is_released_when_early_finish_fails(self):
        plugin = self.make_plugin()
        plugin._finish_not_for_bot = AsyncMock(side_effect=RuntimeError("write failed"))
        plugin._mark_processing_failure = AsyncMock()
        event = FakeEvent(self.raw(invited_id=99999), FakeBot())
        await plugin.on_group_invite(event)
        self.assertEqual(getattr(plugin, "_invite_inflight", set()), set())
        plugin._mark_processing_failure.assert_awaited_once()

    async def test_image_profile_api_respects_concurrency_limit(self):
        plugin = self.make_plugin()
        plugin.config["display"] = {
            "invite_records_show_profile": True,
            "invite_records_show_group_profile": False,
            "invite_records_hide_dealt": False,
            "image_profile_concurrency": 2,
        }
        plugin._kv["invite_records"] = {
            str(30000 + i): [{"inviter": str(20000 + i), "time": i}]
            for i in range(6)
        }
        active = 0
        peak = 0

        async def nickname(bot, qq):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return qq

        plugin._fetch_nickname = nickname
        plugin.html_render = AsyncMock(return_value="limited.png")
        self.assertEqual(await plugin._render_invite_records_image(FakeBot()), "limited.png")
        self.assertEqual(peak, 2)

    async def test_ban_context_caches_only_plugin_records(self):
        plugin = self.make_plugin()
        plugin._read_ban_entries = Mock(side_effect=[[("1", "a")], [("2", "b")]])
        first = await plugin._build_ban_context_text()
        second = await plugin._build_ban_context_text()
        self.assertIn("QQ 1", first)
        self.assertIn("QQ 2", second)
        self.assertEqual(plugin.get_calls.count("invite_records"), 1)
        self.assertEqual(plugin.get_calls.count("mute_records"), 1)

    async def test_mute_kv_failure_rolls_back_fingerprint_and_stops_action(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 1,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        original_put = plugin.put_kv_data
        attempts = 0

        async def put_kv(key, value):
            nonlocal attempts
            if key == "mute_records":
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("mute write failed")
            await original_put(key, value)

        plugin.put_kv_data = put_kv
        bot = FakeBot()
        raw = {
            "group_id": 30000,
            "user_id": 10000,
            "operator_id": 20000,
            "duration": 60,
            "time": 100,
            "sub_type": "ban",
        }
        await plugin.on_group_mute(FakeEvent(raw, bot))
        self.assertNotIn("mute_records", plugin._kv)
        self.assertEqual(bot.calls, [])
        plugin._apply_mute_ban.assert_not_awaited()
        self.assertNotIn(
            plugin._mute_event_fingerprint(raw), plugin._mute_event_fingerprints
        )

        await plugin.on_group_mute(FakeEvent(dict(raw), bot))
        self.assertEqual(plugin._kv["mute_records"], {})
        self.assertEqual(
            [name for name, _ in bot.calls if name == "set_group_leave"],
            ["set_group_leave"],
        )
        plugin._apply_mute_ban.assert_awaited_once_with("20000", bot)

    async def test_mute_kv_read_failure_rolls_back_without_overwrite_or_action(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 3,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._kv["mute_records"] = {"40000": 2}
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        original_get = plugin.get_kv_data
        attempts = 0

        async def get_kv(key, default):
            nonlocal attempts
            if key == "mute_records":
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("mute read failed")
            return await original_get(key, default)

        plugin.get_kv_data = get_kv
        bot = FakeBot()
        raw = {
            "group_id": 30000,
            "user_id": 10000,
            "operator_id": 20000,
            "duration": 60,
            "time": 100,
            "sub_type": "ban",
        }
        await plugin.on_group_mute(FakeEvent(raw, bot))
        self.assertEqual(plugin._kv["mute_records"], {"40000": 2})
        self.assertEqual(plugin.put_calls, [])
        self.assertEqual(bot.calls, [])
        plugin._apply_mute_ban.assert_not_awaited()
        self.assertNotIn(
            plugin._mute_event_fingerprint(raw), plugin._mute_event_fingerprints
        )

        await plugin.on_group_mute(FakeEvent(dict(raw), bot))
        self.assertEqual(
            plugin._kv["mute_records"], {"40000": 2, "30000": 1}
        )
        self.assertEqual(bot.calls, [])

    async def test_non_dict_mute_records_rolls_back_without_write(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 1,
            "mute_notify": False,
        }
        plugin._kv["mute_records"] = ["invalid"]
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        bot = FakeBot()
        raw = {
            "group_id": 30000,
            "user_id": 10000,
            "operator_id": 20000,
            "duration": 60,
            "time": 100,
            "sub_type": "ban",
        }
        await plugin.on_group_mute(FakeEvent(raw, bot))
        self.assertEqual(plugin._kv["mute_records"], ["invalid"])
        self.assertEqual(plugin.put_calls, [])
        self.assertEqual(bot.calls, [])
        plugin._apply_mute_ban.assert_not_awaited()
        self.assertNotIn(
            plugin._mute_event_fingerprint(raw), plugin._mute_event_fingerprints
        )

    async def test_mute_preclear_failure_rolls_back_and_stops_action(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 1,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._kv["mute_records"] = {"40000": 2}
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        original_put = plugin.put_kv_data
        attempts = 0

        async def put_kv(key, value):
            nonlocal attempts
            if key == "mute_records":
                attempts += 1
                if attempts == 2:
                    raise RuntimeError("preclear failed")
            await original_put(key, value)

        plugin.put_kv_data = put_kv
        bot = FakeBot()
        raw = {
            "group_id": 30000,
            "user_id": 10000,
            "operator_id": 20000,
            "duration": 60,
            "time": 100,
            "sub_type": "ban",
        }
        await plugin.on_group_mute(FakeEvent(raw, bot))
        self.assertEqual(
            plugin._kv["mute_records"], {"40000": 2, "30000": 1}
        )
        self.assertEqual(bot.calls, [])
        plugin._apply_mute_ban.assert_not_awaited()
        self.assertNotIn(
            plugin._mute_event_fingerprint(raw), plugin._mute_event_fingerprints
        )

    async def test_mute_preclear_read_failure_rolls_back_and_stops_action(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 1,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._kv["mute_records"] = {"40000": 2}
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        original_get = plugin.get_kv_data
        reads = 0

        async def get_kv(key, default):
            nonlocal reads
            if key == "mute_records":
                reads += 1
                if reads == 2:
                    raise RuntimeError("preclear read failed")
            return await original_get(key, default)

        plugin.get_kv_data = get_kv
        bot = FakeBot()
        raw = {
            "group_id": 30000,
            "user_id": 10000,
            "operator_id": 20000,
            "duration": 60,
            "time": 100,
            "sub_type": "ban",
        }
        await plugin.on_group_mute(FakeEvent(raw, bot))
        self.assertEqual(
            plugin._kv["mute_records"], {"40000": 2, "30000": 1}
        )
        self.assertEqual(bot.calls, [])
        plugin._apply_mute_ban.assert_not_awaited()
        self.assertNotIn(
            plugin._mute_event_fingerprint(raw), plugin._mute_event_fingerprints
        )

    async def test_duplicate_mute_packet_does_not_count_or_survive_threshold_clear(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 2,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        bot = FakeBot()

        def raw(event_time):
            return {
                "group_id": 30000,
                "user_id": 10000,
                "operator_id": 20000,
                "duration": 60,
                "time": event_time,
                "sub_type": "ban",
            }

        first = raw(100)
        await asyncio.gather(
            plugin.on_group_mute(FakeEvent(first, bot)),
            plugin.on_group_mute(FakeEvent(dict(first), bot)),
        )
        self.assertEqual(plugin._kv["mute_records"], {"30000": 1})
        second = raw(101)
        await plugin.on_group_mute(FakeEvent(second, bot))
        self.assertEqual(plugin._kv["mute_records"], {})
        await plugin.on_group_mute(FakeEvent(dict(second), bot))
        self.assertEqual(plugin._kv["mute_records"], {})
        await plugin.on_group_mute(FakeEvent(raw(102), bot))
        self.assertEqual(plugin._kv["mute_records"], {"30000": 1})
        leaves = [name for name, _ in bot.calls if name == "set_group_leave"]
        self.assertEqual(leaves, ["set_group_leave"])
        plugin._apply_mute_ban.assert_awaited_once_with("20000", bot)

    async def test_new_mute_event_after_failed_action_is_not_suppressed(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 1,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        bot = FakeBot()
        bot.fail_action = "set_group_leave"
        base = {
            "group_id": 30000,
            "user_id": 10000,
            "operator_id": 20000,
            "duration": 60,
            "sub_type": "ban",
        }
        await plugin.on_group_mute(FakeEvent({**base, "time": 100}, bot))
        await plugin.on_group_mute(FakeEvent({**base, "time": 101}, bot))
        leaves = [name for name, _ in bot.calls if name == "set_group_leave"]
        self.assertEqual(leaves, ["set_group_leave", "set_group_leave"])
        self.assertEqual(plugin._apply_mute_ban.await_count, 2)

    async def test_cancelled_mute_action_keeps_precleared_count(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 2,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._kv["mute_records"] = {"30000": 1, "40000": 2}
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        leave_entered = asyncio.Event()
        block_leave = asyncio.Event()

        class BlockingBot(FakeBot):
            async def set_group_leave(self, **params):
                self.calls.append(("set_group_leave", params))
                leave_entered.set()
                await block_leave.wait()

        bot = BlockingBot()
        base = {
            "group_id": 30000,
            "user_id": 10000,
            "operator_id": 20000,
            "duration": 60,
            "sub_type": "ban",
        }
        task = asyncio.create_task(
            plugin.on_group_mute(FakeEvent({**base, "time": 100}, bot))
        )
        await leave_entered.wait()
        self.assertEqual(plugin._kv["mute_records"], {"40000": 2})
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        plugin._apply_mute_ban.assert_not_awaited()

        await plugin.on_group_mute(FakeEvent({**base, "time": 101}, bot))
        self.assertEqual(
            plugin._kv["mute_records"], {"40000": 2, "30000": 1}
        )
        self.assertEqual(
            [name for name, _ in bot.calls if name == "set_group_leave"],
            ["set_group_leave"],
        )

    async def test_different_groups_do_not_block_on_slow_mute_action(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 1,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        group_a_entered = asyncio.Event()
        release_group_a = asyncio.Event()
        group_b_done = asyncio.Event()

        class SlowGroupBot(FakeBot):
            async def set_group_leave(self, **params):
                self.calls.append(("set_group_leave", params))
                if params["group_id"] == 30000:
                    group_a_entered.set()
                    await release_group_a.wait()
                else:
                    group_b_done.set()

        bot = SlowGroupBot()

        def raw(group_id, event_time):
            return {
                "group_id": group_id,
                "user_id": 10000,
                "operator_id": 20000 + group_id,
                "duration": 60,
                "time": event_time,
                "sub_type": "ban",
            }

        group_a = asyncio.create_task(
            plugin.on_group_mute(FakeEvent(raw(30000, 100), bot))
        )
        await group_a_entered.wait()
        group_b = asyncio.create_task(
            plugin.on_group_mute(FakeEvent(raw(30001, 101), bot))
        )
        await asyncio.wait_for(group_b_done.wait(), timeout=1)
        await asyncio.wait_for(group_b, timeout=1)
        self.assertFalse(group_a.done())
        release_group_a.set()
        await group_a
        self.assertEqual(
            sorted(p["group_id"] for name, p in bot.calls if name == "set_group_leave"),
            [30000, 30001],
        )
        self.assertEqual(plugin._mute_group_locks, {})

    async def test_same_group_concurrent_threshold_triggers_once(self):
        plugin = self.make_plugin()
        plugin.config["mute_revenge"] = {
            "mute_retaliation_enable": True,
            "mute_threshold": 2,
            "mute_target": "operator",
            "mute_ban_mode": "astrbot_ban",
            "mute_notify": False,
        }
        plugin._apply_mute_ban = AsyncMock(return_value="已拉黑")
        bot = FakeBot()
        base = {
            "group_id": 30000,
            "user_id": 10000,
            "operator_id": 20000,
            "duration": 60,
            "sub_type": "ban",
        }
        await asyncio.gather(
            plugin.on_group_mute(FakeEvent({**base, "time": 100}, bot)),
            plugin.on_group_mute(FakeEvent({**base, "time": 101}, bot)),
        )
        self.assertEqual(
            [name for name, _ in bot.calls if name == "set_group_leave"],
            ["set_group_leave"],
        )
        plugin._apply_mute_ban.assert_awaited_once_with("20000", bot)
        self.assertEqual(plugin._mute_group_locks, {})

    def test_mute_fingerprint_state_is_bounded(self):
        plugin = self.make_plugin()
        for event_time in range(300):
            duplicate = plugin._is_duplicate_mute_event({
                "group_id": 30000,
                "user_id": 10000,
                "operator_id": 20000,
                "duration": 60,
                "time": event_time,
                "sub_type": "ban",
            })
            self.assertFalse(duplicate)
        self.assertEqual(len(plugin._mute_event_fingerprints), 256)
        self.assertFalse(hasattr(plugin, "_mute_locks"))
        self.assertFalse(hasattr(plugin, "_mute_action_times"))

    async def test_versioned_inviter_evidence_excludes_current_request(self):
        plugin = self.make_plugin()
        plugin._kv["invite_records"] = {
            "30000": [
                {"inviter": "20000", "request_key": "current", "decision": "reject"},
                {"inviter": "20000", "request_key": "old", "decision": "approve"},
            ]
        }
        plugin._kv["join_records"] = {
            "30001": {"operator": "20000", "time": 10}
        }
        plugin._kv["mute_records"] = {"30000": 4}
        evidence = await plugin.get_inviter_evidence("20000", exclude_request_key="current")
        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(len(evidence["invite"]["30000"]), 1)
        self.assertEqual(evidence["invite"]["30000"][0]["request_key"], "old")
        self.assertIn("30001", evidence["join"])
        self.assertEqual(evidence["group_mute_context"], {"30000": 4})
        self.assertNotIn("mute", evidence)
        self.assertTrue(evidence["evidence_untrusted"])

    async def test_inviter_evidence_tolerates_dirty_times(self):
        plugin = self.make_plugin()
        plugin._kv["invite_records"] = {
            "30000": [{"inviter": "20000", "time": "bad", "action": "ok"}],
            "30001": [{"inviter": "20000", "time": 12, "action": "ok"}],
        }
        plugin._kv["join_records"] = {
            "30002": {"operator": "20000", "time": []},
            "30003": {"operator": "20000", "time": 13},
        }
        evidence = await plugin.get_inviter_evidence("20000")
        self.assertEqual(evidence["invite"]["30000"][0]["time"], 0)
        self.assertEqual(evidence["invite"]["30001"][0]["time"], 12)
        self.assertEqual(evidence["join"]["30002"]["time"], 0)
        self.assertEqual(evidence["join"]["30003"]["time"], 13)

    def test_llm_json_output_is_control_cleaned_and_bounded(self):
        result = _parse_json(json.dumps({
            "action": "approve",
            "reason": "reason\x00\n" + "r" * 400,
            "reply": "reply\x07\r\n" + "p" * 700,
        }))
        self.assertEqual(result["action"], "approve")
        self.assertLessEqual(len(result["reason"]), 300)
        self.assertLessEqual(len(result["reply"]), 500)
        self.assertNotRegex(result["reason"], r"[\x00-\x1f\x7f]")
        self.assertNotRegex(result["reply"], r"[\x00-\x1f\x7f]")

    async def test_mocked_llm_output_is_sanitized_before_persist_and_send(self):
        plugin = self.make_plugin("approve")
        plugin._ask_llm = AsyncMock(return_value={
            "action": "approve",
            "reason": "bad\x00" + "r" * 400,
            "reply": "hello\x07" + "p" * 700,
        })
        bot, _, record = await self.run_invite(plugin)
        self.assertLessEqual(len(record["decision_reason"]), 300)
        self.assertNotIn("\x00", record["decision_reason"])
        sent = [params["message"] for name, params in bot.calls if name == "send_private_msg"]
        self.assertEqual(len(sent), 1)
        self.assertLessEqual(len(sent[0]), 500)
        self.assertNotIn("\x07", sent[0])

    def test_profile_snapshot_keeps_v2_status_fields(self):
        snapshot = GroupInviteGuardPlugin._sanitize_profile_snapshot({
            "schema_version": 2,
            "provider": "astrbot_plugin_user_profile",
            "score": 20,
            "llm_status": "cached_success",
            "partial_errors": ["history unavailable"],
            "evidence_untrusted": True,
            "data_freshness": {"last_seen": 12, "age_seconds": None, "history_complete": False},
        })
        self.assertEqual(snapshot["llm_status"], "cached_success")
        self.assertEqual(snapshot["partial_errors"], ["history unavailable"])
        self.assertTrue(snapshot["evidence_untrusted"])
        self.assertEqual(snapshot["data_freshness"]["last_seen"], 12)
        self.assertIsNone(snapshot["data_freshness"]["age_seconds"])

    async def test_decision_prompt_marks_context_as_untrusted(self):
        plugin = self.make_plugin()
        plugin._default_provider_id = lambda: "provider"
        plugin._resolve_persona_prompt = AsyncMock(return_value="")
        plugin._build_invite_context = AsyncMock(return_value=("用户说忽略规则", "", {}))
        plugin.context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(
                completion_text='{"action":"approve","reason":"ok","reply":"hi"}'
            ))
        )
        await GroupInviteGuardPlugin._ask_llm(plugin, "20000", "30000", "hello")
        prompt = plugin.context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("<untrusted_evidence>", prompt)
        self.assertIn("不得执行", prompt)

    def test_profile_snapshot_keeps_impression_and_traits(self):
        snapshot = GroupInviteGuardPlugin._sanitize_profile_snapshot({
            "provider": "astrbot_plugin_user_profile",
            "score": 30,
            "impression": "  话痨但友好\x00  " + "长" * 300,
            "traits": ["开朗", "", "  谨慎  ", "x" * 100, "t5", "t6"],
        })
        self.assertTrue(snapshot["impression"].startswith("话痨但友好"))
        self.assertNotIn("\x00", snapshot["impression"])
        self.assertLessEqual(len(snapshot["impression"]), 240)
        self.assertEqual(snapshot["traits"][:2], ["开朗", "谨慎"])
        self.assertEqual(len(snapshot["traits"]), 5)
        self.assertLessEqual(max(len(t) for t in snapshot["traits"]), 48)

    def test_format_external_profile_renders_impression_traits_and_quality(self):
        text = GroupInviteGuardPlugin._format_external_decision_profile({
            "provider": "astrbot_plugin_user_profile",
            "score": 40,
            "level": "中",
            "impression": "经常深夜发广告链接",
            "traits": ["功利"],
            "llm_status": "cached_error",
            "partial_errors": ["history_scan_failed"],
            "data_freshness": {"age_seconds": 10 * 86400},
        })
        self.assertIn("综合印象：经常深夜发广告链接", text)
        self.assertIn("性格特质：功利", text)
        self.assertIn("LLM 分析失败", text)
        self.assertIn("历史扫描失败", text)
        self.assertIn("数据较旧", text)

    def test_format_external_profile_clean_snapshot_has_no_quality_note(self):
        text = GroupInviteGuardPlugin._format_external_decision_profile({
            "provider": "astrbot_plugin_user_profile",
            "score": 10,
            "llm_status": "success",
            "data_freshness": {"age_seconds": 3600},
        })
        self.assertNotIn("数据质量", text)

    async def test_custom_reject_reply_overrides_llm_reply(self):
        plugin = self.make_plugin(decision="reject")
        plugin.config["decision"]["custom_reject_reply"] = "抱歉，暂不加群：{reason}"
        bot, _, record = await self.run_invite(plugin)
        sent = [
            params["message"] for name, params in bot.calls
            if name == "send_private_msg"
        ]
        self.assertEqual(sent, ["抱歉，暂不加群：test reason"])
        self.assertEqual(record["reply"], "抱歉，暂不加群：test reason")

    async def test_custom_reject_reply_only_applies_to_reject(self):
        plugin = self.make_plugin(decision="approve")
        plugin.config["decision"]["custom_reject_reply"] = "抱歉，暂不加群"
        bot, _, record = await self.run_invite(plugin)
        sent = [
            params["message"] for name, params in bot.calls
            if name == "send_private_msg"
        ]
        self.assertEqual(sent, ["test reply"])

    async def test_decision_prompt_mentions_fixed_reject_reply(self):
        plugin = self.make_plugin()
        plugin.config["decision"]["custom_reject_reply"] = "暂不加群"
        plugin._default_provider_id = lambda: "provider"
        plugin._resolve_persona_prompt = AsyncMock(return_value="")
        plugin._build_invite_context = AsyncMock(return_value=("", "", {}))
        plugin.context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(
                completion_text='{"action":"reject","reason":"ok","reply":"hi"}'
            ))
        )
        await GroupInviteGuardPlugin._ask_llm(plugin, "20000", "30000", "hello")
        prompt = plugin.context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("固定拒绝文案", prompt)
        self.assertIn("回应一句", prompt)


if __name__ == "__main__":
    unittest.main()
