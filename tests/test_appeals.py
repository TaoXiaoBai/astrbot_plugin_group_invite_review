import types
import unittest
from unittest.mock import AsyncMock, Mock

import test_invite_flow as fixtures
from main import AdminCommandFilter, GroupInviteGuardPlugin


class AppealEvent(fixtures.FakeEvent):
    def __init__(self, text, sender="21000", group="", message_id=1, wake=True):
        super().__init__({
            "post_type": "message",
            "message_type": "group" if group else "private",
            "group_id": group,
            "self_id": 10000,
            "message_id": message_id,
        }, fixtures.FakeBot())
        self.text = text
        self.sender = sender
        self.group = group
        self.is_at_or_wake_command = wake
        self.unified_msg_origin = f"test:{'GroupMessage' if group else 'FriendMessage'}:{group or sender}"
        self.sent = []
        self.call_llm = True

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def get_self_id(self):
        return "10000"

    def should_call_llm(self, enabled):
        self.call_llm = enabled

    async def send(self, chain):
        self.sent.append(chain)

    def is_admin(self):
        return True


class AppealTests(unittest.IsolatedAsyncioTestCase):
    make_plugin = fixtures.InviteFlowTests.make_plugin

    def plugin(self, decision=None, banned=()):
        plugin = self.make_plugin()
        plugin._ask_blacklist_inquiry = AsyncMock(return_value=decision or {
            "is_inquiry": True,
            "is_appeal": True,
            "target_qq": "20000",
            "reply": "已转交管理员核实",
            "reason": "用户提交申诉",
        })
        plugin._is_user_banned = AsyncMock(
            side_effect=lambda qq: (str(qq) in set(banned), "fixture ban")
        )
        plugin._unban_text = AsyncMock()
        return plugin

    async def test_alt_account_private_appeal_is_recorded_without_auto_unban(self):
        plugin = self.plugin(banned=("20000",))
        event = AppealEvent("我换号了，替 20000 申诉", sender="21000")
        await plugin.on_private_blacklist_inquiry(event)
        self.assertTrue(event.stopped)
        self.assertFalse(event.call_llm)
        self.assertEqual(len(event.sent), 1)
        record = plugin._kv["appeal_records"][0]
        self.assertEqual(record["sender_id"], "21000")
        self.assertEqual(record["target_qq"], "20000")
        self.assertFalse(record["sender_banned"])
        self.assertTrue(record["target_banned_at_submission"])
        self.assertEqual(record["source"], "private")
        note = plugin._notify.await_args.args[1]
        self.assertIn("[解封申诉]", note)
        self.assertIn("原账号提交时已封禁：是", note)
        self.assertIn("/解封 <原QQ> 申诉", note)
        plugin._unban_text.assert_not_awaited()

    async def test_banned_sender_can_appeal_in_shared_group_before_ban_gate(self):
        plugin = self.plugin({
            "is_inquiry": True, "is_appeal": True, "target_qq": "",
            "reply": "", "reason": "本人申诉",
        }, banned=("20000",))
        event = AppealEvent("@bot 我要申诉，之前是误封", sender="20000", group="30000")
        await plugin.on_private_blacklist_inquiry(event)
        self.assertTrue(event.stopped)
        self.assertIn("申诉已收到", event.sent[0][0])
        record = plugin._kv["appeal_records"][0]
        self.assertEqual(record["target_qq"], "20000")
        self.assertEqual(record["source"], "group")
        self.assertEqual(record["group_id"], "30000")
        handler = GroupInviteGuardPlugin.on_private_blacklist_inquiry
        self.assertEqual(handler._event_priority, 10001)
        self.assertGreater(handler._event_priority,
                           GroupInviteGuardPlugin.on_blacklist_message_guard._event_priority)

    async def test_group_appeal_requires_wake_and_ordinary_chat_is_untouched(self):
        plugin = self.plugin()
        event = AppealEvent("大家讨论一下申诉流程", group="30000", wake=False)
        await plugin.on_private_blacklist_inquiry(event)
        self.assertFalse(event.stopped)
        plugin._ask_blacklist_inquiry.assert_not_awaited()
        self.assertNotIn("appeal_records", plugin._kv)

    async def test_alt_without_original_qq_is_asked_to_supply_it(self):
        plugin = self.plugin({
            "is_inquiry": True, "is_appeal": True, "target_qq": "",
            "reply": "已收到", "reason": "换号申诉",
        })
        event = AppealEvent("这是我的小号，我要申诉", sender="21000")
        await plugin.on_private_blacklist_inquiry(event)
        self.assertIn("补充要申诉的原 QQ 号", event.sent[0][0])
        self.assertEqual(plugin._kv["appeal_records"][0]["target_qq"], "")

    async def test_reason_inquiry_still_works_without_creating_appeal(self):
        plugin = self.plugin({
            "is_inquiry": True, "is_appeal": False,
            "reply": "原因已说明", "reason": "询问原因",
        })
        event = AppealEvent("为什么拉黑我", sender="20000")
        await plugin.on_private_blacklist_inquiry(event)
        self.assertTrue(event.stopped)
        self.assertNotIn("appeal_records", plugin._kv)
        self.assertIn("[黑名单/拒绝原因询问]", plugin._notify.await_args.args[1])

    async def test_duplicate_delivery_does_not_duplicate_record_or_notify(self):
        plugin = self.plugin(banned=("20000",))
        event = AppealEvent("替 20000 申诉", sender="21000", message_id=123)
        await plugin.on_private_blacklist_inquiry(event)
        await plugin.on_private_blacklist_inquiry(event)
        self.assertEqual(len(plugin._kv["appeal_records"]), 1)
        self.assertEqual(plugin._notify.await_count, 1)
        self.assertEqual(len(event.sent), 1)

    async def test_storage_failure_never_claims_appeal_was_saved(self):
        plugin = self.plugin()
        plugin._kv["appeal_records"] = {"invalid": True}
        event = AppealEvent("替 20000 申诉", sender="21000")
        await plugin.on_private_blacklist_inquiry(event)
        self.assertTrue(event.stopped)
        self.assertIn("保存失败", event.sent[0][0])
        self.assertIn("记录保存失败", plugin._notify.await_args.args[1])
        self.assertEqual(plugin._kv["appeal_records"], {"invalid": True})

    async def test_admin_can_query_appeal_records(self):
        plugin = self.plugin()
        event = AppealEvent("替 20000 申诉", sender="21000")
        await plugin.on_private_blacklist_inquiry(event)
        query = AppealEvent("/申诉记录")
        result = await plugin._dispatch_admin_command(query)
        self.assertIn("提交人 21000", result)
        self.assertIn("20000", result)
        self.assertIn("不代表已通过", result)
        self.assertTrue(AdminCommandFilter().filter(query, {}))

    async def test_admin_commands_are_not_captured_as_appeals(self):
        plugin = self.plugin()
        for text in ("/申诉记录", "/解封 20000 申诉", "/手动拉黑 QQ 20000"):
            event = AppealEvent(text)
            await plugin.on_private_blacklist_inquiry(event)
            self.assertFalse(event.stopped)
            self.assertFalse(event.sent)
        plugin._ask_blacklist_inquiry.assert_not_awaited()
        self.assertNotIn("appeal_records", plugin._kv)

    async def test_dealt_only_sender_is_reported_as_banned(self):
        plugin = self.plugin()
        plugin._resolve_persona_prompt = AsyncMock(return_value="persona")
        plugin._find_ban_entry = Mock(return_value=None)
        plugin._is_user_banned = AsyncMock(return_value=(True, "邀请记录已标记拉黑"))
        variables = await plugin._build_blacklist_inquiry_variables(
            "20000", "申诉", "test", "umo", "group", "30000"
        )
        self.assertEqual(variables["is_banned"], "是")
        self.assertEqual(variables["ban_reason"], "邀请记录已标记拉黑")
        self.assertEqual(variables["source"], "共同群")
        self.assertEqual(variables["group_id"], "30000")

    def test_legacy_keyword_config_still_accepts_appeals(self):
        plugin = self.plugin()
        plugin.config["private_intent"] = {"blacklist_inquiry_keywords": "自定义"}
        self.assertTrue(plugin._looks_like_blacklist_inquiry("申诉说明"))
        self.assertTrue(plugin._looks_like_blacklist_inquiry("误封申诉"))
        self.assertFalse(plugin._looks_like_blacklist_inquiry("为什么拉黑"))
        plugin.config["private_intent"]["blacklist_inquiry_keywords"] = ""
        self.assertTrue(plugin._looks_like_blacklist_inquiry("为什么拉黑"))


if __name__ == "__main__":
    unittest.main()
