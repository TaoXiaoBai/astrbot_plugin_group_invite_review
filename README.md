<div align="center">
  <img src="logo.png" width="128" alt="logo">
  <h1>加群邀请守卫</h1>
  <p>让 LLM 根据<b>人格设定</b>判断是否通过邀请加群</p>
  <p>
    <img src="https://img.shields.io/badge/version-1.10.0-blue" alt="version">
    <img src="https://img.shields.io/badge/AstrBot-4.x-4a6cf7" alt="astrbot">
    <img src="https://img.shields.io/badge/platform-OneBot%20V11-green" alt="platform">
    <img src="https://img.shields.io/badge/license-MIT-orange" alt="license">
  </p>
</div>

## 这是什么

一个 AstrBot 插件。有人拉你的 bot 进群时，不靠死板的关键词/等级规则，而是交给 LLM 根据你设定的**人格**，结合目标群的成员和历史聊天印象，自己判断「进还是不进」。进了被踢、被禁言，还能记仇报复。

## 功能一览

**进群决策**
- 捕获加群邀请，由 LLM（人格 + 群成员 + 历史印象）判断同意/拒绝
- 三种处理模式：自动同意 / 自动拒绝 / 仅通知管理员（默认）
- 私聊里问"能不能加群"、直接甩邀请链接，也能识别并回复/通知

**记仇与报复**
- 自动记录每次进群的邀请人；被该群踢出后可自动删除好友 / 拉黑邀请人，并通知管理员
- 可选：被踢时连执行踢人的管理员一起拉黑
- 被禁言自动计数，达到阈值自动退群 + 拉黑
- 拉黑前可给对方发一句自定义告别留言
- 管理员直拉进群没有邀请事件？也没关系——进群时间/操作人照样记录，被踢时通知你

**LLM 联动**
- 封禁/邀请/禁言记录自动注入 LLM 上下文，bot 聊天时知道谁有前科
- bot 可自主调用拉黑 / 解封 / 查询工具（可选需管理员）

**管理命令**（仅管理员，需唤醒 bot）

| 命令 | 说明 |
| --- | --- |
| `/邀请记录` | 图片表格展示邀请记录（头像昵称/群名/处理结果，底部附操作提示） |
| `/记录邀请 <群号> <QQ>` | 手动补录一条邀请记录 |
| `/手动拉黑 <QQ或群号>` | 记录驱动：先查邀请记录——是群号则拉黑该群邀请人，是邀请人QQ则收集TA邀请过的所有群；先退群（有几组退几群）再统一拉黑（通知只发一次）；查无记录则按QQ直接拉黑并注明。旧用法 `/手动拉黑 <QQ> <群号>` 不变 |
| `/拉黑列表` | 查看黑名单（含拉黑原因） |
| `/解封 <QQ>` | 移出黑名单 |

插件被禁用时只记录邀请、不接管事件。

## 安装

1. 把本仓库放进 AstrBot 的 `data/plugins/`（或在插件市场搜索安装）
2. 重启 AstrBot
3. WebUI → 插件 →「加群邀请守卫」里配置

> 默认配置就是安全模式：判断要加时**不会**自动进群，只私聊通知管理员，由你拍板。

## 平台要求

OneBot V11（`aiocqhttp`），已在 **SnowLuma** 验证；NapCat / LLOneBot / Lagrange 理论上也可用。

## 配置说明

<details>
<summary>点我展开完整配置表（所有配置都在 WebUI 插件页改）</summary>

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | `true` | 是否启用 |
| `auto_approve` | `false` | 判断要加时自动同意进群 |
| `auto_reject` | `false` | 判断不要加时自动拒绝 |
| `notify_private` | `true` | 私聊通知管理员 |
| `notify_private_qq` | `""` | 通知 QQ（留空用全局管理员） |
| `notify_group` | `false` | 在指定群通知 |
| `notify_group_id` | `""` | 通知群号 |
| `llm_provider_id` | `""` | 判断用的模型（留空用默认） |
| `decision_persona` | `""` | 决策用人格（留空用默认人格） |
| `enable_member_context` | `true` | 参考目标群成员 |
| `enable_impression_context` | `true` | 参考历史印象 |
| `truncate_marker` | `…` | 截断占位符 |
| `enable_private_intent` | `true` | 检测私聊加群意图 |
| `private_intent_reply` | `true` | 检测到意图时回复对方 |
| `private_intent_notify` | `true` | 检测到意图时通知管理员 |
| `revenge_mode` | `off` | 被踢报复：`off` / `delete_friend` / `delete_and_ban` |
| `revenge_notify` | `true` | 报复后通知管理员（查不到邀请人也靠它通知） |
| `record_group_join` | `true` | 记录每次进群的时间/操作人 |
| `kick_ban_operator` | `false` | 被踢时把执行踢人的人也拉黑 |
| `mute_retaliation_enable` | `false` | 被禁言达阈值自动退群并拉黑 |
| `mute_threshold` | `3` | 禁言次数阈值 |
| `mute_target` | `operator` | 拉黑对象：`operator` / `inviter` / `both` |
| `mute_ban_mode` | `astrbot_ban` | 拉黑方式：`astrbot_ban` / `delete_friend` |
| `mute_notify` | `true` | 被禁言/报复后通知管理员 |
| `ban_notice_message` | `""` | 拉黑前私聊发给对方的话（留空不发） |
| `llm_context_inject` | `true` | 把封禁记录注入 LLM 上下文 |
| `llm_tool_ban` | `true` | 允许 LLM 主动拉黑/解封/查询 |
| `llm_tool_require_admin` | `false` | LLM 拉黑/解封需管理员 |
| `invite_records_show_profile` | `true` | 邀请记录图显示邀请人头像昵称 |
| `invite_records_show_group_profile` | `true` | 邀请记录图显示群头像群名 |

</details>

## 常见问题

**为什么 bot 被踢了却没有报复邀请人？**
如果拉 bot 进群的人是那个群的管理员/群主，QQ 直接放行、**不产生邀请事件**，插件无从记录邀请人。这种情况会在被踢时私聊通知你（附进群时间和操作人），你可以用 `/手动拉黑` 处理。

**私聊发邀请链接为什么 bot 只是回复，没有直接进群？**
私聊场景拿不到加群 `request` 事件（没有 `flag`），协议上就无法直接同意，只能通知你手动处理。

## License

MIT
