# CSM Mobile 端 — Phase 设计计划索引

分支：`llj_mobile`（基于 `llj_dev`，独立开发；所有 mobile 代码/测试/脚本/文档集中在 `mobile/`；主 repo 零业务改动）

## 顶层入口
- [`mobile/README.md`](../README.md) — mobile 项目说明 + 启动方式 + 目录结构
- [`docs/mobile_design.md`](../../docs/mobile_design.md) — 宏观设计（目标 / Non-Goals / 架构总图 / 功能矩阵 / 多端并发语义）

## Phase 详细计划

| Phase | 主题 | 估时 |
|---|---|---|
| P1 | 基础设施 + backend_patch + Layout 骨架 | 3-4h |
| P2 | Chat + Sessions 消息流核心 | 8-10h |
| P3 | 监控看板 (Missions + Workflows + Notifications + Tokens) | 6-8h |
| P4 | 次要模块 (AgentDeck + Budgets + Ports + Worktime + Files + Settings + Feedback) | 4-6h |
| P5 | 质量 + 文档 + 构建加固 | 3-4h |
| P6 | PWA 强化 | 2-3h |

**总计**：26-35h（连续跑约 30h）

## Phase 依赖

```
P1 (地基) ─┬─▶ P2 (消息流核心) ─┐
           │                     ├─▶ P4 (次要模块 复用)
           ├─▶ P3 (监控看板)  ───┘
           │
           └─▶ P5 (质量文档) ──▶ P6 (PWA)
```

- **P1** 是所有后续 phase 的地基（含 `mobile/backend_patch/mount.py` + `mobile/scripts/start_with_mobile.sh`）
- **P2** 与 **P3** 内部独立，可并行
- **P4** 依赖 P1-P3 的组件复用
- **P5** 收尾 + wrapper 加固 + ADR + 用户文档
- **P6** 最后加 PWA 特性

## 关键原则（贯穿 6 个 phase）

1. **主 repo 零改动**：`backend/` / `frontend/` / `scripts/` / `tests/` / `docs/` 一律不动（`docs/mobile_design.md` 为已同意的例外）
2. **桌面端零回归**：不跑 `mobile/scripts/start_with_mobile.sh` 时移动端代码不加载；`pytest tests/` 全绿
3. **独立测试目录**：`mobile/tests/backend/` + `mobile/tests/frontend/` 与主 repo `tests/` 完全隔离
4. **无 xterm**：手机端所有 session/chat 走消息流 UI
5. **无 push**：Phase 6 只做 PWA 静态强化，不做 Web Push（Phase N 评估）
6. **iOS 用 Blink Shell 兜底**：不做 iOS 原生 App

## 后续（Phase N，本计划外）

- Web Push (VAPID) or 复用 Lark push
- 原生 Android APK（若 PWA 体验不达预期）
- iOS 原生 App（需要评估必要性）
- 复杂模块的桌面深度集成（Workflow YAML 编辑、AgentAlert 规则生成）

## 常见问题速查

| 想要 | 去 |
|---|---|
| 理解为什么这么设计 | `docs/mobile_design.md` § Non-Goals + § 关键设计决策 |
| 知道现在能不能开工 | phase 内 "验收标准" + "文件清单" |
| 排查风险 | phase 内 "风险与缓解" |
| SSH tunnel 配置 | Phase 5 → `mobile/docs/setup.md`（P5 完成后产出） |
| PWA 添加主屏幕步骤 | Phase 6 → 追加到 `mobile/docs/setup.md` |
| 启动 mobile 端 | `mobile/README.md` § 启动方式 |
