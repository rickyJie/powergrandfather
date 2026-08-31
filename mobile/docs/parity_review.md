# 移动端 ↔ 网页端 功能对齐报告（2026-08-17）

> 目标：把 web 端核心功能同步到手机端。5 个独立 agent 分模块审查得出。
> 图例：✅有 / 🟡简化 / ❌缺失；效率 S(小)/M(中)/L(大)。
> **设计内的桌面专属（不算缺口，保持现状）**：xterm/PTY 交互、workflow YAML 编辑与创作、
> budget 增删改、agent-alert 规则创作、port 注册/释放、backup/restore、proxy env、
> registered-agents 诊断、FilePicker、first-run 向导、WeekCalendar、CSV 导出。

---

## Tier 0 — Bug（不只是对齐，是坏的，先修）

- **Ports 状态标签失色**（`mobile/frontend/src/views/Ports.vue:36-41`）：`statusColor` 匹配
  `in_use/free/stale`,但后端返 `active/registered/conflict/stale`,除 stale 外全落灰。**S**
- （轻）Files `filesApi.raw(path, sid)` 的 `sid` 后端忽略(`files.py` 只认 `path`)——绝对路径下无害,可清掉。**S**

## Tier 1 — 高价值核心同步（手机上真正需要）

| # | 缺口 | 现状 | 建议 | 效率 |
|---|---|---|---|---|
| 1 | **Mission 逐阶段时间线 + 阶段输出 + 跳会话** | mobile 详情只有聚合 N/M 进度,无每阶段状态/输出/review note/session 跳转 | 从 `/api/runs?mission_id` + `/api/runs/{id}` 渲染阶段表,复用桌面逻辑 | **M** |
| 2 | **Schedules(调度)完全缺失** | mobile 无 schedules api、无任何视图,看不到自动化何时触发 | 新建 `api/schedules.ts` + 只读列表 + enable/disable/delete(砍日历/拖拽) | **M** |
| 3 | **Tokens — Claude `/usage` 配额探针面板** | 端点都不在 mobile tokens.ts;手机最想瞥的配额指标完全没有 | 加 `usageLive/usageLiveRefresh` + 两条 bar 卡片 | **M** |
| 4 | **Workflow review 判定 + launch 门禁** | mobile 拿了 `review_status` 却不显示,还允许启动 review-failed 的 workflow | 详情加判定徽章 + 非 pass 规则列表,`fail>0` 时禁 Launch(数据已在响应里) | **S** |
| 5 | **Sessions — rename + resume + 元信息块** | 详情只有 title+status;`patch` api 有但无 UI;resume 端点未接入 mobile api | 加重命名、resume(gate on jsonl_present)、pid/id/duration/exit/current_tool 信息块 | **S** |
| 6 | **AgentDeck — 打开历史会话** | 只显示"N past convo(s)"计数,不可点;`listConversations` api 已有却没用 | 计数可点 → 列出并打开任意 cid(ChatDetail 已能渲染任意 cid) | **S** |

## Tier 2 — 中价值 / 可编辑退化

| # | 缺口 | 现状 | 建议 | 效率 |
|---|---|---|---|---|
| 7 | **Settings 可编辑字段退化** | 默认 agent、session-prompt 正文、Lark chat_id/user_id 全变只读(开关能用) | 加 agent picker、prompt textarea、chat_id/user_id 编辑(PUT 端点都在) | **M** |
| 8 | **Sessions — 搜索 + 类型过滤** | 只按状态过滤,workflow AUTO 与 interactive 分不开;无搜索 | 加 Active/Auto/History 类型 tab + 客户端搜索(title/cwd/agent) | **S** |
| 9 | **Notifications 可读性** | 显示原始 enum 类型串、无严重度配色、长 escalation 报告硬夹 2 行、mission 深链只到列表 | 移植 type→中文标签 + type→颜色 + 点击展开 + 深链带 mission_id | **S** |
| 10 | **Feedback — 状态 + resolve** | 列表隐藏 status、resolved 混在一起、无 resolve/reopen(api 缺 patch) | 显示 status 标签 + 加 patch + 滑动 resolve + 过滤 | **S–M** |
| 11 | **Tokens — 时间窗切换 + cache-hit + 明细** | 固定 5h/14d,只有 input+output | 加窗口 tab + cache-hit 卡 + cache_read/creation 卡 | **S–M** |
| 12 | **Missions — 参数表单化 + 逐阶段/模式 retry** | launch 用裸 JSON textarea;retry 只能 current_stage + rerun | 用 `parameters[]` 渲染表单;retry 加 stage select + rerun/revalidate | **S** |

## Tier 3 — 低价值 / 打磨 / 待定

| # | 缺口 | 建议 | 效率 |
|---|---|---|---|
| 13 | Worktime 实时 ● ticker(现在 15s 轮询显得"死") | 移植 widget 的 1s 本地插值 | S |
| 14 | Chat "Thinking…" 指示 + 跳底按钮"N new"计数 | 小打磨(chat 已近满分,还多了 bash-hint) | S |
| 15 | Sessions archive/unarchive/purge + Session Changes/diff 只读面板 | 生命周期动作 + 只读 diff(适合路上 review) | M |
| 16 | **Sync** 只读 slice(Resources + Pending 计数) 或直接改文案承认桌面专属 | 全量移植价值低工时高;只做"是否在 drift"一瞥 | M |
| 17 | 合并重复的 `/chat` 与 `/agent-deck`(两个几乎一样的 agent 网格) | 结构清理 | S |
| 18 | Worktime heartbeat(mobile 停留是否计入 human 工时)——**先确认意图** | 若要计入则移植 heartbeat composable | S |

---

## 一句话总览
- **没有整块桌面视图在 mobile 缺失**(除 `/sync`)——mobile 甚至把桌面 widget 提升成了独立页。
- 真正的核心缺口集中在:**自动化的深度**(mission 阶段/输出、schedule、workflow 门禁)、**配额可视**(usage 探针)、**Sessions 生命周期动作**(rename/resume/search)、**Settings 可编辑性**。
- Chat 已近满分,Budgets 只读是合理裁剪,Ports/Files 的省略多为设计内。
