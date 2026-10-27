import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_invite_flow as fixtures


class ActorPolicySnapshotTests(unittest.IsolatedAsyncioTestCase):
    make_plugin = fixtures.InviteFlowTests.make_plugin

    def setup_snapshot(self):
        plugin = self.make_plugin()
        config = {"ban_list": []}
        plugin._get_ban_config = Mock(return_value=(config, ""))
        return plugin, config

    async def snapshot(self, plugin, **kwargs):
        return await plugin.get_actor_policy_snapshot(
            "20000", platform_id="test", self_id="10000", **kwargs
        )

    async def test_clear_contract_and_scope(self):
        plugin, _ = self.setup_snapshot()
        with patch("main.time.time", return_value=1234567890.5):
            result = await self.snapshot(plugin, group_id="30000")
        self.assertEqual(set(result), {
            "schema_version", "provider", "captured_at", "scope", "blacklist",
            "protection", "availability", "partial_errors",
        })
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["provider"], "astrbot_plugin_group_invite_guard")
        self.assertEqual(result["captured_at"], 1234567890.5)
        self.assertEqual(result["scope"], {
            "qq": "20000", "platform_id": "test", "self_id": "10000", "group_id": "30000",
        })
        self.assertEqual(result["blacklist"], {"status": "clear", "sources": [], "reason": ""})
        self.assertEqual(result["availability"], "ready")
        self.assertEqual(result["partial_errors"], [])
        self.assertEqual(result["protection"], {
            "status": "ready", "qq_protected": False, "group_protected": False,
            "admin_protected": False,
        })

    async def test_active_legacy_ids_and_reason_bounded(self):
        for field in ("user_id", "qq", "id", "uin"):
            with self.subTest(field=field):
                plugin, config = self.setup_snapshot()
                config["ban_list"] = [{field: "20000", "reason": "secret\n" * 100}]
                result = await self.snapshot(plugin)
                self.assertEqual(result["blacklist"]["status"], "banned")
                self.assertEqual(result["blacklist"]["sources"], ["qq_tools"])
                self.assertLessEqual(len(result["blacklist"]["reason"]), 200)
                self.assertNotIn("\n", result["blacklist"]["reason"])

    async def test_expiry_and_invalid_duration(self):
        for fields, status in (
            ({"duration": -1}, "banned"),
            ({"duration": 20, "ban_time": 90}, "banned"),
            ({"duration": 10, "ban_time": 90}, "clear"),
            ({"duration": 9, "ban_time": 90}, "clear"),
            ({"duration": 0, "ban_time": 90}, "clear"),
            ({"duration": "bad"}, "unknown"),
            ({"duration": True}, "unknown"),
            ({"duration": float("nan")}, "unknown"),
            ({"duration": -2}, "unknown"),
            ({"duration": 20, "ban_time": "bad"}, "unknown"),
            ({"duration": 20, "ban_time": True}, "unknown"),
        ):
            with self.subTest(fields=fields):
                plugin, config = self.setup_snapshot()
                config["ban_list"] = [{"user_id": "20000", **fields}]
                with patch("main.time.time", return_value=100):
                    result = await self.snapshot(plugin)
                self.assertEqual(result["blacklist"]["status"], status)

    async def test_dealt_is_global_not_current_group_and_not_expired_with_ban(self):
        plugin, config = self.setup_snapshot()
        config["ban_list"] = [{"user_id": "20000", "duration": 1, "ban_time": 1}]
        plugin._kv["invite_records"] = {
            "99999": [{"inviter": "20000", "dealt": True, "platform_id": "other", "self_id": "42"}],
        }
        result = await self.snapshot(plugin, group_id="30000")
        self.assertEqual(result["blacklist"]["status"], "banned")
        self.assertEqual(result["blacklist"]["sources"], ["dealt"])
        config["ban_list"] = [{"user_id": "20000"}]
        result = await self.snapshot(plugin)
        self.assertEqual(result["blacklist"]["sources"], ["qq_tools", "dealt"])

    async def test_missing_qq_tools_never_clear_and_dealt_banned_wins(self):
        plugin, _ = self.setup_snapshot()
        del plugin._get_ban_config
        plugin.context.get_registered_star = Mock(return_value=None)
        result = await self.snapshot(plugin)
        self.assertEqual(result["blacklist"]["status"], "unknown")
        self.assertEqual(result["availability"], "partial")
        plugin._kv["invite_records"] = {"30000": {"inviter": "20000", "dealt": "true"}}
        result = await self.snapshot(plugin)
        self.assertEqual(result["blacklist"]["status"], "banned")
        self.assertEqual(result["blacklist"]["sources"], ["dealt"])

    async def test_read_failures_and_malformed_data_are_unknown(self):
        class BrokenConfig(dict):
            def get(self, *args):
                raise OSError("sensitive error must not leak")

        for config in (BrokenConfig(), {}, {"ban_list": None}, {"ban_list": [None]}, {"ban_list": [{}]}):
            plugin, _ = self.setup_snapshot()
            plugin._get_ban_config.return_value = config, ""
            result = await self.snapshot(plugin)
            self.assertEqual(result["blacklist"]["status"], "unknown")
            self.assertNotIn("sensitive", str(result))
        for records in (None, [], {"30000": None}, {"30000": [None]}, {"30000": {"dealt": True}}):
            plugin, _ = self.setup_snapshot()
            plugin._kv["invite_records"] = records
            self.assertEqual((await self.snapshot(plugin))["blacklist"]["status"], "unknown")
        plugin, config = self.setup_snapshot()
        plugin.get_kv_data = AsyncMock(side_effect=OSError("private path"))
        self.assertEqual((await self.snapshot(plugin))["blacklist"]["status"], "unknown")
        config["ban_list"] = [{"user_id": "20000"}]
        result = await self.snapshot(plugin)
        self.assertEqual(result["blacklist"]["status"], "banned")
        self.assertEqual(result["availability"], "partial")
        self.assertNotIn("private path", str(result))

    async def test_complete_unavailability(self):
        plugin, _ = self.setup_snapshot()
        plugin._get_ban_config.side_effect = OSError("failed")
        plugin.get_kv_data = AsyncMock(side_effect=OSError("failed"))
        plugin.config = None
        result = await self.snapshot(plugin)
        self.assertEqual(result["blacklist"]["status"], "unknown")
        self.assertEqual(result["protection"]["status"], "unknown")
        self.assertEqual(result["availability"], "unavailable")
        self.assertEqual(len(result["partial_errors"]), 3)

    async def test_protection_flags_independent_and_do_not_override_ban(self):
        plugin, config = self.setup_snapshot()
        config["ban_list"] = [{"user_id": "20000"}]
        plugin.config["protection"] = {"qq_whitelist": "20000", "group_whitelist": "30000"}
        plugin.context.get_config = lambda: {"admins_id": ["20000"]}
        result = await self.snapshot(plugin, group_id="30000")
        self.assertEqual(result["protection"], {
            "status": "ready", "qq_protected": True, "group_protected": True,
            "admin_protected": True,
        })
        self.assertEqual(result["blacklist"]["status"], "banned")
        plugin.config["protection"]["protect_astrbot_admins"] = False
        plugin.context.get_config = Mock(side_effect=AssertionError("must not read admins"))
        result = await self.snapshot(plugin)
        self.assertFalse(result["protection"]["admin_protected"])
        plugin.context.get_config.assert_not_called()
        plugin.config["protection"]["qq_whitelist"] = "200001"
        self.assertFalse((await self.snapshot(plugin))["protection"]["qq_protected"])

    async def test_protection_read_error_is_not_reported_ready(self):
        plugin, _ = self.setup_snapshot()
        plugin.context.get_config = Mock(side_effect=OSError("secret"))
        result = await self.snapshot(plugin)
        self.assertEqual(result["protection"]["status"], "unknown")
        self.assertEqual(result["availability"], "partial")
        self.assertEqual(result["blacklist"]["status"], "clear")
        self.assertNotIn("secret", str(result))
        plugin.config["protection"] = {"protect_astrbot_admins": "true"}
        self.assertEqual((await self.snapshot(plugin))["protection"]["status"], "unknown")

    async def test_read_only_fresh_data_no_cache_or_protocol(self):
        plugin, config = self.setup_snapshot()
        plugin._kv["invite_records"] = {"30000": [{"inviter": "20000", "dealt": True}]}
        plugin._derived_cache = {"banned_inviters": (9999999999, {"99999"})}
        before = copy.deepcopy((plugin.config, config, plugin._kv, plugin._derived_cache))
        for name in ("_is_user_banned", "_find_ban_entry", "_protection_reason", "_get_qq_tools_instance"):
            setattr(plugin, name, Mock(side_effect=AssertionError(name)))
        for name in ("put_kv_data", "_mark_inviter_dealt", "_manual_blacklist", "_save_ban_config", "_ask_llm", "_notify"):
            setattr(plugin, name, AsyncMock(side_effect=AssertionError(name)))
        result = await self.snapshot(plugin)
        self.assertEqual(result["blacklist"]["status"], "banned")
        self.assertEqual(before, (plugin.config, config, plugin._kv, plugin._derived_cache))
        self.assertEqual(plugin.get_calls, ["invite_records"])
        plugin._kv["invite_records"] = {}
        result["scope"]["qq"] = "changed"
        result["blacklist"]["sources"].append("changed")
        self.assertEqual((await self.snapshot(plugin))["blacklist"]["status"], "clear")
        for name in ("put_kv_data", "_mark_inviter_dealt", "_manual_blacklist", "_save_ban_config", "_ask_llm", "_notify"):
            getattr(plugin, name).assert_not_awaited()

    async def test_malformed_record_cannot_hide_confirmed_ban(self):
        plugin, config = self.setup_snapshot()
        config["ban_list"] = [None, {"user_id": "20000"}]
        plugin._kv["invite_records"] = {"30000": [None, {"inviter": "20000", "dealt": True}]}
        result = await self.snapshot(plugin)
        self.assertEqual(result["blacklist"]["status"], "banned")
        self.assertEqual(result["blacklist"]["sources"], ["qq_tools", "dealt"])
        self.assertEqual(result["availability"], "partial")
        self.assertEqual(result["partial_errors"], [
            "qq_tools_unavailable_or_invalid", "dealt_unavailable_or_invalid",
        ])
        config["ban_list"] = []
        plugin._kv["invite_records"] = {"30000": {"inviter": "20000", "dealt": "unexpected"}}
        self.assertEqual((await self.snapshot(plugin))["blacklist"]["status"], "unknown")

    async def test_legacy_unmarked_invite_records_are_readable(self):
        plugin, _ = self.setup_snapshot()
        plugin._kv["invite_records"] = {
            "30000": "20000", "30001": {"inviter": "20000"},
            "30002": [{"inviter": "20000", "dealt": False}],
        }
        result = await self.snapshot(plugin)
        self.assertEqual(result["blacklist"]["status"], "clear")
        self.assertEqual(result["availability"], "ready")

    async def test_cancellation_propagates(self):
        plugin, _ = self.setup_snapshot()
        plugin.get_kv_data = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.snapshot(plugin)

    async def test_invalid_actor_not_clear(self):
        plugin, _ = self.setup_snapshot()
        result = await plugin.get_actor_policy_snapshot("", platform_id="test", self_id="10000")
        self.assertEqual(result["blacklist"]["status"], "unknown")
        self.assertIn("scope_invalid", result["partial_errors"])
