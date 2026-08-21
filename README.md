# astrbot_plugin_group_invite_review

AstrBot 加群邀请守卫：当有人邀请机器人进群时，让 **LLM 自主判断**要不要加，而不是靠关键词或等级规则。同时支持识别私聊里"问能不能加群"或"直接发邀请链接"的场景。

## 功能

- 捕获 OneBot `request` 事件（`post_type=request / request_type=group / sub_type=invite`）
- 调用 LLM 判断 `approve`（同意）或 `reject`（拒绝）
- 按配置决定是否自动调用 `set_group_add_request`
  - 自动同意进群
  - 或仅通知管理员，等管理员决定（"让人联系管理员"）
- 识别私聊加群意图（`post_type=message / message_type=private`）
  - 有人私聊问"能不能拉你进群"、发 QQ 群邀请链接等
  - LLM 判断是否为加群意图，并生成回复 + 通知管理员
- 无论哪种结果，都按配置通知管理员（QQ 私聊 / 群）

## 配置

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 是否启用 |
| `auto_approve` | bool | `false` | LLM 判断"要加"时是否自动同意进群；关闭则只通知管理员 |
| `auto_reject` | bool | `false` | LLM 判断"不要加"时是否自动拒绝；关闭则只通知管理员 |
| `notify_private` | bool | `true` | 是否私聊通知管理员 |
| `notify_private_qq` | string | `""` | 私聊通知的 QQ 号；留空则使用 AstrBot 全局 `admins_id` 第一个 |
| `notify_group` | bool | `false` | 是否在指定群发通知 |
| `notify_group_id` | string | `""` | 通知群号；留空则不发送 |
| `llm_provider_id` | string | `""` | 判断用的 LLM provider id；留空则使用默认 provider |
| `decision_prompt` | string | 见 schema | 让 LLM 判断是否同意的提示词 |
| `enable_private_intent` | bool | `true` | 是否启用私聊加群意图检测 |
| `private_intent_reply` | bool | `true` | 检测到私聊加群意图时是否自动回复对方（回复内容由 LLM 生成） |
| `private_intent_notify` | bool | `true` | 检测到私聊加群意图时是否通知管理员 |
| `private_intent_prompt` | string | 见 schema | 私聊加群意图检测与回复的提示词 |

默认配置即"让人联系管理员"模式：LLM 判断要加时不会自动进群，只会私聊通知管理员。

## 私聊加群意图说明

私聊场景下机器人拿不到 OneBot 的加群 `request` 事件（没有 `flag`/`group_id`），所以无法直接调用 `set_group_add_request` 进群。插件会：

1. 粗筛私聊消息（邀请链接特征或"进群/加群/拉你/邀请"等关键词）
2. 命中后交给 LLM 精判是否为加群意图
3. 若为意图：按配置回复对方（LLM 生成），并通知管理员

正常情况下（非加群意图的私聊）不会调用 LLM，不影响日常聊天。

## 安装

1. 将本目录放到 AstrBot 的 `data/plugins/` 下，目录名保持 `astrbot_plugin_group_invite_guard`（插件标识；仓库名已改为 `astrbot_plugin_group_invite_review`）
2. 重启 AstrBot
3. 在 WebUI → 插件 → 加群邀请守卫 中配置

## 平台

- OneBot V11（`aiocqhttp`），已在 SnowLuma 上验证；NapCat / LLOneBot / Lagrange 等 OneBot 实现理论可用

## License

MIT
