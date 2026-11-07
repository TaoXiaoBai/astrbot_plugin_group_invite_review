import asyncio
import types
import unittest
from unittest.mock import AsyncMock

import test_invite_flow as fixtures
from main import GroupInviteGuardPlugin


def response(text="ok", role="ai"):
    return types.SimpleNamespace(completion_text=text, role=role)


def config_with_fallback(ids, legacy=None):
    return {
        "agent_runner": {"config": {"model": {"fallback_provider_ids": ids}}},
        "provider_settings": {"fallback_chat_models": legacy or []},
    }


class GuardLLMFallbackTests(unittest.IsolatedAsyncioTestCase):
    make_plugin = fixtures.InviteFlowTests.make_plugin

    def plugin(self, config, side_effect):
        plugin = self.make_plugin()
        plugin.context = types.SimpleNamespace(
            get_config=lambda **kwargs: config,
            llm_generate=AsyncMock(side_effect=side_effect),
        )
        return plugin

    async def test_primary_failure_uses_fallback_in_order_and_deduplicates(self):
        plugin = self.plugin(
            config_with_fallback(["backup1", "primary", "backup2", "backup1"]),
            [RuntimeError("primary down"), response("approved")],
        )
        result = await plugin._llm_generate_with_fallback(
            "primary", "prompt", "test:GroupMessage:30000", "persona"
        )
        self.assertEqual(result.completion_text, "approved")
        calls = plugin.context.llm_generate.await_args_list
        self.assertEqual([call.kwargs["chat_provider_id"] for call in calls], ["primary", "backup1"])
        for call in calls:
            self.assertEqual(call.kwargs["prompt"], "prompt")
            self.assertEqual(call.kwargs["system_prompt"], "persona")

    async def test_error_role_and_empty_output_trigger_next_provider(self):
        plugin = self.plugin(
            config_with_fallback(["backup1", "backup2"]),
            [response("error", role="err"), response("  "), response("valid")],
        )
        result = await plugin._llm_generate_with_fallback("primary", "prompt")
        self.assertEqual(result.completion_text, "valid")
        self.assertEqual(plugin.context.llm_generate.await_count, 3)

    async def test_success_does_not_call_fallback(self):
        plugin = self.plugin(config_with_fallback(["backup"]), [response("ok")])
        await plugin._llm_generate_with_fallback("primary", "prompt")
        self.assertEqual(plugin.context.llm_generate.await_count, 1)

    async def test_all_failures_raise_last_error(self):
        plugin = self.plugin(
            config_with_fallback(["backup"]), [RuntimeError("first"), TimeoutError("last")]
        )
        with self.assertRaisesRegex(TimeoutError, "last"):
            await plugin._llm_generate_with_fallback("primary", "prompt")
        self.assertEqual(plugin.context.llm_generate.await_count, 2)

    async def test_cancellation_is_not_treated_as_provider_failure(self):
        plugin = self.plugin(
            config_with_fallback(["backup"]), [asyncio.CancelledError()]
        )
        with self.assertRaises(asyncio.CancelledError):
            await plugin._llm_generate_with_fallback("primary", "prompt")
        self.assertEqual(plugin.context.llm_generate.await_count, 1)

    async def test_session_scope_and_legacy_config(self):
        calls = []
        config = {
            "agent_runner": {"config": {"model": {}}},
            "provider_settings": {"fallback_chat_models": ["legacy"]},
        }
        def get_config(**kwargs):
            calls.append(kwargs)
            return config
        plugin = self.make_plugin()
        plugin.context = types.SimpleNamespace(get_config=get_config)
        self.assertEqual(plugin._fallback_provider_ids("test:GroupMessage:30000"), ["legacy"])
        self.assertEqual(calls, [{"umo": "test:GroupMessage:30000"}])

    async def test_new_empty_list_precedes_legacy_and_invalid_lists_are_ignored(self):
        plugin = self.make_plugin()
        plugin.context = types.SimpleNamespace(
            get_config=lambda: config_with_fallback([], ["legacy"])
        )
        self.assertEqual(plugin._fallback_provider_ids(), [])
        plugin.context.get_config = lambda: config_with_fallback("invalid", ["legacy"])
        self.assertEqual(plugin._fallback_provider_ids(), [])
        plugin.context = types.SimpleNamespace()
        self.assertEqual(plugin._fallback_provider_ids(), [])

    async def test_real_invite_review_uses_fallback_without_losing_persona(self):
        plugin = self.make_plugin()
        plugin.config["decision"]["llm_provider_id"] = "primary"
        plugin._resolve_persona_prompt = AsyncMock(return_value="persona")
        plugin._build_invite_context = AsyncMock(return_value=("context", "", {}))
        plugin.context = types.SimpleNamespace(
            get_config=lambda **kwargs: config_with_fallback(["backup"]),
            llm_generate=AsyncMock(side_effect=[
                RuntimeError("down"),
                response('{"action":"approve","reason":"ok","reply":"hello"}'),
            ]),
        )
        decision = await GroupInviteGuardPlugin._ask_llm(
            plugin, "20000", "30000", "hello", history_umo="test:GroupMessage:30000"
        )
        self.assertEqual(decision["action"], "approve")
        calls = plugin.context.llm_generate.await_args_list
        self.assertEqual([call.kwargs["chat_provider_id"] for call in calls], ["primary", "backup"])
        self.assertTrue(calls[1].kwargs["system_prompt"].startswith("persona"))


if __name__ == "__main__":
    unittest.main()
