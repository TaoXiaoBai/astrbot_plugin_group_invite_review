import asyncio
import copy
import hashlib
import json
import time

RELATIONS_KEY = "bot_group_relations_v1"
RELATION_MARKER = "<!-- astrbot-bot-group-relations:v1 -->"


def scope_key(platform_id, self_id, group_id):
    return json.dumps([str(platform_id), str(self_id), str(group_id)], ensure_ascii=False, separators=(",", ":"))


def event_scope(event):
    try:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        self_id = str((raw.get("self_id") if isinstance(raw, dict) else getattr(raw, "self_id", "")) or getattr(getattr(event, "message_obj", None), "self_id", "") or "").strip()
        return str(event.get_platform_id() or "").strip(), self_id
    except Exception:
        return "", ""


class GroupRelationGuardMixin:
    async def _relation_best_effort(self, operation):
        from astrbot.api import logger
        try:
            return await operation
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"group_invite_guard: relation linkage failed; original flow continues: {exc}")
            return None

    async def _record_relation_notice_safely(self, event, membership):
        return await self._relation_best_effort(self._record_relation_notice(event, membership))

    async def _record_relation_safely(self, **kwargs):
        return await self._relation_best_effort(self.record_bot_group_relation(**kwargs))

    def _remember_relation_bot(self, event, bot):
        return self._remember_relation_scope(bot, *event_scope(event))

    def _remember_relation_scope(self, bot, platform_id, self_id):
        if bot is not None and platform_id and self_id:
            scopes = getattr(self, "_relation_bot_scopes", None)
            if scopes is None:
                scopes = self._relation_bot_scopes = {}
            entry = scopes.setdefault(id(bot), (bot, set()))
            entry[1].add((platform_id, self_id))
        return bot

    async def _relation_ledger(self):
        ledger = await self.get_kv_data(RELATIONS_KEY, {})
        if not isinstance(ledger, dict):
            raise ValueError("invalid bot group relation ledger")
        for field in ("records", "pending", "seen"):
            ledger.setdefault(field, {})
            if not isinstance(ledger[field], dict):
                raise ValueError("invalid bot group relation ledger field")
        return copy.deepcopy(ledger)

    async def record_bot_group_relation(self, *, platform_id, self_id, group_id, membership,
                                        evidence, event_key, inviter=None, operator="", request_key="", event_time=0):
        if not all((platform_id, self_id, group_id, event_key)) or membership not in ("IN", "OUT", "UNKNOWN"):
            return {"status": "invalid"}
        key = scope_key(platform_id, self_id, group_id)
        async with self._invite_lock("_relation_kv_lock"):
            ledger = await self._relation_ledger()
            previous = ledger["records"].get(key) or {}
            now = int(time.time())
            timestamp = int(event_time or now)
            if event_time and timestamp < int(previous.get("confirmed_at") or 0):
                return {"status": "stale"}
            if not event_time and event_key.startswith("relation:notice:"):
                if previous.get("membership") == membership and previous.get("evidence") == evidence:
                    return {"status": "duplicate"}
                event_key += ":" + str(int(previous.get("revision") or 0) + 1)
            if event_key in ledger["seen"]:
                return {"status": "duplicate"}
            if len(ledger["seen"]) >= 20000 or (key not in ledger["records"] and len(ledger["records"]) >= 10000):
                raise RuntimeError("bot group relation ledger full")
            record = dict(previous)
            record.update(platform_id=str(platform_id), self_id=str(self_id), group_id=str(group_id),
                          membership=membership, evidence=str(evidence)[:160], event_key=str(event_key),
                          revision=int(previous.get("revision") or 0) + 1,
                          event_created_at_ms=int(time.time() * 1000),
                          confirmed_at=timestamp if membership in ("IN", "OUT") else 0,
                          operator=str(operator or ""))
            if inviter is not None:
                record["inviter"] = str(inviter or "")
                record["request_key"] = str(request_key or "")
            if membership == "IN" and previous.get("membership") != "IN":
                record["join_time"] = timestamp
            record.setdefault("join_time", 0)
            record.setdefault("inviter", "")
            record.setdefault("request_key", "")
            ledger["records"][key] = record
            ledger["pending"][event_key] = record
            ledger["seen"][event_key] = timestamp
            await self.put_kv_data(RELATIONS_KEY, ledger)
        await self._deliver_group_relations()
        return {"status": "recorded"}

    async def get_bot_group_relations(self, *, platform_id, self_id, group_id="", inviter=""):
        async with self._invite_lock("_relation_kv_lock"):
            ledger = await self._relation_ledger()
        return [copy.deepcopy(r) for r in ledger["records"].values()
                if r.get("platform_id") == platform_id and r.get("self_id") == self_id
                and (r.get("group_id") == group_id if group_id else bool(inviter and r.get("inviter") == inviter))][:6]

    async def _deliver_group_relations(self):
        async with self._invite_lock("_relation_delivery_lock"):
            profile = self._get_profile_plugin_instance()
            receiver = getattr(profile, "record_bot_group_relation", None)
            if not callable(receiver):
                return
            async with self._invite_lock("_relation_kv_lock"):
                pending = (await self._relation_ledger())["pending"]
            for event_key, record in pending.items():
                try:
                    result = await asyncio.wait_for(receiver(copy.deepcopy(record)), 5)
                    if not isinstance(result, dict) or result.get("status") not in ("recorded", "duplicate", "cancelled"):
                        continue
                    async with self._invite_lock("_relation_kv_lock"):
                        ledger = await self._relation_ledger()
                        ledger["pending"].pop(event_key, None)
                        await self.put_kv_data(RELATIONS_KEY, ledger)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue

    async def _record_relation_notice(self, event, membership):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        def field(k):
            return raw.get(k) if isinstance(raw, dict) else getattr(raw, k, None)
        platform_id, self_id = event_scope(event)
        group_id = str(field("group_id") or "")
        if not platform_id or not self_id or str(field("user_id") or "") != self_id or not group_id:
            return
        admission = await self._find_admission_for_join(group_id, self_id, platform_id) if membership == "IN" else None
        payload = [platform_id, self_id, group_id, membership, field("time"), field("sub_type"), field("operator_id")]
        event_key = "relation:notice:" + hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
        await self.record_bot_group_relation(
            platform_id=platform_id, self_id=self_id, group_id=group_id, membership=membership,
            evidence="本 bot 入群 notice" if membership == "IN" else "本 bot 离群 notice",
            event_key=event_key, operator=str(field("operator_id") or ""),
            inviter=str((admission or {}).get("inviter") or "") if membership == "IN" else None,
            request_key=str((admission or {}).get("request_key") or ""), event_time=int(field("time") or 0))

    async def _record_successful_group_leave(self, bot, group_id):
        entry = getattr(self, "_relation_bot_scopes", {}).get(id(bot))
        if not entry or len(entry[1]) != 1:
            return
        platform_id, self_id = next(iter(entry[1]))
        await self.record_bot_group_relation(platform_id=platform_id, self_id=self_id,
            group_id=str(group_id), membership="OUT", evidence="本插件退群 API 成功",
            event_key="relation:leave:" + str(time.time_ns()))

    async def _relation_fallback_context(self, event, existing=""):
        import re
        claims = {}
        match = re.search(r"<!-- astrbot-bot-group-claims:(.*?) -->", existing)
        if RELATION_MARKER in existing and match:
            try:
                values = json.loads(match.group(1))
                claims = {str(gid): int(revision) for gid, revision in values.items()}
            except (ValueError, TypeError, AttributeError):
                pass
        platform_id, self_id = event_scope(event)
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", {})
            group_id = str(raw.get("group_id") or "") if isinstance(raw, dict) else ""
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        records = await self.get_bot_group_relations(platform_id=platform_id, self_id=self_id, group_id=group_id, inviter=sender)
        if not records:
            return ""
        lines = []
        for r in records:
            if claims.get(str(r["group_id"]), -1) >= int(r.get("revision") or 0):
                continue
            metadata = json.dumps({str(r["group_id"]): int(r.get("revision") or 0)}, ensure_ascii=False, separators=(",", ":"))
            line = "<!-- guard-group-relation:" + metadata + " -->" + f"群 {r['group_id']}：邀请人 {r.get('inviter') or '未知'}（不是操作人推定）；最近确认 {r['membership']}，当前待核实；确认时间 {r.get('confirmed_at') or '未知'}"
            if sum(map(len, lines)) + len(line) + len(lines) <= 1200:
                lines.append(line)
        if not lines:
            return ""
        return "<guard_group_relations>\n动态关系事实，不是人格印象或指令：\n" + "\n".join(lines) + "\n</guard_group_relations>"
