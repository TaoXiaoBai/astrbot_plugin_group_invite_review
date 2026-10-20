<div align="center">
  <img src="logo.png" width="128" alt="logo">
  <h1>加群邀请守卫</h1>
  <p>让 LLM 根据<b>人格设定</b>判断是否通过邀请加群</p>
  <p>
    <img src="https://img.shields.io/badge/version-1.24.0-blue" alt="version">
    <img src="https://img.shields.io/badge/AstrBot-4.x-4a6cf7" alt="astrbot">
    <img src="https://img.shields.io/badge/platform-OneBot%20V11-green" alt="platform">
    <img src="https://img.shields.io/badge/license-MIT-orange" alt="license">
  </p>
</div>

## 这是什么

一个 AstrBot 插件。有人拉你的 bot 进群时，管理员及 QQ 白名单的已核验真实邀请默认免审核；其他邀请交给 LLM 根据你设定的**人格**，结合目标群的成员和历史聊天印象，判断「进还是不进」。进了被踢、被禁言，还能记仇报复。

## v1.24 权威动态关系与画像同步

`bot_group_relations_v1` 独立 KV 以 `[platform_id,self_id,group_id]` 隔离，保存 inviter（明确邀请人）、operator（独立操作人）、request_key、join_time、membership（IN/OUT/UNKNOWN）、confirmed_at、evidence、稳定 event_key、revision 和投递创建毫秒时间。`record_bot_group_relation(...)` 为可信生命周期写入 API，`get_bot_group_relations(platform_id=..., self_id=..., group_id="", inviter="")` 为有界读取 API，均不是聊天权限授权。

- 收到邀请或审批 API 成功只说明请求/动作状态；只有本 bot 的真实入群 notice 或 bot/群返回匹配的成员查询确认 IN。查询失败保持 UNKNOWN，不能当 OUT。邀请归因要求平台、bot 账号、近期 VERIFIED 请求完整且唯一；历史缺范围、多个候选或管理员直拉都保持未知，操作人不冒充邀请人。
- 离群/被踢 notice 记录 OUT；本插件所有成功 `set_group_leave` 调用经统一入口更新 OUT，失败不写。记录不依赖报复总开关、旧 `record_group_join` 或处罚白名单（白名单继续保护动作，不掩盖客观生命周期）。客户端范围不唯一时不猜写其它 bot；收到真实离群 notice 后可补确认。协议返回成功仅为动作证据，不是永久当前在群判断。
- 同一 KV 原子保存快照、去重键和 pending 投递。首次投递与每 30 秒恢复使用相同事件键及创建时间；重启自动恢复；画像未安装/旧接口/禁用/失败/超时均保留 pending。画像 recorded/duplicate/cancelled 才确认投递完成，确认落盘失败可安全重试。画像删除用 cutoff 和世代保护拒收旧事件，后续新生命周期仍可同步。
- 不扫描旧历史、不调用模型补归因，不把旧全局前二十条邀请当当前关系。日常群聊 fallback 只看当前群/当前 bot，私聊只看当前发送者明确作为 inviter 的关系，最多六条、1200 字；状态表述为最近确认/待核实。画像必须实际注入关系并提供群号/版本 claims 才认领；仅去重已覆盖版本，缺失或更新关系仍提供 fallback，黑名单不被删除。新关系 KV/同步故障告警并放行旧 join/kick/审批/退群主流程，已成功退群不会因联动失败变为动作失败；取消仍向上传播。
- 账本上限为一万条作用域关系、两万个事件键；达到上限会显式拒写，避免悄悄遗忘去重。KV 错误不覆盖旧账本；退群这种外部动作与 KV 无法跨系统事务化，动作成功至本地保存之间进程崩溃仍有窗口，后续真实 notice 可补齐，不伪造历史。无时间戳 notice 仅能按收到的生命周期顺序去重，无法从协议缺失数据推断真实先后。

## 功能一览

**进群决策**
- 收到针对 bot 的真实邀请后立即持久化；默认对最新 AstrBot `admins_id` 或 `protection.qq_whitelist` 精准匹配且目标 VERIFIED 的邀请免审核，规则直接建议同意，不调用 LLM、画像或群成员背景
- 免审核与处罚保护开关独立，群白名单不赋予邀请人信任；仍受插件启用、`auto_approve`、成员状态、flag 和动作前持久化约束。配置读取异常记录 unknown/失败并通知，不交给 LLM 拒绝，也不默认信任
- 普通邀请或关闭免审核开关时，收集上下文、调用 LLM、执行前复核成员状态，最后同意或拒绝；记录和通知通过 `decision_source` 区分规则与 LLM
- 外部动作前持久化执行中状态；重启遇到未确认动作只对账和通知，不自动重放
- 非免审核邀请即使 bot 已提前入群或审核期间入群也不会跳过 LLM；LLM 拒绝时可仅通知、直接退群或发自定义消息后退群
- 三种处理模式：自动同意 / 自动拒绝 / 仅通知管理员（默认）
- 审批或补偿结果确定后才按人格私聊邀请人；动作失败时不会发送承诺成功的话术
- 决策附邀请人画像：历史邀请次数、黑名单前科、活跃度，并排除当前审核记录（纯本地数据，不额外调 LLM）
- 安装用户画像插件时优先读取结构化风险分、标签、活跃度和社交来源，并把决策时快照写入邀请记录；旧版画像插件自动降级兼容
- 抓到邀请人历史发言原话时，由 LLM 浓缩成一段 100 字内的印象小结（按发言条数缓存，陌生人零开销）
- 小号识别：被拉黑的人换号再来？附言/昵称相似度命中即在决策上下文和通知里提示"疑似小号"；相似度处于灰色区间时再交 LLM 复判一次（结论缓存 7 天）
- 单次审核复用邀请记录与历史会话快照，人格和背景并发加载；同键 LLM 小结/复判合并为一次调用，不改变审核提示词与状态机
- 私聊里问"能不能加群"、直接甩邀请链接，也能识别并回复/通知

**记仇与报复**
- 自动记录每次进群的邀请人；被该群踢出后可自动删除好友 / 拉黑邀请人，并通知管理员
- 可选：被踢时连执行踢人的管理员一起拉黑
- 可选：跨群连坐——被踢或被禁言达阈值时，连带退出该邀请人邀请过的所有群再拉黑 TA
- 被禁言自动计数，达到次数阈值直接退群 + 拉黑，不调用 LLM；未达次数但单次达到 24 小时（可调）时按现有人格与邀请人/群背景交 LLM 复核，仅明确决定退群才执行同一套动作
- 默认把可验证的禁言操作者写入用户画像并增加 35 风险（累计默认最多 70）；可改为邀请人关联责任或两者。邀请人不会伪装成操作者，未知/非法/机器人自身 QQ 不归因。该联动与禁言报复总开关独立
- 可另行开启“即时拉黑画像归因对象”（默认关闭），它独立于禁言报复开关，并可与画像风险增量同时启用；任务先持久化为 pending，成功后才标 completed，失败或重启后自动重试，同一事件不会和阈值报复重复拉黑
- 长期禁言复核为默认开启的子开关，但禁言报复总开关默认关闭；LLM 保留、异常或无效输出只通知管理员并保留次数。协议时长为秒，非法或超过 366 天的值不参与复核，普通通知显示可读时长
- 长期禁言 LLM 退群可选在所有退群/连坐/拉黑动作前私聊邀请人（默认关闭），使用独立的 `long_mute_inviter_message`；留空回退安全默认文案。它不读取被踢/审核场景的 `ban_notice_message`，LLM `reply`/`reason` 也不会外发。发送失败不影响后续动作
- 动作前会清理本群计数以防并发重复；当前群退群明确失败或等待退群时任务被取消，会以不覆盖更大新值的方式恢复计数。退群是跨系统外部动作，极端进程崩溃窗口无法事务化，计数仍可能丢失；不会在重启后猜测或重放未知结果
- 管理员直拉进群没有邀请事件？也没关系——进群时间/操作人照样记录，被踢时通知你

**LLM 联动**
- 黑名单及当前平台/bot/群或明确邀请人的动态关系按范围注入；不再日常注入全局邀请与群禁言前二十条。完整历史仍可通过显式查询工具读取，历史邀请不代表当前成员关系
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

单独安装即可完整使用。如果装了「[用户画像](https://github.com/TaoXiaoBai/astrbot_plugin_user_profile)」插件，邀请审核优先调用 `get_decision_profile()`，读取结构化风险分、标签、活跃度、数据新鲜度、LLM 状态和社交来源；当前审核记录按 `request_key` 排除。旧画像插件依次降级到标签 API 或文本 API。

守卫同时提供版本化只读 API `get_inviter_evidence(qq, exclude_request_key="")`。画像插件优先通过该 API 取得有限的邀请/进群摘要，旧守卫才回退 KV；旧或脏 KV 的非法时间会安全降级为 `0`。守卫 1.21.0+ 调用画像 1.10.0+ 的禁言写入 API：先持久化待投递目标，只有 `recorded`/`duplicate` 才完成；插件缺失、旧 API、禁用、失败或返回 false 都保留 pending，由启动恢复循环继续投递。稳定事件 completed 账本不淘汰，`mute_event_jobs` 全局最多 5000 条，满后明确拒绝新事件而不删除旧键。邀请人归因和即时拉黑只接受当前群 `join_records.inviter` 明确字段，不使用 `join_records.operator` 或旧邀请记录兜底；联动 job 达上限或 KV 故障只会跳过画像/direct，不会阻断原禁言计数、阈值退群或长期复核。

守卫 1.22.0 配合画像 1.11.0+ 时，还会在动作**真正成功**后 fail-open 写入通用管理账本：qq_tools 黑名单成功持久化记 `astrbot_banned`，解封成功持久化记 `astrbot_unbanned`，被踢报复、禁言报复或手动拉黑中的 `delete_friend` 成功记 `bot_deleted_friend`。画像插件缺失、版本过旧、超时或抛错不会反转已经成功的守卫动作，也不会阻断原流程；失败动作绝不写成成功历史。

恢复客户端按 `self_id` 隔离：`delete_friend` 必须等对应 bot 账号出现后才能恢复，绝不借用其它账号；纯 AstrBot 黑名单模式无需协议客户端。初始化后尚未见到该账号时 pending 等待是正常行为。画像 `/画像删除` 会调用可信取消接口，把该 QQ 未完成的画像投递标为 cancelled 并等待在途调用收敛，但不会取消 direct blacklist；completed 账本继续保留，防止旧稳定事件重放。

结构化画像快照不复制发言原话，并保留 `age_seconds=null` 的“新鲜度未知”语义。邀请附言、好友验证语、历史印象、画像 evidence 等内容在决策 prompt 中统一标记为不可信证据并限长，模型不得执行其中指令。模型输出的 `reason` 会清控制字符并限制为 300 字，`reply` 限制为 500 字，然后才落盘或发送。`/邀请记录` 可同时查看 LLM 建议与实际执行结果。

## 平台要求

OneBot V11（`aiocqhttp`），已在 **SnowLuma** 验证；NapCat / LLOneBot / Lagrange 理论上也可用。

## 配置说明

<details>
<summary>点我展开完整配置表（所有配置都在 WebUI 插件页改，按分组展示）</summary>

> 1.14.0 起配置改为分组结构；旧版平铺配置会在启动时自动迁移到对应分组，值不变，无需手动处理。

**白名单保护 `protection`（1.23.0）**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `protect_astrbot_admins` | `true` | 实时保护全局 `admins_id` 中全部管理员，不只是通知接收人 |
| `qq_whitelist` | `""` | QQ 名单，逗号或空白分隔，精确匹配 |
| `group_whitelist` | `""` | 群名单，逗号或空白分隔，精确匹配 |

保护操作者禁言/踢 bot，或事件发生在保护群时，整个处罚链跳过：不累计次数、不复核、不退群、不删好友、不拉黑、不增加禁言画像。普通操作者涉及保护邀请人时，只跳过邀请人的归因、处罚与跨群退出。动作前再次检查最新名单，手动命令和 LLM 工具也不能绕过；跨群逐群跳过保护群，异常提前入群补偿也不退出保护群。配置读取异常（含管理员字段缺失/非法）时暂停危险动作。

恢复旧画像/direct pending 时，保护目标标为 `cancelled`，不是处罚成功；已 completed 去重账本保留。保护 QQ 不新增守卫画像事件或管理记录。不会自动回滚旧风险/黑名单、加回 QQ 好友或重新进群；旧黑名单可单独 `/解封`，好友/群关系需人工恢复。名单在外部 API/画像 API 已提交后才变化时，不能撤回在途动作。

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
| `trusted_inviter_bypass_review` | `true` | 已核验管理员/QQ 白名单真实邀请由规则建议同意，免 LLM 审核；读取异常暂停处理，与处罚保护独立 |
| `auto_approve` | `false` | LLM 或可信规则判断要加时自动同意进群；关闭时只记录建议并通知 |
| `auto_reject` | `false` | 判断不要加时自动拒绝 |
| `reply_inviter_on_decision` | `true` | 审批/退群结果确定且成功后私聊邀请人 |
| `custom_reject_reply` | `""` | 固定拒绝文案（留空由 LLM 按人格和上下文写；`{reason}` 替换为拒绝原因） |
| `llm_provider_id` | `""` | 判断用的模型（留空用默认） |
| `decision_persona` | `""` | 决策用人格（留空用默认人格） |
| `enable_member_context` | `true` | 参考目标群成员 |
| `enable_impression_context` | `true` | 参考历史印象 |
| `impression_llm_summary` | `true` | 抓到发言原话时生成 LLM 印象小结（按发言条数缓存） |
| `enable_user_profile` | `true` | 决策时附邀请人画像（本地记录，不额外调 LLM） |
| `use_profile_plugin` | `true` | 安装用户画像插件时优先接入其结构化画像，失败自动降级 |
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
| `enable_blacklist_inquiry` | `true` | 识别私聊询问拉黑、拒绝、被踢或解封原因 |
| `blacklist_inquiry_reply` | `true` | 识别后自动回复 |
| `blacklist_inquiry_notify` | `true` | 识别后通知管理员 |
| `blacklist_inquiry_keywords` | 内置关键词 | 逗号分隔的粗筛关键词；未命中不调用 LLM |
| `blacklist_inquiry_tone` | `persona` | 回复语气：`persona` / `serious` / `polite` / `cold` |
| `blacklist_inquiry_show_reason` | `true` | 回复中是否简要说明原因 |
| `blacklist_inquiry_max_length` | `120` | 自动回复最大字数 |
| `blacklist_inquiry_system_prompt` | `""` | 自定义系统提示词；支持 schema 所列变量 |
| `blacklist_inquiry_user_prompt` | `""` | 自定义用户提示词；支持 schema 所列变量 |

**被踢报复 `kick_revenge`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `revenge_mode` | `off` | 被踢报复：`off` / `delete_friend` / `delete_and_ban` |
| `revenge_notify` | `true` | 报复后通知管理员（查不到邀请人也靠它通知） |
| `kick_ban_operator` | `false` | 被踢时把执行踢人的人也拉黑 |
| `record_group_join` | `true` | 记录每次进群的时间/操作人 |
| `cross_group_retaliation` | `false` | 被踢或被禁言达阈值时，连带退出该邀请人邀请过的所有群并拉黑 TA |
| `ban_notice_message` | `""` | 被踢报复/手动拉黑等原有流程的拉黑前留言；长期禁言通知不读取此项 |
| `intercept_banned_messages` | `true` | 兜底拦截已拉黑用户的群聊/私聊消息 |

**禁言报复 `mute_revenge`**

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `mute_retaliation_enable` | `false` | 只控制禁言次数和长期复核/退群，不控制画像联动或即时拉黑 |
| `mute_threshold` | `3` | 次数达到后直接退群并拉黑，不调用 LLM |
| `long_mute_llm_review` | `true` | 未达次数时，长期禁言交给 LLM 复核 |
| `long_mute_threshold_hours` | `24` | 单次禁言触发复核的小时数（运行时限制 1-8760） |
| `long_mute_notify_inviter` | `false` | 仅长期禁言经 LLM 决定退群后、所有动作前私聊邀请人 |
| `long_mute_inviter_message` | `这次禁言时间太长，我先退出本群。祝好。` | 长期禁言退群专用文案；留空回退该默认值，不读取 `ban_notice_message`，不外发 LLM reply/reason |
| `mute_target` | `operator` | 拉黑对象：`operator` / `inviter` / `both` |
| `mute_ban_mode` | `astrbot_ban` | 拉黑方式：`astrbot_ban` / `delete_friend` |
| `mute_notify` | `true` | 被禁言/报复后通知管理员 |
| `profile_mute_enable` | `true` | 将个人禁言证据写入画像；与 `mute_retaliation_enable` 独立 |
| `profile_mute_target` | `operator` | `operator` / `inviter`（关联责任）/ `both` |
| `profile_mute_risk_increment` | `35` | 每个去重事件的画像风险增量，限制 0-100 |
| `profile_mute_risk_cap` | `70` | 单人禁言风险累计上限，限制 0-100 |
| `profile_mute_direct_blacklist` | `false` | 立即拉黑画像归因对象；可与风险叠加同时开启 |

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
| `invite_records_hide_dealt` | `true` | 隐藏邀请人已拉黑的记录 |
| `invite_records_show_decision_detail` | `true` | 文字/图片记录显示 LLM 建议、理由、画像风险分、主要标签和精简社交来源 |
| `image_profile_concurrency` | `6` | 图片生成时昵称/群名 API 最大并发数（运行时限制 1-20） |

图片版使用纵向卡片：顶栏展示时间与决策状态，邀请人和目标群分栏展示，执行/成员状态使用短标签；邀请附言、LLM 理由、画像标签和社交来源各自占用独立内容块，避免大量文字挤在同一个表格单元格。过长附言和理由会自动截断，画像标签以可换行 chips 展示。

普通消息拦截会短时复用插件邀请记录派生索引，插件写入时立即失效；`qq_tools` 黑名单每次实时读取，不会被长期缓存。LLM 封禁上下文同样只短时缓存插件自身的邀请/禁言派生部分。动作前后的成员状态仍通过 OneBot 实时查询。

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
