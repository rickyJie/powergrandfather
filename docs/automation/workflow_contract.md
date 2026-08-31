# 自动化任务 workflow 契约

本文档说明：要把一个项目交给 CSM 自动化模块跑，这个项目目录里需要长什么样。

## TL;DR

```
<你的项目>/
├── .workflow/                  ← 必须存在的契约文件夹
│   ├── README.md               ← 必须：流程说明
│   ├── done_criteria.md        ← 必须：完成判据
│   └── outputs.md              ← 可选但强烈建议：预期产出清单
└── 你的其它文件...
```

创建自动化 task 时，CSM 会扫这个 `.workflow/` 文件夹，按下面的规则检查。**前 3 条不过直接拒掉**，后 5 条 LLM 软判，可以 override。

## 规则清单

### 硬卡规则（不过不让进 LLM 步）

| 规则 | 含义 |
|---|---|
| **R1** | 项目 cwd 下必须存在 `.workflow/` 文件夹且非空 |
| **R2** | `.workflow/README.md` 必须存在 |
| **R3** | `.workflow/done_criteria.md` 必须存在 |

### 软判规则（LLM 审，可 override）

| 规则 | 含义 |
|---|---|
| **R4** | `done_criteria.md` 必须包含**可机器验证**的判据（如文件存在、特定字符串、退出码、数字阈值）。"我觉得做得差不多了" 这种不算。 |
| **R5** | 流程描述里不能包含**自动化无法跨越**的步骤——典型反例："等待用户确认"、"人工审阅"、"手动登录某网站"。 |
| **R6** | 预期产出明确：哪些文件、放在哪个路径、什么格式。建议写在 `outputs.md` 或 `README.md` 里。 |
| **R7** | 错误处理 / 重试策略至少有提及。卡死时怎么办、能不能重跑、有副作用吗。 |
| **R8** | 流程粒度合理。不能一句 "完成整个项目" 就完事；也不能拆到 50 步那种碎片化。 |

## review 结果四态

| 状态 | 含义 |
|---|---|
| `pending` | 还没审，或正在审 |
| `passed` | R1-R8 全过 |
| `passed_with_overrides` | R1-R3 全过，R4-R8 有 fail 但被用户知情忽略 |
| `failed` | R1-R3 任一不过；或 R4-R8 fail 且未 override |

只有 `passed` / `passed_with_overrides` 状态的 task 才能被启动（手动 launch 或 schedule 触发）。

## 触发审阅

- **自动**：创建 TaskDef 时后台异步跑一次（不阻塞创建）
- **手动**：`POST /api/tasks/{id}/review`（同步，10-30s 返回）

## Override 不过的规则

如果你确定 R4-R8 某条 fail 是误判（比如你的产出格式 LLM 没识别），可以：

```bash
curl -X POST http://localhost:8000/api/tasks/<task_id>/review/override \
  -H 'Content-Type: application/json' \
  -d '{"rule_id":"R6","reason":"产出走的是上游 service 推送，不在 cwd 下"}'
```

override 会记录到 `task_definition.review_overrides` JSON 字段，状态变 `passed_with_overrides`。

## 模板

参考 `docs/automation/template/.workflow/` 下的三个文件，照抄改成你项目的实际内容即可。
