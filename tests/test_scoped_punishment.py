import asyncio
import unittest

import test_invite_flow as fixtures
import test_blacklist_safety as safety
from test_blacklist_safety import Config
from group_relations import scope_key


class ScopedPunishmentTests(unittest.IsolatedAsyncioTestCase):
    make_plugin = fixtures.InviteFlowTests.make_plugin
    make_mute_plugin = fixtures.InviteFlowTests.make_mute_plugin
    setup_direct = safety.BlacklistSafetyTests.setup_direct

    async def wait_for_lock_waiter(self, lock):
        async def waiting():
            while not lock._waiters:
                await asyncio.sleep(0)
        await asyncio.wait_for(waiting(), 1)

    async def test_direct_checks_switch_after_qqtools_lock_wait(self):
        for section, setting in (("basic", "enable"), ("mute_revenge", "profile_mute_direct_blacklist")):
            with self.subTest(section=section):
                p, config, _ = self.setup_direct()
                lock = p._invite_lock("_qq_tools_lock")
                await lock.acquire()
                task = asyncio.create_task(p._run_direct_job("old"))
                try:
                    await self.wait_for_lock_waiter(lock)
                    p.config[section][setting] = False
                finally:
                    lock.release()
                await asyncio.wait_for(task, 1)
                self.assertEqual(config["ban_list"], [])
                self.assertEqual(p._kv["mute_event_jobs"]["old"]["direct_targets"][0]["state"], "pending")
                p.config[section][setting] = True
                await p._run_direct_job("old")
                self.assertEqual(config["ban_list"][0]["user_id"], "20000")

    async def test_explicit_admin_ban_not_disabled_by_automatic_guard(self):
        p, config, _ = self.setup_direct()
        p.config["basic"]["enable"] = False
        result = await p._ban_inviter("20000", "explicit")
        self.assertTrue(result.persisted)

    async def test_external_join_approve_keeps_explicit_inviter_after_final_save(self):
        for decision in ("approve", "reject", "unknown"):
            with self.subTest(decision=decision):
                p = self.make_plugin(decision, membership=["OUT", "IN"])
                await p.on_group_invite(fixtures.FakeEvent(fixtures.InviteFlowTests.raw(), fixtures.FakeBot()))
                records = await p.get_bot_group_relations(platform_id="test", self_id="10000", group_id="30000")
                self.assertEqual(records[0]["membership"], "IN")
                self.assertEqual(records[0]["inviter"], "20000" if decision == "approve" else "")
                if decision == "approve":
                    rec = fixtures.InviteFlowTests.record(p)
                    self.assertEqual(rec["execution_state"], "EXTERNAL_JOIN_APPROVED")

    def mute_event(self, bot, *, platform="test", self_id="10000", event_time=100):
        raw = fixtures.InviteFlowTests.mute_raw(self_id=self_id, user_id=self_id, time=event_time)
        event = fixtures.FakeEvent(raw, bot)
        event.get_platform_id = lambda: platform
        return event

    async def test_same_group_two_bots_and_platforms_have_separate_counts_and_dedupe(self):
        p = self.make_mute_plugin(mute_threshold=2, profile_mute_enable=False)
        a, b, c = fixtures.FakeBot(), fixtures.FakeBot(), fixtures.FakeBot()
        await p.on_group_mute(self.mute_event(a))
        await p.on_group_mute(self.mute_event(b, self_id="10001"))
        await p.on_group_mute(self.mute_event(c, platform="other"))
        expected = {scope_key("test", "10000", "30000"): 1,
                    scope_key("test", "10001", "30000"): 1,
                    scope_key("other", "10000", "30000"): 1}
        self.assertEqual(p._kv["mute_counts_scoped_v1"], expected)
        self.assertFalse(a.calls or b.calls or c.calls)
        await p.on_group_mute(self.mute_event(a, event_time=101))
        expected.pop(scope_key("test", "10000", "30000"))
        self.assertEqual(p._kv["mute_counts_scoped_v1"], expected)
        self.assertEqual([name for name, _ in a.calls], ["set_group_leave"])
        self.assertFalse(b.calls or c.calls)

    async def test_scope_a_clear_and_restore_never_changes_b(self):
        p = self.make_mute_plugin(mute_threshold=2, profile_mute_enable=False)
        a = scope_key("test", "10000", "30000")
        b = scope_key("test", "10001", "30000")
        p._kv["mute_counts_scoped_v1"] = {a: 1, b: 9}
        bot = fixtures.FakeBot()
        bot.fail_action = "set_group_leave"
        await p.on_group_mute(self.mute_event(bot))
        self.assertEqual(p._kv["mute_counts_scoped_v1"], {a: 2, b: 9})
        self.assertTrue(await p._clear_mute_count(a, ("fixture",)))
        self.assertEqual(p._kv["mute_counts_scoped_v1"], {b: 9})
        await p._restore_mute_count(a, 3)
        self.assertEqual(p._kv["mute_counts_scoped_v1"], {a: 3, b: 9})

    async def test_scoped_locks_do_not_block_other_bot_same_group(self):
        p = self.make_mute_plugin(mute_threshold=1, profile_mute_enable=False)
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowBot(fixtures.FakeBot):
            async def set_group_leave(inner, **params):
                entered.set()
                await release.wait()
        a, b = SlowBot(), fixtures.FakeBot()
        task = asyncio.create_task(p.on_group_mute(self.mute_event(a)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(p.on_group_mute(self.mute_event(b, self_id="10001")), 1)
            self.assertFalse(task.done())
            self.assertEqual([name for name, _ in b.calls], ["set_group_leave"])
        finally:
            release.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(p._mute_group_locks, {})

    async def test_legacy_counts_remain_history_and_do_not_trigger_current_threshold(self):
        p = self.make_mute_plugin(mute_threshold=3, profile_mute_enable=False)
        p._kv["mute_records"] = {"30000": 99}
        p._kv["invite_records"] = {"30000": [{"inviter": "20000"}]}
        fixtures.seed_relation(p, inviter="20000")
        bot = fixtures.FakeBot()
        await p.on_group_mute(self.mute_event(bot))
        self.assertFalse(bot.calls)
        self.assertEqual(p._kv["mute_records"], {"30000": 99})
        self.assertEqual(p._kv["mute_counts_scoped_v1"], {scope_key("test", "10000", "30000"): 1})
        evidence = await p.get_inviter_evidence("20000")
        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(evidence["group_mute_context"], {"30000": 99})
        self.assertEqual(evidence["group_mute_context_scope"], "legacy_unscoped_history")
        self.assertEqual(evidence["scoped_mute_context"], [dict(platform_id="test", self_id="10000", group_id="30000", count=1)])
        data = await p._collect_profile_data("20000")
        self.assertEqual(data["mute_total"], 99)
        self.assertEqual(data["scoped_mute_context"][0]["count"], 1)

    async def test_missing_event_scope_cannot_trigger_threshold(self):
        for platform, self_id in (("", "10000"), ("test", "")):
            p = self.make_mute_plugin(mute_threshold=1, profile_mute_enable=False)
            bot = fixtures.FakeBot()
            await p.on_group_mute(self.mute_event(bot, platform=platform, self_id=self_id))
            self.assertFalse(bot.calls)
            self.assertNotIn("mute_counts_scoped_v1", p._kv)
            p._apply_mute_ban.assert_not_awaited()

    async def test_llm_ban_respects_actual_group_and_private_context(self):
        for group, protected in (("30000", True), ("30001", False), ("", False)):
            p = self.make_plugin()
            p.config["protection"] = {"group_whitelist": "30000"}
            p._tool_allowed = lambda event: (True, "")
            config = Config(ban_list=[])
            p._get_ban_config = lambda: (config, "")
            event = fixtures.FakeEvent({"group_id": group}, fixtures.FakeBot())
            result = await p.llm_ban_user(event, "20000")
            self.assertEqual(result.protected, protected)
            self.assertEqual(result.persisted, not protected)
            self.assertEqual(len(config["ban_list"]), int(not protected))

    async def test_llm_group_protection_rechecked_after_lock_wait(self):
        p = self.make_plugin()
        p._tool_allowed = lambda event: (True, "")
        config = Config(ban_list=[])
        p._get_ban_config = lambda: (config, "")
        lock = p._invite_lock("_qq_tools_lock")
        await lock.acquire()
        task = asyncio.create_task(p.llm_ban_user(fixtures.FakeEvent({"group_id": "30000"}, fixtures.FakeBot()), "20000"))
        try:
            await self.wait_for_lock_waiter(lock)
            p.config["protection"] = {"group_whitelist": "30000"}
        finally:
            lock.release()
        result = await asyncio.wait_for(task, 1)
        self.assertTrue(result.protected)
        self.assertEqual(config["ban_list"], [])

    async def test_query_cannot_overwrite_leave_or_kick_during_reply(self):
        for notice in ("leave", "kick_me"):
            with self.subTest(notice=notice):
                p = self.make_plugin("approve", membership=["OUT", "IN"])
                entered, release = asyncio.Event(), asyncio.Event()
                class ReplyBot(fixtures.FakeBot):
                    async def send_private_msg(inner, **params):
                        entered.set()
                        await release.wait()
                bot = ReplyBot()
                event = fixtures.FakeEvent(fixtures.InviteFlowTests.raw(), bot)
                task = asyncio.create_task(p.on_group_invite(event))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    lifecycle = fixtures.FakeEvent(dict(post_type="notice", notice_type="group_decrease",
                        sub_type=notice, group_id=30000, user_id=10000, self_id=10000, operator_id=40000), bot)
                    handler = p.on_group_leave_lifecycle if notice == "leave" else p.on_group_kick
                    await asyncio.wait_for(handler(lifecycle), 1)
                    current = await p.get_bot_group_relations(platform_id="test", self_id="10000", group_id="30000")
                    self.assertEqual(current[0]["membership"], "OUT")
                    revision = current[0]["revision"]
                finally:
                    release.set()
                    await asyncio.wait_for(task, 1)
                record = fixtures.InviteFlowTests.record(p)
                self.assertEqual(record["execution_state"], "EXTERNAL_JOIN_APPROVED")
                current = await p.get_bot_group_relations(platform_id="test", self_id="10000", group_id="30000")
                self.assertEqual(current[0]["membership"], "OUT")
                self.assertEqual(current[0]["revision"], revision)
                self.assertEqual(await p._danger_relations(event=event), [])

    async def test_initial_membership_query_uses_revision_from_before_network(self):
        p = self.make_plugin("unknown")
        fixtures.seed_relation(p, inviter="20000")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def membership(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return "IN"
            return "UNKNOWN"
        p._membership_state.side_effect = membership
        bot = fixtures.FakeBot()
        event = fixtures.FakeEvent(fixtures.InviteFlowTests.raw(), bot)
        task = asyncio.create_task(p.on_group_invite(event))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(p.on_group_leave_lifecycle(fixtures.FakeEvent(dict(
                post_type="notice", notice_type="group_decrease", sub_type="leave",
                group_id=30000, user_id=10000, self_id=10000), bot)), 1)
        finally:
            release.set()
            await asyncio.wait_for(task, 1)
        current = await p.get_bot_group_relations(platform_id="test", self_id="10000", group_id="30000")
        self.assertEqual(current[0]["membership"], "OUT")
        self.assertEqual(current[0]["revision"], 2)
        self.assertEqual(await p._danger_relations(event=event), [])

    async def test_relation_cas_rejects_changes_even_when_membership_returns_to_same_state(self):
        p = self.make_plugin()
        fixtures.seed_relation(p, inviter="20000")
        revision = await p._relation_revision("test", "10000", "30000")
        for state in ("OUT", "IN"):
            result = await p.record_bot_group_relation(platform_id="test", self_id="10000", group_id="30000",
                membership=state, evidence="lifecycle", event_key="lifecycle:" + state, inviter="21000")
            self.assertEqual(result["status"], "recorded")
        before = await p.get_bot_group_relations(platform_id="test", self_id="10000", group_id="30000")
        result = await p.record_bot_group_relation(platform_id="test", self_id="10000", group_id="30000",
            membership="IN", evidence="old query", event_key="old-query", inviter="20000", expected_revision=revision)
        self.assertEqual(result["status"], "stale")
        after = await p.get_bot_group_relations(platform_id="test", self_id="10000", group_id="30000")
        self.assertEqual(after, before)
