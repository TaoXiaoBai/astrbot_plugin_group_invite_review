import asyncio
import inspect
import json
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register


def _get_value(raw: Any, key: str, default: Any = None) -> Any:
    try:
        return raw.get(key, default)
    except Exception:
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "是")


def _parse_json(text: str) -> dict:
    if not text:
        return {"action": "unknown", "reason": ""}
    match = re.search(r"\{.*\}", text, re.S)
    candidate = match.group(0) if match else text
    try:
        return json.loads(candidate)
    except Exception:
        return {"action": "unknown", "reason": text[:200]}


_INVITE_LINK_MARKS = (
    "qm.qq.com",
    "jq.qq.com",
    "qun.qq.com",
    "group/invite",
    "join_group",
)

_INVITE_KEYWORDS = (
    "拉你进群", "拉你进", "拉你入群", "拉我进群", "拉我进", "拉我入群",
    "进群", "入群", "加群", "加个群", "加下群", "加一下群",
    "邀请你进群", "邀请你进", "邀请你加群", "邀请你加",
    "邀你进群", "邀你加群", "邀我进群", "邀我加群",
    "能不能加群", "可以加群吗", "能加群吗", "想拉你进群", "想拉你",
    "邀请链接", "群邀请", "进我们群", "来我们群", "来群里",
)


class GroupInviteRequestFilter(filter.CustomFilter):
    """只匹配 OneBot 加群邀请请求（post_type=request, request_type=group, sub_type=invite）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return False
        return (
            _get_value(raw, "post_type") == "request"
            and _get_value(raw, "request_type") == "group"
            and _get_value(raw, "sub_type") == "invite"
        )


class PrivateInviteIntentFilter(filter.CustomFilter):
    """只匹配私聊消息（post_type=message, message_type=private）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return False
        return (
            _get_value(raw, "post_type") == "message"
            and _get_value(raw, "message_type") == "private"
        )


class GroupKickFilter(filter.CustomFilter):
    """只匹配机器人被踢出群的通知（post_type=notice, notice_type=group_decrease, sub_type=kick/kick_me，且被移出的是机器人自己）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return False
        if (
            _get_value(raw, "post_type") != "notice"
            or _get_value(raw, "notice_type") != "group_decrease"
        ):
            return False
        sub_type = str(_get_value(raw, "sub_type") or "")
        if sub_type not in ("kick", "kick_me"):
            return False
        user_id = str(_get_value(raw, "user_id") or "")
        self_id = str(_get_value(raw, "self_id") or "")
        return bool(user_id and self_id and user_id == self_id)


class GroupMuteFilter(filter.CustomFilter):
    """只匹配机器人被禁言的通知（post_type=notice, notice_type=group_ban, sub_type=ban，且被禁言的是机器人自己）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return False
        if (
            _get_value(raw, "post_type") != "notice"
            or _get_value(raw, "notice_type") != "group_ban"
        ):
            return False
        if str(_get_value(raw, "sub_type") or "") != "ban":
            return False
        user_id = str(_get_value(raw, "user_id") or "")
        self_id = str(_get_value(raw, "self_id") or "")
        return bool(user_id and self_id and user_id == self_id)


@register(
    "astrbot_plugin_group_invite_guard",
    "Kimi",
    "加群邀请自动处理：LLM 判断是否同意，支持自动同意/拒绝或仅通知管理员；私聊问能否加群/发邀请链接也会被识别",
    "1.1.0",
)
class GroupInviteGuardPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}

    @filter.custom_filter(GroupInviteRequestFilter)
    async def on_group_invite(self, event: AstrMessageEvent):
        # 先阻止这条空的 request 消息继续进入 LLM 回复阶段
        try:
            event.stop_event()
        except Exception:
            pass

        if not self.config.get("enable", True):
            return

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return

        inviter_qq = str(_get_value(raw, "user_id") or "")
        group_id = str(_get_value(raw, "group_id") or "")
        comment = str(_get_value(raw, "comment") or "")
        flag = str(_get_value(raw, "flag") or "")
        sub_type = str(_get_value(raw, "sub_type") or "invite")

        if not flag:
            logger.warning("group_invite_guard: request event missing flag, skip")
            return

        bot = self._find_onebot_client(event)
        platform_id = None
        try:
            platform_id = event.get_platform_id()
        except Exception:
            pass

        try:
            decision = await self._ask_llm(inviter_qq, group_id, comment, bot, platform_id)
        except Exception as exc:
            logger.error(f"group_invite_guard: LLM decision failed: {exc}")
            decision = {"action": "unknown", "reason": f"LLM error: {exc}"}

        action = str(decision.get("action") or "unknown").strip().lower()
        reason = str(decision.get("reason") or "").strip()

        if bot is None:
            logger.error("group_invite_guard: no OneBot client found")
            return

        if action == "approve" and self.config.get("auto_approve", False):
            try:
                await self._call_action(
                    bot,
                    "set_group_add_request",
                    flag=flag,
                    sub_type=sub_type,
                    approve=True,
                    reason="",
                )
                logger.info(
                    f"group_invite_guard: approved invite from {inviter_qq} to group {group_id}"
                )
                # 自动同意成功后，记录群号 -> 邀请人，供被踢后报复使用
                try:
                    records = await self.get_kv_data("invite_records", {})
                    if not isinstance(records, dict):
                        records = {}
                    records[str(group_id)] = str(inviter_qq)
                    await self.put_kv_data("invite_records", records)
                    logger.info(
                        f"group_invite_guard: recorded inviter {inviter_qq} for group {group_id}"
                    )
                except Exception as exc:
                    logger.warning(f"group_invite_guard: record inviter failed: {exc}")
            except Exception as exc:
                logger.error(f"group_invite_guard: approve failed: {exc}")
        elif action == "reject" and self.config.get("auto_reject", False):
            try:
                await self._call_action(
                    bot,
                    "set_group_add_request",
                    flag=flag,
                    sub_type=sub_type,
                    approve=False,
                    reason=reason or "bot 自动拒绝",
                )
                logger.info(
                    f"group_invite_guard: rejected invite from {inviter_qq} to group {group_id}"
                )
            except Exception as exc:
                logger.error(f"group_invite_guard: reject failed: {exc}")

        note = self._compose_note(inviter_qq, group_id, comment, action, reason)
        await self._notify(bot, note)

    @filter.custom_filter(PrivateInviteIntentFilter)
    async def on_private_invite_intent(self, event: AstrMessageEvent):
        if not self.config.get("enable", True):
            return
        if not self.config.get("enable_private_intent", True):
            return

        text = (event.get_message_str() or "").strip()
        if not text:
            return

        # 粗筛：明显不是加群意图就交还给正常私聊，不额外调 LLM
        if not self._looks_like_invite_intent(text):
            return

        platform_id = None
        try:
            platform_id = event.get_platform_id()
        except Exception:
            pass

        try:
            decision = await self._ask_private_intent(text, platform_id)
        except Exception as exc:
            logger.error(f"group_invite_guard: private intent LLM failed: {exc}")
            return

        if not _as_bool(decision.get("is_invite_intent")):
            return

        # 确认是加群意图，接管这条私聊，阻止默认 LLM 回复
        try:
            event.stop_event()
            event.should_call_llm(False)
        except Exception:
            pass

        sender_id = event.get_sender_id() or ""
        reply = str(decision.get("reply") or "").strip()
        reason = str(decision.get("reason") or "").strip()

        if self.config.get("private_intent_reply", True) and reply:
            try:
                await event.send(MessageChain(chain=[Plain(reply)]))
            except Exception as exc:
                logger.error(f"group_invite_guard: reply private failed: {exc}")

        note = self._compose_private_note(sender_id, text, reply, reason)
        bot = self._find_onebot_client(event)
        if bot is None:
            logger.error("group_invite_guard: no OneBot client for private intent notify")
            return
        if self.config.get("private_intent_notify", True):
            await self._notify(bot, note)

    @filter.custom_filter(GroupKickFilter)
    async def on_group_kick(self, event: AstrMessageEvent):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return

        group_id = str(_get_value(raw, "group_id") or "")
        if not group_id:
            return

        mode = str(self.config.get("revenge_mode", "off") or "off").strip().lower()
        if mode not in ("delete_friend", "delete_and_ban"):
            return

        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load invite_records failed: {exc}")
            records = {}
        inviter_qq = records.get(group_id) if isinstance(records, dict) else None
        if not inviter_qq:
            logger.info(
                f"group_invite_guard: no inviter record for group {group_id}, skip revenge"
            )
            return

        bot = self._find_onebot_client(event)
        if bot is None:
            logger.error("group_invite_guard: no OneBot client for revenge")
            return

        result = await self._take_revenge(inviter_qq, bot)
        logger.info(
            f"group_invite_guard: revenge on {inviter_qq} for group {group_id}: {result}"
        )

        if self.config.get("revenge_notify", True):
            note = self._compose_revenge_note(group_id, inviter_qq, result)
            await self._notify(bot, note)

    @filter.custom_filter(GroupMuteFilter)
    async def on_group_mute(self, event: AstrMessageEvent):
        if not self.config.get("mute_retaliation_enable", False):
            return

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return

        group_id = str(_get_value(raw, "group_id") or "")
        operator_id = str(_get_value(raw, "operator_id") or "")
        if not group_id:
            return

        try:
            records = await self.get_kv_data("mute_records", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load mute_records failed: {exc}")
            records = {}
        if not isinstance(records, dict):
            records = {}

        count = int(records.get(group_id, 0) or 0) + 1
        records[group_id] = count
        try:
            await self.put_kv_data("mute_records", records)
        except Exception as exc:
            logger.warning(f"group_invite_guard: save mute_records failed: {exc}")

        threshold = int(self.config.get("mute_threshold", 3) or 3)

        bot = self._find_onebot_client(event)
        if bot is None:
            logger.error("group_invite_guard: no OneBot client for mute retaliation")
            return

        if count < threshold:
            if self.config.get("mute_notify", True):
                note = (
                    f"[被禁言通知]\n"
                    f"群号：{group_id}\n"
                    f"操作者 QQ：{operator_id or '(未知)'}\n"
                    f"累计被禁言：第 {count} 次（阈值 {threshold}）"
                )
                await self._notify(bot, note)
            return

        # 达到阈值：退群
        try:
            await self._call_action(
                bot, "set_group_leave", group_id=int(group_id), is_dismiss=False
            )
            leave_result = f"已退出群 {group_id}"
        except Exception as exc:
            leave_result = f"退群失败：{exc}"

        # 收集拉黑目标
        target = str(self.config.get("mute_target", "operator") or "operator").strip().lower()
        targets = []
        if target in ("operator", "both") and operator_id:
            targets.append(operator_id)
        if target in ("inviter", "both"):
            try:
                invite_records = await self.get_kv_data("invite_records", {})
            except Exception as exc:
                logger.warning(f"group_invite_guard: load invite_records failed: {exc}")
                invite_records = {}
            inviter_qq = invite_records.get(group_id) if isinstance(invite_records, dict) else None
            inviter_qq = str(inviter_qq or "").strip()
            if inviter_qq:
                targets.append(inviter_qq)

        # 去重、去空，逐一对目标执行拉黑
        seen = set()
        ban_results = []
        for qq in targets:
            qq = str(qq or "").strip()
            if not qq or qq in seen:
                continue
            seen.add(qq)
            ban_results.append(await self._apply_mute_ban(qq, bot))

        # 清空该群的禁言记录
        try:
            records.pop(group_id, None)
            await self.put_kv_data("mute_records", records)
        except Exception as exc:
            logger.warning(f"group_invite_guard: clear mute_records failed: {exc}")

        if self.config.get("mute_notify", True):
            note = self._compose_mute_revenge_note(group_id, count, leave_result, ban_results)
            await self._notify(bot, note)

    def _looks_like_invite_intent(self, text: str) -> bool:
        low = text.lower()
        if any(mark in low for mark in _INVITE_LINK_MARKS):
            return True
        if any(kw in text for kw in _INVITE_KEYWORDS):
            return True
        return False

    async def _ask_llm(
        self, inviter_qq: str, group_id: str, comment: str, bot=None, platform_id: str = None
    ) -> dict:
        provider_id = self.config.get("llm_provider_id") or self._default_provider_id()
        if not provider_id:
            raise RuntimeError("no llm provider id configured")

        decision_persona = str(self.config.get("decision_persona") or "").strip()
        persona_prompt = await self._resolve_persona_prompt(decision_persona, platform_id)
        system_prompt = persona_prompt or "你是一个 QQ 机器人助手。"

        context = await self._build_invite_context(inviter_qq, group_id, bot)
        prompt = (
            f"收到一个加群邀请：\n"
            f"邀请人 QQ：{inviter_qq}\n"
            f"群号：{group_id}\n"
            f"附言：{comment or '(无)'}\n"
        )
        if context:
            prompt += f"\n背景信息：\n{context}\n"
        prompt += (
            "\n请以你的身份和性格判断是否同意这个加群邀请。"
            "只输出一个 JSON 对象：{\"action\": \"approve\" 或 \"reject\", \"reason\": \"简短理由\"}。"
        )

        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        text = (getattr(resp, "completion_text", "") or "").strip()
        return _parse_json(text)

    async def _resolve_persona_prompt(self, persona_id: str, platform_id: str = None) -> str:
        """加载人格设定的完整 system_prompt，用于给决策 LLM 充当性格；失败返回空。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        if not persona_id:
            # 未显式指定人格时，自动用当前账号的默认人格
            try:
                umo = f"{platform_id}:GroupMessage:0" if platform_id else None
                persona = await pm.get_default_persona_v3(umo=umo)
                persona_id = persona["name"] if persona else None
            except Exception as exc:
                logger.warning(f"group_invite_guard: resolve default persona failed: {exc}")
                persona_id = None
        if not persona_id or persona_id == "default":
            return ""
        try:
            persona = await pm.get_persona(persona_id)
        except Exception as exc:
            logger.warning(f"group_invite_guard: load persona '{persona_id}' failed: {exc}")
            return ""
        return str(getattr(persona, "system_prompt", "") or "").strip()

    async def _build_invite_context(self, inviter_qq: str, group_id: str, bot) -> str:
        """收集目标群成员列表与历史印象，拼成给 LLM 参考的背景信息；两块都拿不到时返回空字符串。"""
        sections = []
        if self.config.get("enable_member_context", True):
            member_section = await self._build_member_section(group_id, bot)
            if member_section:
                sections.append(member_section)
        if self.config.get("enable_impression_context", True):
            impression_section = await self._build_impression_section(inviter_qq, group_id)
            if impression_section:
                sections.append(impression_section)

        if not sections:
            return ""

        context = "\n\n".join(sections)
        marker = self.config.get("truncate_marker", "…")
        if len(context) > 4000:
            context = context[:4000] + marker
        return context

    async def _build_member_section(self, group_id: str, bot) -> str:
        header = "目标群成员列表："
        fallback = f"{header}\n机器人尚未进群，无法获取成员列表"
        if bot is None:
            return fallback
        try:
            members = await self._call_action(
                bot, "get_group_member_list", group_id=int(group_id), no_cache=True
            )
        except Exception as exc:
            logger.warning(f"group_invite_guard: get_group_member_list failed: {exc}")
            return fallback

        lines = []
        for member in members or []:
            if not isinstance(member, dict):
                continue
            name = _get_value(member, "card") or _get_value(member, "nickname") or ""
            qq = _get_value(member, "user_id") or ""
            if not name and not qq:
                continue
            identity = self._member_role_label(_get_value(member, "role"))
            lines.append(f"[{identity}] {name} (QQ: {qq})")

        if not lines:
            return fallback
        return header + "\n" + "\n".join(lines)

    async def _build_impression_section(self, inviter_qq: str, group_id: str) -> str:
        inviter_lines = await self._search_impression_lines(inviter_qq)
        speaker_lines = await self._extract_speaker_quotes(inviter_qq)
        group_lines = await self._search_impression_lines(group_id)

        parts = []
        if speaker_lines:
            parts.append(f"邀请人（QQ {inviter_qq}）在历史里说过的原话：\n" + "\n".join(speaker_lines))
        if inviter_lines:
            parts.append("对邀请人 QQ 的既有印象：\n" + "\n".join(inviter_lines))
        if group_lines:
            parts.append("对该群的既有印象：\n" + "\n".join(group_lines))
        return "\n\n".join(parts)

    async def _search_impression_lines(self, query: str) -> list:
        query = str(query or "").strip()
        if not query:
            return []
        try:
            conversations, _ = await self.context.conversation_manager.get_filtered_conversations(
                page=1, page_size=5, search_query=query, include_history=True
            )
        except Exception as exc:
            logger.warning(f"group_invite_guard: search history for '{query}' failed: {exc}")
            return []

        lines = []
        for conv in conversations or []:
            history = getattr(conv, "history", None)
            if not history:
                continue
            lines.extend(self._extract_history_lines(history))
            if len(lines) >= 30:
                break
        return lines[:30]

    async def _extract_speaker_quotes(self, inviter_qq: str) -> list:
        """从历史会话（含群聊）里抓取邀请人本人的发言原话，形成对 TA 的印象。"""
        qq = str(inviter_qq or "").strip()
        if not qq:
            return []
        try:
            conversations, _ = await self.context.conversation_manager.get_filtered_conversations(
                page=1, page_size=10, search_query=qq, include_history=True
            )
        except Exception as exc:
            logger.warning(f"group_invite_guard: search speaker history '{qq}' failed: {exc}")
            return []

        pattern = re.compile(r"ID:\s*" + re.escape(qq))
        quotes = []
        for conv in conversations or []:
            history = getattr(conv, "history", None)
            if not history:
                continue
            try:
                items = json.loads(history)
            except Exception:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().lower()
                if role not in ("user", "assistant"):
                    continue
                text = self._content_to_text(item.get("content"))
                for raw_line in text.splitlines():
                    if not pattern.search(raw_line):
                        continue
                    line = re.sub(r"^\s*\[[^\]]*\]\s*", "", raw_line)
                    line = re.sub(r"^\s*\S+\s*\(ID:[^)]*\)\s*[:：]\s*", "", line)
                    line = re.sub(r"^\s*\[At:[^\]]*\]\s*", "", line).strip()
                    if not line or len(line) < 2:
                        continue
                    if len(line) > 300:
                        line = line[:300] + self.config.get("truncate_marker", "…")
                    if line not in quotes:
                        quotes.append(line)
                if len(quotes) >= 20:
                    break
            if len(quotes) >= 20:
                break
        return quotes[:20]

    def _extract_history_lines(self, history: str) -> list:
        try:
            items = json.loads(history)
        except Exception:
            return []
        if not isinstance(items, list):
            return []

        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            text = self._content_to_text(item.get("content"))
            if not text:
                continue
            if len(text) > 300:
                text = text[:300] + self.config.get("truncate_marker", "…")
            label = "用户" if role == "user" else "机器人"
            lines.append(f"{label}：{text}")
        return lines[-6:]

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return " ".join(parts).strip()
        return str(content).strip()

    @staticmethod
    def _member_role_label(role: Any) -> str:
        mapping = {"owner": "群主", "admin": "管理员", "member": "成员"}
        return mapping.get(str(role or "").strip().lower(), str(role or "成员"))

    async def _ask_private_intent(self, text: str, platform_id: str = None) -> dict:
        provider_id = self.config.get("llm_provider_id") or self._default_provider_id()
        if not provider_id:
            raise RuntimeError("no llm provider id configured")

        decision_persona = str(self.config.get("decision_persona") or "").strip()
        persona_prompt = await self._resolve_persona_prompt(decision_persona, platform_id)
        system_prompt = persona_prompt or "你是一个 QQ 机器人助手。"

        prompt = (
            f"对方私聊发来一条消息：\n{text}\n\n"
            "请以你的身份和性格判断这是否表达了“想邀请/拉机器人进群”的意图"
            "（包括询问能不能加群、直接发群邀请链接等）。"
            "只输出一个 JSON 对象：{\"is_invite_intent\": true 或 false, "
            "\"reply\": \"若为加群意图，回复对方的话（否则空字符串）\", "
            "\"reason\": \"给管理员的简短说明（否则空字符串）\"}。"
        )

        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        result_text = (getattr(resp, "completion_text", "") or "").strip()
        return _parse_json(result_text)

    def _default_provider_id(self) -> str:
        provider_settings = self._nested(self._global_config(), "provider_settings")
        if isinstance(provider_settings, dict):
            return str(provider_settings.get("default_provider_id") or "")
        return str(self._nested(provider_settings, "default_provider_id") or "")

    def _global_config(self) -> Any:
        try:
            return self.context.get_config()
        except Exception:
            return None

    @staticmethod
    def _nested(obj: Any, *keys: str) -> Any:
        for key in keys:
            if obj is None:
                return None
            if isinstance(obj, dict):
                obj = obj.get(key)
                continue
            getter = getattr(obj, "get", None)
            if callable(getter):
                try:
                    obj = getter(key, None)
                    continue
                except Exception:
                    pass
            obj = getattr(obj, key, None)
        return obj

    def _find_onebot_client(self, event: AstrMessageEvent) -> Any:
        message_obj = getattr(event, "message_obj", None)
        for owner in (event, message_obj):
            if owner is None:
                continue
            if self._has_action_caller(owner):
                return owner
            for attr in (
                "bot",
                "platform",
                "adapter",
                "client",
                "protocol_client",
                "driver",
                "impl",
            ):
                nested = getattr(owner, attr, None)
                if nested is not None and self._has_action_caller(nested):
                    return nested
        return None

    @staticmethod
    def _has_action_caller(obj: Any) -> bool:
        for name in (
            "call_action",
            "call_api",
            "set_group_add_request",
            "send_private_msg",
            "send_group_msg",
        ):
            try:
                if callable(getattr(obj, name, None)):
                    return True
            except Exception:
                continue
        return False

    async def _call_action(self, bot: Any, action: str, **params: Any) -> Any:
        method = getattr(bot, action, None)
        if callable(method):
            result = method(**params)
            return await result if inspect.isawaitable(result) else result
        for name in ("call_action", "call_api"):
            fn = getattr(bot, name, None)
            if callable(fn):
                result = fn(action, **params)
                return await result if inspect.isawaitable(result) else result
        raise RuntimeError(f"no usable OneBot action caller for {action}")

    async def _take_revenge(self, inviter_qq: str, bot) -> str:
        """按 revenge_mode 对邀请人执行报复，返回人类可读结果；单个动作失败不抛出，写入结果字符串。"""
        mode = str(self.config.get("revenge_mode", "off") or "off").strip().lower()
        if mode not in ("delete_friend", "delete_and_ban"):
            return "未启用报复"

        parts = []
        try:
            await self._call_action(bot, "delete_friend", user_id=int(inviter_qq), block=True)
            parts.append(f"已删除并拉黑好友 {inviter_qq}")
        except Exception as exc:
            parts.append(f"删除好友失败：{exc}")

        if mode == "delete_and_ban":
            parts.append(await self._ban_inviter(inviter_qq))
        return "；".join(parts)

    async def _ban_inviter(self, inviter_qq: str) -> str:
        """把邀请人加入 AstrBot 黑名单（qq_tools 插件），不可用时降级并注明。"""
        try:
            md = self.context.get_registered_star("astrbot_plugin_qq_tools")
        except Exception as exc:
            return f"获取 qq_tools 实例失败：{exc}"

        instance = getattr(md, "star_cls", None)
        if instance is None:
            return "黑名单功能不可用（未安装/未启用 qq_tools）"

        try:
            config = getattr(instance, "config", None)
            if config is None:
                return "黑名单功能不可用（qq_tools 配置不可读）"
            ban_list = config.get("ban_list")
            if not isinstance(ban_list, list):
                ban_list = []
            # 移除旧的同 QQ 记录，再追加永久拉黑
            ban_list = [
                item
                for item in ban_list
                if not (isinstance(item, dict) and str(item.get("user_id")) == str(inviter_qq))
            ]
            ban_list.append(
                {"user_id": str(inviter_qq), "ban_time": int(time.time()), "duration": -1}
            )
            config["ban_list"] = ban_list

            save = getattr(config, "save_config", None)
            if callable(save):
                if inspect.iscoroutinefunction(save):
                    await save()
                else:
                    await asyncio.to_thread(save)
            return f"已加入 AstrBot 黑名单：{inviter_qq}"
        except Exception as exc:
            return f"加入黑名单失败：{exc}"

    def _compose_revenge_note(self, group_id, inviter_qq, result) -> str:
        lines = [
            "[被踢报复通知]",
            f"群号：{group_id}",
            f"邀请人 QQ：{inviter_qq}",
            f"报复结果：{result}",
        ]
        return "\n".join(lines)

    async def _apply_mute_ban(self, qq: str, bot) -> str:
        """按 mute_ban_mode 对目标执行拉黑，返回人类可读结果；单个动作失败不抛出，写入结果字符串。"""
        mode = str(self.config.get("mute_ban_mode", "astrbot_ban") or "astrbot_ban").strip().lower()
        if mode == "astrbot_ban":
            return await self._ban_inviter(qq)
        if mode == "delete_friend":
            try:
                await self._call_action(bot, "delete_friend", user_id=int(qq), block=True)
                return f"已删除并拉黑好友 {qq}"
            except Exception as exc:
                return f"删除好友失败：{exc}"
        return "未启用拉黑"

    def _compose_mute_revenge_note(self, group_id, count, leave_result, ban_results) -> str:
        ban_text = "；".join(ban_results) if ban_results else "(无拉黑目标)"
        lines = [
            "[被禁言报复通知]",
            f"群号：{group_id}",
            f"累计被禁言次数：{count}",
            f"退群结果：{leave_result}",
            f"拉黑结果：{ban_text}",
        ]
        return "\n".join(lines)

    def _compose_note(self, inviter_qq, group_id, comment, action, reason) -> str:
        action_label = {
            "approve": "同意加入",
            "reject": "拒绝",
            "unknown": "判断失败（未处理）",
        }.get(action, action)
        lines = [
            "[加群邀请通知]",
            f"邀请人 QQ：{inviter_qq}",
            f"群号：{group_id}",
            f"附言：{comment or '(无)'}",
            f"LLM 决策：{action_label}",
        ]
        if reason:
            lines.append(f"理由：{reason}")
        return "\n".join(lines)

    def _compose_private_note(self, sender_id, text, reply, reason) -> str:
        lines = [
            "[私聊加群意图通知]",
            f"对方 QQ：{sender_id or '(未知)'}",
            f"对方消息：{text}",
        ]
        if reply:
            lines.append(f"已回复：{reply}")
        if reason:
            lines.append(f"说明：{reason}")
        return "\n".join(lines)

    async def _notify(self, bot: Any, note: str) -> None:
        targets = self._notify_targets()
        if targets.get("private"):
            try:
                await self._call_action(
                    bot,
                    "send_private_msg",
                    user_id=int(targets["private"]),
                    message=note,
                )
            except Exception as exc:
                logger.error(f"group_invite_guard: notify private failed: {exc}")
        if targets.get("group"):
            try:
                await self._call_action(
                    bot,
                    "send_group_msg",
                    group_id=int(targets["group"]),
                    message=note,
                )
            except Exception as exc:
                logger.error(f"group_invite_guard: notify group failed: {exc}")

    def _notify_targets(self) -> dict:
        private_qq = str(self.config.get("notify_private_qq") or "").strip()
        group_id = str(self.config.get("notify_group_id") or "").strip()

        if not private_qq and self.config.get("notify_private", True):
            admins = self._nested(self._global_config(), "admins_id") or []
            if admins:
                private_qq = str(admins[0])

        result = {"private": None, "group": None}
        if self.config.get("notify_private", True) and private_qq:
            result["private"] = private_qq
        if self.config.get("notify_group", False) and group_id:
            result["group"] = group_id
        return result
