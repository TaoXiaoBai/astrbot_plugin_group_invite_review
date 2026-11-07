import copy
import json
import types
import unittest
from unittest.mock import AsyncMock

try:
    import test_invite_flow as fixtures
except ModuleNotFoundError:
    from tests import test_invite_flow as fixtures

from main import GroupInviteGuardPlugin


class ProfileDecisionPayloadTests(unittest.TestCase):
    sanitize = staticmethod(GroupInviteGuardPlugin._sanitize_profile_snapshot)
    render = staticmethod(GroupInviteGuardPlugin._format_external_decision_profile)

    def test_guard_invitation_descriptions_do_not_displace_people_or_risk_tags(self):
        guard_tags = [
            {"tag": tag, "source": "guard", "confidence": 1}
            for _ in range(20)
            for tag in ("inviter", "frequent_inviter", "invite_rejected")
        ]
        for tag in guard_tags:
            self.assertFalse(GroupInviteGuardPlugin._is_profile_risk_tag(tag))
        people = [{"tag": "thoughtful", "source": "llm", "confidence": 0.3}]
        risk = {"tag": "scam_suspect", "source": "llm", "confidence": 0.05}
        for risks in ([], [risk]):
            with self.subTest(risks=risks):
                snapshot = self.sanitize({"tags": guard_tags + people + risks})
                self.assertLessEqual(len(snapshot["tags"]), 12)
                names = [item["tag"] for item in snapshot["tags"]]
                self.assertIn("thoughtful", names)
                text = self.render(snapshot)
                self.assertIn("thoughtful", text)
                self.assertNotIn("inviter", text)
                if risks:
                    self.assertEqual(names[0], "scam_suspect")
                    self.assertIn("scam_suspect", text)

    def test_legacy_review_status_requires_explicit_completed_review(self):
        cases = (
            ({"score": 20}, "unreviewed"),
            ({"score": None}, "unreviewed"),
            ({"score": 20, "risk_status": "ready"}, "unreviewed"),
            ({"score": 20, "review_pending": False}, "ready"),
            ({"score": 0, "review_pending": False}, "ready"),
            ({"score": 20, "review_pending": True, "risk_status": "ready"}, "unreviewed"),
            ({"score": 20, "review_pending": 0}, "unreviewed"),
            ({"score": None, "review_pending": False}, "unreviewed"),
            ({"score": None, "risk_status": "no_evidence"}, "no_evidence"),
            ({"score": 20, "risk_status": "no_evidence"}, "unreviewed"),
            ({"score": 20, "risk_status": "stale"}, "unreviewed"),
        )
        for schema in ({}, {"schema_version": 4}):
            for payload, expected in cases:
                with self.subTest(schema=schema, payload=payload):
                    snapshot = self.sanitize({**schema, **payload})
                    self.assertEqual(snapshot["risk_status"], expected)
                    self.assertEqual("已完成当前审查" in self.render(snapshot), expected == "ready")
                    self.assertEqual(snapshot["score"], payload["score"])

    def test_new_schema_trusts_valid_status_and_defaults_to_unreviewed(self):
        for schema in (5, 6):
            for status in ("ready", "stale", "unreviewed", "no_evidence", "invalid", None):
                with self.subTest(schema=schema, status=status):
                    snapshot = self.sanitize({
                        "schema_version": schema, "score": 20,
                        "review_pending": False, "risk_status": status,
                    })
                    self.assertEqual(snapshot["risk_status"], status if status in (
                        "ready", "stale", "unreviewed", "no_evidence",
                    ) else "unreviewed")
        self.assertEqual(self.sanitize({"schema_version": 5, "score": 20})["risk_status"], "unreviewed")

    def test_long_term_stability_fields_are_bounded_and_refs_are_dropped(self):
        raw = {"long_term_traits": [
            {"trait": "耐心", "confidence": 0.9, "reason": "多次表现一致",
             "stability": "established", "support_count": 12, "last_confirmed": "123",
             "refs": ["RAW-SECRET"], "evidence_refs": ["RAW-SECRET"], "days": ["RAW-SECRET"]},
            {"trait": "直率", "stability": "tentative", "support_count": 10**12,
             "last_confirmed": -1},
            {"trait": "谨慎", "stability": "invalid", "support_count": -10,
             "last_confirmed": float("inf")},
            {"trait": "旧特点", "confidence": 0.5, "reason": "历史摘要"},
        ]}
        before = copy.deepcopy(raw)
        snapshot = self.sanitize(raw)
        self.assertEqual(raw, before)
        traits = snapshot["long_term_traits"]
        self.assertEqual(traits[0]["stability"], "established")
        self.assertEqual(traits[0]["support_count"], 12)
        self.assertEqual(traits[0]["last_confirmed"], 123)
        self.assertEqual(traits[1]["support_count"], 1000000)
        self.assertEqual(traits[1]["last_confirmed"], 0)
        self.assertEqual(traits[2]["stability"], "tentative")
        self.assertEqual(traits[2]["support_count"], 0)
        self.assertEqual(traits[2]["last_confirmed"], 0)
        self.assertNotIn("stability", traits[3])
        text = self.render(snapshot)
        self.assertIn("耐心(90%)：多次表现一致[跨日稳定]", text)
        self.assertIn("直率(0%)[待复核]", text)
        self.assertIn("旧特点(50%)：历史摘要[待复核]", text)
        serialized = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        for forbidden in ("refs", "days", "RAW-SECRET"):
            self.assertNotIn(forbidden, serialized + text)

    def test_positive_dimension_scores_render_without_tags_or_refs(self):
        snapshot = self.sanitize({"risk_dimensions": {
            "content_boundary": {"score": 25.5, "episodes": 2, "evidence_refs": ["RAW-SECRET"]},
            "harassment": {"score": 0, "episodes": 0},
            "fraud": {"score": 40, "episodes": 3},
            "unknown": {"score": 90, "episodes": 9},
        }})
        text = self.render(snapshot)
        self.assertIn("风险维度：内容边界 25.5/100（独立事件 2 次）；骚扰 0/100（独立事件 0 次）；欺诈 40/100（独立事件 3 次）", text)
        self.assertNotIn("画像标签：", text)
        for forbidden in ("evidence_refs", "RAW-SECRET", "unknown"):
            self.assertNotIn(forbidden, text)
        missing = self.render(self.sanitize({"risk_dimensions": {"fraud": {"score": 1}}}))
        self.assertIn("内容边界 未知（独立事件 0 次）", missing)
        self.assertIn("欺诈 1/100", missing)
        for dimensions in ({}, {"fraud": {"score": 0}}, {"fraud": {"score": None}},
                           {"unknown": {"score": 99}}, {"fraud": {"score": float("nan")}}):
            with self.subTest(dimensions=dimensions):
                self.assertNotIn("风险维度：", self.render(self.sanitize({"risk_dimensions": dimensions})))

    def test_low_confidence_risks_survive_high_confidence_interests(self):
        interests = [
            {"tag": f"interest_{i}", "source": "llm", "confidence": 0.99}
            for i in range(20)
        ]
        risks = [
            {"tag": tag, "source": "llm", "confidence": 0.1,
             "severity": "severe", "evidence": "风险摘要"}
            for tag in (
                "spam_suspect", "ad_suspect", "troll", "nsfw_tendency",
                "political_sensitive", "scam_suspect",
            )
        ]
        snapshot = self.sanitize({"tags": interests + risks})
        self.assertEqual(len(snapshot["tags"]), 12)
        self.assertEqual([item["tag"] for item in snapshot["tags"][:6]],
                         [item["tag"] for item in risks])
        text = self.render(snapshot)
        for risk in risks:
            self.assertIn(risk["tag"], text)
        self.assertIn("[严重]：风险摘要", text)
        self.assertIn("interest_0", text)
        self.assertLess(text.index("spam_suspect"), text.index("interest_0"))

    def test_management_risks_have_priority_without_reintroducing_guard_tags(self):
        tags = [{"tag": f"hobby_{i}", "confidence": 1} for i in range(20)]
        tags += [
            {"tag": "astrbot_banned", "source": "management_event", "confidence": 0.1},
            {"tag": "mute_operator", "source": "mute", "confidence": 0.2},
            {"tag": "bot_mute_operator", "source": "mute_event", "confidence": 0.1},
            {"tag": "bot_mute_associated", "source": "mute_event", "confidence": 0.1},
            {"tag": "ban_history", "source": "ban_list", "confidence": 0.1},
            {"tag": "management_risk", "source": "management", "confidence": 0.1},
            {"tag": "guard_risk", "source": "guard", "confidence": 1},
        ]
        snapshot = self.sanitize({"tags": tags})
        self.assertIn("ban_history", [item["tag"] for item in snapshot["tags"]])
        text = self.render(snapshot)
        for tag in ("astrbot_banned", "mute_operator", "bot_mute_operator",
                    "bot_mute_associated", "management_risk", "hobby_0"):
            self.assertIn(tag, text)
        self.assertNotIn("guard_risk", text)
        self.assertNotIn("ban_history", text)

    def test_review_fields_and_severity_are_preserved(self):
        raw = {
            "schema_version": 5, "score": None, "risk_status": "stale", "review_pending": 1,
            "semantic_status": "cached_success", "semantic_time": "123",
            "dimension_score": 35.5, "dimension_status": "evidence",
            "tags": [{"tag": "troll", "severity": "clear", "reason": "重复挑衅摘要"}],
        }
        before = copy.deepcopy(raw)
        snapshot = self.sanitize(raw)
        self.assertEqual(raw, before)
        for key in ("risk_status", "semantic_status", "dimension_score", "dimension_status"):
            self.assertEqual(snapshot[key], raw[key])
        self.assertIs(snapshot["review_pending"], True)
        self.assertEqual(snapshot["semantic_time"], 123)
        self.assertEqual(snapshot["tags"][0]["severity"], "clear")
        self.assertIn("[明确]：重复挑衅摘要", self.render(snapshot))
        self.assertIn("沿用上次成功画像/待复核", self.render(snapshot))

    def test_dimensions_and_sections_use_only_whitelisted_summary_fields(self):
        raw = {
            "risk_dimensions": {
                "content_boundary": {"score": 42, "episodes": 3, "persistence": 1.3,
                                     "evidence_refs": ["RAW-SECRET"], "quote": "RAW-SECRET"},
                "harassment": {"score": None, "episodes": 1, "persistence": 1},
                "fraud": {"score": 0, "episodes": 0, "persistence": 0},
                "unknown": {"score": 99, "evidence_refs": ["RAW-SECRET"]},
            },
            "sections": {"群聊表现": "行为摘要", "quote": "RAW-SECRET",
                         "私聊表现": {"evidence_refs": ["RAW-SECRET"]}},
            "tags": [{"tag": "troll", "evidence_refs": ["RAW-SECRET"]}],
            "evidence_refs": ["RAW-SECRET"],
        }
        snapshot = self.sanitize(raw)
        self.assertEqual(set(snapshot["risk_dimensions"]), {"content_boundary", "harassment", "fraud"})
        dimension = snapshot["risk_dimensions"]["content_boundary"]
        self.assertEqual(dimension, {"score": 42, "episodes": 3, "persistence": 1.3})
        self.assertEqual(snapshot["sections"], {"群聊表现": "行为摘要"})
        serialized = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("evidence_refs", serialized)
        self.assertNotIn("RAW-SECRET", serialized)

    def test_new_numeric_fields_are_bounded_and_serializable(self):
        for value in (None, True, "bad", float("inf"), float("nan")):
            with self.subTest(value=value):
                snapshot = self.sanitize({
                    "dimension_score": value, "semantic_time": value,
                    "risk_dimensions": {"fraud": {"score": value, "episodes": value,
                                                   "persistence": value}},
                    "tags": [{"tag": "troll", "severity": "invalid", "confidence": value}],
                })
                self.assertIsNone(snapshot["dimension_score"])
                self.assertIsNone(snapshot["risk_dimensions"]["fraud"]["score"])
                self.assertNotIn("severity", snapshot["tags"][0])
                self.assertGreaterEqual(snapshot["semantic_time"], 0)
                json.dumps(snapshot, allow_nan=False)
        snapshot = self.sanitize({"dimension_score": 150, "semantic_time": -10,
                                  "risk_dimensions": {"fraud": {"score": -1, "episodes": 10**10}}})
        self.assertEqual(snapshot["dimension_score"], 100)
        self.assertEqual(snapshot["semantic_time"], 0)
        self.assertEqual(snapshot["risk_dimensions"]["fraud"]["score"], 0)
        self.assertEqual(snapshot["risk_dimensions"]["fraud"]["episodes"], 100000)

    def test_legacy_payload_remains_usable(self):
        raw = {
            "score": 23, "level": "低", "impression": "乐于交流",
            "traits": ["热心"], "tags": [{"tag": "gaming", "confidence": 0.8}],
            "activity": {"group_messages": 8},
            "social_origin": {"friend_relation": "friend"},
            "management": {"counts": {"astrbot_banned": 1}},
        }
        snapshot = self.sanitize(raw)
        self.assertEqual(snapshot["score"], 23)
        text = self.render(snapshot)
        for expected in ("23/100", "gaming(80%)", "乐于交流", "热心", "群聊 8", "当前确认是好友", "守卫成功拉黑 1 次"):
            self.assertIn(expected, text)
        self.assertEqual(snapshot["sections"], {})
        self.assertEqual(self.sanitize({"unexpected": "value"}), {})

    def test_none_score_is_unknown_and_unreviewed_score_is_not_reviewed(self):
        for score in (None, 0, 45):
            with self.subTest(score=score):
                text = self.render(self.sanitize({"score": score, "risk_status": "unreviewed"}))
                self.assertIn("只有规则/历史资料、尚未完成当前语义审查", text)
                self.assertNotIn("已完成当前审查", text)
                if score is None:
                    self.assertIn("风险 未知（资料不足，不等于安全）", text)
                    self.assertNotIn("None/100", text)
                else:
                    self.assertIn(f"{score}/100", text)
        for status, expected in (("ready", "已完成当前审查"), ("no_evidence", "无画像证据")):
            self.assertIn(expected, self.render(self.sanitize({"schema_version": 5, "risk_status": status})))

    def test_fused_sections_are_bounded_in_snapshot_and_context(self):
        sections = {key: key + "摘要" * 400 for key in (
            "群聊表现", "私聊表现", "共通特征", "场景差异", "置信信息",
        )}
        snapshot = self.sanitize({"risk_scope": "fused", "sections": sections})
        self.assertEqual(set(snapshot["sections"]), set(sections))
        for value in snapshot["sections"].values():
            self.assertLessEqual(len(value), 240)
        text = self.render(snapshot)
        self.assertIn("群私融合证据", text)
        for key in ("群聊表现", "私聊表现", "场景差异"):
            prefix = f"{key}（画像摘要）："
            line = next(line for line in text.splitlines() if line.startswith(prefix))
            self.assertLessEqual(len(line.removeprefix(prefix)), 240)
        self.assertNotIn("共通特征（画像摘要）", text)
        direct = self.render({"sections": sections})
        for line in direct.splitlines():
            if "（画像摘要）：" in line:
                self.assertLessEqual(len(line.split("（画像摘要）：", 1)[1]), 240)


class ProfileDecisionPayloadIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_to_llm_context_keeps_risk_review_and_scene_summaries(self):
        plugin = fixtures.InviteFlowTests.make_plugin(self)
        getter = AsyncMock(return_value={
            "score": None, "risk_status": "unreviewed", "review_pending": True,
            "risk_scope": "fused", "sections": {"群聊表现": "克制", "私聊表现": "主动"},
            "tags": [{"tag": "interest", "confidence": 1}] * 15 + [
                {"tag": "scam_suspect", "confidence": 0.1, "severity": "mild",
                 "evidence": "交易疑点摘要", "evidence_refs": ["RAW-SECRET"]},
            ],
        })
        profile = types.SimpleNamespace(get_decision_profile=getter)
        plugin.context = types.SimpleNamespace(
            get_registered_star=lambda name: types.SimpleNamespace(star_cls=profile),
        )
        text, snapshot = await plugin._fetch_external_decision_profile(
            "20000", exclude_request_key="current-request",
        )
        getter.assert_awaited_once_with("20000", None, exclude_request_key="current-request")
        plugin._default_provider_id = lambda *_: "provider"
        plugin._resolve_persona_prompt = AsyncMock(return_value="")
        plugin._build_invite_context = AsyncMock(return_value=(text, "", snapshot))
        plugin.context.llm_generate = AsyncMock(return_value=types.SimpleNamespace(
            completion_text='{"action":"approve","reason":"ok","reply":"hi"}',
        ))
        decision = await GroupInviteGuardPlugin._ask_llm(
            plugin, "20000", "30000", "hello", request_key="current-request",
        )
        prompt = plugin.context.llm_generate.await_args.kwargs["prompt"]
        for expected in ("尚未完成当前语义审查", "scam_suspect", "[轻度]", "群聊表现（画像摘要）：克制", "私聊表现（画像摘要）：主动"):
            self.assertIn(expected, prompt)
        self.assertEqual(decision["action"], "approve")
        self.assertEqual(decision["_profile_snapshot"], snapshot)
        serialized = json.dumps(decision["_profile_snapshot"], ensure_ascii=False)
        self.assertNotIn("evidence_refs", serialized)
        self.assertNotIn("RAW-SECRET", prompt + serialized)
        self.assertEqual(plugin.put_calls, [])
