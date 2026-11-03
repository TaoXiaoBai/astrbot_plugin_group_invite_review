import copy
import unittest

import test_invite_flow as flow


class ExistingMemberSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_member_never_rejects_leaves_or_reassigns_inviter(self):
        fixture = flow.InviteFlowTests()
        for mode in ("notify_only", "leave", "message_then_leave"):
            with self.subTest(mode=mode):
                plugin = fixture.make_plugin("reject", mode, ["IN"])
                flow.seed_relation(plugin, inviter="21000")
                previous = copy.deepcopy(plugin._kv[flow.RELATIONS_KEY])
                plugin.config["decision"]["custom_reject_reply"] = "审核未通过"
                bot, event, rec = await fixture.run_invite(plugin)
                plugin._ask_llm.assert_not_awaited()
                self.assertTrue(event.stopped)
                self.assertEqual(bot.calls, [])
                self.assertEqual(plugin._kv[flow.RELATIONS_KEY], previous)
                self.assertEqual(rec["execution_state"], "ALREADY_MEMBER")
                self.assertEqual(rec["decision"], "skipped")
                self.assertEqual(rec["decision_source"], "existing_member_rule")
                self.assertFalse(rec["action_attempted"])
                self.assertFalse(rec["action_succeeded"])
                self.assertEqual(rec["reply"], "")
                self.assertEqual(rec["membership_before"], "IN")
                self.assertEqual(rec["membership_after"], "IN")
                note = plugin._notify.await_args.args[1]
                self.assertIn("规则（已在群） 决策：跳过审核", note)
                self.assertNotIn("LLM", note)
                self.assertNotIn("邀请人话术", note)
                detail = plugin._invite_record_detail(rec)
                self.assertNotIn("LLM", repr(detail))
                await plugin.on_group_invite(flow.FakeEvent(fixture.raw(), bot))
                self.assertEqual(plugin._notify.await_count, 1)
                self.assertEqual(bot.calls, [])

    async def test_unknown_initial_membership_does_not_leave_or_send_decision(self):
        fixture = flow.InviteFlowTests()
        for action in ("approve", "reject"):
            with self.subTest(action=action):
                plugin = fixture.make_plugin(action, "message_then_leave", ["UNKNOWN", "IN"])
                flow.seed_relation(plugin, inviter="21000")
                bot, _, rec = await fixture.run_invite(plugin)
                relation = plugin._kv[flow.RELATIONS_KEY]["records"][flow.scope_key("test", "10000", "30000")]
                self.assertEqual(relation["inviter"], "21000")
                self.assertEqual(bot.calls, [])
                self.assertEqual(rec["execution_state"], "UNEXPECTED_JOIN_UNVERIFIED")
                self.assertFalse(rec["action_succeeded"])
                self.assertIn("未发送", rec["reply_status"])
                self.assertEqual(rec["membership_after"], "IN")

    async def test_existing_member_notification_uses_current_bot_and_configured_targets(self):
        fixture = flow.InviteFlowTests()
        for fail_private in (False, True):
            with self.subTest(fail_private=fail_private):
                plugin = fixture.make_plugin("reject", "leave", ["IN"])
                plugin.config["basic"].update(
                    notify_private=True, notify_private_qq="1599170798",
                    notify_group=True, notify_group_id="40000",
                )
                plugin._notify = flow.GroupInviteGuardPlugin._notify.__get__(plugin)
                bot = flow.FakeBot()
                bot.fail_private = fail_private
                _, _, rec = await fixture.run_invite(plugin, bot)
                self.assertEqual([name for name, _ in bot.calls],
                                 ["send_private_msg", "send_group_msg"])
                self.assertEqual(bot.calls[0][1]["user_id"], 1599170798)
                self.assertEqual(bot.calls[1][1]["group_id"], 40000)
                self.assertEqual(bot.calls[0][1]["message"], bot.calls[1][1]["message"])
                self.assertIn("跳过审核", bot.calls[0][1]["message"])
                self.assertNotIn("邀请人话术", bot.calls[0][1]["message"])
                self.assertEqual(rec["execution_state"], "ALREADY_MEMBER")

    async def test_executor_defaults_to_no_compensation_without_initial_evidence(self):
        fixture = flow.InviteFlowTests()
        for before in (None, "UNKNOWN", "IN"):
            with self.subTest(before=before):
                plugin = fixture.make_plugin("reject", "leave")
                bot = flow.FakeBot()
                kwargs = {} if before is None else {"membership_before": before}
                outcome = await plugin._execute_invite_decision(
                    bot, "30000", "record", "flag", "invite", "10000",
                    "reject", "Bot已在目标群成员列表中", "IN", "VERIFIED", **kwargs,
                )
                self.assertFalse(outcome["attempted"])
                self.assertFalse(outcome["decision_succeeded"])
                self.assertEqual(bot.calls, [])

    async def test_verified_join_during_review_still_obeys_compensation_setting(self):
        fixture = flow.InviteFlowTests()
        for mode, state in (("notify_only", "UNEXPECTED_JOIN_NOTIFIED"),
                            ("leave", "UNEXPECTED_JOIN_LEFT"),
                            ("message_then_leave", "UNEXPECTED_JOIN_LEFT")):
            with self.subTest(mode=mode):
                plugin = fixture.make_plugin("reject", mode, ["OUT", "IN", "OUT"])
                bot, _, rec = await fixture.run_invite(plugin)
                self.assertEqual(rec["execution_state"], state)
                self.assertEqual(any(name == "set_group_leave" for name, _ in bot.calls),
                                 mode != "notify_only")
                self.assertFalse(any(name == "set_group_add_request" for name, _ in bot.calls))
