# CSM Mobile 端重构设计（v1 draft，待确认）

> 状态：**设计草案，未开工**。等用户 confirm 本文档 + 日程后再分阶段执行。
> 约束（用户 2026-08-17 明确）：**web 与 mobile 维护两套独立 UI；不改动 web 端**
> （`frontend/**` 与 `backend/**` 一行不动，所有改动限定在 `mobile/`）。

---

## 1. 为什么要重构（问题根因）

本轮修了 codex 审出的一大批 P0/P1，但那些是**症状**，不是病根。病根有三个结构性问题：

### 病根 1：mobile 与 web 前端仍有隐性耦合
- 现在 `mobile/frontend/src/api/agents.ts`、`sessions.ts` 通过 Vite/tsconfig 别名
  `@shared-api` → `../../frontend/src/api` 直接引 **web 前端**的类型。
- 后果：**web 改一次 api 类型，就可能静默打断 mobile 的构建/类型检查**。这与
  "两套独立 UI" 的目标直接冲突——名义上两套，实际 mobile 的编译依赖 web 源码。

### 病根 2：契约漂移无护栏
- 除上面 2 个文件外，其余 api client 都是**手抄后端契约**的本地类型。
- 本轮 P0/P1 里 Tokens/Notifications/Feedback/Budgets/Worktime/Ports/Files/Lark
  的字段全错，就是"手抄 → 后端演进 → mobile 没跟 → 静默错"的漂移。
- 现在字段修对了，但**允许漂移的结构没变**——下次后端一改，同样的坑会重来，
  而且 CI 抓不到（无契约测试）。

### 病根 3：横切模式不一致（半成品化）
- api 层归一化（raw 契约 → 展示模型）只在 Tokens/Notifications 等做了，其余零散。
- 错误态：只有 Missions/Sessions/Workflows 用了 `ErrorRetry`，其余 view 仍把
  500/403 吞成"空列表"（误导）。
- 残留死代码：`ws.ts` 的 `useSessionSocket` / `sendBytes` 改造后已无调用者。

---

## 2. 目标（重构后的状态）

| # | 目标 | 判定标准 |
|---|---|---|
| G1 | mobile 成为**完全自包含**的独立 UI | 删除 `@shared-api`/`@shared-types` 别名后，`npm run build` 仍绿；grep 不到任何 `../../frontend` 引用 |
| G2 | **后端是唯一共享契约源**（不是 web 前端代码） | 两套 UI 各自持有 client，但都对齐同一份后端 API；共享的是"后端契约"这个事实，不是 TS 源文件 |
| G3 | **契约漂移有护栏** | 一个 mobile 侧契约测试，用 mount factory 起真实 app，断言关键端点字段名；后端改契约 → 测试红 |
| G4 | 横切模式统一 | 所有列表 view 有一致的 loading/empty/**error+retry**；所有 api client 遵循同一"raw→归一化"范式 |
| G5 | **web 端零改动、零回归** | `git status` 下 `frontend/**`、`backend/**` 无 mobile 引入的改动；桌面 `pytest` 全绿 |

**非目标（本次不做）**：
- 不合并成单套 responsive 前端（用户明确要两套）。
- 不动 web 端 UI / api / 类型。
- 不重做 mobile 的视觉设计语言（除非用户在确认时勾选，见 §6 开放项）。

---

## 3. 目标架构（mobile 独立四层）

```
┌─────────────────────────────────────────────┐
│  views / components         (Vant UI, 触屏)   │
├─────────────────────────────────────────────┤
│  stores (Pinia)             状态 + WS 生命周期  │
├─────────────────────────────────────────────┤
│  api clients                http + 归一化       │  ← raw契约→展示模型 统一在此
├─────────────────────────────────────────────┤
│  contracts/ (types)         mobile 自有类型     │  ← 不再引 web；后端为准
└─────────────────────────────────────────────┘
                    │
                    ▼ (仅 HTTP/WS，运行时)
             后端 /api /ws  (唯一共享契约)
```

关键转变：**"跟 web 共享 TS 源码" → "跟 web 共享后端 API"**。
两套 UI 在**运行时**都打同一个后端;在**源码层**彻底独立。

---

## 4. 工作分解（Workstreams）

### W1 — 彻底解耦 web（G1/G2）
- 把 `agents.ts`、`sessions.ts` 依赖的 `@shared-api` 类型**下沉为 mobile 本地类型**
  （对照后端契约抄一份自有的，只保留 mobile 实际用到的字段）。
- 删除 `vite.config.ts` / `tsconfig.json` 里的 `@shared-api`、`@shared-types` 别名。
- 验证：grep 无 `../../frontend`；build + typecheck 绿。

### W2 — 契约护栏（G3）
- 新增 `mobile/tests/backend/test_contract_fields.py`：用 `mobile.backend_patch` 的
  factory 起**真实 app**，对关键只读端点（tokens/notifications/budgets/worktime/
  ports/files/workflows/lark/preferences）断言响应 JSON 的**字段名集合**，与 mobile
  client 期望一致。后端契约漂移 → 立即红。
  - 注意：这是 mobile 自己的隔离测试目录，仍不碰主 repo `tests/`。
- （可选）加一个 `contracts.ts` 单一清单，把"mobile 期望的字段"集中声明，测试和
  client 都引它，单点对账。

### W3 — 横切模式统一（G4）
- api 层：所有 client 统一"raw 契约类型 + `normalizeX()` → 展示模型"范式
  （Tokens/Notifications 已是样板，推广到其余）。
- 错误态：抽 `useAsyncList` composable（loading/error/empty/retry 一把梭），
  Missions/Sessions/Workflows 的 `ErrorRetry` 推广到所有列表 view。
- 清死代码：`ws.ts` 移除 `useSessionSocket`/`sendBytes`（若确认无用）。

### W4 —（可选）UX/结构打磨
- 视 §6 用户是否勾选：消息流虚拟滚动、骨架屏、手势返回、主题细化等。

---

## 5. 分阶段日程（小时级，待确认）

| Phase | 内容 | 估时 | 交付 |
|---|---|---|---|
| R0 | 现状基线：跑通 build + 全测试，记录 grep 耦合清单作 before 快照 | 0.5h | 基线报告 |
| R1 | **W1 解耦**：下沉 agents/sessions 类型，删别名，build/typecheck 绿 | 1.5–2h | mobile 无 web 源码依赖 |
| R2 | **W2 护栏**：契约字段测试（起真实 app），跑红→跑绿验证 | 2–3h | 漂移守卫测试 |
| R3 | **W3 归一化推广**：api client 统一范式 + `useAsyncList` | 2–3h | 一致的 api/错误态 |
| R4 | **W3 错误态铺开** + 清死代码 | 1–1.5h | 全 view 一致 loading/error/empty |
| R5 | 回归收尾：mobile vitest + mobile pytest + 桌面 `pytest tests/`（零回归 gate）+ rebuild | 1h | 全绿 + dist |
| R6 |（可选 W4）视勾选而定 | TBD | — |

**合计（R0–R5）≈ 8–11h**。R6 视 §6 决定。

节奏纪律：跑得快不提前进下一 phase，buffer 转深化 / edge case / 独立审查，不压缩日程。

---

## 6. 需要你在确认时拍板的开放项

1. **是否包含 W4（UX/视觉打磨）**？默认**不含**（纯架构重构，风险最低）。
2. **契约护栏形态**：轻量（字段名断言测试）还是重一点（生成式契约快照 + diff）？
   默认**轻量**。
3. **`sendBytes`/`useSessionSocket` 是否确认删**？我改造 SessionDetail 后已无调用；
   若你还想保留"未来接结构化 session 流"的可能，可留着标 deprecated。默认**删**。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 删 `@shared-api` 后漏改导致 build 断 | R1 结束以 build+typecheck 绿为准出口 |
| 契约测试起真实 app 时依赖环境（DB/env） | 复用现有 mobile `conftest` 隔离 fixture；`CSM_CLAUDE_ARGV='bash -i'` 防烧 token |
| 误触 web/backend | 所有改动限 `mobile/`；每 phase 末 `git status` 核 `frontend/**`/`backend/**` 无改动 |
| 回滚 | mobile 目录整体 untracked，可整目录 `git stash`/丢弃；web 从未被动 |

---

## 8. 与本轮已完成修复的关系

本文档是**结构性**重构，建立在本轮**功能性**修复之上：
- 本轮：把漂移的字段**逐个修对**（症状）。
- 本重构：**消除允许漂移的结构**（病根）——解耦 + 护栏 + 统一范式。
两者不冲突；重构不回退本轮任何修复。
