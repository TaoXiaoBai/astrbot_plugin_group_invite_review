import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_invite_flow as flow
from main import AdminCommandFilter


class ProfileCommandCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, metadata=None):
        plugin = flow.InviteFlowTests.make_plugin(self)
        plugin.context = types.SimpleNamespace(get_registered_star=lambda name: metadata)
        plugin._profile_text = AsyncMock(return_value="守卫信息")
        return plugin

    def event(self, text="/画像 20000", *, handlers=(), plugins=None, admin=True):
        return types.SimpleNamespace(
            get_message_str=lambda: text,
            get_extra=lambda name, default=None: list(handlers),
            plugins_name=plugins,
            is_admin=lambda: admin,
            send=AsyncMock(), stop_event=lambda: self.fail("让位不应停止事件"),
        )

    def metadata(self, activated=True):
        return types.SimpleNamespace(activated=activated, star_cls=object(), module_path="profile.main")

    def handler(self):
        return types.SimpleNamespace(handler_module_path="profile.main")

    async def test_external_handler_yields_without_send_query_or_stop(self):
        plugin = self.make_plugin(self.metadata())
        event = self.event(handlers=[self.handler()])
        await plugin.on_admin_command(event)
        event.send.assert_not_awaited()
        plugin._profile_text.assert_not_awaited()

    async def test_missing_plugin_preserves_shared_command(self):
        plugin = self.make_plugin()
        event = self.event()
        await plugin.on_admin_command(event)
        plugin._profile_text.assert_awaited_once_with(event, ["20000"])
        event.send.assert_awaited_once()

    async def test_disabled_or_unloaded_plugin_preserves_shared_command(self):
        for metadata in [self.metadata(False), types.SimpleNamespace(activated=True, star_cls=None)]:
            with self.subTest(metadata=metadata):
                plugin = self.make_plugin(metadata)
                event = self.event(handlers=[self.handler()])
                await plugin.on_admin_command(event)
                event.send.assert_awaited_once()

    async def test_config_exclusion_preserves_shared_command(self):
        plugin = self.make_plugin(self.metadata())
        event = self.event(handlers=[self.handler()], plugins=["astrbot_plugin_group_invite_guard"])
        await plugin.on_admin_command(event)
        event.send.assert_awaited_once()

    async def test_session_filtered_handler_preserves_shared_command(self):
        plugin = self.make_plugin(self.metadata())
        event = self.event(handlers=[types.SimpleNamespace(handler_module_path="other.main")])
        await plugin.on_admin_command(event)
        event.send.assert_awaited_once()

    async def test_explicit_guard_command_ignores_external_plugin(self):
        plugin = self.make_plugin(self.metadata())
        event = self.event("/守卫画像 20000", handlers=[self.handler()])
        await plugin.on_admin_command(event)
        plugin._profile_text.assert_awaited_once_with(event, ["20000"])
        event.send.assert_awaited_once()

    async def test_detection_failure_yields_without_query(self):
        plugin = self.make_plugin()
        def broken(name):
            raise RuntimeError("registry unavailable")
        plugin.context.get_registered_star = broken
        event = self.event()
        await plugin.on_admin_command(event)
        event.send.assert_not_awaited()
        plugin._profile_text.assert_not_awaited()

    async def test_hot_load_state_is_not_cached(self):
        metadata = self.metadata()
        plugin = self.make_plugin(metadata)
        first = self.event(handlers=[self.handler()])
        await plugin.on_admin_command(first)
        metadata.activated = False
        second = self.event(handlers=[self.handler()])
        await plugin.on_admin_command(second)
        first.send.assert_not_awaited()
        second.send.assert_awaited_once()

    async def test_legacy_framework_checks_session_without_handler_list(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                plugin = self.make_plugin(self.metadata())
                event = self.event()
                event.get_extra = lambda name, default=None: default
                event.unified_msg_origin = "test:FriendMessage:20000"
                checker = AsyncMock(return_value=enabled)
                module = types.ModuleType("astrbot.core.star.session_plugin_manager")
                module.SessionPluginManager = types.SimpleNamespace(is_plugin_enabled_for_session=checker)
                with patch.dict(sys.modules, {module.__name__: module}):
                    await plugin.on_admin_command(event)
                checker.assert_awaited_once_with(event.unified_msg_origin, "astrbot_plugin_user_profile")
                self.assertEqual(event.send.await_count, 0 if enabled else 1)

    async def test_broken_session_check_does_not_duplicate_reply(self):
        plugin = self.make_plugin(self.metadata())
        event = self.event()
        event.get_extra = lambda name, default=None: default
        event.unified_msg_origin = "test:FriendMessage:20000"
        module = types.ModuleType("astrbot.core.star.session_plugin_manager")
        module.SessionPluginManager = types.SimpleNamespace(
            is_plugin_enabled_for_session=AsyncMock(side_effect=OSError("unavailable"))
        )
        with patch.dict(sys.modules, {module.__name__: module}):
            await plugin.on_admin_command(event)
        event.send.assert_not_awaited()
        plugin._profile_text.assert_not_awaited()

    async def test_shared_command_supports_hash_and_bare_prefixes(self):
        for text in ("#画像 20000", "画像 20000"):
            with self.subTest(text=text):
                plugin = self.make_plugin(self.metadata())
                event = self.event(text, handlers=[self.handler()])
                await plugin.on_admin_command(event)
                event.send.assert_not_awaited()

    def test_admin_filter_preserves_permissions_and_command_boundaries(self):
        matcher = AdminCommandFilter()
        for text in ["/画像 20000", "/守卫画像 20000", "画像 20000"]:
            self.assertTrue(matcher.filter(self.event(text), {}))
            self.assertFalse(matcher.filter(self.event(text, admin=False), {}))
        for text in ["/画像排名", "/画像扫描 20000", "/画像删除 20000"]:
            self.assertFalse(matcher.filter(self.event(text), {}))
