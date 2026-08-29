import asyncio
import inspect
import json
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register


try:
    from astrbot.core.agent.run_context import ContextWrapper
except Exception:  # 兼容旧版 / 内部 API 缺失
    ContextWrapper = None


def _unwrap_event(event):
    """@filter.llm_tool 在 v4.26+ 传入 ContextWrapper，这里取出内部 AstrMessageEvent。"""
    if ContextWrapper is not None and isinstance(event, ContextWrapper):
        try:
            return event.context.event
        except Exception:
            return event
    return event


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


class AdminCommandFilter(filter.CustomFilter):
    """只匹配管理员发起的命令消息（邀请记录/记录邀请/拉黑列表/解封/手动拉黑）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            if not event.is_admin():
                return False
        except Exception:
            return False
        text = (event.get_message_str() or "").strip()
        cmd = text.lstrip("/").strip()
        first = cmd.split(" ", 1)[0] if cmd else ""
        return first in ("邀请记录", "邀请列表", "记录邀请", "拉黑列表", "黑名单", "解封", "手动拉黑")


@register(
    "astrbot_plugin_group_invite_guard",
    "Kimi",
    "加群邀请自动处理：LLM 判断是否同意，支持自动同意/拒绝或仅通知管理员；私聊问能否加群/发邀请链接也会被识别",
    "1.3.2",
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
        inviter_qq = ""
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

        # 去重、去空，逐一对目标执行拉黑（拉黑邀请人前先私聊通知）
        seen = set()
        ban_results = []
        for qq in targets:
            qq = str(qq or "").strip()
            if not qq or qq in seen:
                continue
            seen.add(qq)
            if qq == inviter_qq:
                notice = await self._send_ban_notice(bot, qq)
                if notice:
                    ban_results.append(notice)
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

    async def _send_ban_notice(self, bot, qq: str) -> str:
        """拉黑前给邀请人发一条自定义私聊消息；未配置则跳过。"""
        msg = str(self.config.get("ban_notice_message") or "").strip()
        if not msg:
            return ""
        try:
            await self._call_action(bot, "send_private_msg", user_id=int(qq), message=msg)
            return f"已私聊通知 {qq}"
        except Exception as exc:
            return f"通知 {qq} 失败：{exc}"

    async def _take_revenge(self, inviter_qq: str, bot) -> str:
        """按 revenge_mode 对邀请人执行报复，返回人类可读结果；单个动作失败不抛出，写入结果字符串。"""
        mode = str(self.config.get("revenge_mode", "off") or "off").strip().lower()
        if mode not in ("delete_friend", "delete_and_ban"):
            return "未启用报复"

        parts = []
        notice = await self._send_ban_notice(bot, inviter_qq)
        if notice:
            parts.append(notice)
        try:
            await self._call_action(bot, "delete_friend", user_id=int(inviter_qq), block=True)
            parts.append(f"已删除并拉黑好友 {inviter_qq}")
        except Exception as exc:
            parts.append(f"删除好友失败：{exc}")

        if mode == "delete_and_ban":
            parts.append(await self._ban_inviter(inviter_qq, "被踢后报复"))
        return "；".join(parts)

    def _get_qq_tools_instance(self) -> Any:
        """获取 qq_tools 插件实例，未安装或不可用时返回 None。"""
        try:
            md = self.context.get_registered_star("astrbot_plugin_qq_tools")
        except Exception as exc:
            logger.warning(f"group_invite_guard: get qq_tools instance failed: {exc}")
            return None
        return getattr(md, "star_cls", None)

    async def _ban_inviter(self, inviter_qq: str, reason: str = "拉黑") -> str:
        """把邀请人加入 AstrBot 黑名单（qq_tools 插件），不可用时降级并注明。"""
        instance = self._get_qq_tools_instance()
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
                {
                    "user_id": str(inviter_qq),
                    "ban_time": int(time.time()),
                    "duration": -1,
                    "reason": reason,
                }
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
            return await self._ban_inviter(qq, "被禁言后报复")
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

    async def _list_invite_records_text(self) -> str:
        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception as exc:
            return f"读取邀请记录失败：{exc}"
        if not isinstance(records, dict) or not records:
            return "暂无邀请记录"
        return "\n".join(f"群号 {gid} -> 邀请人 {qq}" for gid, qq in records.items())

    async def _record_invite_text(self, group_id: str, inviter_qq: str) -> str:
        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception as exc:
            return f"读取邀请记录失败：{exc}"
        if not isinstance(records, dict):
            records = {}
        records[str(group_id)] = str(inviter_qq)
        try:
            await self.put_kv_data("invite_records", records)
        except Exception as exc:
            return f"写入邀请记录失败：{exc}"
        return f"已记录 群号 {group_id} -> 邀请人 {inviter_qq}"

    async def _list_ban_list_text(self) -> str:
        instance = self._get_qq_tools_instance()
        if instance is None:
            return "黑名单功能不可用（未安装/未启用 qq_tools）"
        try:
            config = getattr(instance, "config", None)
            if config is None:
                return "黑名单功能不可用（qq_tools 配置不可读）"
            ban_list = config.get("ban_list")
        except Exception as exc:
            return f"读取黑名单失败：{exc}"
        if not isinstance(ban_list, list) or not ban_list:
            return "黑名单为空"
        lines = []
        for item in ban_list:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id") or "")
            ban_time = item.get("ban_time")
            try:
                ban_time_str = datetime.fromtimestamp(int(ban_time)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ban_time_str = str(ban_time if ban_time is not None else "-")
            duration = item.get("duration")
            duration_str = "永久" if duration == -1 else str(duration if duration is not None else "-")
            reason = str(item.get("reason") or "-")
            lines.append(f"QQ {user_id} | 拉黑时间 {ban_time_str} | 时长 {duration_str} | 原因 {reason}")
        return "\n".join(lines) if lines else "黑名单为空"

    async def _unban_text(self, qq: str) -> str:
        instance = self._get_qq_tools_instance()
        if instance is None:
            return "黑名单功能不可用（未安装/未启用 qq_tools）"
        try:
            config = getattr(instance, "config", None)
            if config is None:
                return "黑名单功能不可用（qq_tools 配置不可读）"
            ban_list = config.get("ban_list")
            if not isinstance(ban_list, list):
                ban_list = []
            new_list = [
                item
                for item in ban_list
                if not (isinstance(item, dict) and str(item.get("user_id")) == str(qq))
            ]
            if len(new_list) == len(ban_list):
                return f"{qq} 不在黑名单中"
            config["ban_list"] = new_list
            save = getattr(config, "save_config", None)
            if callable(save):
                if inspect.iscoroutinefunction(save):
                    await save()
                else:
                    await asyncio.to_thread(save)
            return f"已解封 {qq}"
        except Exception as exc:
            return f"解封失败：{exc}"

    async def _manual_revenge(self, inviter_qq: str, bot) -> str:
        """手动拉黑邀请人：发自定义通知 + 删除并拉黑好友 + 加入 AstrBot 黑名单。"""
        parts = []
        notice = await self._send_ban_notice(bot, inviter_qq)
        if notice:
            parts.append(notice)
        try:
            await self._call_action(bot, "delete_friend", user_id=int(inviter_qq), block=True)
            parts.append(f"已删除并拉黑好友 {inviter_qq}")
        except Exception as exc:
            parts.append(f"删除好友失败：{exc}")
        parts.append(await self._ban_inviter(inviter_qq, "手动拉黑"))
        return "；".join(parts)

    async def _manual_ban_text(self, event: AstrMessageEvent, args) -> str:
        """手动拉黑：退群 + 拉黑邀请人（找不到邀请人则只退群）。"""
        if not args:
            return "用法：/手动拉黑 <群号> [邀请人QQ]"
        group_id = str(args[0] or "").strip()
        if not group_id:
            return "用法：/手动拉黑 <群号> [邀请人QQ]"
        inviter_qq = str(args[1] or "").strip() if len(args) > 1 else ""

        bot = self._find_onebot_client(event)
        if bot is None:
            return "手动拉黑失败：未找到 OneBot 客户端"

        if not inviter_qq:
            try:
                records = await self.get_kv_data("invite_records", {})
            except Exception as exc:
                return f"读取邀请记录失败：{exc}"
            if isinstance(records, dict):
                inviter_qq = str(records.get(group_id) or "").strip()

        parts = []
        try:
            await self._call_action(bot, "set_group_leave", group_id=int(group_id), is_dismiss=False)
            parts.append(f"已退出群 {group_id}")
        except Exception as exc:
            parts.append(f"退群失败：{exc}")

        if inviter_qq:
            try:
                result = await self._manual_revenge(inviter_qq, bot)
            except Exception as exc:
                result = f"报复失败：{exc}"
            parts.append(f"邀请人 {inviter_qq}：{result}")
        else:
            parts.append("未找到该群的邀请人记录，仅退群、未拉黑")

        return "；".join(parts)

    async def _dispatch_admin_command(self, event: AstrMessageEvent) -> str:
        text = (event.get_message_str() or "").strip().lstrip("/").strip()
        parts = text.split()
        cmd = parts[0] if parts else ""
        args = parts[1:]

        if cmd in ("邀请记录", "邀请列表"):
            return await self._list_invite_records_text()
        if cmd == "记录邀请":
            if len(args) < 2:
                return "用法：/记录邀请 <群号> <邀请人QQ>"
            return await self._record_invite_text(args[0], args[1])
        if cmd in ("拉黑列表", "黑名单"):
            return await self._list_ban_list_text()
        if cmd == "解封":
            if not args:
                return "用法：/解封 <QQ>"
            return await self._unban_text(args[0])
        if cmd == "手动拉黑":
            return await self._manual_ban_text(event, args)
        return "未知命令"

    @filter.custom_filter(AdminCommandFilter)
    async def on_admin_command(self, event: AstrMessageEvent):
        result = await self._dispatch_admin_command(event)
        try:
            await event.send(MessageChain(chain=[Plain(result)]))
        except Exception as exc:
            logger.error(f"group_invite_guard: send command result failed: {exc}")

    def _read_ban_entries(self) -> list:
        """读取 qq_tools 黑名单，返回 [(user_id, reason), ...]。"""
        instance = self._get_qq_tools_instance()
        if instance is None:
            return []
        try:
            config = getattr(instance, "config", None)
            if config is None:
                return []
            ban_list = config.get("ban_list")
        except Exception as exc:
            logger.warning(f"group_invite_guard: read ban_list failed: {exc}")
            return []
        if not isinstance(ban_list, list):
            return []
        entries = []
        for item in ban_list:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id") or "").strip()
            if not user_id:
                continue
            reason = str(item.get("reason") or "未注明").strip()
            entries.append((user_id, reason))
        return entries

    async def _build_ban_context_text(self) -> str:
        """把黑名单/邀请记录/禁言记录拼成一段给 LLM 的上下文；全空返回空字符串。"""
        lines = []
        entries = self._read_ban_entries()
        if entries:
            lines.append("当前拉黑名单：")
            for user_id, reason in entries[:20]:
                lines.append(f"- QQ {user_id}（{reason}）")

        try:
            invite = await self.get_kv_data("invite_records", {})
        except Exception:
            invite = {}
        if isinstance(invite, dict) and invite:
            lines.append("被邀请进群记录（群号 -> 邀请人）：")
            for gid, qq in list(invite.items())[:20]:
                lines.append(f"- 群 {gid} 由 {qq} 邀请")

        try:
            mute = await self.get_kv_data("mute_records", {})
        except Exception:
            mute = {}
        if isinstance(mute, dict) and mute:
            lines.append("被禁言记录（群号：次数）：")
            for gid, cnt in list(mute.items())[:20]:
                lines.append(f"- 群 {gid}：{cnt} 次")

        if not lines:
            return ""
        return "[加群邀请守卫·封禁记录]\n" + "\n".join(lines)

    def _tool_allowed(self, event):
        """判断 LLM 主动拉黑/解封是否放行；返回 (是否放行, 拒绝原因)。"""
        if not self.config.get("enable", True):
            return False, "插件未启用。"
        if not self.config.get("llm_tool_ban", True):
            return False, "LLM 主动拉黑功能未启用。"
        if self.config.get("llm_tool_require_admin", False):
            try:
                if not event.is_admin():
                    return False, "没有权限：当前工具仅管理员可触发。"
            except Exception:
                return False, "没有权限：当前工具仅管理员可触发。"
        return True, ""

    @filter.on_llm_request(priority=-1)
    async def on_llm_request(self, event, req):
        if not self.config.get("enable", True):
            return
        if not self.config.get("llm_context_inject", True):
            return
        try:
            block = await self._build_ban_context_text()
        except Exception as exc:
            logger.warning(f"group_invite_guard: build ban context failed: {exc}")
            return
        if not block:
            return
        existing = getattr(req, "system_prompt", None) or ""
        req.system_prompt = f"{existing}\n\n{block}" if existing else block

    @filter.llm_tool(name="group_invite_ban_user")
    async def llm_ban_user(self, event, user_id: str, reason: str = ""):
        """把某个 QQ 用户加入机器人黑名单（拉黑）。当用户有骚扰、辱骂、恶意邀请后踢机器人等行为，或历史已多次违规时，主动调用此工具拉黑。"""
        event = _unwrap_event(event)
        allowed, msg = self._tool_allowed(event)
        if not allowed:
            return msg
        user_id = str(user_id or "").strip()
        if not user_id:
            return "拉黑失败：缺少 user_id（QQ 号）。"
        reason = str(reason or "").strip() or "LLM 主动拉黑"
        return await self._ban_inviter(user_id, reason)

    @filter.llm_tool(name="group_invite_unban_user")
    async def llm_unban_user(self, event, user_id: str):
        """把某个 QQ 用户从机器人黑名单移除（解封）。当确认是误伤、用户已道歉或不再需要拉黑时主动调用。"""
        event = _unwrap_event(event)
        allowed, msg = self._tool_allowed(event)
        if not allowed:
            return msg
        user_id = str(user_id or "").strip()
        if not user_id:
            return "解封失败：缺少 user_id（QQ 号）。"
        return await self._unban_text(user_id)

    @filter.llm_tool(name="group_invite_query_ban")
    async def llm_query_ban(self, event):
        """查询机器人当前的黑名单、被邀请进群记录、被禁言记录。需要了解当前封禁/邀请状态时调用。"""
        event = _unwrap_event(event)
        if not self.config.get("enable", True):
            return "插件未启用。"
        try:
            block = await self._build_ban_context_text()
        except Exception as exc:
            return f"查询失败：{exc}"
        return block or "当前没有封禁记录。"
