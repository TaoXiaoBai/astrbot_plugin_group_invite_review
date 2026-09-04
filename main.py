import asyncio
import difflib
import hashlib
import inspect
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Image
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


def _invite_status_label(action: str, dealt: bool) -> tuple[str, str]:
    """把邀请记录的 action 与 dealt 标志转成 (状态文本, css 类名)。"""
    if dealt:
        return "已拉黑", "banned"
    a = str(action or "").strip().lower()
    if a == "reject" or "拒绝" in a or "不同意" in a or "未同意" in a:
        return str(action or "已拒绝"), "rejected"
    if a == "approve" or "同意" in a:
        return str(action or "已同意"), "approved"
    if a == "手动记录" or "仅记录" in a or "非本机器人" in a:
        return str(action or "已记录"), "recorded"
    if a in ("unknown", "", "-") or "待审核" in a or "审核中" in a:
        return str(action or "待处理"), "pending"
    return str(action or "其他").strip(), "other"


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

_BLACKLIST_INQUIRY_KEYWORDS = (
    "拉黑", "为什么拉黑", "为啥拉黑", "怎么拉黑", "为何拉黑",
    "踢我", "为什么踢", "为啥踢", "为何踢", "被踢",
    "拒绝", "为什么拒绝", "为啥拒绝", "为何拒绝", "不同意",
    "进不了群", "加不了群", "为什么加不了", "为啥加不了",
    "解封", "放出来", "取消拉黑", "撤销拉黑",
)


# 配置分组：group -> {key: 默认值}。嵌套读取与旧平铺配置迁移都以它为准，
# 新增配置项时同步修改这里和 _conf_schema.json。
_CONFIG_GROUPS = {
    "basic": {
        "enable": True,
        "notify_private": True,
        "notify_private_qq": "",
        "notify_group": False,
        "notify_group_id": "",
    },
    "decision": {
        "auto_approve": False,
        "auto_reject": False,
        "reply_inviter_on_decision": True,
        "llm_provider_id": "",
        "decision_persona": "",
        "enable_member_context": True,
        "enable_impression_context": True,
        "impression_llm_summary": True,
        "enable_user_profile": True,
        "use_profile_plugin": True,
        "truncate_marker": "…",
    },
    "unexpected_join": {
        "mode": "notify_only",
        "custom_leave_message": "",
    },
    "alt_detect": {
        "alt_account_detect": True,
        "alt_similarity_threshold": 70,
        "alt_gray_low": 40,
        "alt_llm_review": True,
    },
    "private_intent": {
        "enable_private_intent": True,
        "private_intent_reply": True,
        "private_intent_notify": True,
        "enable_blacklist_inquiry": True,
        "blacklist_inquiry_reply": True,
        "blacklist_inquiry_notify": True,
        "blacklist_inquiry_keywords": "拉黑,为什么拉黑,为啥拉黑,怎么拉黑,踢我,为什么踢,为啥踢,被踢,拒绝,为什么拒绝,为啥拒绝,不同意,进不了群,加不了群,解封,放出来,取消拉黑",
        "blacklist_inquiry_tone": "persona",
        "blacklist_inquiry_show_reason": True,
        "blacklist_inquiry_max_length": 120,
        "blacklist_inquiry_system_prompt": "",
        "blacklist_inquiry_user_prompt": "",
    },
    "kick_revenge": {
        "revenge_mode": "off",
        "revenge_notify": True,
        "kick_ban_operator": False,
        "record_group_join": True,
        "cross_group_retaliation": False,
        "ban_notice_message": "",
        "intercept_banned_messages": True,
    },
    "mute_revenge": {
        "mute_retaliation_enable": False,
        "mute_threshold": 3,
        "mute_target": "operator",
        "mute_ban_mode": "astrbot_ban",
        "mute_notify": True,
    },
    "llm_integration": {
        "llm_context_inject": True,
        "llm_tool_ban": True,
        "llm_tool_require_admin": False,
    },
    "display": {
        "invite_records_show_profile": True,
        "invite_records_show_group_profile": True,
        "invite_records_hide_dealt": True,
        "invite_records_show_decision_detail": True,
    },
}

# 旧平铺配置已迁入分组的标记键（在 schema 中以 invisible 保留，防止被完整性检查剔除）
_CONFIG_MIGRATED_KEY = "_flat_config_migrated"


_INVITE_RECORDS_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  body { margin:0; background:#f6f7fb; font-family:"Microsoft YaHei",-apple-system,"PingFang SC",sans-serif; color:#1a1a2e; }
  .wrap { padding:28px; }
  h1 { font-size:30px; margin:0 0 6px; }
  .sub { color:#6b7184; font-size:15px; margin:0 0 20px 0; }
  table { width:100%; border-collapse:collapse; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 6px 18px rgba(0,0,0,.06); }
  th, td { padding:10px 12px; text-align:left; font-size:16px; border-bottom:1px solid #eceef3; vertical-align:middle; }
  th { background:#4a6cf7; color:#fff; font-weight:600; white-space:nowrap; }
  tr:nth-child(even) td { background:#f8f9fc; }
  .idx { color:#6b7184; font-family:ui-monospace,Menlo,Consolas,monospace; font-weight:700; }
  .comment { color:#555; max-width:200px; word-break:break-all; }
  .empty { color:#6b7184; padding:24px; text-align:center; background:#fff; border-radius:12px; }
  .person { display:flex; align-items:center; gap:10px; }
  .avatar { width:44px; height:44px; border-radius:50%; object-fit:cover; background:#eef0f6; border:1px solid #eceef3; }
  .gavatar { border-radius:12px; }
  .nick { font-weight:600; font-size:16px; max-width:150px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .qq { color:#6b7184; font-size:13px; }
  .hint { margin-top:16px; font-size:12px; color:#9aa0b0; line-height:1.7; }
  .dealt-tag { display:inline-block; margin-left:6px; padding:1px 8px; font-size:12px; color:#fff; background:#e67e22; border-radius:8px; }
  tr.dealt td { opacity:.55; }
  .status-tag { display:inline-block; padding:3px 10px; font-size:13px; color:#fff; border-radius:10px; font-weight:600; white-space:nowrap; }
  .status-tag.banned { background:#e74c3c; }
  .status-tag.approved { background:#2ecc71; }
  .status-tag.rejected { background:#95a5a6; }
  .status-tag.recorded { background:#3498db; }
  .status-tag.pending { background:#f39c12; }
  .status-tag.other { background:#bdc3c7; color:#333; }
</style>
</head>
<body>
  <div class="wrap">
    <h1>加群邀请记录</h1>
    <div class="sub">共 {{ total }} 条（新→旧）</div>
    {% if items %}
    <table>
      <tr><th>#</th><th>群</th><th>邀请人</th><th>时间</th><th>状态</th><th>成员</th><th>附言</th></tr>
      {% for it in items %}
      <tr{% if it.dealt %} class="dealt"{% endif %}>
        <td class="idx">{{ it.index }}</td>
        <td>
          <div class="person">
            {% if it.gavatar %}<img class="avatar gavatar" src="{{ it.gavatar }}" onerror="this.style.display='none'">{% endif %}
            <div>
              {% if it.gname %}<div class="nick">{{ it.gname }}</div>{% endif %}
              <div class="qq">{{ it.group }}</div>
            </div>
          </div>
        </td>
        <td>
          <div class="person">
            {% if it.avatar %}<img class="avatar" src="{{ it.avatar }}" onerror="this.style.display='none'">{% endif %}
            <div>
              {% if it.nickname %}<div class="nick">{{ it.nickname }}</div>{% endif %}
              <div class="qq">{{ it.inviter }}</div>
            </div>
          </div>
        </td>
        <td>{{ it.time }}</td>
        <td>
          {% if it.decision %}<div class="qq">LLM：{{ it.decision }}</div>{% endif %}
          <span class="status-tag {{ it.status_class }}">{{ it.status_text }}</span>
          <div class="qq">{{ it.execution }}</div>
        </td>
        <td>{{ it.membership }}</td>
        <td class="comment">
          {{ it.comment }}
          {% if it.detail %}<div class="qq">{{ it.detail }}</div>{% endif %}
        </td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">暂无邀请记录</div>
    {% endif %}
    <div class="hint">
      操作提示：/手动拉黑 &lt;QQ或群号&gt; —— 按邀请记录反查并拉黑该人（匹配到邀请人时自动退其邀请的所有群；旧用法 /手动拉黑 &lt;QQ&gt; &lt;群号&gt; 仍可用） ｜ /解封 &lt;QQ&gt; —— 移出黑名单 ｜ /拉黑列表 —— 查看黑名单 ｜ /画像 &lt;QQ&gt; —— 查看完整画像 ｜ /记录邀请 &lt;群号&gt; &lt;邀请人QQ&gt; —— 补录一条
    </div>
  </div>
</body>
</html>"""


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


class PrivateBlacklistInquiryFilter(filter.CustomFilter):
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


class GroupJoinFilter(filter.CustomFilter):
    """只匹配机器人进群成功的通知（post_type=notice, notice_type=group_increase, sub_type=invite/approve，且进群的是机器人自己）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return False
        if (
            _get_value(raw, "post_type") != "notice"
            or _get_value(raw, "notice_type") != "group_increase"
        ):
            return False
        sub_type = str(_get_value(raw, "sub_type") or "")
        if sub_type not in ("invite", "approve"):
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
    """只匹配管理员发起的命令消息（邀请记录/记录邀请/拉黑列表/解封/手动拉黑/画像）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            if not event.is_admin():
                return False
        except Exception:
            return False
        text = (event.get_message_str() or "").strip()
        cmd = text.lstrip("/#").strip()
        first = cmd.split(" ", 1)[0] if cmd else ""
        return first in ("邀请记录", "邀请列表", "记录邀请", "拉黑列表", "黑名单", "解封", "手动拉黑", "画像")


@register(
    "astrbot_plugin_group_invite_guard",
    "Kimi",
    "让 LLM 根据人格设定判断是否通过邀请加群，支持自动同意/拒绝或仅通知管理员；私聊问能否加群/发邀请链接也会被识别",
    "1.18.0",
)
class GroupInviteGuardPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self._invite_kv_lock = asyncio.Lock()
        self._invite_state_lock = asyncio.Lock()
        self._invite_inflight = set()
        self._migrate_flat_config()
        if self._cfg("basic", "enable", True):
            self._migrate_ban_list_sync()

    def _cfg(self, group: str, key: str, default: Any = None) -> Any:
        """读取嵌套分组配置 self.config[group][key]；分组/键缺失时回退旧平铺顶层 key，最后回退 default。"""
        try:
            section = self.config.get(group)
        except Exception:
            section = None
        if isinstance(section, dict):
            value = section.get(key)
            if value is not None:
                return value
        try:
            if key in self.config:
                value = self.config.get(key)
                if value is not None:
                    return value
        except Exception:
            pass
        return default

    def _migrate_flat_config(self) -> None:
        """旧版平铺配置 → 嵌套分组的一次性迁移。

        AstrBotConfig 加载时会剔除 schema 之外的键并立刻落盘，因此旧键以 invisible
        形式保留在 _conf_schema.json 中，插件初始化时才能读到旧值。
        只搬「与默认值不同」的旧值：缺失的旧键会被 AstrBotConfig 自动补成默认值，
        与默认值相同的值搬与不搬等价，且不会覆盖用户在嵌套组里已设置的新值。
        """
        cfg = self.config
        if not isinstance(cfg, dict) or not cfg:
            return
        try:
            if cfg.get(_CONFIG_MIGRATED_KEY):
                return
            moved = []
            for group, keys in _CONFIG_GROUPS.items():
                section = cfg.get(group)
                if not isinstance(section, dict):
                    section = {}
                    cfg[group] = section
                for key, default in keys.items():
                    if key not in cfg:
                        continue
                    flat_value = cfg.get(key)
                    if flat_value is None or flat_value == default:
                        continue
                    if section.get(key, default) != default:
                        continue  # 嵌套组里已有非默认值，以嵌套为准
                    section[key] = flat_value
                    moved.append(key)
            cfg[_CONFIG_MIGRATED_KEY] = True
            save = getattr(cfg, "save_config", None)
            if callable(save):
                try:
                    save()
                except Exception as exc:
                    logger.warning(
                        f"group_invite_guard: 迁移配置保存失败（运行期仍生效）: {exc}"
                    )
            if moved:
                logger.info(
                    f"group_invite_guard: 已迁移 {len(moved)} 项旧平铺配置到分组：{'、'.join(moved)}"
                )
        except Exception as exc:
            logger.warning(f"group_invite_guard: 配置迁移失败（不影响运行）: {exc}")

    def _migrate_ban_list_sync(self) -> None:
        """启动时自动修复 qq_tools 黑名单的历史格式：把 qq / id / uin 统一成 user_id，并去重。"""
        config, err = self._get_ban_config()
        if config is None:
            return
        try:
            ban_list = config.get("ban_list")
        except Exception as exc:
            logger.warning(f"group_invite_guard: 读取黑名单做迁移失败: {exc}")
            return
        if not isinstance(ban_list, list):
            return

        new_list = []
        seen = set()
        migrated = 0
        for item in ban_list:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id") or "").strip()
            if not user_id:
                for field in ("qq", "id", "uin"):
                    val = str(item.get(field) or "").strip()
                    if val:
                        user_id = val
                        migrated += 1
                        break
            if not user_id:
                continue
            if user_id in seen:
                continue
            seen.add(user_id)
            new_item = dict(item)
            new_item["user_id"] = user_id
            for field in ("qq", "id", "uin"):
                new_item.pop(field, None)
            new_list.append(new_item)

        if migrated > 0 or len(new_list) != len(ban_list):
            config["ban_list"] = new_list
            save = getattr(config, "save_config", None)
            if callable(save):
                try:
                    if inspect.iscoroutinefunction(save):
                        loop = asyncio.get_running_loop()
                        loop.create_task(save())
                    else:
                        save()
                except RuntimeError:
                    pass
                except Exception as exc:
                    logger.warning(f"group_invite_guard: 黑名单迁移保存失败（运行期仍生效）: {exc}")
            logger.info(
                f"group_invite_guard: 已迁移 {migrated} 条旧格式黑名单记录，"
                f"去重后 {len(new_list)} 条"
            )

    @filter.custom_filter(GroupInviteRequestFilter)
    async def on_group_invite(self, event: AstrMessageEvent):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return

        inviter_qq = str(_get_value(raw, "user_id") or "").strip()
        group_id = str(_get_value(raw, "group_id") or "").strip()
        comment = str(_get_value(raw, "comment") or "").strip()
        flag = str(_get_value(raw, "flag") or "").strip()
        sub_type = str(_get_value(raw, "sub_type") or "invite").strip()
        self_id = str(_get_value(raw, "self_id") or "").strip()
        invited_id = str(_get_value(raw, "invited_id") or "").strip()
        target_state = "UNVERIFIED"
        if invited_id and self_id:
            target_state = "VERIFIED" if invited_id == self_id else "NOT_FOR_BOT"
        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            pass

        request_key = self._make_request_key(raw, flag)
        record_id, duplicate, reconcile = await self._begin_invite_request(
            group_id, inviter_qq, comment, request_key, target_state,
            platform_id, self_id, invited_id, bool(flag),
        )
        if duplicate:
            logger.info(f"group_invite_guard: duplicate invite ignored: {request_key}")
            return

        if target_state == "NOT_FOR_BOT":
            await self._finish_not_for_bot(group_id, record_id)
            await self._release_invite_request(request_key)
            return

        if not self._cfg("basic", "enable", True):
            await self._update_invite_record(
                group_id, record_id,
                action="仅记录（插件已禁用）",
                review_state="DISABLED",
                execution_state="DISABLED_RECORDED",
                execution_result="插件已禁用，仅持久化记录",
            )
            await self._release_invite_request(request_key)
            return

        try:
            event.stop_event()
        except Exception:
            pass

        bot = self._find_onebot_client(event)
        target_state, self_id, target_error = await self._resolve_invite_target(
            bot, self_id, invited_id
        )
        await self._update_invite_record(
            group_id, record_id, target_state=target_state, self_id=self_id,
            target_error=target_error,
        )
        if target_state == "NOT_FOR_BOT":
            await self._finish_not_for_bot(group_id, record_id, target_error)
            await self._release_invite_request(request_key)
            return

        try:
            if reconcile:
                await self._reconcile_inflight_request(
                    bot, group_id, record_id, inviter_qq, comment, self_id
                )
            else:
                await self._review_invite_request(
                    bot=bot,
                    group_id=group_id,
                    inviter_qq=inviter_qq,
                    comment=comment,
                    flag=flag,
                    sub_type=sub_type,
                    self_id=self_id,
                    platform_id=platform_id,
                    request_key=request_key,
                    record_id=record_id,
                    target_state=target_state,
                    target_error=target_error,
                )
        except Exception as exc:
            logger.error(f"group_invite_guard: invite review crashed: {exc}")
            await self._mark_processing_failure(group_id, record_id, exc)
        finally:
            await self._release_invite_request(request_key)

    @staticmethod
    def _make_request_key(raw: Any, flag: str) -> str:
        if flag:
            source = f"flag:{flag}"
        else:
            fields = {
                key: str(_get_value(raw, key) or "")
                for key in ("self_id", "invited_id", "group_id", "user_id", "sub_type", "comment", "time")
            }
            source = "event:" + json.dumps(fields, ensure_ascii=False, sort_keys=True)
        return "req_" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]

    def _invite_lock(self, name: str) -> asyncio.Lock:
        lock = getattr(self, name, None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, name, lock)
        return lock

    async def _begin_invite_request(
        self, group_id: str, inviter_qq: str, comment: str, request_key: str,
        target_state: str, platform_id: str, self_id: str, invited_id: str,
        has_flag: bool,
    ) -> tuple[str, bool, bool]:
        async with self._invite_lock("_invite_state_lock"):
            inflight = getattr(self, "_invite_inflight", None)
            if inflight is None:
                inflight = set()
                self._invite_inflight = inflight
            if request_key in inflight:
                return "", True, False
            try:
                records = await self.get_kv_data("invite_records", {})
            except Exception as exc:
                logger.warning(f"group_invite_guard: load invite_records for dedupe failed: {exc}")
                records = {}
            for recs in self._normalize_invite_records(records).values():
                for rec in recs:
                    if str(rec.get("request_key") or "") != request_key:
                        continue
                    state = str(rec.get("execution_state") or "")
                    if state.endswith("_IN_FLIGHT") or state == "ACTION_IN_FLIGHT":
                        inflight.add(request_key)
                        return str(rec.get("record_id") or ""), False, True
                    if state and state not in ("RECEIVED", "REVIEWING", "DECIDED"):
                        return str(rec.get("record_id") or ""), True, False
                    inflight.add(request_key)
                    return str(rec.get("record_id") or ""), False, False

            record_id = uuid.uuid4().hex[:8]
            protocol_error = "" if has_flag else "邀请请求缺少 flag，无法调用审批接口"
            persisted = await self._record_invite(
                group_id, inviter_qq, comment, "已收到，待审核", record_id=record_id,
                request_key=request_key,
                target_state=target_state,
                review_state="RECEIVED",
                membership_before="UNKNOWN",
                membership_after="UNKNOWN",
                action_attempted=False,
                action_succeeded=False,
                auto_executed=False,
                execution_state="RECEIVED",
                execution_result="已持久化，等待审核",
                protocol_error=protocol_error,
                platform_id=platform_id,
                self_id=self_id,
                invited_id=invited_id,
                decision="unknown",
            )
            if not persisted:
                raise RuntimeError("邀请记录持久化失败，已停止自动处理")
            inflight.add(request_key)
            return record_id, False, False

    async def _release_invite_request(self, request_key: str) -> None:
        async with self._invite_lock("_invite_state_lock"):
            getattr(self, "_invite_inflight", set()).discard(request_key)

    async def _get_invite_record(self, group_id: str, record_id: str) -> dict | None:
        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load invite record failed: {exc}")
            return None
        storage_group = str(group_id or "").strip() or "(未知群)"
        for rec in self._normalize_invite_records(records).get(storage_group, []):
            if str(rec.get("record_id") or "") == str(record_id or ""):
                return rec
        return None

    async def _finish_not_for_bot(
        self, group_id: str, record_id: str, error: str = ""
    ) -> None:
        await self._update_invite_record(
            group_id, record_id,
            action="非本机器人邀请",
            review_state="NOT_REQUIRED",
            execution_state="NOT_FOR_BOT",
            execution_result="非本机器人邀请，未审核未执行",
            protocol_error=error,
        )

    async def _resolve_invite_target(
        self, bot, raw_self_id: str, invited_id: str
    ) -> tuple[str, str, str]:
        raw_self_id = str(raw_self_id or "").strip()
        invited_id = str(invited_id or "").strip()
        login_id = ""
        if bot is not None:
            try:
                info = await self._call_action(bot, "get_login_info")
                login_id = str(_get_value(info, "user_id") or "").strip()
            except Exception as exc:
                logger.warning(f"group_invite_guard: get_login_info for target check failed: {exc}")

        if raw_self_id and login_id and raw_self_id != login_id:
            return "NOT_FOR_BOT", login_id, "事件 self_id 与当前客户端账号不一致"
        self_id = login_id or raw_self_id
        if invited_id and self_id and invited_id != self_id:
            return "NOT_FOR_BOT", self_id, "invited_id 与当前客户端账号不一致"
        if self_id:
            return "VERIFIED", self_id, ""
        return "UNVERIFIED", "", "无法确认当前客户端账号，已禁止自动外部动作"

    async def _reconcile_inflight_request(
        self, bot, group_id: str, record_id: str, inviter_qq: str,
        comment: str, self_id: str,
    ) -> None:
        rec = await self._get_invite_record(group_id, record_id)
        if not rec:
            logger.error("group_invite_guard: cannot reconcile missing in-flight record")
            return
        old_state = str(rec.get("execution_state") or "ACTION_IN_FLIGHT")
        membership = await self._membership_state(bot, group_id, self_id)
        action = str(rec.get("decision") or "unknown")
        reason = str(rec.get("decision_reason") or "")
        succeeded = False
        if old_state == "LEAVE_IN_FLIGHT" and membership == "OUT":
            state = "UNEXPECTED_JOIN_LEFT"
            result = "重启后对账：已确认机器人不在群内，退群结果成立"
            succeeded = True
        elif old_state == "LEAVE_IN_FLIGHT" and membership == "IN":
            state = "LEAVE_UNCONFIRMED"
            result = "重启后对账：机器人仍在群内，未重放退群动作"
        elif old_state == "APPROVE_IN_FLIGHT" and membership == "IN":
            state = "APPROVED_RECONCILED"
            result = "重启后对账：已确认入群，未重放同意动作"
            succeeded = True
        else:
            state = "ACTION_OUTCOME_UNKNOWN"
            result = f"重启后对账：{old_state} 的结果无法确认，未重放外部动作"
        saved = await self._update_invite_record(
            group_id, record_id,
            action=result,
            review_state="COMPLETED",
            membership_after=membership,
            action_attempted=True,
            action_succeeded=succeeded,
            auto_executed=True,
            execution_state=state,
            execution_result=result,
            protocol_error="动作结果需人工确认" if not succeeded else "",
        )
        if not saved:
            logger.error(
                "group_invite_guard: failed to persist in-flight reconciliation; "
                "external action will still not be replayed in this process"
            )
        if bot is not None:
            await self._notify(
                bot,
                self._compose_note(
                    inviter_qq, group_id, comment, action, reason,
                    result_label=result, membership_before=str(rec.get("membership_before") or "UNKNOWN"),
                    membership_after=membership,
                    protocol_error="动作结果需人工确认" if not succeeded else "",
                ),
            )

    async def _mark_processing_failure(
        self, group_id: str, record_id: str, exc: Exception
    ) -> None:
        rec = await self._get_invite_record(group_id, record_id)
        state = str((rec or {}).get("execution_state") or "")
        if state.endswith("_IN_FLIGHT") or state == "ACTION_IN_FLIGHT":
            await self._update_invite_record(
                group_id, record_id,
                action="动作结果未知",
                review_state="COMPLETED",
                execution_state="ACTION_OUTCOME_UNKNOWN",
                execution_result="外部动作后处理异常，结果需人工确认",
                protocol_error=str(exc)[:300],
            )
            return
        await self._update_invite_record(
            group_id, record_id,
            action="处理异常",
            review_state="FAILED",
            execution_state="FAILED",
            execution_result="处理流程异常终止",
            protocol_error=str(exc)[:300],
        )

    async def _review_invite_request(
        self, *, bot, group_id: str, inviter_qq: str, comment: str, flag: str,
        sub_type: str, self_id: str, platform_id: str, request_key: str,
        record_id: str, target_state: str, target_error: str,
    ) -> None:
        membership_before = await self._membership_state(bot, group_id, self_id)
        await self._update_invite_record(
            group_id, record_id,
            action="审核中",
            review_state="REVIEWING",
            membership_before=membership_before,
            execution_state="REVIEWING",
        )
        try:
            decision = await self._ask_llm(
                inviter_qq, group_id, comment, bot, platform_id, request_key=request_key
            )
        except Exception as exc:
            logger.error(f"group_invite_guard: LLM decision failed: {exc}")
            decision = {"action": "unknown", "reason": f"LLM error: {exc}"}

        action = str(decision.get("action") or "unknown").strip().lower()
        if action not in ("approve", "reject"):
            action = "unknown"
        reason = str(decision.get("reason") or "").strip()
        reply = str(decision.get("reply") or "").strip()
        alt_warning = str(decision.get("alt_warning") or "").strip()
        profile_snapshot = decision.get("_profile_snapshot")
        if not isinstance(profile_snapshot, dict):
            profile_snapshot = {}
        await self._update_invite_record(
            group_id, record_id,
            decision=action,
            decision_reason=reason,
            review_state="DECIDED",
            execution_state="DECIDED",
            alt_warning=alt_warning,
            profile_snapshot=profile_snapshot,
        )

        membership_after = await self._membership_state(bot, group_id, self_id)
        outcome = await self._execute_invite_decision(
            bot, group_id, record_id, flag, sub_type, self_id, action, reason,
            membership_after, target_state,
        )
        if "membership_after" in outcome:
            membership_after = str(outcome["membership_after"])
        elif outcome.get("refresh_membership"):
            membership_after = await self._membership_state(bot, group_id, self_id)

        reply_status = ""
        may_reply = bool(outcome.get("decision_succeeded"))
        if reply and self._cfg("decision", "reply_inviter_on_decision", True):
            if not may_reply:
                reply_status = "未发送（实际动作未成功，避免承诺结果）"
            elif bot is None:
                reply_status = "未发送（无 OneBot 客户端）"
            elif not inviter_qq:
                reply_status = "未发送（协议端未识别邀请人 QQ）"
            else:
                try:
                    await self._call_action(
                        bot, "send_private_msg", user_id=int(inviter_qq), message=reply
                    )
                    reply_status = "已私聊发送"
                except Exception as exc:
                    logger.error(f"group_invite_guard: reply inviter {inviter_qq} failed: {exc}")
                    reply_status = f"发送失败：{exc}"
        elif reply:
            reply_status = "未发送（回复开关关闭）"

        errors = [str(target_error or ""), str(outcome.get("protocol_error") or "")]
        if not flag:
            errors.append("邀请请求缺少 flag，无法调用审批接口")
        protocol_error = "；".join(dict.fromkeys(err for err in errors if err))
        result_label = str(outcome["result"])
        saved = await self._update_invite_record(
            group_id, record_id,
            action=result_label,
            decision=action,
            decision_reason=reason,
            review_state="COMPLETED",
            membership_after=membership_after,
            action_attempted=bool(outcome["attempted"]),
            action_succeeded=bool(outcome["succeeded"]),
            auto_executed=bool(outcome["attempted"]),
            execution_state=str(outcome["state"]),
            execution_result=result_label,
            protocol_error=protocol_error,
            reply=reply,
            reply_status=reply_status,
            alt_warning=alt_warning,
        )
        if not saved:
            replay_guard = (
                "persisted IN_FLIGHT state prevents automatic replay"
                if outcome["attempted"] else "no external action was attempted"
            )
            logger.error(
                f"group_invite_guard: final invite state persistence failed for "
                f"{record_id}; {replay_guard}"
            )

        if bot is not None:
            note = self._compose_note(
                inviter_qq, group_id, comment, action, reason, reply,
                reply_status, alt_warning, result_label,
                membership_before=membership_before,
                membership_after=membership_after,
                protocol_error=protocol_error,
            )
            await self._notify(bot, note)

    async def _execute_invite_decision(
        self, bot, group_id: str, record_id: str, flag: str, sub_type: str,
        self_id: str, action: str, reason: str, membership: str,
        target_state: str,
    ) -> dict:
        outcome = {
            "attempted": False, "succeeded": False, "decision_succeeded": False,
            "refresh_membership": False, "state": "NO_ACTION",
            "result": "仅通知管理员（未自动处理）", "protocol_error": "",
        }
        if action == "unknown":
            outcome.update(state="LLM_FAILED", result="LLM 未给出有效决策")
            return outcome
        if bot is None:
            outcome.update(state="NO_CLIENT", result="未执行（无 OneBot 客户端）")
            return outcome
        if target_state != "VERIFIED":
            outcome.update(
                state="TARGET_UNVERIFIED",
                result="目标机器人身份未确认，仅保留 LLM 审核结果",
            )
            return outcome
        if membership == "UNKNOWN":
            outcome.update(state="MEMBERSHIP_UNKNOWN", result="未执行（成员状态未知）")
            return outcome

        if membership == "IN":
            if action == "approve":
                outcome.update(
                    succeeded=True, decision_succeeded=True,
                    state="EXTERNAL_JOIN_APPROVED",
                    result="外部提前入群，LLM 审核同意（未重复审批）",
                )
                return outcome
            return await self._compensate_unexpected_join(
                bot, group_id, self_id, record_id, action, reason
            )

        enabled = (
            action == "approve" and _as_bool(self._cfg("decision", "auto_approve", False))
        ) or (
            action == "reject" and _as_bool(self._cfg("decision", "auto_reject", False))
        )
        if not enabled:
            return outcome
        if not flag:
            outcome.update(state="MISSING_FLAG", result="未执行（请求缺少 flag）")
            return outcome

        in_flight = "APPROVE_IN_FLIGHT" if action == "approve" else "REJECT_IN_FLIGHT"
        prepared = await self._update_invite_record(
            group_id, record_id,
            decision=action,
            decision_reason=reason,
            review_state="DECIDED",
            execution_state=in_flight,
            execution_result="外部审批动作即将执行",
            action_attempted=True,
            action_succeeded=False,
            auto_executed=True,
        )
        if not prepared:
            logger.error(
                f"group_invite_guard: refused {action}; failed to persist {in_flight}"
            )
            outcome.update(
                state="PRE_ACTION_PERSIST_FAILED",
                result="动作前状态持久化失败，未执行自动审批",
                protocol_error="无法持久化动作前状态",
            )
            return outcome

        outcome["attempted"] = True
        outcome["refresh_membership"] = True
        try:
            await self._call_action(
                bot, "set_group_add_request", flag=flag, sub_type=sub_type,
                approve=action == "approve",
                reason="" if action == "approve" else (reason or "bot 自动拒绝"),
            )
            outcome.update(
                succeeded=True, decision_succeeded=True,
                state="APPROVED" if action == "approve" else "REJECTED",
                result="自动同意进群" if action == "approve" else "自动拒绝",
            )
        except Exception as exc:
            error = str(exc)
            if action == "approve" and self._is_explicit_already_agreed(exc):
                verified = await self._membership_state(bot, group_id, self_id)
                if verified == "IN":
                    outcome.update(
                        succeeded=True, decision_succeeded=True,
                        state="ALREADY_APPROVED_VERIFIED",
                        result="协议端明确已同意，且已核实入群",
                    )
                    return outcome
            logger.error(f"group_invite_guard: {action} action failed: {exc}")
            outcome.update(
                state="ACTION_FAILED",
                result="自动同意失败" if action == "approve" else "自动拒绝失败",
                protocol_error=error[:300],
            )
        return outcome

    async def _compensate_unexpected_join(
        self, bot, group_id: str, self_id: str, record_id: str,
        action: str, reason: str,
    ) -> dict:
        mode = str(
            self._cfg("unexpected_join", "mode", "notify_only") or "notify_only"
        ).strip().lower()
        if mode not in ("notify_only", "leave", "message_then_leave"):
            mode = "notify_only"
        if mode == "notify_only":
            return {
                "attempted": False, "succeeded": False, "decision_succeeded": False,
                "refresh_membership": False, "state": "UNEXPECTED_JOIN_NOTIFIED",
                "result": "异常提前入群；LLM 拒绝，按配置仅通知未退群", "protocol_error": "",
            }

        prepared = await self._update_invite_record(
            group_id, record_id,
            decision=action,
            decision_reason=reason,
            review_state="DECIDED",
            execution_state="LEAVE_IN_FLIGHT",
            execution_result="异常入群补偿即将执行",
            action_attempted=True,
            action_succeeded=False,
            auto_executed=True,
        )
        if not prepared:
            logger.error("group_invite_guard: refused leave; failed to persist LEAVE_IN_FLIGHT")
            return {
                "attempted": False, "succeeded": False, "decision_succeeded": False,
                "refresh_membership": False, "state": "PRE_ACTION_PERSIST_FAILED",
                "result": "动作前状态持久化失败，未执行退群",
                "protocol_error": "无法持久化动作前状态",
            }

        errors = []
        if mode == "message_then_leave":
            message = str(
                self._cfg("unexpected_join", "custom_leave_message", "") or ""
            ).strip()
            if message:
                try:
                    await self._call_action(
                        bot, "send_group_msg", group_id=int(group_id), message=message
                    )
                except Exception as exc:
                    errors.append(f"群消息发送失败：{exc}")
            else:
                errors.append("自定义退群语句为空")
        try:
            await self._call_action(
                bot, "set_group_leave", group_id=int(group_id), is_dismiss=False
            )
        except Exception as exc:
            errors.append(f"退群失败：{exc}")
            membership_after = await self._membership_state(bot, group_id, self_id)
            return {
                "attempted": True, "succeeded": False, "decision_succeeded": False,
                "refresh_membership": False, "membership_after": membership_after,
                "state": "UNEXPECTED_JOIN_LEAVE_FAILED",
                "result": "异常提前入群；LLM 拒绝，但退群调用失败",
                "protocol_error": "；".join(errors),
            }

        membership_after = await self._membership_state(bot, group_id, self_id)
        if membership_after == "OUT":
            result = "异常提前入群；LLM 拒绝，已核实退群"
            if errors:
                result += "（" + "；".join(errors) + "）"
            return {
                "attempted": True, "succeeded": True, "decision_succeeded": True,
                "refresh_membership": False, "membership_after": membership_after,
                "state": "UNEXPECTED_JOIN_LEFT", "result": result,
                "protocol_error": "；".join(errors),
            }
        if membership_after == "IN":
            errors.append("退群调用后机器人仍在群内")
            state = "LEAVE_UNCONFIRMED"
            result = "异常提前入群；退群调用未确认成功，机器人仍在群内"
        else:
            errors.append("退群调用后成员状态未知")
            state = "ACTION_OUTCOME_UNKNOWN"
            result = "异常提前入群；退群结果未知，需人工确认"
        return {
            "attempted": True, "succeeded": False, "decision_succeeded": False,
            "refresh_membership": False, "membership_after": membership_after,
            "state": state, "result": result, "protocol_error": "；".join(errors),
        }

    @staticmethod
    def _is_explicit_already_agreed(exc: Exception) -> bool:
        text = str(exc or "")
        return "120162002" in text or bool(
            re.search(r"\balready[ _-]+agree(?:d)?\b", text, re.I)
            or re.search(r"(?:请求|邀请)?已(?:经)?同意", text)
        )

    @filter.custom_filter(PrivateInviteIntentFilter)
    async def on_private_invite_intent(self, event: AstrMessageEvent):
        if not self._cfg("basic", "enable", True):
            return
        if not self._cfg("private_intent", "enable_private_intent", True):
            return

        text = (event.get_message_str() or "").strip()
        if not text:
            return

        # 粗筛：明显不是加群意图就交还给正常私聊，不额外调 LLM
        if not self._looks_like_invite_intent(text):
            return
        if (
            self._cfg("private_intent", "enable_blacklist_inquiry", True)
            and self._looks_like_blacklist_inquiry(text)
        ):
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

        if self._cfg("private_intent", "private_intent_reply", True) and reply:
            try:
                await event.send(MessageChain(chain=[Plain(reply)]))
            except Exception as exc:
                logger.error(f"group_invite_guard: reply private failed: {exc}")

        note = self._compose_private_note(sender_id, text, reply, reason)
        bot = self._find_onebot_client(event)
        if bot is None:
            logger.error("group_invite_guard: no OneBot client for private intent notify")
            return
        if self._cfg("private_intent", "private_intent_notify", True):
            await self._notify(bot, note)

    def _looks_like_blacklist_inquiry(self, text: str) -> bool:
        """粗筛：消息里出现配置的关键词时，才交 LLM 判断。"""
        raw = str(self._cfg("private_intent", "blacklist_inquiry_keywords") or "").strip()
        if not raw:
            return any(kw in text for kw in _BLACKLIST_INQUIRY_KEYWORDS)
        keywords = [k.strip() for k in raw.split(",") if k.strip()]
        if not keywords:
            return any(kw in text for kw in _BLACKLIST_INQUIRY_KEYWORDS)
        return any(kw in text for kw in keywords)

    @staticmethod
    def _tone_description(tone: str) -> str:
        """把语气配置转成给 LLM 的自然语言描述。"""
        return {
            "serious": "严肃、正式，不带情绪，明确说明规则",
            "polite": "委婉、客气，给对方留面子，但立场坚定",
            "cold": "冷漠、简短，不解释太多，只陈述结果",
        }.get(str(tone or "").strip().lower(), "使用你当前人格设定的语气和风格")

    async def _build_blacklist_inquiry_variables(self, sender_id: str, text: str, platform_id: str = None) -> dict:
        """拼装黑名单询问提示词可用的所有变量。"""
        decision_persona = str(self._cfg("decision", "decision_persona") or "").strip()
        persona_prompt = await self._resolve_persona_prompt(decision_persona, platform_id)

        ban_entry = self._find_ban_entry(sender_id)
        ban_reason = ""
        ban_time_str = ""
        is_banned = ban_entry is not None
        if ban_entry:
            ban_reason = str(ban_entry.get("reason") or "未注明").strip()
            try:
                ban_ts = int(ban_entry.get("ban_time") or 0)
                ban_time_str = datetime.fromtimestamp(ban_ts).strftime("%Y-%m-%d %H:%M") if ban_ts else ""
            except Exception:
                ban_time_str = ""

        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception:
            records = {}
        invited_records = self._find_invite_records_by_inviter(
            self._normalize_invite_records(records), sender_id
        )
        invite_summary = ""
        if invited_records:
            rec = invited_records[0]
            action = str(rec.get("action") or "").strip()
            decision_reason = str(rec.get("decision_reason") or "").strip()
            invite_summary = f"处理结果={action}"
            if decision_reason:
                invite_summary += f"，理由={decision_reason}"

        tone = str(self._cfg("private_intent", "blacklist_inquiry_tone") or "persona").strip().lower()
        show_reason = _as_bool(self._cfg("private_intent", "blacklist_inquiry_show_reason", True))
        max_length = int(self._cfg("private_intent", "blacklist_inquiry_max_length", 120) or 120)

        return {
            "sender_id": str(sender_id or "").strip(),
            "text": str(text or "").strip(),
            "is_banned": "是" if is_banned else "否",
            "ban_reason": ban_reason or "未注明",
            "ban_time": ban_time_str or "未知",
            "invite_summary": invite_summary or "无邀请记录",
            "persona_prompt": persona_prompt or "你是一个 QQ 机器人助手。",
            "tone": self._tone_description(tone),
            "show_reason": "是" if show_reason else "否",
            "max_length": str(max_length),
        }

    @staticmethod
    def _format_prompt_template(template: str, variables: dict) -> str:
        """用变量字典替换模板中的 {key}；模板为空返回空字符串。"""
        if not template:
            return ""
        result = template
        for key, val in variables.items():
            result = result.replace("{" + key + "}", str(val))
        return result

    async def _ask_blacklist_inquiry(self, sender_id: str, text: str, platform_id: str = None) -> dict:
        """让 LLM 判断对方是否在询问拉黑/拒绝/踢人原因，并生成人格化回复。"""
        provider_id = self._cfg("decision", "llm_provider_id") or self._default_provider_id()
        if not provider_id:
            raise RuntimeError("no llm provider id configured")

        variables = await self._build_blacklist_inquiry_variables(sender_id, text, platform_id)

        custom_system = str(self._cfg("private_intent", "blacklist_inquiry_system_prompt") or "").strip()
        if custom_system:
            system_prompt = self._format_prompt_template(custom_system, variables)
        else:
            system_prompt = (
                "{persona_prompt}\n\n"
                "现在有人私聊你，询问为什么被拉黑、被踢、被拒绝进群或能不能解封。"
                "你的回复语气基调是：{tone}。"
                "不要暴露管理员 QQ、具体群号、具体审核细节等敏感信息。"
                "如果对方不是被拉黑 QQ 本人（如朋友代问或开小号），说明只能由本人沟通。"
            ).format(**variables)

        custom_user = str(self._cfg("private_intent", "blacklist_inquiry_user_prompt") or "").strip()
        if custom_user:
            prompt = self._format_prompt_template(custom_user, variables)
        else:
            prompt = (
                "对方 QQ：{sender_id}\n"
                "对方私聊消息：{text}\n"
                "该 QQ 是否在你的黑名单中：{is_banned}\n"
                "拉黑原因：{ban_reason}\n"
                "拉黑时间：{ban_time}\n"
                "邀请记录摘要：{invite_summary}\n"
                "回复中是否说明原因：{show_reason}\n"
                "回复最大字数：{max_length}\n\n"
                "请判断对方是否在询问拉黑/拒绝/踢人/解封相关问题。"
                "只输出一个 JSON 对象：{"
                "\"is_inquiry\": true 或 false, "
                "\"reply\": \"若为询问，给对方的简短回复（控制在 {max_length} 字内）；否则空字符串\", "
                "\"reason\": \"给管理员的简短说明\"}"
            ).format(**variables)

        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        result_text = (getattr(resp, "completion_text", "") or "").strip()
        result = _parse_json(result_text)

        # 后处理：截断过长回复
        try:
            max_len = int(self._cfg("private_intent", "blacklist_inquiry_max_length", 120) or 120)
        except Exception:
            max_len = 120
        reply = str(result.get("reply") or "").strip()
        if reply and max_len > 0 and len(reply) > max_len:
            result["reply"] = reply[:max_len] + "…"
        return result

    def _compose_blacklist_inquiry_note(self, sender_id: str, text: str, reply: str, reason: str, is_banned: bool) -> str:
        lines = [
            "[私聊询问拉黑/拒绝原因]",
            f"对方 QQ：{sender_id or '(未知)'}",
            f"是否在黑名单：{'是' if is_banned else '否'}",
            f"对方消息：{text}",
        ]
        if reason:
            lines.append(f"判断说明：{reason}")
        if reply:
            lines.append(f"已自动回复：{reply}")
        return "\n".join(lines)

    @filter.custom_filter(PrivateBlacklistInquiryFilter)
    async def on_private_blacklist_inquiry(self, event: AstrMessageEvent):
        if not self._cfg("basic", "enable", True):
            return
        if not self._cfg("private_intent", "enable_blacklist_inquiry", True):
            return

        text = (event.get_message_str() or "").strip()
        if not text:
            return

        if not self._looks_like_blacklist_inquiry(text):
            return

        platform_id = None
        try:
            platform_id = event.get_platform_id()
        except Exception:
            pass

        sender_id = event.get_sender_id() or ""
        try:
            decision = await self._ask_blacklist_inquiry(sender_id, text, platform_id)
        except Exception as exc:
            logger.error(f"group_invite_guard: blacklist inquiry LLM failed: {exc}")
            return

        if not _as_bool(decision.get("is_inquiry")):
            return

        # 确认是询问，接管这条私聊
        try:
            event.stop_event()
            event.should_call_llm(False)
        except Exception:
            pass

        reply = str(decision.get("reply") or "").strip()
        reason = str(decision.get("reason") or "").strip()

        if self._cfg("private_intent", "blacklist_inquiry_reply", True) and reply:
            try:
                await event.send(MessageChain(chain=[Plain(reply)]))
            except Exception as exc:
                logger.error(f"group_invite_guard: reply blacklist inquiry failed: {exc}")

        bot = self._find_onebot_client(event)
        if bot is None:
            logger.error("group_invite_guard: no OneBot client for blacklist inquiry notify")
            return
        if self._cfg("private_intent", "blacklist_inquiry_notify", True):
            is_banned = self._find_ban_entry(sender_id) is not None
            note = self._compose_blacklist_inquiry_note(sender_id, text, reply, reason, is_banned)
            await self._notify(bot, note)

    @filter.custom_filter(GroupJoinFilter)
    async def on_group_join(self, event: AstrMessageEvent):
        if not self._cfg("basic", "enable", True):
            return
        if not self._cfg("kick_revenge", "record_group_join", True):
            return

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return

        group_id = str(_get_value(raw, "group_id") or "")
        if not group_id:
            return
        operator_id = str(_get_value(raw, "operator_id") or "")
        self_id = str(_get_value(raw, "self_id") or "")
        admission = await self._find_admission_for_join(group_id, self_id)
        await self._record_join(
            group_id,
            operator_id,
            request_key=str((admission or {}).get("request_key") or ""),
            inviter=str((admission or {}).get("inviter") or ""),
        )
        if admission:
            await self._update_invite_record(
                group_id,
                str(admission.get("record_id") or ""),
                membership_after="IN",
                admission_bound=True,
                admission_time=int(time.time()),
            )

    @filter.custom_filter(GroupKickFilter)
    async def on_group_kick(self, event: AstrMessageEvent):
        if not self._cfg("basic", "enable", True):
            return
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return

        group_id = str(_get_value(raw, "group_id") or "")
        if not group_id:
            return

        operator_id = str(_get_value(raw, "operator_id") or "").strip()
        self_id = str(_get_value(raw, "self_id") or "").strip()
        if operator_id and self_id and operator_id == self_id:
            operator_id = ""  # 机器人自己退群，不算被踢操作者

        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load invite_records failed: {exc}")
            records = {}
        inviter_qq = await self._admission_inviter(group_id, records)

        mode = str(self._cfg("kick_revenge", "revenge_mode", "off") or "off").strip().lower()
        revenge_on = mode in ("delete_friend", "delete_and_ban")
        ban_operator_on = _as_bool(self._cfg("kick_revenge", "kick_ban_operator", False))
        notify_on = self._cfg("kick_revenge", "revenge_notify", True)

        need_bot = (inviter_qq and revenge_on) or ban_operator_on or (not inviter_qq and notify_on)
        bot = None
        if need_bot:
            bot = self._find_onebot_client(event)
            if bot is None:
                logger.error("group_invite_guard: no OneBot client for kick handling")
                return

        # 报复邀请人（仅 revenge_mode 开启且查到邀请人时）
        banned = set()
        result = ""
        cross_line = ""
        if inviter_qq and revenge_on:
            # 跨群连坐：先退出该邀请人邀请过的其它群，再报复（消息仍只发一次）
            cross_line = await self._cross_group_leave(inviter_qq, group_id, records, bot)
            result = await self._take_revenge(inviter_qq, bot)
            await self._mark_inviter_dealt(inviter_qq)
            banned.add(inviter_qq)
            logger.info(
                f"group_invite_guard: revenge on {inviter_qq} for group {group_id}: {result}"
            )

        # 拉黑踢人者（独立开关，不依赖 revenge_mode 与邀请人记录）
        ban_line = await self._ban_kick_operator(operator_id, bot, banned)

        if not inviter_qq:
            logger.info(
                f"group_invite_guard: no inviter record for group {group_id}, skip revenge"
            )
            # 查不到邀请人时不依赖 revenge_mode，只要 revenge_notify 开着就通知管理员
            if notify_on:
                note = await self._compose_kick_no_inviter_note(group_id)
                if ban_line:
                    note += "\n" + ban_line
                await self._notify(bot, note)
            return

        if not revenge_on:
            # 报复关闭但拉黑了踢人者时，通知里告知管理员
            if ban_line and notify_on:
                note = f"[被踢通知]\n群号：{group_id}\n{ban_line}"
                await self._notify(bot, note)
            return

        if notify_on:
            note = self._compose_revenge_note(group_id, inviter_qq, result)
            if cross_line:
                note += "\n" + cross_line
            if ban_line:
                note += "\n" + ban_line
            await self._notify(bot, note)

    async def _cross_group_leave(self, inviter_qq: str, exclude_group: str, records, bot) -> str:
        """跨群连坐：退出该邀请人邀请过的所有其它群（invite_records 记录保留不删）；返回通知用结果行，未执行返回空。"""
        if not _as_bool(self._cfg("kick_revenge", "cross_group_retaliation", False)):
            return ""
        inviter_qq = str(inviter_qq or "").strip()
        if not inviter_qq or bot is None:
            return ""
        normalized = self._normalize_invite_records(records)
        groups = [
            str(gid)
            for gid, recs in normalized.items()
            if str(gid) != str(exclude_group)
            and any(self._record_inviter(r) == inviter_qq for r in recs)
        ]
        if not groups:
            return ""
        parts = []
        for gid in groups:
            try:
                await self._call_action(bot, "set_group_leave", group_id=int(gid), is_dismiss=False)
                parts.append(f"已连带退出群 {gid}")
            except Exception as exc:
                parts.append(f"连带退群 {gid} 失败：{exc}")
        logger.info(
            f"group_invite_guard: cross-group leave for inviter {inviter_qq}: {'；'.join(parts)}"
        )
        return "连坐退群：" + "；".join(parts)

    async def _ban_kick_operator(self, operator_id: str, bot, already_banned: set) -> str:
        """被踢时按 kick_ban_operator 拉黑踢人者，走与手动拉黑相同的完整流程；返回通知用结果行，未执行返回空。"""
        if not _as_bool(self._cfg("kick_revenge", "kick_ban_operator", False)):
            return ""
        operator_id = str(operator_id or "").strip()
        if not operator_id:
            return ""
        if operator_id in already_banned:
            return f"踢人者 {operator_id} 与邀请人为同一人，已在报复中拉黑"
        if bot is None:
            return ""
        result = await self._manual_blacklist(operator_id, bot)
        logger.info(f"group_invite_guard: banned kick operator {operator_id}: {result}")
        return f"已按设置拉黑踢人者 {operator_id}：{result}"

    async def _find_admission_for_join(self, group_id: str, self_id: str) -> dict | None:
        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception:
            return None
        now = int(time.time())
        candidates = []
        for rec in self._normalize_invite_records(records).get(str(group_id), []):
            if str(rec.get("target_state") or "") == "NOT_FOR_BOT":
                continue
            target_self = str(rec.get("self_id") or "")
            if self_id and target_self and self_id != target_self:
                continue
            try:
                age = now - int(rec.get("time") or 0)
            except Exception:
                continue
            if age < -60 or age > 600:
                continue
            state = str(rec.get("execution_state") or "")
            if state == "REJECTED" and _as_bool(rec.get("action_succeeded")):
                continue
            if state == "UNEXPECTED_JOIN_LEFT" and _as_bool(rec.get("action_succeeded")):
                continue
            candidates.append(rec)
        if not candidates:
            return None
        return max(candidates, key=lambda rec: int(rec.get("time") or 0))

    async def _admission_inviter(self, group_id: str, invite_records=None) -> str:
        try:
            joins = await self.get_kv_data("join_records", {})
        except Exception:
            joins = {}
        join = joins.get(str(group_id)) if isinstance(joins, dict) else None
        if isinstance(join, dict):
            inviter = str(join.get("inviter") or "").strip()
            if inviter:
                return inviter
        if invite_records is None:
            try:
                invite_records = await self.get_kv_data("invite_records", {})
            except Exception:
                invite_records = {}
        relevant = [
            rec for rec in self._normalize_invite_records(invite_records).get(str(group_id), [])
            if str(rec.get("target_state") or "") != "NOT_FOR_BOT"
        ]
        if not relevant:
            return ""
        latest = max(relevant, key=lambda rec: int(rec.get("time") or 0))
        return self._record_inviter(latest)

    async def _record_join(
        self, group_id: str, operator_id: str, request_key: str = "", inviter: str = ""
    ) -> None:
        """记录机器人进群及其关联邀请。"""
        group_id = str(group_id or "").strip()
        if not group_id:
            return
        try:
            records = await self.get_kv_data("join_records", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load join_records failed: {exc}")
            records = {}
        if not isinstance(records, dict):
            records = {}
        records[group_id] = {
            "time": int(time.time()),
            "operator": str(operator_id or "").strip(),
            "request_key": str(request_key or "").strip(),
            "inviter": str(inviter or "").strip(),
        }
        if len(records) > 100:
            def _join_ts(item):
                rec = item[1]
                if isinstance(rec, dict):
                    try:
                        return int(rec.get("time") or 0)
                    except Exception:
                        return 0
                return 0

            keep = sorted(records.items(), key=_join_ts, reverse=True)[:100]
            records = dict(keep)
        try:
            await self.put_kv_data("join_records", records)
        except Exception as exc:
            logger.warning(f"group_invite_guard: save join_records failed: {exc}")

    async def _compose_kick_no_inviter_note(self, group_id: str) -> str:
        """被踢但查不到邀请人记录时的管理员通知；附带已知进群记录。"""
        kick_time = datetime.fromtimestamp(int(time.time())).strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "[被踢通知（无邀请人记录）]",
            f"群号：{group_id}",
            f"被踢时间：{kick_time}",
            "未记录到邀请人（可能是管理员直接拉群，无邀请事件）",
        ]
        try:
            join_records = await self.get_kv_data("join_records", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load join_records failed: {exc}")
            join_records = {}
        rec = join_records.get(group_id) if isinstance(join_records, dict) else None
        if isinstance(rec, dict):
            try:
                join_ts = datetime.fromtimestamp(int(rec.get("time") or 0)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                join_ts = "-"
            lines.append(f"进群时间：{join_ts}")
            operator = str(rec.get("operator") or "").strip()
            lines.append(f"进群操作人 QQ：{operator or '(未知)'}")
        return "\n".join(lines)

    @filter.custom_filter(GroupMuteFilter)
    async def on_group_mute(self, event: AstrMessageEvent):
        if not self._cfg("basic", "enable", True):
            return
        if not self._cfg("mute_revenge", "mute_retaliation_enable", False):
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

        try:
            count = int(records.get(group_id, 0) or 0) + 1
        except (TypeError, ValueError):
            logger.warning(f"group_invite_guard: mute_records[{group_id}] 非数字，按 1 计")
            count = 1
        records[group_id] = count
        try:
            await self.put_kv_data("mute_records", records)
        except Exception as exc:
            logger.warning(f"group_invite_guard: save mute_records failed: {exc}")

        try:
            threshold = int(self._cfg("mute_revenge", "mute_threshold", 3) or 3)
        except (TypeError, ValueError):
            threshold = 3

        bot = self._find_onebot_client(event)
        if bot is None:
            logger.error("group_invite_guard: no OneBot client for mute retaliation")
            return

        if count < threshold:
            if self._cfg("mute_revenge", "mute_notify", True):
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
        target = str(self._cfg("mute_revenge", "mute_target", "operator") or "operator").strip().lower()
        inviter_qq = ""
        invite_records = {}
        targets = []
        if target in ("operator", "both") and operator_id:
            targets.append(operator_id)
        if target in ("inviter", "both"):
            try:
                invite_records = await self.get_kv_data("invite_records", {})
            except Exception as exc:
                logger.warning(f"group_invite_guard: load invite_records failed: {exc}")
                invite_records = {}
            inviter_qq = await self._admission_inviter(group_id, invite_records)
            if inviter_qq:
                targets.append(inviter_qq)

        # 跨群连坐：拉黑对象包含邀请人时，先连带退出 TA 邀请过的其它群（operator 模式不连坐）
        cross_line = ""
        if inviter_qq:
            cross_line = await self._cross_group_leave(inviter_qq, group_id, invite_records, bot)

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

        if self._cfg("mute_revenge", "mute_notify", True):
            note = self._compose_mute_revenge_note(group_id, count, leave_result, ban_results)
            if cross_line:
                note += "\n" + cross_line
            await self._notify(bot, note)

    def _looks_like_invite_intent(self, text: str) -> bool:
        low = text.lower()
        if any(mark in low for mark in _INVITE_LINK_MARKS):
            return True
        if any(kw in text for kw in _INVITE_KEYWORDS):
            return True
        return False

    async def _ask_llm(
        self, inviter_qq: str, group_id: str, comment: str, bot=None,
        platform_id: str = None, request_key: str = "",
    ) -> dict:
        provider_id = self._cfg("decision", "llm_provider_id") or self._default_provider_id()
        if not provider_id:
            raise RuntimeError("no llm provider id configured")

        decision_persona = str(self._cfg("decision", "decision_persona") or "").strip()
        persona_prompt = await self._resolve_persona_prompt(decision_persona, platform_id)
        system_prompt = persona_prompt or "你是一个 QQ 机器人助手。"

        context, alt_warning, profile_snapshot = await self._build_invite_context(
            inviter_qq, group_id, bot, comment, request_key=request_key
        )
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
            "只输出一个 JSON 对象：{\"action\": \"approve\" 或 \"reject\", \"reason\": \"简短理由\", "
            "\"reply\": \"以你人格身份对邀请人说的话\"}。"
            "其中 reply 要简短（一两句）、符合你的人格、不要暴露详细审核细节："
            "approve 时是同意前的打招呼/说明，reject 时是委婉的拒绝（不要写太详细的原因）。"
        )

        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        text = (getattr(resp, "completion_text", "") or "").strip()
        decision = _parse_json(text)
        if alt_warning:
            decision["alt_warning"] = alt_warning
        if profile_snapshot:
            decision["_profile_snapshot"] = profile_snapshot
        return decision

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

    async def _build_invite_context(
        self, inviter_qq: str, group_id: str, bot, comment: str = "",
        request_key: str = "",
    ):
        """收集决策背景，返回 (文本, 小号提示, 结构化画像快照)。"""
        async def _member():
            if self._cfg("decision", "enable_member_context", True):
                return await self._build_member_section(group_id, bot)
            return ""

        async def _impression():
            if self._cfg("decision", "enable_impression_context", True):
                return await self._build_impression_section(inviter_qq, group_id)
            return ("", 0)

        fetched = await asyncio.gather(_member(), _impression(), return_exceptions=True)
        member_section = fetched[0] if isinstance(fetched[0], str) else ""
        impression = fetched[1] if isinstance(fetched[1], tuple) else ("", 0)
        impression_section, speaker_count = impression

        profile_snapshot = {}
        external_section = ""
        internal_section = ""
        if self._cfg("decision", "enable_user_profile", True):
            if self._cfg("decision", "use_profile_plugin", True):
                external_section, profile_snapshot = await self._fetch_external_decision_profile(
                    inviter_qq, exclude_request_key=request_key
                )
            # 守卫前科始终独立补充，不能因外部画像有内容而丢失。
            internal_section = await self._build_profile_section(
                inviter_qq, speaker_count, exclude_request_key=request_key
            )

        alt_warning = await self._detect_alt_account(
            inviter_qq, comment, bot, exclude_request_key=request_key
        )

        marker = str(self._cfg("decision", "truncate_marker", "…") or "…")

        def _clip(text: str, limit: int) -> str:
            text = str(text or "").strip()
            return text if len(text) <= limit else text[:limit] + marker

        # 重要风险信息优先，且每个区块单独限长，避免成员/原话挤掉画像。
        sections = [
            _clip(external_section, 1200),
            _clip(internal_section, 800),
            _clip(member_section, 900),
            _clip(impression_section, 1300),
        ]
        context = "\n\n".join(section for section in sections if section)
        if alt_warning:
            context = f"{_clip(alt_warning, 600)}\n\n{context}" if context else _clip(alt_warning, 600)
        if len(context) > 4000:
            context = context[:4000] + marker
        return context, alt_warning, profile_snapshot

    async def _collect_profile_data(self, qq: str, exclude_request_key: str = "") -> dict:
        """汇总某 QQ 的本地画像数据，可排除正在审核的邀请。"""
        qq = str(qq or "").strip()
        data = {
            "qq": qq,
            "invited": [],      # [(群号, 记录)]：该 QQ 邀请 bot 进过的群
            "operated": [],     # [群号]：该 QQ 作为操作人拉 bot 进过的群
            "rejected": 0,      # 历史邀请被拒绝次数
            "mute_total": 0,    # TA 邀请的群累计禁言 bot 次数
            "ban_entry": None,  # 黑名单记录 dict（含原因/拉黑时间），不在黑名单为 None
        }
        if not qq:
            return data

        async def _load(key):
            try:
                return await self.get_kv_data(key, {})
            except Exception:
                return {}

        invite_records, join_records, mute_records = await asyncio.gather(
            _load("invite_records"), _load("join_records"), _load("mute_records")
        )
        invite_records = invite_records if isinstance(invite_records, dict) else {}
        join_records = join_records if isinstance(join_records, dict) else {}
        mute_records = mute_records if isinstance(mute_records, dict) else {}

        normalized = self._normalize_invite_records(invite_records)
        invited = [
            (str(gid), rec)
            for gid, recs in normalized.items()
            for rec in recs
            if self._record_inviter(rec) == qq
            and (
                not exclude_request_key
                or str(rec.get("request_key") or "") != exclude_request_key
            )
        ]
        data["invited"] = invited
        data["operated"] = [
            str(gid)
            for gid, rec in join_records.items()
            if isinstance(rec, dict) and str(rec.get("operator") or "").strip() == qq
        ]
        data["ban_entry"] = self._find_ban_entry(qq)
        data["rejected"] = sum(
            1
            for _, rec in invited
            if isinstance(rec, dict)
            and (
                str(rec.get("decision") or "").strip().lower() == "reject"
                or str(rec.get("execution_state") or "").strip().upper() == "REJECTED"
                or "拒绝" in str(rec.get("action") or "")
            )
        )
        mute_total = 0
        # mute_records 是按群累计；同群多次邀请不能重复叠加同一群的禁言数。
        for gid in {group_id for group_id, _ in invited}:
            try:
                mute_total += int(mute_records.get(gid, 0) or 0)
            except (TypeError, ValueError):
                continue
        data["mute_total"] = mute_total
        return data

    def _find_ban_entry(self, qq: str) -> dict | None:
        """在 qq_tools 黑名单里找某 QQ 的完整记录（含原因/拉黑时间），找不到或黑名单不可用返回 None。

        兼容历史格式字段：优先 user_id，也接受 qq / id / uin。
        """
        config, _ = self._get_ban_config()
        if config is None:
            return None
        try:
            ban_list = config.get("ban_list")
        except Exception:
            return None
        if not isinstance(ban_list, list):
            return None
        qq = str(qq or "").strip()
        for item in ban_list:
            if not isinstance(item, dict):
                continue
            for field in ("user_id", "qq", "id", "uin"):
                if str(item.get(field) or "").strip() == qq:
                    return item
        return None

    async def _is_user_banned(self, qq: str) -> tuple[bool, str]:
        """综合判断某 QQ 是否应被拦截：qq_tools 黑名单 / 邀请记录已拉黑。

        返回 (是否拦截, 原因文本)。
        """
        qq = str(qq or "").strip()
        if not qq:
            return False, ""

        entry = self._find_ban_entry(qq)
        if entry:
            reason = str(entry.get("reason") or "未注明").strip()
            return True, f"在黑名单中（{reason}）"

        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception:
            records = {}
        if isinstance(records, dict):
            normalized = self._normalize_invite_records(records)
            for recs in normalized.values():
                for rec in recs:
                    if self._record_inviter(rec) == qq and _as_bool(rec.get("dealt")):
                        return True, "邀请记录已标记拉黑"

        return False, ""

    def _get_profile_plugin_instance(self):
        """获取用户画像插件实例；未安装或不可用时返回 None。"""
        try:
            md = self.context.get_registered_star("astrbot_plugin_user_profile")
        except Exception:
            return None
        return getattr(md, "star_cls", None) if md else None

    @staticmethod
    def _sanitize_profile_snapshot(raw: Any) -> dict:
        """限制外部画像快照字段和体积，避免复制原话或不可序列化对象。"""
        if not isinstance(raw, dict) or not raw:
            return {}
        recognized = {"provider", "score", "level", "tags", "activity", "social_origin"}
        if not recognized.intersection(raw):
            return {}

        def _safe_int(value, default=0) -> int:
            try:
                return int(value or default)
            except (TypeError, ValueError):
                return int(default)

        tags = []
        raw_tags = raw.get("tags")
        if not isinstance(raw_tags, list):
            raw_tags = []
        for item in raw_tags:
            if not isinstance(item, dict):
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
            except (TypeError, ValueError):
                confidence = 0.0
            tags.append(
                {
                    "tag": str(item.get("tag") or "")[:64],
                    "confidence": round(confidence, 2),
                    "source": str(item.get("source") or "")[:32],
                    "evidence": str(item.get("evidence") or "")[:200],
                }
            )
            if len(tags) >= 12:
                break

        activity_raw = raw.get("activity") if isinstance(raw.get("activity"), dict) else {}
        activity = {}
        for key in (
            "group_messages", "private_messages", "active_groups", "first_seen", "last_seen"
        ):
            try:
                activity[key] = max(0, int(activity_raw.get(key) or 0))
            except (TypeError, ValueError):
                activity[key] = 0

        social_raw = raw.get("social_origin") if isinstance(raw.get("social_origin"), dict) else {}
        joins = []
        raw_joins = social_raw.get("join_sources")
        if not isinstance(raw_joins, list):
            raw_joins = []
        for item in raw_joins:
            if not isinstance(item, dict):
                continue
            joins.append(
                {
                    "gid": str(item.get("gid") or "")[:32],
                    "sub_type": str(item.get("sub_type") or "")[:24],
                    "operator": str(item.get("operator") or "")[:32],
                    "time": _safe_int(item.get("time")),
                }
            )
            if len(joins) >= 10:
                break
        try:
            score = max(0, min(100, int(raw.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0
        return {
            "schema_version": _safe_int(raw.get("schema_version"), 1),
            "provider": str(raw.get("provider") or "astrbot_plugin_user_profile")[:64],
            "captured_at": _safe_int(raw.get("captured_at"), int(time.time())),
            "score": score,
            "level": str(raw.get("level") or "")[:16],
            "tags": tags,
            "activity": activity,
            "social_origin": {
                "friend_add_time": _safe_int(social_raw.get("friend_add_time")),
                "friend_request_time": _safe_int(social_raw.get("friend_request_time")),
                "friend_request_comment": str(
                    social_raw.get("friend_request_comment") or ""
                )[:200],
                "join_sources": joins,
            },
        }

    @staticmethod
    def _format_external_decision_profile(snapshot: dict) -> str:
        """把结构化画像变成决策上下文；守卫来源标签交给内置前科区块展示。"""
        if not snapshot:
            return ""
        lines = [
            f"用户画像插件：综合风险 {snapshot.get('score', 0)}/100"
            f"（{snapshot.get('level') or '未知'}）"
        ]
        tags = [
            item
            for item in snapshot.get("tags") or []
            if item.get("tag") and item.get("source") not in ("guard", "ban_list")
        ]
        if tags:
            rendered = []
            for item in tags[:8]:
                label = f"{item['tag']}({int(float(item.get('confidence') or 0) * 100)}%)"
                evidence = str(item.get("evidence") or "").strip()
                rendered.append(label + (f"：{evidence}" if evidence else ""))
            lines.append("画像标签：" + "；".join(rendered))

        activity = snapshot.get("activity") or {}
        total = int(activity.get("group_messages") or 0) + int(activity.get("private_messages") or 0)
        if total or int(activity.get("active_groups") or 0):
            lines.append(
                "活跃度：发言 %d 条（群聊 %d / 私聊 %d），活跃群 %d 个"
                % (
                    total,
                    int(activity.get("group_messages") or 0),
                    int(activity.get("private_messages") or 0),
                    int(activity.get("active_groups") or 0),
                )
            )

        social = snapshot.get("social_origin") or {}
        if int(social.get("friend_add_time") or 0):
            lines.append("社交来源：已记录添加 bot 好友")
        comment = str(social.get("friend_request_comment") or "").strip()
        if comment:
            lines.append(f"好友申请验证语：{comment}")
        joins = social.get("join_sources") or []
        if joins:
            rendered = []
            for item in joins[-5:]:
                rendered.append(
                    f"群 {item.get('gid') or '?'} / {item.get('sub_type') or '未知方式'}"
                    f" / 操作者 {item.get('operator') or '未知'}"
                )
            lines.append("进群关系：" + "；".join(rendered))
        return "\n".join(lines)

    async def _fetch_external_decision_profile(
        self, qq: str, exclude_request_key: str = ""
    ) -> tuple[str, dict]:
        """读取结构化决策画像；兼容旧 API，任何失败都优雅降级。"""
        instance = self._get_profile_plugin_instance()
        if instance is None:
            return "", {}

        getter = getattr(instance, "get_decision_profile", None)
        if callable(getter):
            try:
                try:
                    params = inspect.signature(getter).parameters.values()
                    supports_exclude = any(
                        param.name == "exclude_request_key"
                        or param.kind == inspect.Parameter.VAR_KEYWORD
                        for param in params
                    )
                except (TypeError, ValueError):
                    supports_exclude = False
                if supports_exclude:
                    result = getter(qq, None, exclude_request_key=exclude_request_key)
                else:
                    result = getter(qq, None)
                raw = await result if inspect.isawaitable(result) else result
                snapshot = self._sanitize_profile_snapshot(raw)
                if snapshot:
                    return self._format_external_decision_profile(snapshot), snapshot
                # 新 API 成功返回空，表示确实无历史画像；不能再用无排除参数回查。
                return "", {}
            except Exception as exc:
                logger.warning(f"group_invite_guard: decision profile failed: {exc}")

        score_getter = getattr(instance, "get_profile_tags_with_score", None)
        social_getter = getattr(instance, "get_social_origin", None)
        if callable(score_getter):
            try:
                try:
                    params = inspect.signature(score_getter).parameters.values()
                    supports_exclude = any(
                        param.name == "exclude_request_key"
                        or param.kind == inspect.Parameter.VAR_KEYWORD
                        for param in params
                    )
                except (TypeError, ValueError):
                    supports_exclude = False
                if supports_exclude:
                    score_result = score_getter(
                        qq, None, exclude_request_key=exclude_request_key
                    )
                else:
                    score_result = score_getter(qq, None)
                score_data = await score_result if inspect.isawaitable(score_result) else score_result
                social_data = {}
                if callable(social_getter):
                    social_result = social_getter(qq)
                    social_data = await social_result if inspect.isawaitable(social_result) else social_result
                raw = dict(score_data) if isinstance(score_data, dict) else {}
                raw.update(
                    {
                        "schema_version": 0,
                        "provider": "astrbot_plugin_user_profile_compat",
                        "captured_at": int(time.time()),
                        "social_origin": social_data if isinstance(social_data, dict) else {},
                    }
                )
                snapshot = self._sanitize_profile_snapshot(raw)
                if snapshot and (snapshot.get("tags") or social_data):
                    return self._format_external_decision_profile(snapshot), snapshot
            except Exception as exc:
                logger.warning(f"group_invite_guard: structured profile fallback failed: {exc}")

        legacy = await self._fetch_external_profile(qq)
        if legacy:
            return legacy, {
                "schema_version": 0,
                "provider": "astrbot_plugin_user_profile_legacy",
                "captured_at": int(time.time()),
            }
        return "", {}

    async def _fetch_external_profile(self, qq: str, event=None) -> str:
        """尝试从「用户画像」插件读取画像文本（其预留接口 get_profile_text）；未安装/失败/无数据返回空。"""
        try:
            md = self.context.get_registered_star("astrbot_plugin_user_profile")
        except Exception:
            return ""
        instance = getattr(md, "star_cls", None) if md else None
        getter = getattr(instance, "get_profile_text", None) if instance else None
        if not callable(getter):
            return ""
        try:
            result = getter(qq, event)
            text = await result if inspect.isawaitable(result) else result
        except Exception as exc:
            logger.warning(f"group_invite_guard: external profile failed: {exc}")
            return ""
        return str(text or "").strip()

    async def _full_profile(self, qq: str, bot=None, event=None) -> str:
        """完整画像：装了「用户画像」插件且开关开时优先用它，否则用内置完整画像。"""
        if self._cfg("decision", "use_profile_plugin", True):
            external = await self._fetch_external_profile(qq, event)
            if external:
                return external
        return await self._build_full_profile(qq, bot)

    async def _build_profile_section(
        self, inviter_qq: str, speaker_count: int, exclude_request_key: str = ""
    ) -> str:
        """邀请人画像（决策用精简版）；全空返回空。"""
        if not self._cfg("decision", "enable_user_profile", True):
            return ""
        qq = str(inviter_qq or "").strip()
        if not qq:
            return ""

        data = await self._collect_profile_data(qq, exclude_request_key)
        invited = [gid for gid, _ in data["invited"]]

        lines = []
        if invited:
            lines.append(f"历史互动：邀请过 bot {len(invited)} 次（群：{'、'.join(invited[:10])}）")
        operated = data["operated"]
        if operated:
            lines.append(f"曾操作拉 bot 进群：{'、'.join(operated[:10])}")
        ban_entry = data["ban_entry"]
        if ban_entry is not None:
            reason = str(ban_entry.get("reason") or "未注明").strip()
            lines.append(f"⚠️ 在黑名单中（原因：{reason}）")
        if data["rejected"]:
            lines.append(f"历史邀请被拒绝 {data['rejected']} 次")
        if data["mute_total"]:
            lines.append(f"TA 邀请的群累计禁言 bot {data['mute_total']} 次")

        if speaker_count:
            lines.append(f"活跃度：历史会话中本人发言 {speaker_count} 条")

        if not lines:
            return ""
        return "邀请人画像：\n" + "\n".join(lines)

    async def _build_full_profile(self, qq: str, bot=None) -> str:
        """完整画像（/画像 命令与 LLM 工具用）：比决策版更全，附黑名单详情与所有 ≥30% 的小号相似度候选。"""
        qq = str(qq or "").strip()
        if not qq:
            return "用法：/画像 <QQ>"

        data = await self._collect_profile_data(qq)
        lines = [f"QQ {qq} 的画像："]

        invited = data["invited"]
        if invited:
            lines.append(f"邀请记录：共邀请 bot {len(invited)} 次")
            for gid, rec in invited[:20]:
                if isinstance(rec, dict):
                    try:
                        ts = datetime.fromtimestamp(int(rec.get("time") or 0)).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        ts = "-"
                    action = str(rec.get("action") or "").strip() or "-"
                    comment = str(rec.get("comment") or "").strip() or "(无附言)"
                    lines.append(f"· 群 {gid} | {ts} | {action} | {comment}")
                else:
                    lines.append(f"· 群 {gid}")
            if len(invited) > 20:
                lines.append(f"· …另有 {len(invited) - 20} 条从略")
        else:
            lines.append("邀请记录：无")

        operated = data["operated"]
        lines.append(f"曾操作拉 bot 进群：{'、'.join(operated[:10]) if operated else '无'}")

        ban_entry = data["ban_entry"]
        if ban_entry is not None:
            reason = str(ban_entry.get("reason") or "未注明").strip()
            try:
                ban_time = datetime.fromtimestamp(
                    int(ban_entry.get("ban_time") or 0)
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ban_time = "-"
            lines.append(f"黑名单：⚠️ 已拉黑（原因：{reason}，拉黑时间：{ban_time}）")
        else:
            lines.append("黑名单：未拉黑")

        priors = []
        if data["rejected"]:
            priors.append(f"历史邀请被拒绝 {data['rejected']} 次")
        if data["mute_total"]:
            priors.append(f"TA 邀请的群累计禁言 bot {data['mute_total']} 次")
        lines.append("前科：" + ("；".join(priors) if priors else "无"))

        speaker_lines = await self._extract_speaker_quotes(qq)
        lines.append(f"活跃度：历史会话中本人发言 {len(speaker_lines)} 条")

        # 小号相似度：用 TA 最近一次邀请附言 + 当前昵称与黑名单逐一比较，列出所有 ≥30% 的候选
        latest_comment = ""
        latest_ts = -1
        for _, rec in invited:
            if not isinstance(rec, dict):
                continue
            c = str(rec.get("comment") or "").strip()
            if not c:
                continue
            try:
                ts = int(rec.get("time") or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts >= latest_ts:
                latest_ts, latest_comment = ts, c

        threshold = self._alt_threshold()
        try:
            candidates = await self._alt_similarity_candidates(qq, latest_comment, bot)
        except Exception as exc:
            logger.warning(f"group_invite_guard: alt candidates for {qq} failed: {exc}")
            candidates = []
        candidates = [c for c in candidates if c[1] >= 30]
        if candidates:
            lines.append(f"与黑名单用户的小号相似度（阈值 {threshold}%）：")
            for uid, sim, dim in candidates[:10]:
                mark = " ⚠️ 达到阈值，疑似小号" if sim >= threshold else ""
                lines.append(f"· 与 {uid} 相似度 {sim:.0f}%（{dim}）{mark}")
        else:
            lines.append(f"与黑名单用户的小号相似度：无 ≥30% 的候选（阈值 {threshold}%）")

        return "\n".join(lines)

    def _alt_threshold(self) -> int:
        """小号相似度阈值（0-100）。"""
        try:
            threshold = int(self._cfg("alt_detect", "alt_similarity_threshold", 70) or 70)
        except (TypeError, ValueError):
            threshold = 70
        return max(0, min(100, threshold))

    async def _alt_similarity_candidates(
        self, inviter_qq: str, comment: str, bot, exclude_request_key: str = ""
    ) -> list:
        """计算邀请人与黑名单用户的附言/昵称相似度。"""
        config, _ = self._get_ban_config()
        if config is None:
            return []
        try:
            ban_list = config.get("ban_list")
        except Exception:
            return []
        if not isinstance(ban_list, list) or not ban_list:
            return []

        inviter_qq = str(inviter_qq or "").strip()
        comment = str(comment or "").strip()

        banned_qqs = []
        banned_nicks = {}
        for item in ban_list:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("user_id") or "").strip()
            if not uid:
                continue
            banned_qqs.append(uid)
            nick = str(item.get("nickname") or "").strip()
            if nick:
                banned_nicks[uid] = nick

        # 附言维度：黑名单用户历史邀请时的附言（本地 kv）
        past_comments = {}
        if comment:
            try:
                records = await self.get_kv_data("invite_records", {})
            except Exception:
                records = {}
            normalized = self._normalize_invite_records(records)
            for recs in normalized.values():
                for rec in recs:
                    if str(rec.get("request_key") or "") == exclude_request_key:
                        continue
                    uid = self._record_inviter(rec)
                    if uid in banned_qqs:
                        c = str(rec.get("comment") or "").strip()
                        if c:
                            past_comments.setdefault(uid, c)

        # 昵称维度：仅黑名单里存了昵称时才比（邀请人昵称走现有 _fetch_nickname 路径）
        inviter_nick = ""
        if banned_nicks:
            inviter_nick = await self._fetch_nickname(bot, inviter_qq)

        candidates = []
        for uid in banned_qqs:
            best_sim, best_dim = 0.0, ""
            past = past_comments.get(uid, "")
            if comment and past:
                sim = difflib.SequenceMatcher(None, comment, past).ratio() * 100
                if sim > best_sim:
                    best_sim, best_dim = sim, "附言相似"
            nick = banned_nicks.get(uid, "")
            if inviter_nick and nick:
                sim = difflib.SequenceMatcher(None, inviter_nick, nick).ratio() * 100
                if sim > best_sim:
                    best_sim, best_dim = sim, "昵称相似"
            if best_dim:
                candidates.append((uid, best_sim, best_dim))
        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates

    def _alt_gray_low(self) -> int:
        """小号灰色区间下限（0-100）：相似度在 [该值, 阈值) 之间时交给 LLM 复判。"""
        try:
            low = int(self._cfg("alt_detect", "alt_gray_low", 40) or 40)
        except (TypeError, ValueError):
            low = 40
        return max(0, min(100, low))

    async def _detect_alt_account(
        self, inviter_qq: str, comment: str, bot, exclude_request_key: str = ""
    ) -> str:
        """识别邀请人是否疑似黑名单用户的小号。"""
        if not _as_bool(self._cfg("alt_detect", "alt_account_detect", True)):
            return ""
        candidates = await self._alt_similarity_candidates(
            inviter_qq, comment, bot, exclude_request_key
        )
        if not candidates:
            return ""
        best_uid, best_sim, best_dim = candidates[0]
        threshold = self._alt_threshold()
        if best_sim >= threshold:
            return f"⚠️ 该邀请人与黑名单用户 {best_uid} 相似度 {best_sim:.0f}%（{best_dim}），疑似小号，请谨慎"
        # 灰色区间 [alt_gray_low, threshold)：difflib 拿不准，调一次 LLM 复判（结果缓存 7 天）
        if not _as_bool(self._cfg("alt_detect", "alt_llm_review", True)):
            return ""
        if best_sim < self._alt_gray_low():
            return ""
        return await self._llm_review_alt(inviter_qq, comment, best_uid, best_sim, best_dim)

    async def _past_comment_of(self, qq: str) -> str:
        """从邀请记录里取某 QQ 历史邀请时的附言（本地 kv，与 _alt_similarity_candidates 的附言维度同源）；找不到返回空。"""
        qq = str(qq or "").strip()
        if not qq:
            return ""
        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception:
            return ""
        normalized = self._normalize_invite_records(records)
        for recs in normalized.values():
            for rec in recs:
                if self._record_inviter(rec) == qq:
                    comment = str(rec.get("comment") or "").strip()
                    if comment:
                        return comment
        return ""

    async def _llm_review_alt(self, inviter_qq: str, comment: str, old_uid: str, sim: float, dim: str) -> str:
        """灰色区间小号复判：一次轻量 LLM 调用判断是否同一人，判定缓存 kv（alt_verdict_cache）7 天；失败按未复判处理返回空。"""
        inviter_qq = str(inviter_qq or "").strip()
        old_uid = str(old_uid or "").strip()
        if not inviter_qq or not old_uid:
            return ""

        cache_key = f"{inviter_qq}|{old_uid}"
        now = int(time.time())
        try:
            cache = await self.get_kv_data("alt_verdict_cache", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load alt_verdict_cache failed: {exc}")
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        entry = cache.get(cache_key)
        if isinstance(entry, dict):
            try:
                updated = int(entry.get("updated") or 0)
            except (TypeError, ValueError):
                updated = 0
            if updated and now - updated < 7 * 86400:
                return self._alt_verdict_line(entry, old_uid, sim, dim)

        # 双方可对比材料：新邀请人附言/近期发言 vs 黑名单用户旧附言/旧发言
        comment = str(comment or "").strip()
        old_comment = await self._past_comment_of(old_uid)
        fetched = await asyncio.gather(
            self._extract_speaker_quotes(inviter_qq),
            self._extract_speaker_quotes(old_uid),
            return_exceptions=True,
        )
        new_quotes = fetched[0] if isinstance(fetched[0], list) else []
        old_quotes = fetched[1] if isinstance(fetched[1], list) else []
        if not comment and not old_comment and not new_quotes and not old_quotes:
            return ""  # 双方都没附言没发言，无可对比数据，零 LLM 开销

        provider_id = self._cfg("decision", "llm_provider_id") or self._default_provider_id()
        if not provider_id:
            return ""

        def _fmt(items):
            return "\n".join(f"  · {q}" for q in items[:10]) if items else "  (无)"

        prompt = (
            "判断两个 QQ 用户是否可能是同一个人（小号）。\n\n"
            f"用户 A（新邀请人，QQ {inviter_qq}）：\n"
            f"  · 本次邀请附言：{comment or '(无)'}\n"
            f"  · 近期发言原话：\n{_fmt(new_quotes)}\n\n"
            f"用户 B（已被拉黑，QQ {old_uid}）：\n"
            f"  · 历史邀请附言：{old_comment or '(无)'}\n"
            f"  · 历史发言原话：\n{_fmt(old_quotes)}\n\n"
            f"两者的附言/昵称文本相似度约 {sim:.0f}%（{dim}）。\n"
            "请根据用词习惯、语气、内容风格综合判断是否为同一人。"
            "只输出一个 JSON 对象：{\"same_person\": true 或 false, \"confidence\": 0-100 的整数, \"reason\": \"简短理由\"}。"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            result = _parse_json(text)
        except Exception as exc:
            logger.warning(f"group_invite_guard: alt review {inviter_qq} vs {old_uid} failed: {exc}")
            return ""
        if result.get("same_person") is None:
            return ""  # 复判结果不可解析，按未复判处理

        try:
            confidence = int(result.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        verdict = {
            "same_person": _as_bool(result.get("same_person")),
            "confidence": max(0, min(100, confidence)),
            "reason": str(result.get("reason") or "").strip()[:200],
            "updated": now,
        }
        cache[cache_key] = verdict
        try:
            await self.put_kv_data("alt_verdict_cache", cache)
        except Exception as exc:
            logger.warning(f"group_invite_guard: save alt_verdict_cache failed: {exc}")
        return self._alt_verdict_line(verdict, old_uid, sim, dim)

    @staticmethod
    def _alt_verdict_line(verdict: dict, old_uid: str, sim: float, dim: str) -> str:
        """把 LLM 复判结论渲染成决策上下文提示行；未复判/结论不明确返回空。"""
        if not isinstance(verdict, dict) or verdict.get("same_person") is None:
            return ""
        same = _as_bool(verdict.get("same_person"))
        try:
            confidence = int(verdict.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        reason = str(verdict.get("reason") or "").strip()
        if same and confidence >= 70:
            line = f"⚠️ LLM 判定与黑名单用户 {old_uid} 高度疑似同一人（置信度 {confidence}%）"
            if reason:
                line += f"：{reason}"
            return line
        if not same:
            return f"与黑名单用户 {old_uid} 有少量相似（{sim:.0f}%），LLM 复核认为非同一人"
        return f"与黑名单用户 {old_uid} 相似度 {sim:.0f}%（{dim}），LLM 复核倾向同一人但置信度不足（{confidence}%）"

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

    async def _build_impression_section(self, inviter_qq: str, group_id: str):
        """返回 (印象小节文本, 邀请人发言条数)；发言条数供画像复用，避免重复搜索。"""
        # 三次历史搜索并发执行，单个失败按空列表处理，不影响其它
        searched = await asyncio.gather(
            self._search_impression_lines(inviter_qq),
            self._extract_speaker_quotes(inviter_qq),
            self._search_impression_lines(group_id),
            return_exceptions=True,
        )
        inviter_lines, speaker_lines, group_lines = [
            item if isinstance(item, list) else [] for item in searched
        ]

        parts = []
        if speaker_lines:
            summary = ""
            if _as_bool(self._cfg("decision", "impression_llm_summary", True)):
                summary = await self._summarize_impression(inviter_qq, speaker_lines)
            if summary:
                # 有小结时原话只留 5 条节选，避免上下文膨胀
                parts.append(f"对邀请人（QQ {inviter_qq}）的印象小结：{summary}")
                excerpt = "\n".join(speaker_lines[:5])
                parts.append(f"邀请人（QQ {inviter_qq}）在历史里说过的原话（节选）：\n{excerpt}")
            else:
                parts.append(f"邀请人（QQ {inviter_qq}）在历史里说过的原话：\n" + "\n".join(speaker_lines))
        if inviter_lines:
            parts.append("对邀请人 QQ 的既有印象：\n" + "\n".join(inviter_lines))
        if group_lines:
            parts.append("对该群的既有印象：\n" + "\n".join(group_lines))
        return "\n\n".join(parts), len(speaker_lines)

    async def _summarize_impression(self, inviter_qq: str, quotes: list) -> str:
        """把邀请人的历史发言原话浓缩成 100 字内的印象小结（一次轻量 LLM 调用）。

        带 kv 缓存（impression_cache）：发言条数比缓存时变多才重新生成，否则直接用缓存；
        抓不到原话/未配置模型/调用失败一律返回空，调用方降级为只用原话。
        """
        qq = str(inviter_qq or "").strip()
        quotes = [q for q in (quotes or []) if str(q or "").strip()]
        if not qq or not quotes:
            return ""

        try:
            cache = await self.get_kv_data("impression_cache", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load impression_cache failed: {exc}")
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        entry = cache.get(qq)
        if isinstance(entry, dict):
            try:
                cached_count = int(entry.get("quote_count") or 0)
            except (TypeError, ValueError):
                cached_count = 0
            cached_summary = str(entry.get("summary") or "").strip()
            if cached_summary and len(quotes) <= cached_count:
                return cached_summary

        provider_id = self._cfg("decision", "llm_provider_id") or self._default_provider_id()
        if not provider_id:
            return ""
        prompt = (
            "以下是一个 QQ 用户在历史群聊/私聊里的发言原话：\n"
            + "\n".join(f"· {q}" for q in quotes[:20])
            + "\n\n请用 100 字以内总结对此人的印象：语言风格、素质、是否可疑。"
            "直接输出小结文本，不要输出任何额外解释。"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            summary = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"group_invite_guard: impression summary for {qq} failed: {exc}")
            return ""
        if not summary:
            return ""

        cache[qq] = {
            "summary": summary,
            "quote_count": len(quotes),
            "updated": int(time.time()),
        }
        try:
            await self.put_kv_data("impression_cache", cache)
        except Exception as exc:
            logger.warning(f"group_invite_guard: save impression_cache failed: {exc}")
        return summary

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
                        line = line[:300] + self._cfg("decision", "truncate_marker", "…")
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
                text = text[:300] + self._cfg("decision", "truncate_marker", "…")
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
        provider_id = self._cfg("decision", "llm_provider_id") or self._default_provider_id()
        if not provider_id:
            raise RuntimeError("no llm provider id configured")

        decision_persona = str(self._cfg("decision", "decision_persona") or "").strip()
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
        try:
            return callable(getattr(getattr(obj, "api", None), "call_action", None))
        except Exception:
            return False

    @staticmethod
    def _unwrap_onebot_response(response: Any) -> Any:
        if not isinstance(response, dict):
            return response
        is_wrapper = (
            "retcode" in response
            or "status" in response
            or (
                "data" in response
                and any(key in response for key in ("msg", "message", "wording"))
            )
        )
        if not is_wrapper:
            return response
        status = str(response.get("status") or "").strip().lower()
        retcode = response.get("retcode")
        try:
            retcode_failed = retcode is not None and int(retcode) != 0
        except (TypeError, ValueError):
            retcode_failed = bool(str(retcode or "").strip())
        if status in ("failed", "failure", "error") or retcode_failed:
            message = response.get("message") or response.get("msg") or ""
            wording = response.get("wording") or ""
            raise RuntimeError(
                "OneBot action failed: "
                f"status={status or '-'}, retcode={retcode!r}, "
                f"message={message or '-'}, wording={wording or '-'}"
            )
        return response.get("data")

    @staticmethod
    def _is_unsupported_action_error(exc: Exception) -> bool:
        if isinstance(exc, AttributeError):
            return True
        text = str(exc or "").strip().lower()
        marks = (
            "unsupported method", "method not supported", "not implemented",
            "no such action", "unknown action", "api not found",
            "接口不存在", "方法不存在", "不支持此接口", "不支持的api",
        )
        return any(mark in text for mark in marks)

    async def _invoke_action(self, fn, action: str | None, params: dict) -> Any:
        result = fn(**params) if action is None else fn(action, **params)
        result = await result if inspect.isawaitable(result) else result
        return self._unwrap_onebot_response(result)

    async def _call_action(self, bot: Any, action: str, **params: Any) -> Any:
        unsupported_error = None
        method = getattr(bot, action, None)
        if callable(method):
            try:
                return await self._invoke_action(method, None, params)
            except Exception as exc:
                if not self._is_unsupported_action_error(exc):
                    raise
                unsupported_error = exc

        for name in ("call_action", "call_api"):
            fn = getattr(bot, name, None)
            if not callable(fn):
                continue
            try:
                return await self._invoke_action(fn, action, params)
            except Exception as exc:
                if not self._is_unsupported_action_error(exc):
                    raise
                unsupported_error = exc

        api_call = getattr(getattr(bot, "api", None), "call_action", None)
        if callable(api_call):
            return await self._invoke_action(api_call, action, params)
        if unsupported_error is not None:
            raise unsupported_error
        raise RuntimeError(f"no usable OneBot action caller for {action}")

    async def _membership_state(self, bot: Any, group_id: str, self_id: str = "") -> str:
        """返回 IN / OUT / UNKNOWN；查询异常绝不当作 OUT。"""
        if bot is None or not str(group_id or "").strip():
            return "UNKNOWN"
        uid = str(self_id or "").strip()
        if not uid:
            try:
                info = await self._call_action(bot, "get_login_info")
                uid = str(_get_value(info, "user_id") or "").strip()
            except Exception:
                uid = ""
        if uid:
            try:
                await self._call_action(
                    bot, "get_group_member_info", group_id=int(group_id),
                    user_id=int(uid), no_cache=True,
                )
                return "IN"
            except Exception:
                pass
        try:
            groups = await self._call_action(bot, "get_group_list", no_cache=True)
            if isinstance(groups, dict):
                groups = groups.get("data", groups.get("groups"))
            if not isinstance(groups, list):
                return "UNKNOWN"
            group_ids = {
                str(_get_value(item, "group_id") or "").strip()
                for item in groups
            }
            return "IN" if str(group_id) in group_ids else "OUT"
        except Exception:
            return "UNKNOWN"

    async def _verify_self_in_group(self, bot: Any, group_id: str, self_id: str = "") -> bool:
        """旧内部接口兼容。"""
        return await self._membership_state(bot, group_id, self_id) == "IN"

    async def _send_ban_notice(self, bot, qq: str) -> str:
        """拉黑前给邀请人发一条自定义私聊消息；未配置则跳过。"""
        msg = str(self._cfg("kick_revenge", "ban_notice_message") or "").strip()
        if not msg:
            return ""
        try:
            await self._call_action(bot, "send_private_msg", user_id=int(qq), message=msg)
            return f"已私聊通知 {qq}"
        except Exception as exc:
            return f"通知 {qq} 失败：{exc}"

    async def _take_revenge(self, inviter_qq: str, bot) -> str:
        """按 revenge_mode 对邀请人执行报复，返回人类可读结果；单个动作失败不抛出，写入结果字符串。"""
        mode = str(self._cfg("kick_revenge", "revenge_mode", "off") or "off").strip().lower()
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

    @staticmethod
    def _normalize_invite_records(records: Any) -> dict:
        """把旧格式 {group_id: dict|str} 统一转成 {group_id: [dict, ...]}，返回新 dict。"""
        if not isinstance(records, dict):
            return {}
        normalized = {}
        for gid, val in records.items():
            gid = str(gid)
            if isinstance(val, list):
                normalized[gid] = [r for r in val if isinstance(r, dict)]
            elif isinstance(val, dict):
                normalized[gid] = [val]
            elif isinstance(val, str):
                # 极旧格式：群号 -> QQ 字符串
                normalized[gid] = [{"inviter": val, "time": 0, "action": "", "comment": ""}]
            else:
                normalized[gid] = []
        return normalized

    @staticmethod
    def _record_inviter(record) -> str:
        """从单条邀请记录取邀请人 QQ；兼容旧的纯字符串格式。"""
        if isinstance(record, dict):
            return str(record.get("inviter") or "").strip()
        return str(record or "").strip()

    @classmethod
    def _latest_invite_record(cls, records: dict, group_id: str) -> dict | None:
        """取某群最新一条邀请记录；无记录返回 None。"""
        group_id = str(group_id or "").strip()
        if not group_id:
            return None
        recs = cls._normalize_invite_records(records).get(group_id, [])
        if not recs:
            return None
        return max(recs, key=lambda r: int(r.get("time") or 0))

    @classmethod
    def _find_invite_records_by_inviter(cls, records: dict, inviter_qq: str) -> list[dict]:
        """返回某邀请人的所有邀请记录（按时间新→旧）。"""
        inviter_qq = str(inviter_qq or "").strip()
        if not inviter_qq:
            return []
        result = []
        for recs in cls._normalize_invite_records(records).values():
            for rec in recs:
                if cls._record_inviter(rec) == inviter_qq:
                    result.append(rec)
        result.sort(key=lambda r: int(r.get("time") or 0), reverse=True)
        return result

    async def _mutate_invite_records(self, mutate) -> bool:
        async with self._invite_lock("_invite_kv_lock"):
            try:
                records = await self.get_kv_data("invite_records", {})
            except Exception as exc:
                logger.warning(f"group_invite_guard: load invite_records failed: {exc}")
                records = {}
            records = self._normalize_invite_records(records)
            if not mutate(records):
                return False
            try:
                await self.put_kv_data("invite_records", records)
                return True
            except Exception as exc:
                logger.warning(f"group_invite_guard: save invite_records failed: {exc}")
                return False

    async def _record_invite(
        self, group_id: str, inviter_qq: str, comment: str = "", action: str = "",
        record_id: str = "", **extra,
    ) -> bool:
        """追加一条邀请记录；未知群号或邀请人也不会丢失。"""
        storage_group = str(group_id or "").strip() or "(未知群)"
        inviter_qq = str(inviter_qq or "").strip()

        def mutate(records):
            old_dealt = False
            old_dealt_time = 0
            if inviter_qq:
                for old in records.get(storage_group, []):
                    if self._record_inviter(old) == inviter_qq and _as_bool(old.get("dealt")):
                        old_dealt = True
                        old_dealt_time = int(old.get("dealt_time") or 0)
                        break
            now = int(time.time())
            new_record = {
                "inviter": inviter_qq,
                "comment": str(comment or "").strip(),
                "time": now,
                "action": str(action or "").strip(),
                "record_id": str(record_id or uuid.uuid4().hex[:8]),
            }
            if old_dealt:
                new_record["dealt"] = True
                new_record["dealt_time"] = old_dealt_time or now
            for key, value in extra.items():
                if key not in new_record:
                    new_record[key] = value
            records.setdefault(storage_group, []).append(new_record)
            return True

        return await self._mutate_invite_records(mutate)

    async def _update_invite_record(
        self, group_id: str, record_id: str, **fields,
    ) -> bool:
        """按 record_id 更新某条邀请记录。"""
        storage_group = str(group_id or "").strip() or "(未知群)"
        record_id = str(record_id or "").strip()
        if not record_id:
            return

        def mutate(records):
            for rec in records.get(storage_group, []):
                if str(rec.get("record_id") or "") == record_id:
                    rec.update(fields)
                    return True
            return False

        return await self._mutate_invite_records(mutate)

    async def _mark_inviter_dealt(self, inviter_qq: str, dealt: bool = True) -> None:
        """给该邀请人的所有邀请记录打上或清除已拉黑标记。"""
        inviter_qq = str(inviter_qq or "").strip()
        if not inviter_qq:
            return

        def mutate(records):
            changed = False
            now = int(time.time())
            for recs in records.values():
                for rec in recs:
                    if self._record_inviter(rec) != inviter_qq:
                        continue
                    if dealt:
                        rec["dealt"] = True
                        rec["dealt_time"] = int(rec.get("dealt_time") or now)
                    else:
                        rec.pop("dealt", None)
                        rec.pop("dealt_time", None)
                    changed = True
            return changed

        await self._mutate_invite_records(mutate)

    def _get_ban_config(self):
        """读取 qq_tools 配置；返回 (config, err)。err 为空表示成功。"""
        instance = self._get_qq_tools_instance()
        if instance is None:
            return None, "黑名单功能不可用（未安装/未启用 qq_tools）"
        config = getattr(instance, "config", None)
        if config is None:
            return None, "黑名单功能不可用（qq_tools 配置不可读）"
        return config, ""

    @staticmethod
    def _filter_ban_list(ban_list, qq):
        """从 ban_list 移除指定 QQ 的旧记录并返回新列表；非 list 输入按空列表处理。"""
        if not isinstance(ban_list, list):
            ban_list = []
        return [
            item
            for item in ban_list
            if not (isinstance(item, dict) and str(item.get("user_id")) == str(qq))
        ]

    async def _save_ban_config(self, config):
        """持久化 qq_tools 配置；save_config 缺失时告警并返回 False。"""
        save = getattr(config, "save_config", None)
        if callable(save):
            if inspect.iscoroutinefunction(save):
                await save()
            else:
                await asyncio.to_thread(save)
            return True
        logger.warning("group_invite_guard: qq_tools 配置缺少 save_config，未能持久化")
        return False

    async def _ban_inviter(self, inviter_qq: str, reason: str = "拉黑") -> str:
        """把邀请人加入 AstrBot 黑名单（qq_tools 插件），不可用时降级并注明。"""
        config, err = self._get_ban_config()
        if config is None:
            return err
        try:
            ban_list = self._filter_ban_list(config.get("ban_list"), inviter_qq)
            ban_list.append(
                {
                    "user_id": str(inviter_qq),
                    "ban_time": int(time.time()),
                    "duration": -1,
                    "reason": reason,
                }
            )
            config["ban_list"] = ban_list
            persisted = await self._save_ban_config(config)
            msg = f"已加入 AstrBot 黑名单：{inviter_qq}"
            if not persisted:
                msg += "（未能持久化）"
            return msg
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
        mode = str(self._cfg("mute_revenge", "mute_ban_mode", "astrbot_ban") or "astrbot_ban").strip().lower()
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

    def _compose_note(
        self, inviter_qq, group_id, comment, action, reason, reply="",
        reply_status="", alt_warning="", result_label="",
        membership_before="UNKNOWN", membership_after="UNKNOWN", protocol_error="",
    ) -> str:
        action_label = {
            "approve": "同意加入", "reject": "拒绝", "unknown": "判断失败（未处理）",
        }.get(action, action)
        lines = [
            "[加群邀请通知]",
            f"邀请人 QQ：{inviter_qq or '未知（协议端未识别）'}",
            f"群号：{group_id or '(未知)'}",
            f"附言：{comment or '(无)'}",
            f"LLM 决策：{action_label}",
            f"实际执行：{result_label or '未记录'}",
            f"成员状态：审核前 {membership_before} / 执行前后 {membership_after}",
        ]
        if reason:
            lines.append(f"审核理由：{reason}")
        if protocol_error:
            lines.append(f"异常原因：{protocol_error}")
        if reply:
            status = f"（{reply_status}）" if reply_status else ""
            lines.append(f"邀请人话术{status}：{reply}")
        if alt_warning:
            lines.append(alt_warning)
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
        private_qq = str(self._cfg("basic", "notify_private_qq") or "").strip()
        group_id = str(self._cfg("basic", "notify_group_id") or "").strip()

        if not private_qq and self._cfg("basic", "notify_private", True):
            admins = self._nested(self._global_config(), "admins_id") or []
            if admins:
                private_qq = str(admins[0])

        result = {"private": None, "group": None}
        if self._cfg("basic", "notify_private", True) and private_qq:
            result["private"] = private_qq
        if self._cfg("basic", "notify_group", False) and group_id:
            result["group"] = group_id
        return result

    @staticmethod
    def _invite_record_detail(rec: dict) -> dict:
        """把新旧邀请记录统一成文本和图片共用的决策详情。"""
        decision = str(rec.get("decision") or "unknown").strip().lower()
        decision_text = {
            "approve": "建议同意",
            "reject": "建议拒绝",
            "skipped": "跳过",
            "unknown": "未知/未判断",
        }.get(decision, decision or "未知/未判断")
        reason = str(rec.get("decision_reason") or "").strip()
        snapshot = rec.get("profile_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        details = []
        if reason:
            details.append("理由：" + reason[:160])
        if snapshot:
            score = snapshot.get("score")
            level = str(snapshot.get("level") or "").strip()
            if score is not None:
                details.append(f"画像：{score}/100" + (f"（{level}）" if level else ""))
            raw_tags = snapshot.get("tags")
            raw_tags = raw_tags if isinstance(raw_tags, list) else []
            tags = [
                str(item.get("tag") or "").strip()
                for item in raw_tags
                if isinstance(item, dict) and str(item.get("tag") or "").strip()
            ]
            if tags:
                details.append("标签：" + "、".join(tags[:5]))
            social = snapshot.get("social_origin")
            social = social if isinstance(social, dict) else {}
            joins = social.get("join_sources")
            joins = joins if isinstance(joins, list) else []
            if joins and isinstance(joins[-1], dict):
                latest = joins[-1]
                details.append(
                    f"关系：群 {latest.get('gid') or '?'} / "
                    f"{latest.get('sub_type') or '未知方式'} / "
                    f"操作者 {latest.get('operator') or '未知'}"
                )
            elif social.get("friend_add_time"):
                details.append("关系：已添加 bot 好友")
        elif str(rec.get("execution_state") or ""):
            details.append("画像：无快照")
        else:
            details.append("旧记录：无画像快照")
        return {"decision": decision_text, "detail": "；".join(details)}

    async def _list_invite_records_text(self) -> str:
        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception as exc:
            return f"读取邀请记录失败：{exc}"
        records = self._normalize_invite_records(records)
        if not records:
            return "暂无邀请记录"

        hide_dealt = _as_bool(self._cfg("display", "invite_records_hide_dealt", True))
        show_detail = _as_bool(
            self._cfg("display", "invite_records_show_decision_detail", True)
        )
        # 把所有记录拉平，按时间新→旧排序
        flat = []
        for gid, recs in records.items():
            for rec in recs:
                flat.append((str(gid), rec))
        flat.sort(key=lambda x: int(x[1].get("time") or 0), reverse=True)

        lines = []
        for gid, rec in flat:
            inviter = self._record_inviter(rec) or "(未知)"
            dealt = _as_bool(rec.get("dealt"))
            if dealt and hide_dealt:
                continue
            try:
                ts = datetime.fromtimestamp(int(rec.get("time") or 0)).strftime("%m-%d %H:%M")
            except Exception:
                ts = "-"
            comment = str(rec.get("comment") or "").strip() or "(无附言)"
            action = str(rec.get("action") or "").strip() or "-"
            status_text, _ = _invite_status_label(action, dealt)
            before = str(rec.get("membership_before") or "-")
            after = str(rec.get("membership_after") or "-")
            execution = str(rec.get("execution_state") or "旧记录")
            view = self._invite_record_detail(rec)
            decision_part = f" | LLM {view['decision']}" if show_detail else ""
            lines.append(
                f"群 {gid} -> {inviter} | {ts}{decision_part} "
                f"| 实际 [{status_text}]/{execution} | 成员 {before}→{after} | {comment}"
            )
            if show_detail and view["detail"]:
                lines.append("  └ " + view["detail"])
        if not lines:
            return "暂无邀请记录"
        return "\n".join(lines)

    async def _fetch_nickname(self, bot, qq: str) -> str:
        """尽力取某个 QQ 的昵称（get_stranger_info），拿不到返回空字符串。"""
        if bot is None or not qq:
            return ""
        try:
            info = await self._call_action(bot, "get_stranger_info", user_id=int(qq), no_cache=False)
        except Exception:
            return ""
        if isinstance(info, dict):
            return str(info.get("nickname") or "").strip()
        return ""

    async def _fetch_group_name(self, bot, group_id: str) -> str:
        """尽力取群名（get_group_info）；拿不到（比如已退群）返回空字符串。"""
        if bot is None or not group_id:
            return ""
        try:
            info = await self._call_action(bot, "get_group_info", group_id=int(group_id), no_cache=False)
        except Exception:
            return ""
        if isinstance(info, dict):
            return str(info.get("group_name") or "").strip()
        return ""

    async def _render_invite_records_image(self, bot=None) -> str:
        """把邀请记录渲染成图片，返回本地图片路径；无记录或渲染失败返回空字符串。"""
        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load invite_records failed: {exc}")
            return ""
        if not isinstance(records, dict) or not records:
            return ""

        show_profile = _as_bool(self._cfg("display", "invite_records_show_profile", True))
        show_group_profile = _as_bool(self._cfg("display", "invite_records_show_group_profile", True))
        hide_dealt = _as_bool(self._cfg("display", "invite_records_hide_dealt", True))
        show_detail = _as_bool(
            self._cfg("display", "invite_records_show_decision_detail", True)
        )

        items = []
        records = self._normalize_invite_records(records)
        for gid, recs in records.items():
            for rec in recs:
                inviter = self._record_inviter(rec) or "(未知)"
                dealt = _as_bool(rec.get("dealt"))
                if dealt and hide_dealt:
                    continue
                try:
                    ts_int = int(rec.get("time") or 0)
                    ts = datetime.fromtimestamp(ts_int).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    ts_int, ts = 0, "-"
                comment = str(rec.get("comment") or "").strip() or "(无附言)"
                action = str(rec.get("action") or "").strip() or "-"
                status_text, status_class = _invite_status_label(action, dealt)
                view = self._invite_record_detail(rec)
                items.append(
                    {
                        "group": str(gid),
                        "inviter": inviter,
                        "time": ts,
                        "action": action,
                        "comment": comment,
                        "dealt": dealt,
                        "status_text": status_text,
                        "status_class": status_class,
                        "decision": view["decision"] if show_detail else "",
                        "detail": view["detail"] if show_detail else "",
                        "execution": str(rec.get("execution_state") or "旧记录"),
                        "membership": (
                            f"{rec.get('membership_before') or '-'}→"
                            f"{rec.get('membership_after') or '-'}"
                        ),
                        "avatar": "",
                        "nickname": "",
                        "gavatar": "",
                        "gname": "",
                        "_ts": ts_int,
                    }
                )
        items.sort(key=lambda x: x["_ts"], reverse=True)
        for i, it in enumerate(items, 1):
            it["index"] = i

        # 按唯一 QQ/群号并发拉取资料，同一页不重复请求 OneBot。
        qqs = sorted(
            {it["inviter"] for it in items if it["inviter"] not in ("", "(未知)")}
        ) if show_profile else []
        gids = sorted({it["group"] for it in items if it["group"]}) if show_group_profile else []
        calls = [self._fetch_nickname(bot, qq) for qq in qqs]
        calls.extend(self._fetch_group_name(bot, gid) for gid in gids)
        fetched = await asyncio.gather(*calls, return_exceptions=True) if calls else []
        nicknames = {
            qq: (value if isinstance(value, str) else "")
            for qq, value in zip(qqs, fetched[: len(qqs)])
        }
        group_names = {
            gid: (value if isinstance(value, str) else "")
            for gid, value in zip(gids, fetched[len(qqs) :])
        }
        for it in items:
            qq = it["inviter"]
            gid = it["group"]
            if qq in nicknames:
                it["avatar"] = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"
                it["nickname"] = nicknames[qq]
            if gid in group_names:
                it["gavatar"] = f"https://p.qlogo.cn/gh/{gid}/{gid}/100"
                it["gname"] = group_names[gid]

        for it in items:
            it.pop("_ts", None)

        try:
            return await self.html_render(
                _INVITE_RECORDS_TEMPLATE,
                {"items": items, "total": len(items)},
                return_url=False,
                options={"full_page": True, "type": "png"},
            )
        except Exception as exc:
            logger.warning(f"group_invite_guard: render invite image failed: {exc}")
            return ""

    async def _send_invite_records_image(self, event: AstrMessageEvent) -> bool:
        """尝试以图片形式发送邀请记录；成功返回 True，失败返回 False 让调用方回退文本。"""
        bot = self._find_onebot_client(event)
        path = await self._render_invite_records_image(bot)
        if not path:
            return False
        try:
            await event.send(MessageChain(chain=[Image(file=path)]))
            return True
        except Exception as exc:
            logger.error(f"group_invite_guard: send invite image failed: {exc}")
            return False

    async def _record_invite_text(self, group_id: str, inviter_qq: str) -> str:
        await self._record_invite(group_id, inviter_qq, action="手动记录")
        return f"已记录 群号 {group_id} -> 邀请人 {inviter_qq}"

    async def _list_ban_list_text(self) -> str:
        config, err = self._get_ban_config()
        if config is None:
            return err
        try:
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
        config, err = self._get_ban_config()
        if config is None:
            return err
        try:
            ban_list = config.get("ban_list")
            normalized = ban_list if isinstance(ban_list, list) else []
            new_list = self._filter_ban_list(normalized, qq)
            if len(new_list) == len(normalized):
                return f"{qq} 不在黑名单中"
            config["ban_list"] = new_list
            persisted = await self._save_ban_config(config)
            msg = f"已解封 {qq}"
            if not persisted:
                msg += "（未能持久化）"
            await self._mark_inviter_dealt(qq, dealt=False)
            return msg
        except Exception as exc:
            return f"解封失败：{exc}"

    async def _manual_blacklist(self, target_qq: str, bot) -> str:
        """手动拉黑某人：发自定义通知 + 删除并拉黑好友 + 加入 AstrBot 黑名单。"""
        parts = []
        notice = await self._send_ban_notice(bot, target_qq)
        if notice:
            parts.append(notice)
        try:
            await self._call_action(bot, "delete_friend", user_id=int(target_qq), block=True)
            parts.append(f"已删除并拉黑好友 {target_qq}")
        except Exception as exc:
            parts.append(f"删除好友失败：{exc}")
        parts.append(await self._ban_inviter(target_qq, "手动拉黑"))
        return "；".join(parts)

    async def _manual_ban_text(self, event: AstrMessageEvent, args) -> str:
        """手动拉黑：/手动拉黑 <QQ或群号>，单参数时按邀请记录反查目标并退出相关群；旧用法 /手动拉黑 <QQ> <群号> 行为不变。"""
        usage = "用法：/手动拉黑 <QQ或群号>（旧用法：/手动拉黑 <QQ> <群号>）"
        if not args:
            return usage
        target = str(args[0] or "").strip()
        if not target:
            return usage

        bot = self._find_onebot_client(event)
        if bot is None:
            return "手动拉黑失败：未找到 OneBot 客户端"

        # 旧两参数用法：给了群号就退群，再拉黑该 QQ，行为与旧版一致
        if len(args) > 1:
            group_id = str(args[1] or "").strip()
            parts = []
            if group_id:
                try:
                    await self._call_action(bot, "set_group_leave", group_id=int(group_id), is_dismiss=False)
                    parts.append(f"已退出群 {group_id}")
                except Exception as exc:
                    parts.append(f"退群失败：{exc}")
            try:
                result = await self._manual_blacklist(target, bot)
            except Exception as exc:
                result = f"拉黑失败：{exc}"
            parts.append(f"拉黑 {target}：{result}")
            await self._mark_inviter_dealt(target)
            return "；".join(parts)

        # 单参数：先查邀请记录
        try:
            records = await self.get_kv_data("invite_records", {})
        except Exception as exc:
            logger.warning(f"group_invite_guard: load invite_records failed: {exc}")
            records = {}
        if not isinstance(records, dict):
            records = {}

        target_qq = ""
        groups = []
        normalized = self._normalize_invite_records(records)
        if target in normalized:
            # 参数是群号：取该群最新记录的邀请人
            latest = self._latest_invite_record(records, target)
            inviter = self._record_inviter(latest) if latest else ""
            if inviter:
                target_qq = inviter
                groups = [target]
        else:
            # 参数是邀请人 QQ：收集 TA 邀请过的所有群（一人可能邀请多群）
            for gid in normalized:
                if any(self._record_inviter(r) == target for r in normalized[gid]):
                    groups.append(gid)
            if groups:
                target_qq = target

        parts = []
        if not target_qq:
            target_qq = target
            parts.append(f"未找到 {target} 的相关邀请记录，按 QQ 直接拉黑")

        # 先退群（单个失败不影响后面），再统一拉黑一次（消息只发一次）
        for gid in groups:
            try:
                await self._call_action(bot, "set_group_leave", group_id=int(gid), is_dismiss=False)
                parts.append(f"已退出群 {gid}")
            except Exception as exc:
                parts.append(f"退群 {gid} 失败：{exc}")

        try:
            result = await self._manual_blacklist(target_qq, bot)
        except Exception as exc:
            result = f"拉黑失败：{exc}"
        parts.append(f"拉黑 {target_qq}：{result}")
        await self._mark_inviter_dealt(target_qq)

        return "；".join(parts)

    async def _profile_text(self, event: AstrMessageEvent, args) -> str:
        """/画像 <QQ>：输出该 QQ 的完整画像（邀请记录/黑名单/前科/活跃度/小号相似度候选）。"""
        if not args:
            return "用法：/画像 <QQ>"
        qq = str(args[0] or "").strip()
        if not qq:
            return "用法：/画像 <QQ>"
        bot = self._find_onebot_client(event)
        try:
            return await self._full_profile(qq, bot, event)
        except Exception as exc:
            logger.error(f"group_invite_guard: build profile for {qq} failed: {exc}")
            return f"查询画像失败：{exc}"

    async def _dispatch_admin_command(self, event: AstrMessageEvent) -> str:
        text = (event.get_message_str() or "").strip().lstrip("/#").strip()
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
        if cmd == "画像":
            return await self._profile_text(event, args)
        return "未知命令"

    @filter.custom_filter(AdminCommandFilter)
    async def on_admin_command(self, event: AstrMessageEvent):
        text = (event.get_message_str() or "").strip().lstrip("/#").strip()
        cmd = text.split(" ", 1)[0] if text else ""
        # 邀请记录优先发图片表格，渲染/发送失败则回退纯文本
        if cmd in ("邀请记录", "邀请列表"):
            if await self._send_invite_records_image(event):
                return
        result = await self._dispatch_admin_command(event)
        try:
            await event.send(MessageChain(chain=[Plain(result)]))
        except Exception as exc:
            logger.error(f"group_invite_guard: send command result failed: {exc}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=9999)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=9999)
    async def on_blacklist_message_guard(self, event: AstrMessageEvent):
        """兜底拦截：被拉黑用户发来的群聊/私聊消息直接停止传播。"""
        if not self._cfg("basic", "enable", True):
            return
        if not _as_bool(self._cfg("kick_revenge", "intercept_banned_messages", True)):
            return
        sender_id = event.get_sender_id()
        if not sender_id:
            return
        try:
            is_banned, why = await self._is_user_banned(str(sender_id))
        except Exception:
            is_banned, why = False, ""
        if is_banned:
            logger.info(
                f"group_invite_guard: intercepted message from {sender_id}, reason: {why}"
            )
            event.stop_event()

    def _read_ban_entries(self) -> list:
        """读取 qq_tools 黑名单，返回 [(user_id, reason), ...]。"""
        config, err = self._get_ban_config()
        if config is None:
            return []
        try:
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

        async def _load_kv(key):
            try:
                return await self.get_kv_data(key, {})
            except Exception:
                return {}

        # 两个 kv 并发读取，失败按空 dict 处理
        invite, mute = await asyncio.gather(
            _load_kv("invite_records"), _load_kv("mute_records")
        )
        normalized = self._normalize_invite_records(invite)
        if normalized:
            lines.append("被邀请进群记录（群号 -> 邀请人）：")
            shown = 0
            for gid, recs in normalized.items():
                if shown >= 20:
                    break
                for rec in recs:
                    if shown >= 20:
                        break
                    inviter = self._record_inviter(rec)
                    lines.append(f"- 群 {gid} 由 {inviter or '(未知)'} 邀请")
                    shown += 1

        if isinstance(mute, dict) and mute:
            lines.append("被禁言记录（群号：次数）：")
            for gid, cnt in list(mute.items())[:20]:
                lines.append(f"- 群 {gid}：{cnt} 次")

        if not lines:
            return ""
        return "[加群邀请守卫·封禁记录]\n" + "\n".join(lines)

    def _tool_allowed(self, event):
        """判断 LLM 主动拉黑/解封是否放行；返回 (是否放行, 拒绝原因)。"""
        if not self._cfg("basic", "enable", True):
            return False, "插件未启用。"
        if not self._cfg("llm_integration", "llm_tool_ban", True):
            return False, "LLM 主动拉黑功能未启用。"
        if self._cfg("llm_integration", "llm_tool_require_admin", False):
            try:
                if not event.is_admin():
                    return False, "没有权限：当前工具仅管理员可触发。"
            except Exception:
                return False, "没有权限：当前工具仅管理员可触发。"
        return True, ""

    @filter.on_llm_request(priority=-1)
    async def on_llm_request(self, event, req):
        if not self._cfg("basic", "enable", True):
            return
        if not self._cfg("llm_integration", "llm_context_inject", True):
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
        allowed, msg = self._tool_allowed(event)
        if not allowed:
            return msg
        try:
            block = await self._build_ban_context_text()
        except Exception as exc:
            return f"查询失败：{exc}"
        return block or "当前没有封禁记录。"

    @filter.llm_tool(name="group_invite_query_profile")
    async def llm_query_profile(self, event, qq: str):
        """查询某个 QQ 用户的完整画像：历史邀请记录、黑名单状态（原因/时间）、被拒与禁言前科、发言活跃度，以及与黑名单用户的小号相似度候选。需要评估某个邀请人/用户是否可信、是否疑似黑名单用户的小号时调用。"""
        event = _unwrap_event(event)
        allowed, msg = self._tool_allowed(event)
        if not allowed:
            return msg
        qq = str(qq or "").strip()
        if not qq:
            return "查询失败：缺少 qq 参数（QQ 号）。"
        bot = self._find_onebot_client(event)
        try:
            return await self._full_profile(qq, bot, event)
        except Exception as exc:
            return f"查询失败：{exc}"
