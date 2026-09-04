import asyncio
import copy
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


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

from main import GroupInviteGuardPlugin, _invite_status_label


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
        plugin.fail_put_states = {}
        plugin.put_delay = 0

        async def get_kv(key, default):
            return copy.deepcopy(plugin._kv.get(key, default))

        async def put_kv(key, value):
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
        self.assertEqual(items[0]["detail"], "")

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


if __name__ == "__main__":
    unittest.main()
