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

## License

MIT
