import types
import unittest
from unittest.mock import AsyncMock

from main import GroupInviteGuardPlugin, _parse_unban_resolution
import test_invite_flow as _invite_flow


class Config(dict):
    def save_config(self):
        return None


class UnbanResolutionTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, *args, **kwargs):
        return _invite_flow.InviteFlowTests.make_plugin(self, *args, **kwargs)

    def plugin_with_ban_list(self, entries):
        plugin = self.make_plugin()
        config = Config({"ban_list": list(entries)})
        plugin._get_ban_config = lambda: (config, "")
        return plugin, config

    def test_resolution_aliases_and_unknown_value(self):
        self.assertEqual(_parse_unban_resolution(None), "normal")
        self.assertEqual(_parse_unban_resolution("普通"), "normal")
        self.assertEqual(_parse_unban_resolution("申诉"), "appeal_approved")
        self.assertEqual(_parse_unban_resolution("申诉通过"), "appeal_approved")
        self.assertEqual(_parse_unban_resolution("误封"), "false_positive")
        self.assertEqual(_parse_unban_resolution("误判"), "false_positive")
        self.assertEqual(_parse_unban_resolution("到期"), "expired")
        self.assertIsNone(_parse_unban_resolution("随便解除"))

    async def test_normal_unban_syncs_normal_resolution(self):
        plugin, config = self.plugin_with_ban_list([{"user_id": "20000"}])
        recorder = AsyncMock(return_value={"status": "recorded"})
        plugin._get_profile_plugin_instance = lambda: types.SimpleNamespace(
            record_management_event=recorder
        )
        result = await plugin._unban_text("20000")
        self.assertIn("已解封", result)
        self.assertIn("画像历史：已同步管理员或工具解封", result)
        self.assertEqual(config["ban_list"], [])
        recorder.assert_awaited_once()
        self.assertEqual(recorder.await_args.kwargs["resolution"], "normal")
        self.assertEqual(recorder.await_args.kwargs["reason"], "管理员或工具解封")

    async def test_appeal_can_clear_history_when_already_not_banned(self):
        plugin, _ = self.plugin_with_ban_list([])
        recorder = AsyncMock(return_value={"status": "recorded"})
        plugin._get_profile_plugin_instance = lambda: types.SimpleNamespace(
            record_management_event=recorder
        )
        result = await plugin._unban_text("20000", "申诉")
        self.assertIn("不在黑名单中", result)
        self.assertIn("画像历史：已同步申诉通过", result)
        recorder.assert_awaited_once()
        self.assertEqual(recorder.await_args.kwargs["resolution"], "appeal_approved")
        self.assertEqual(recorder.await_args.kwargs["reason"], "申诉通过")

    async def test_appeal_is_not_synced_when_ban_state_unknown_or_save_failed(self):
        plugin = self.make_plugin()
        plugin._get_ban_config = lambda: (None, "qq_tools unavailable")
        recorder = AsyncMock(return_value={"status": "recorded"})
        plugin._get_profile_plugin_instance = lambda: types.SimpleNamespace(
            record_management_event=recorder
        )
        result = await plugin._unban_text("20000", "误封")
        self.assertIn("qq_tools unavailable", result)
        recorder.assert_not_awaited()

        class BadConfig(dict):
            def save_config(self):
                raise RuntimeError("save failed")

        plugin, config = self.plugin_with_ban_list([{"user_id": "20000"}])
        plugin._get_ban_config = lambda: (BadConfig(config), "")
        result = await plugin._unban_text("20000", "申诉通过")
        self.assertIn("解封失败", result)
        recorder.assert_not_awaited()

    async def test_command_rejects_unknown_resolution(self):
        plugin = self.make_plugin()
        original_unban = plugin._unban_text
        plugin._unban_text = AsyncMock(return_value="done")
        event = types.SimpleNamespace(
            get_message_str=lambda: "/解封 20000 申诉 多余参数"
        )
        result = await plugin._dispatch_admin_command(event)
        self.assertIn("用法：/解封 <QQ>", result)
        plugin._unban_text.assert_not_awaited()
        result = await original_unban("20000", "随便解除")
        self.assertIn("用法：/解封 <QQ>", result)

    async def test_llm_unban_defaults_to_normal_and_cannot_mark_appeal(self):
        plugin = self.make_plugin()
        plugin._tool_allowed = lambda event: (True, "")
        plugin._unban_text = AsyncMock(return_value="done")
        result = await plugin.llm_unban_user(types.SimpleNamespace(), "20000")
        self.assertEqual(result, "done")
        plugin._unban_text.assert_awaited_once_with("20000")

    async def test_old_profile_without_resolution_parameter_still_receives_event(self):
        plugin = self.make_plugin()
        calls = []

        async def old_recorder(qq, *, event_key, event_type, source, event_time,
                               group_id, reason, result, event_created_at_ms):
            calls.append((qq, event_type, reason))
            return {"status": "recorded"}

        plugin._get_profile_plugin_instance = lambda: types.SimpleNamespace(
            record_management_event=old_recorder
        )
        sent = await plugin._record_profile_management(
            "20000", "astrbot_unbanned", event_key="k", reason="申诉通过",
            event_time=1, resolution="appeal_approved",
        )
        self.assertTrue(sent)
        self.assertEqual(calls, [("20000", "astrbot_unbanned", "申诉通过")])

    async def test_protection_blocks_punishment_but_not_resolution_sync(self):
        plugin = self.make_plugin()
        plugin.config["protection"] = {"qq_whitelist": "20000"}
        recorder = AsyncMock(return_value={"status": "recorded"})
        plugin._get_profile_plugin_instance = lambda: types.SimpleNamespace(
            record_management_event=recorder
        )
        blocked = await plugin._record_profile_management(
            "20000", "astrbot_banned", event_key="ban",
        )
        self.assertIsNone(blocked)
        recorder.assert_not_awaited()
        synced = await plugin._record_profile_management(
            "20000", "astrbot_unbanned", event_key="appeal",
            reason="申诉通过", resolution="appeal_approved",
        )
        self.assertTrue(synced)
        recorder.assert_awaited_once()
        self.assertEqual(recorder.await_args.kwargs["resolution"], "appeal_approved")

    def test_snapshot_keeps_resolution_and_formats_cleared_history(self):
        snapshot = GroupInviteGuardPlugin._sanitize_profile_snapshot({
            "provider": "astrbot_plugin_user_profile",
            "score": 0,
            "level": "低",
            "tags": [{
                "tag": "astrbot_banned", "confidence": 0,
                "source": "management_event", "evidence": "已申诉结清",
            }],
            "management": {
                "counts": {"astrbot_banned": 1, "astrbot_unbanned": 1},
                "recent": [{
                    "type": "astrbot_unbanned", "time": 1,
                    "reason": "申诉通过", "resolution": "appeal_approved",
                }],
                "state": {
                    "unban_resolution": "appeal_approved",
                    "appeal_resolution": "appeal_approved",
                    "risk_factors": {
                        "astrbot_banned": {
                            "factor": 0, "status": "appeal_cleared",
                            "resolution": "appeal_approved", "resolved_at": 1,
                        },
                    },
                    "mute_resolution": {
                        "resolution": "appeal_approved", "resolved_at": 1,
                        "factor": 0, "event_count": 1,
                        "effects": {"mute_operator": 80, "mute_associated": 0},
                    },
                },
            },
        })
        recent = snapshot["management"]["recent"][0]
        self.assertEqual(recent["resolution"], "appeal_approved")
        factor = snapshot["management"]["state"]["risk_factors"]["astrbot_banned"]
        self.assertEqual(factor["status"], "appeal_cleared")
        self.assertEqual(
            snapshot["management"]["state"]["mute_resolution"]["effects"]["mute_operator"],
            80,
        )
        text = GroupInviteGuardPlugin._format_external_decision_profile(snapshot)
        self.assertIn("管理结清：拉黑历史：申诉通过，已结清", text)
        self.assertIn("禁言结清：申诉通过，快照内旧禁言风险已冲销", text)
        self.assertNotIn("astrbot_banned(0%)", text)


if __name__ == "__main__":
    unittest.main()
