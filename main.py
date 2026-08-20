import inspect
import json
import re
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

        try:
            decision = await self._ask_llm(inviter_qq, group_id, comment)
        except Exception as exc:
            logger.error(f"group_invite_guard: LLM decision failed: {exc}")
            decision = {"action": "unknown", "reason": f"LLM error: {exc}"}

        action = str(decision.get("action") or "unknown").strip().lower()
        reason = str(decision.get("reason") or "").strip()

        bot = self._find_onebot_client(event)
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

        try:
            decision = await self._ask_private_intent(text)
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

    def _looks_like_invite_intent(self, text: str) -> bool:
        low = text.lower()
        if any(mark in low for mark in _INVITE_LINK_MARKS):
            return True
        if any(kw in text for kw in _INVITE_KEYWORDS):
            return True
        return False

    async def _ask_llm(self, inviter_qq: str, group_id: str, comment: str) -> dict:
        provider_id = self.config.get("llm_provider_id") or self._default_provider_id()
        if not provider_id:
            raise RuntimeError("no llm provider id configured")

        system_prompt = self.config.get(
            "decision_prompt",
            "你是 QQ 机器人的加群邀请决策助手。根据邀请信息判断是否同意机器人加入该群。只输出 JSON：{\"action\": \"approve\" 或 \"reject\", \"reason\": \"简短理由\"}。approve 表示同意进群，reject 表示拒绝。",
        )
        prompt = (
            f"收到一个加群邀请：\n"
            f"邀请人 QQ：{inviter_qq}\n"
            f"群号：{group_id}\n"
            f"附言：{comment or '(无)'}\n"
            f"请判断是否同意机器人加入该群。"
        )

        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        text = (getattr(resp, "completion_text", "") or "").strip()
        return _parse_json(text)

    async def _ask_private_intent(self, text: str) -> dict:
        provider_id = self.config.get("llm_provider_id") or self._default_provider_id()
        if not provider_id:
            raise RuntimeError("no llm provider id configured")

        system_prompt = self.config.get(
            "private_intent_prompt",
            "你是 QQ 机器人。判断这条私聊消息是否表达了“想邀请/拉机器人进群”的意图（包括询问能不能加群、直接发群邀请链接等）。只输出 JSON：{\"is_invite_intent\": true 或 false, \"reply\": \"若为加群意图，回复对方的话\", \"reason\": \"给管理员的简短说明\"}。若不是加群意图，is_invite_intent 为 false，reply 和 reason 都留空字符串。",
        )
        prompt = (
            f"对方私聊发来一条消息：\n{text}\n\n"
            f"请判断这是否是加群邀请意图，并给出相应回复。"
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
