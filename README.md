# 加群邀请守卫 (astrbot_plugin_group_invite_guard)

## 关于 / About

让机器人用**自己的人格设定**判断要不要接受加群邀请，并参考目标群成员和历史印象，而不是靠死板的关键词或等级规则；同时识别私聊里的加群意图。

A group-invite guard that decides whether to accept an invite **using the bot's own persona**, with target-group members and chat history as context — instead of hard-coded keyword/level rules. It also detects invite intent in private chats.

## 功能

- 捕获 OneBot 加群邀请请求（`post_type=request / request_type=group / sub_type=invite`）
- 用机器人人格 + 目标群成员 + 历史印象判断 `approve` / `reject`
- 按配置决定：自动同意进群、自动拒绝、或仅通知管理员
- 识别私聊加群意图（问"能不能加群"、发邀请链接）
- 结果通知管理员（私聊 / 群）
- 被踢后报复邀请人：自动同意进群时记录邀请人，被该群踢出后按配置删除并拉黑 / 加入 AstrBot 黑名单，并通知管理员
- 被禁言报复：按群记录被禁言次数，达到阈值自动退群，并按配置拉黑禁言者 / 邀请人，通知管理员
- 管理员命令：查看/维护邀请记录、查看 AstrBot 黑名单、解封、手动拉黑（退群+拉黑邀请人）（详见「命令」小节）；插件禁用时也会记录邀请但不接管事件
- LLM 主动拉黑/解封/查询（通过工具调用），并把封禁/邀请/禁言记录注入 LLM 上下文

## 配置

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 是否启用 |
| `auto_approve` | bool | `false` | 判断要加时是否自动同意进群 |
| `auto_reject` | bool | `false` | 判断不要加时是否自动拒绝 |
| `notify_private` | bool | `true` | 是否私聊通知管理员 |
| `notify_private_qq` | string | `""` | 通知 QQ（留空用全局管理员） |
| `notify_group` | bool | `false` | 是否在指定群通知 |
| `notify_group_id` | string | `""` | 通知群号 |
| `llm_provider_id` | string | `""` | 判断用的模型（留空用默认） |
| `enable_member_context` | bool | `true` | 是否参考目标群成员 |
| `enable_impression_context` | bool | `true` | 是否参考历史印象 |
| `truncate_marker` | string | `…` | 截断占位符 |
| `decision_persona` | string | `""` | 决策用人格（留空用当前账号默认人格） |
| `enable_private_intent` | bool | `true` | 是否检测私聊加群意图 |
| `private_intent_reply` | bool | `true` | 检测到意图时是否回复对方 |
| `private_intent_notify` | bool | `true` | 检测到意图时是否通知管理员 |
| `revenge_mode` | string | `"off"` | 被踢后的报复方式：`off` 关闭 / `delete_friend` 删除并拉黑 / `delete_and_ban` 删除并加入AstrBot黑名单 |
| `revenge_notify` | bool | `true` | 报复后是否通知管理员 |
| `mute_retaliation_enable` | bool | `false` | 被禁言达到次数后自动退群并拉黑 |
| `mute_threshold` | int | `3` | 被禁言达到该次数触发退群拉黑 |
| `mute_target` | string | `"operator"` | 拉黑对象：`operator` 禁言者 / `inviter` 邀请人 / `both` 都拉黑 |
| `mute_ban_mode` | string | `"astrbot_ban"` | 拉黑方式：`astrbot_ban` 加入AstrBot黑名单 / `delete_friend` 删除好友并拉黑 |
| `mute_notify` | bool | `true` | 被禁言/报复后是否通知管理员 |
| `ban_notice_message` | string | `""` | 拉黑前私聊发给邀请人的话（留空不发） |
| `llm_context_inject` | bool | `true` | 是否把封禁记录注入 LLM 上下文 |
| `llm_tool_ban` | bool | `true` | 是否允许 LLM 主动拉黑/解封/查询 |
| `llm_tool_require_admin` | bool | `false` | LLM 拉黑/解封是否需管理员 |

默认配置即"让人联系管理员"：判断要加时不会自动进群，只私聊通知管理员。

## 私聊加群意图说明

私聊场景拿不到加群 `request` 事件（没有 `flag`/`group_id`），无法直接调用 `set_group_add_request` 进群，插件会：

1. 粗筛私聊消息（邀请链接特征或"进群/加群/拉你/邀请"等关键词）
2. 命中后交给 LLM 精判是否为加群意图
3. 若为意图：按配置回复对方并通知管理员

非加群意图的私聊不会调用 LLM，不影响日常聊天。

## 安装

1. 将本目录放到 AstrBot 的 `data/plugins/` 下，目录名保持 `astrbot_plugin_group_invite_guard`
2. 重启 AstrBot
3. 在 WebUI → 插件 → 加群邀请守卫 中配置

## 平台

- OneBot V11（`aiocqhttp`），已在 SnowLuma 验证；NapCat / LLOneBot / Lagrange 等 OneBot 实现理论可用

## 命令

以下命令仅管理员可用，需先唤醒机器人（@机器人 或唤醒词），命令支持带 `/` 前缀（如 `/邀请记录`）：

| 命令 | 别名 | 参数 | 说明 |
| --- | --- | --- | --- |
| `/邀请记录` | `/邀请列表` | 无 | 以图片表格列出邀请记录（群号/邀请人/时间/处理结果/附言），渲染失败时回退纯文本 |
| `/记录邀请` | 无 | `群号` `邀请人QQ` | 手动写入一条邀请记录 |
| `/拉黑列表` | `/黑名单` | 无 | 列出 AstrBot 黑名单（QQ、拉黑时间、时长、原因） |
| `/解封` | 无 | `QQ` | 从 AstrBot 黑名单移除指定 QQ |
| `/手动拉黑` | 无 | `QQ` `[群号]` | 拉黑该QQ（发通知+删好友+加入AstrBot黑名单）；给了群号就先退群 |

## LLM 主动拉黑

机器人聊天时可自主调用三个工具：`group_invite_ban_user`（拉黑）、`group_invite_unban_user`（解封）、`group_invite_query_ban`（查询），并把当前封禁/邀请/禁言记录注入上下文。默认无需管理员，可在配置里开启管理员限制。

## License

MIT
