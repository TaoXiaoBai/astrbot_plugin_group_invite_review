import json
import types
import unittest
from unittest.mock import AsyncMock

import test_invite_flow as fixtures
from main import GroupInviteGuardPlugin


class BotLoopMuteTests(unittest.IsolatedAsyncioTestCase):
    make_plugin = fixtures.InviteFlowTests.make_plugin
    make_mute_plugin = fixtures.InviteFlowTests.make_mute_plugin
    mute_raw = staticmethod(fixtures.InviteFlowTests.mute_raw)

    def plugin(self, decision=None, **config):
        plugin = self.make_mute_plugin("stay", mute_threshold=1, **config)
        plugin._ask_long_mute_review = AsyncMock(return_value=decision if decision is not None else {
            "action": "stay", "reason": "确认是 bot 间循环", "reply": "", "bot_loop": True,
        })
        return plugin

    async def test_confirmed_bot_loop_allows_exactly_two_hours(self):
        plugin = self.plugin()
        bot = fixtures.FakeBot()
        await plugin.on_group_mute(fixtures.FakeEvent(self.mute_raw(duration=7200), bot))
        plugin._ask_long_mute_review.assert_awaited_once()
        self.assertEqual(bot.calls, [])
        plugin._apply_mute_ban.assert_not_awaited()
        self.assertIn("机器人循环聊天禁言例外", plugin._notify.await_args.args[1])
        self.assertIn("累计次数：已保留", plugin._notify.await_args.args[1])
        self.assertEqual(len(plugin._kv["mute_counts_scoped_v1"]), 1)

    async def test_over_two_hours_keeps_existing_threshold_action(self):
        plugin = self.plugin()
        bot = fixtures.FakeBot()
        await plugin.on_group_mute(fixtures.FakeEvent(self.mute_raw(duration=7201), bot))
        plugin._ask_long_mute_review.assert_not_awaited()
        self.assertTrue(any(name == "set_group_leave" for name, _ in bot.calls))

    async def test_unconfirmed_or_legacy_response_cannot_use_exception(self):
        for decision in (
            {"action": "stay", "reason": "普通禁言", "reply": "", "bot_loop": False},
            {"action": "stay", "reason": "旧模型响应", "reply": ""},
            {"action": "leave", "reason": "字段不能单独放行", "reply": "", "bot_loop": True},
        ):
            with self.subTest(decision=decision):
                plugin = self.plugin(decision)
                bot = fixtures.FakeBot()
                await plugin.on_group_mute(fixtures.FakeEvent(self.mute_raw(duration=3600), bot))
                self.assertTrue(any(name == "set_group_leave" for name, _ in bot.calls))

    async def test_review_failure_or_disabled_switch_keeps_existing_action(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                plugin = self.plugin(long_mute_llm_review=not disabled)
                if disabled:
                    plugin._ask_long_mute_review = AsyncMock()
                else:
                    plugin._ask_long_mute_review = AsyncMock(side_effect=RuntimeError("down"))
                bot = fixtures.FakeBot()
                await plugin.on_group_mute(fixtures.FakeEvent(self.mute_raw(duration=3600), bot))
                if disabled:
                    plugin._ask_long_mute_review.assert_not_awaited()
                self.assertTrue(any(name == "set_group_leave" for name, _ in bot.calls))

    async def test_review_schema_and_bot_loop_type_are_strict(self):
        for raw_bot_loop, expected in ((True, True), ("true", False), (None, False)):
            with self.subTest(raw_bot_loop=raw_bot_loop):
                plugin = self.plugin()
                plugin.config["decision"]["llm_provider_id"] = "provider"
                plugin._resolve_persona_prompt = AsyncMock(return_value="persona")
                plugin._build_invite_context = AsyncMock(return_value=("bot 循环对话证据", "", {}))
                plugin.context = types.SimpleNamespace(llm_generate=AsyncMock(
                    return_value=types.SimpleNamespace(completion_text=json.dumps({
                        "action": "stay", "reason": "复核", "reply": "", "bot_loop": raw_bot_loop,
                    }))
                ))
                decision = await GroupInviteGuardPlugin._ask_long_mute_review(
                    plugin, "", "30000", "20000", 7200, 1, fixtures.FakeBot()
                )
                self.assertIs(decision["bot_loop"], expected)
                kwargs = plugin.context.llm_generate.await_args.kwargs
                self.assertIn("不超过 7200", kwargs["system_prompt"])
                self.assertIn("无休止地相互回复", kwargs["system_prompt"])
                self.assertEqual(json.loads(kwargs["prompt"])["duration_seconds"], 7200)


if __name__ == "__main__":
    unittest.main()
