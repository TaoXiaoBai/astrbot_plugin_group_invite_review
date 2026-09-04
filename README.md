<div align="center">
  <img src="logo.png" width="128" alt="logo">
  <h1>加群邀请守卫</h1>
  <p>让 LLM 根据<b>人格设定</b>判断是否通过邀请加群</p>
  <p>
    <img src="https://img.shields.io/badge/version-1.17.0-blue" alt="version">
    <img src="https://img.shields.io/badge/AstrBot-4.x-4a6cf7" alt="astrbot">
    <img src="https://img.shields.io/badge/platform-OneBot%20V11-green" alt="platform">
    <img src="https://img.shields.io/badge/license-MIT-orange" alt="license">
  </p>
</div>

## 这是什么

一个 AstrBot 插件。有人拉你的 bot 进群时，不靠死板的关键词/等级规则，而是交给 LLM 根据你设定的**人格**，结合目标群的成员和历史聊天印象，自己判断「进还是不进」。进了被踢、被禁言，还能记仇报复。

## 功能一览

**进群决策**
- 收到针对 bot 的邀请后立即持久化，再收集上下文、调用 LLM、执行前复核成员状态，最后同意或拒绝
- 外部动作前持久化执行中状态；重启遇到未确认动作只对账和通知，不自动重放
- bot 已提前入群或审核期间入群也不会跳过 LLM；LLM 拒绝时可仅通知、直接退群或发自定义消息后退群
- 三种处理模式：自动同意 / 自动拒绝 / 仅通知管理员（默认）
- 审批或补偿结果确定后才按人格私聊邀请人；动作失败时不会发送承诺成功的话术
- 决策附邀请人画像：历史邀请次数、黑名单前科、活跃度，并排除当前审核记录（纯本地数据，不额外调 LLM）
- 抓到邀请人历史发言原话时，由 LLM 浓缩成一段 100 字内的印象小结（按发言条数缓存，陌生人零开销）
- 小号识别：被拉黑的人换号再来？附言/昵称相似度命中即在决策上下文和通知里提示"疑似小号"；相似度处于灰色区间时再交 LLM 复判一次（结论缓存 7 天）
- 私聊里问"能不能加群"、直接甩邀请链接，也能识别并回复/通知

**记仇与报复**
- 自动记录每次进群的邀请人；被该群踢出后可自动删除好友 / 拉黑邀请人，并通知管理员
- 可选：被踢时连执行踢人的管理员一起拉黑
- 可选：跨群连坐——被踢或被禁言达阈值时，连带退出该邀请人邀请过的所有群再拉黑 TA
- 被禁言自动计数，达到阈值自动退群 + 拉黑
- 拉黑前可给对方发一句自定义告别留言
- 管理员直拉进群没有邀请事件？也没关系——进群时间/操作人照样记录，被踢时通知你

**LLM 联动**
- 封禁/邀请/禁言记录自动注入 LLM 上下文，bot 聊天时知道谁有前科
- bot 可自主调用 4 个工具（可选需管理员）：`group_invite_ban_user` 拉黑、`group_invite_unban_user` 解封、`group_invite_query_ban` 查封禁状态、`group_invite_query_profile` 查某个 QQ 的完整画像

**管理命令**（仅管理员，需唤醒 bot，支持 `/` 前缀）

| 命令 | 说明 |
| --- | --- |
| `/邀请记录` | 图片表格展示邀请记录（别名 `/邀请列表`） |
| `/记录邀请 <群号> <QQ>` | 手动补录一条邀请记录 |
| `/画像 <QQ>` | 查看完整画像：邀请明细、黑名单状态、前科、发言条数、疑似小号检测 |
| `/手动拉黑 <QQ或群号>` | 自动反查邀请记录：退相关群 + 拉黑（查无记录则直接拉黑该 QQ） |
| `/拉黑列表` | 查看黑名单（别名 `/黑名单`） |
| `/解封 <QQ>` | 移出黑名单 |

> `/手动拉黑` 兼容旧用法 `/手动拉黑 <QQ> <群号>`（指定退哪个群）。各命令的详细行为见配置页分组说明。

插件被禁用时只记录邀请；不会自动审批、退群、拉黑或拦截，管理员显式手动命令与查询仍可用。

## 安装

1. 把本仓库放进 AstrBot 的 `data/plugins/`（或在插件市场搜索安装）
2. 重启 AstrBot
3. WebUI → 插件 →「加群邀请守卫」里配置

> 默认配置就是安全模式：判断要加时**不会**自动进群，只私聊通知管理员，由你拍板。

## 可选联动

单独安装即可完整使用。如果装了「[用户画像](https://github.com/TaoXiaoBai/astrbot_plugin_user_profile)」插件，决策、`/画像` 命令和 LLM 查画像工具会优先使用它的完整画像（活跃度/发言风格/前科），没装则自动用内置精简画像，无影响。

## 平台要求

OneBot V11（`aiocqhttp`），已在 **SnowLuma** 验证；NapCat / LLOneBot / Lagrange 理论上也可用。

## 配置说明

<details>
<summary>点我展开完整配置表（所有配置都在 WebUI 插件页改，按分组展示）</summary>

> 1.14.0 起配置改为分组结构；旧版平铺配置会在启动时自动迁移到对应分组，值不变，无需手动处理。

**基础 `basic`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | `true` | 是否启用 |
| `notify_private` | `true` | 私聊通知管理员 |
| `notify_private_qq` | `""` | 通知 QQ（留空用全局管理员） |
| `notify_group` | `false` | 在指定群通知 |
| `notify_group_id` | `""` | 通知群号 |

**邀请决策 `decision`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `auto_approve` | `false` | 判断要加时自动同意进群 |
| `auto_reject` | `false` | 判断不要加时自动拒绝 |
| `reply_inviter_on_decision` | `true` | 审批/退群结果确定且成功后私聊邀请人 |
| `llm_provider_id` | `""` | 判断用的模型（留空用默认） |
| `decision_persona` | `""` | 决策用人格（留空用默认人格） |
| `enable_member_context` | `true` | 参考目标群成员 |
| `enable_impression_context` | `true` | 参考历史印象 |
| `impression_llm_summary` | `true` | 抓到发言原话时生成 LLM 印象小结（按发言条数缓存） |
| `enable_user_profile` | `true` | 决策时附邀请人画像（本地记录，不额外调 LLM） |
| `truncate_marker` | `…` | 截断占位符 |

**异常提前入群 `unexpected_join`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `mode` | `notify_only` | LLM 拒绝但 bot 已入群时：`notify_only` / `leave` / `message_then_leave` |
| `custom_leave_message` | `""` | `message_then_leave` 模式退群前发送；发送失败仍会继续退群 |

**小号识别 `alt_detect`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `alt_account_detect` | `true` | 识别黑名单用户的小号 |
| `alt_similarity_threshold` | `70` | 相似度（0-100）达到该值视为同一人 |
| `alt_gray_low` | `40` | 灰色区间下限：相似度在 [该值, 阈值) 时交 LLM 复判 |
| `alt_llm_review` | `true` | 灰色区间 LLM 复判开关（结论缓存 7 天） |

**私聊意图 `private_intent`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enable_private_intent` | `true` | 检测私聊加群意图 |
| `private_intent_reply` | `true` | 检测到意图时回复对方 |
| `private_intent_notify` | `true` | 检测到意图时通知管理员 |

**被踢报复 `kick_revenge`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `revenge_mode` | `off` | 被踢报复：`off` / `delete_friend` / `delete_and_ban` |
| `revenge_notify` | `true` | 报复后通知管理员（查不到邀请人也靠它通知） |
| `kick_ban_operator` | `false` | 被踢时把执行踢人的人也拉黑 |
| `record_group_join` | `true` | 记录每次进群的时间/操作人 |
| `cross_group_retaliation` | `false` | 被踢或被禁言达阈值时，连带退出该邀请人邀请过的所有群并拉黑 TA |
| `ban_notice_message` | `""` | 拉黑前私聊发给对方的话，手动拉黑也会发送（留空不发） |

**禁言报复 `mute_revenge`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `mute_retaliation_enable` | `false` | 被禁言达阈值自动退群并拉黑 |
| `mute_threshold` | `3` | 禁言次数阈值 |
| `mute_target` | `operator` | 拉黑对象：`operator` / `inviter` / `both` |
| `mute_ban_mode` | `astrbot_ban` | 拉黑方式：`astrbot_ban` / `delete_friend` |
| `mute_notify` | `true` | 被禁言/报复后通知管理员 |

**LLM 联动 `llm_integration`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `llm_context_inject` | `true` | 把封禁记录注入 LLM 上下文 |
| `llm_tool_ban` | `true` | 允许 LLM 主动拉黑/解封/查询/查画像 |
| `llm_tool_require_admin` | `false` | LLM 拉黑/解封/查画像需管理员 |

**显示 `display`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `invite_records_show_profile` | `true` | 邀请记录图显示邀请人头像昵称 |
| `invite_records_show_group_profile` | `true` | 邀请记录图显示群头像群名 |

</details>

## 常见问题

**为什么 bot 被踢/被禁言/进群后插件完全没反应？**
检查 AstrBot「平台设置」里的 **忽略机器人自身的消息**（`ignore_bot_self_message`）。开着它时，所有针对 bot 自身的通知（被踢、进群、被禁言）会在 waking_check 阶段被框架直接丢弃，插件根本收不到。本插件的这些功能依赖该开关**关闭**。只要协议端不上报 bot 自己发的消息（如 NapCat/SnowLuma 的 `reportSelfMessage=false`），关掉它是安全的。

**为什么 bot 被踢了却没有报复邀请人？**
如果拉 bot 进群的人是那个群的管理员/群主，QQ 直接放行、**不产生邀请事件**，插件无从记录邀请人。这种情况会在被踢时私聊通知你（附进群时间和操作人），你可以用 `/手动拉黑` 处理。

**私聊发邀请链接为什么 bot 只是回复，没有直接进群？**
私聊场景拿不到加群 `request` 事件（没有 `flag`），协议上就无法直接同意，只能通知你手动处理。

## License

MIT
