import asyncio
import copy
import time
import types
import unittest
from unittest.mock import AsyncMock

import test_invite_flow as fixtures
from main import GroupInviteGuardPlugin
from group_relations import RELATIONS_KEY, RELATION_MARKER


class GroupRelationTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, **kwargs):
        return fixtures.InviteFlowTests.make_plugin(self, **kwargs)

    def notice(self, state="IN", **updates):
        raw = dict(post_type="notice", notice_type="group_increase" if state == "IN" else "group_decrease",
                   sub_type="invite" if state == "IN" else "kick_me", user_id=10000, self_id=10000,
                   group_id=30000, operator_id=40000, time=int(time.time()))
        raw.update(updates)
        return fixtures.FakeEvent(raw, fixtures.FakeBot())

    def candidate(self, **updates):
        result = dict(target_state="VERIFIED", self_id="10000", platform_id="test", inviter="20000",
                      request_key="request", time=int(time.time()), execution_state="APPROVED")
        result.update(updates)
        return result

    async def records(self, p):
        return await p.get_bot_group_relations(platform_id="test", self_id="10000", group_id="30000")

    async def test_join_is_independent_of_legacy_record_switch_and_operator_is_not_inviter(self):
        p = self.make_plugin()
        p.config["kick_revenge"] = {"record_group_join": False, "revenge_mode": "off"}
        event = self.notice()
        await p.on_group_join(event)
        record = (await self.records(p))[0]
        self.assertEqual(record["membership"], "IN")
        self.assertEqual(record["inviter"], "")
        self.assertEqual(record["operator"], "40000")
        await p.on_group_join(event)
        self.assertEqual((await self.records(p))[0]["revision"], 1)
        await p.on_group_join(self.notice(user_id=20000))
        self.assertEqual((await self.records(p))[0]["revision"], 1)

    async def test_join_attribution_requires_unique_exact_platform_bot_candidate(self):
        for candidates, expected in (([self.candidate()], "20000"),
                                     ([self.candidate(), self.candidate(inviter="50000")], ""),
                                     ([self.candidate(platform_id="")], ""),
                                     ([self.candidate(self_id="99999")], ""),
                                     ([self.candidate(platform_id="other")], "")):
            p = self.make_plugin()
            p._kv["invite_records"] = {"30000": candidates}
            await p.on_group_join(self.notice())
            self.assertEqual((await self.records(p))[0]["inviter"], expected)

    async def test_successful_approval_not_confirmation_then_real_join(self):
        p = self.make_plugin(membership=["OUT", "OUT", "OUT"])
        event = fixtures.FakeEvent(dict(post_type="request", request_type="group", sub_type="invite",
                                      group_id=30000, user_id=20000, self_id=10000, flag="flag", time=int(time.time())), fixtures.FakeBot())
        await p.on_group_invite(event)
        self.assertEqual((await self.records(p))[0]["membership"], "OUT")
        self.assertTrue(any(action == "set_group_add_request" for action, _ in event.bot.calls))
        await p.on_group_join(self.notice())
        record = (await self.records(p))[0]
        self.assertEqual(record["membership"], "IN")
        self.assertEqual(record["inviter"], "20000")
        self.assertTrue(record["request_key"])

    async def test_kick_and_voluntary_leave_record_even_retaliation_disabled(self):
        p = self.make_plugin()
        p.config["kick_revenge"] = {"revenge_mode": "off", "revenge_notify": False}
        await p.on_group_join(self.notice())
        await p.on_group_kick(self.notice("OUT"))
        self.assertEqual((await self.records(p))[0]["membership"], "OUT")
        await p.on_group_join(self.notice(time=int(time.time())+1))
        await p.on_group_leave_lifecycle(self.notice("OUT", sub_type="leave", time=int(time.time())+2))
        self.assertEqual((await self.records(p))[0]["membership"], "OUT")

    async def test_successful_api_leave_only_current_bot_and_failure_does_not_mark_out(self):
        p = self.make_plugin()
        bot = fixtures.FakeBot()
        event = self.notice()
        p._remember_relation_bot(event, bot)
        await p.on_group_join(event)
        await p._call_action(bot, "set_group_leave", group_id=30000, is_dismiss=False)
        self.assertEqual((await self.records(p))[0]["membership"], "OUT")
        await p.on_group_join(self.notice(time=int(time.time())+1))
        bot.fail_action = "set_group_leave"
        with self.assertRaises(RuntimeError):
            await p._call_action(bot, "set_group_leave", group_id=30000, is_dismiss=False)
        self.assertEqual((await self.records(p))[0]["membership"], "IN")
        other = self.notice(self_id=99999, user_id=99999)
        await p.on_group_join(other)
        self.assertEqual(len(p._kv[RELATIONS_KEY]["records"]), 2)

    async def test_durable_pending_failure_ack_loss_and_restart(self):
        p = self.make_plugin()
        receiver = AsyncMock(side_effect=OSError("down"))
        p._get_profile_plugin_instance = lambda: types.SimpleNamespace(record_bot_group_relation=receiver)
        event = self.notice()
        await p.on_group_join(event)
        ledger = copy.deepcopy(p._kv)
        original_record = next(iter(ledger[RELATIONS_KEY]["pending"].values()))
        restarted = self.make_plugin()
        restarted._kv = ledger
        receiver.side_effect = None
        receiver.return_value = {"status": "recorded"}
        restarted._get_profile_plugin_instance = p._get_profile_plugin_instance
        original_put = restarted.put_kv_data
        async def lose_ack(key, value):
            if key == RELATIONS_KEY and not value["pending"]:
                raise OSError("ack save")
            await original_put(key, value)
        restarted.put_kv_data = lose_ack
        await restarted._deliver_group_relations()
        self.assertTrue(restarted._kv[RELATIONS_KEY]["pending"])
        restarted.put_kv_data = original_put
        receiver.return_value = {"status": "duplicate"}
        await restarted._deliver_group_relations()
        self.assertFalse(restarted._kv[RELATIONS_KEY]["pending"])
        self.assertEqual(receiver.await_args.args[0]["event_key"], original_record["event_key"])
        self.assertEqual(receiver.await_args.args[0]["event_created_at_ms"], original_record["event_created_at_ms"])

    async def test_relation_kv_failure_keeps_join_kick_leave_and_blacklist_flows(self):
        for fault in ("read", "write", "corrupt"):
            p = self.make_plugin()
            get, put = p.get_kv_data, p.put_kv_data
            async def failing_get(key, default):
                if key == RELATIONS_KEY and fault == "read":
                    raise OSError("relation read")
                return await get(key, default)
            async def failing_put(key, value):
                if key == RELATIONS_KEY and fault == "write":
                    raise OSError("relation write")
                return await put(key, value)
            p.get_kv_data, p.put_kv_data = failing_get, failing_put
            if fault == "corrupt":
                p._kv[RELATIONS_KEY] = "corrupt"
            event = self.notice()
            await p.on_group_join(event)
            self.assertIn("30000", p._kv["join_records"])
            p.config["kick_revenge"] = {"revenge_mode": "delete_friend", "revenge_notify": True}
            p._admission_inviter = AsyncMock(return_value="20000")
            p._cross_group_leave = AsyncMock(return_value="")
            p._take_revenge = AsyncMock(return_value="已删除好友")
            p._mark_inviter_dealt = AsyncMock()
            await p.on_group_kick(self.notice("OUT"))
            p._take_revenge.assert_awaited_once()
            p._mark_inviter_dealt.assert_awaited_once_with("20000")
            await p.on_group_leave_lifecycle(self.notice("OUT", sub_type="leave"))
            p._remember_relation_bot(event, event.bot)
            result = await p._call_action(event.bot, "set_group_leave", group_id=30000, is_dismiss=False)
            self.assertIsNone(result)
            self.assertTrue(any(action == "set_group_leave" for action, _ in event.bot.calls))
            p._read_ban_entries = lambda: [("55555", "keep blacklist")]
            req = types.SimpleNamespace(system_prompt="base")
            await p.on_llm_request(event, req)
            self.assertIn("keep blacklist", req.system_prompt)

    async def test_relation_failure_does_not_block_invite_approval(self):
        p = self.make_plugin(membership=["OUT", "OUT", "OUT"])
        p._kv[RELATIONS_KEY] = "corrupt"
        event = fixtures.FakeEvent(dict(post_type="request", request_type="group", sub_type="invite", group_id=30000, user_id=20000, self_id=10000, flag="fault-flag", time=int(time.time())), fixtures.FakeBot())
        await p.on_group_invite(event)
        self.assertTrue(any(action == "set_group_add_request" for action, _ in event.bot.calls))
        self.assertEqual(p._kv["invite_records"]["30000"][-1]["execution_state"], "APPROVED")

    async def test_relation_linkage_cancellation_still_propagates(self):
        for hook, state, updates in (("on_group_join", "IN", {}), ("on_group_kick", "OUT", {}), ("on_group_leave_lifecycle", "OUT", {"sub_type": "leave"})):
            p = self.make_plugin()
            p._record_relation_notice = AsyncMock(side_effect=asyncio.CancelledError)
            with self.assertRaises(asyncio.CancelledError):
                await getattr(p, hook)(self.notice(state, **updates))
        p = self.make_plugin()
        p._record_successful_group_leave = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await p._call_action(fixtures.FakeBot(), "set_group_leave", group_id=30000)

    async def test_invalid_ledger_and_record_save_fail_without_delivery(self):
        p = self.make_plugin()
        receiver = AsyncMock(return_value={"status": "recorded"})
        p._get_profile_plugin_instance = lambda: types.SimpleNamespace(record_bot_group_relation=receiver)
        p._kv[RELATIONS_KEY] = "corrupt"
        with self.assertRaises(ValueError):
            await p._record_relation_notice(self.notice(), "IN")
        receiver.assert_not_awaited()
        p._kv.clear()
        p.put_kv_data = AsyncMock(side_effect=OSError("disk"))
        with self.assertRaises(OSError):
            await p._record_relation_notice(self.notice(), "IN")
        receiver.assert_not_awaited()

    async def test_scoped_fallback_marker_does_not_remove_blacklist_or_use_global_invites(self):
        p = self.make_plugin()
        p._read_ban_entries = lambda: [("55555", "must retain blacklist")]
        p._kv["invite_records"] = {"77777": [{"inviter": "66666"}]}
        await p.on_group_join(self.notice())
        event = self.notice()
        event.get_group_id = lambda: "30000"
        event.get_sender_id = lambda: "20000"
        req = types.SimpleNamespace(system_prompt="base")
        await p.on_llm_request(event, req)
        self.assertIn("30000", req.system_prompt)
        self.assertNotIn("77777", req.system_prompt)
        self.assertIn("must retain blacklist", req.system_prompt)
        before = req.system_prompt
        await p.on_llm_request(event, req)
        self.assertEqual(before, req.system_prompt)
        req = types.SimpleNamespace(system_prompt=RELATION_MARKER)
        await p.on_llm_request(event, req)
        self.assertIn("must retain blacklist", req.system_prompt)
        self.assertIn("guard_group_relations", req.system_prompt)
        req = types.SimpleNamespace(system_prompt=RELATION_MARKER + '\n<!-- astrbot-bot-group-claims:{"30000":1} -->')
        await p.on_llm_request(event, req)
        self.assertIn("must retain blacklist", req.system_prompt)
        self.assertNotIn("guard_group_relations", req.system_prompt)
        event.get_group_id = lambda: ""
        event.get_sender_id = lambda: "40000"
        self.assertEqual(await p._relation_fallback_context(event), "")

    async def test_stale_and_undated_notices_do_not_pin_old_membership(self):
        p = self.make_plugin()
        now = int(time.time())
        await p.on_group_join(self.notice(time=now))
        await p._record_relation_notice(self.notice("OUT", time=now+1), "OUT")
        await p.on_group_join(self.notice(time=now-10))
        self.assertEqual((await self.records(p))[0]["membership"], "OUT")
        p = self.make_plugin()
        await p.on_group_join(self.notice(time=0))
        await p.on_group_join(self.notice(time=0))
        self.assertEqual((await self.records(p))[0]["revision"], 1)
        await p._record_relation_notice(self.notice("OUT", time=0), "OUT")
        await p.on_group_join(self.notice(time=0))
        self.assertEqual((await self.records(p))[0]["membership"], "IN")
        self.assertEqual((await self.records(p))[0]["revision"], 3)

    async def test_real_membership_requires_matching_bot_response(self):
        p = self.make_plugin()
        del p._membership_state
        class Bot:
            async def get_login_info(self): return {"user_id": 10000}
            async def get_group_member_info(self, **kwargs): return {"user_id": 99999, "group_id": 30000}
            async def get_group_list(self, **kwargs): raise OSError("offline")
        self.assertEqual(await p._membership_state(Bot(), "30000", "10000"), "UNKNOWN")
        self.assertEqual(await p._membership_state(Bot(), "30000", "99999"), "UNKNOWN")
        bot = Bot()
        bot.get_group_member_info = AsyncMock(return_value={"user_id": 10000, "group_id": 30000})
        self.assertEqual(await p._membership_state(bot, "30000", "10000"), "IN")
        bot.get_group_member_info = AsyncMock(side_effect=OSError("missing"))
        bot.get_group_list = AsyncMock(return_value=[])
        self.assertEqual(await p._membership_state(bot, "30000", "10000"), "OUT")
