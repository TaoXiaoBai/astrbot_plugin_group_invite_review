import unittest
from unittest.mock import AsyncMock

import test_invite_flow as fixtures
from test_blacklist_safety import Config
from main import ActionResult


class ManualBanReasonTests(unittest.IsolatedAsyncioTestCase):
    make_plugin = fixtures.InviteFlowTests.make_plugin

    async def test_reason_syntax_and_legacy_compatibility(self):
        cases = [
            (["QQ", "20000", "恶意邀请", "持续骚扰"], "恶意邀请 持续骚扰", False),
            (["群", "30000", "恶意邀请"], "恶意邀请", True),
            (["20000", "30000", "反复骚扰"], "反复骚扰", True),
            (["20000", "反复骚扰"], "反复骚扰", True),
            (["QQ", "20000", "12345"], "12345", False),
            (["20000", "30000"], "手动拉黑", True),
            (["QQ", "20000"], "手动拉黑", False),
        ]
        for args, reason, leaves in cases:
            with self.subTest(args=args):
                p = self.make_plugin()
                fixtures.seed_relation(p, inviter="20000")
                p._manual_blacklist = AsyncMock(return_value=ActionResult("done", persisted=True))
                bot = fixtures.ApiOnly()
                event = fixtures.FakeEvent({"self_id": 10000}, bot)
                result = await p._manual_ban_text(event, args)
                expected_group = "30000" if args[0] == "群" or args[:2] == ["20000", "30000"] else ""
                p._manual_blacklist.assert_awaited_once_with("20000", bot, group_id=expected_group, reason=reason)
                # QQ form also leaves the inviter's confirmed groups.
                self.assertTrue(any(action == "set_group_leave" for action, _ in bot.calls))
                self.assertIn("原因：" + reason, result)

    async def test_reason_reaches_blacklist_profile_and_not_private_notice(self):
        p = self.make_plugin()
        config = Config(ban_list=[])
        p._get_ban_config = lambda: (config, "")
        p._record_profile_management = AsyncMock(return_value=True)
        p.config["kick_revenge"] = {"ban_notice_message": "自定义通知，不公开内部原因"}
        bot = fixtures.ApiOnly()
        reason = "内部审核：恶意邀请"
        event = fixtures.FakeEvent({"self_id": 10000}, bot)
        event.get_message_str = lambda: "/手动拉黑 QQ 20000 " + reason
        result = await p._dispatch_admin_command(event)
        self.assertIn(reason, result)
        self.assertEqual(config["ban_list"][0]["reason"], reason)
        self.assertEqual([call.args[1] for call in p._record_profile_management.await_args_list],
                         ["bot_deleted_friend", "astrbot_banned"])
        self.assertTrue(all(call.kwargs["reason"] == reason for call in p._record_profile_management.await_args_list))
        notice = next(args["message"] for action, args in bot.calls if action == "send_private_msg")
        self.assertEqual(notice, "自定义通知，不公开内部原因")

    async def test_invalid_or_oversized_reason_has_no_actions(self):
        for args in ([], ["QQ"], ["QQ", "invalid", "原因"], ["QQ", "20000", "长" * 501]):
            p = self.make_plugin()
            bot = fixtures.ApiOnly()
            p._manual_blacklist = AsyncMock()
            result = await p._manual_ban_text(fixtures.FakeEvent({"self_id": 10000}, bot), args)
            self.assertTrue("用法" in result or "过长" in result)
            self.assertEqual(bot.calls, [])
            p._manual_blacklist.assert_not_awaited()

    async def test_ambiguous_target_with_reason_still_refuses_guessing(self):
        p = self.make_plugin()
        fixtures.seed_relation(p, inviter="20000")
        fixtures.seed_relation(p, group="40000", inviter="30000")
        bot = fixtures.ApiOnly()
        result = await p._manual_ban_text(fixtures.FakeEvent({"self_id": 10000}, bot), ["30000", "恶意邀请"])
        self.assertIn("歧义", result)
        self.assertEqual(bot.calls, [])
