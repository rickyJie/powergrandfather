# CSM Workflow YAML Generation Manual

> **本文档面向 agent（不是用户）**。你（agent）读完这份手册后，
> 要把用户给的**一句话自然语言需求**转成一份**可工作**的 workflow YAML。
>
> 用户对 YAML 语法一无所知，也不知道 stages / poll / validation 是什么。
> **拆解、结构化、避坑、自检**都是你的活。
>
> 本文档随 repo 一起发布：`docs/workflow_authoring_guide.md`。

---

## 目录

- **[§0 你的任务（一句话总结）](#0-你的任务一句话总结)**
- **[§1 如何拆解用户的一句话需求](#1-如何拆解用户的一句话需求)** ★ 核心方法论
- **[§2 Workflow YAML 骨架](#2-workflow-yaml-骨架)**
- **[§3 Placeholder 词汇表（封闭）](#3-placeholder-词汇表封闭)**
- **[§4 三个模板 skeleton](#4-三个模板-skeleton)**
- **[§5 Validation Primitives 全表](#5-validation-primitives-全表)**
- **[§6 硬规则 F1-F9（生成时必须避开）](#6-硬规则-f1-f9生成时必须避开)**
- **[§7 自检 R9-R19（返回给用户前必过）](#7-自检-r9-r19返回给用户前必过)**
- **[§8 输出格式（写入路径 + 交付确认）](#8-输出格式写入路径--交付确认)**
- **[Appendix A: Hello World 完整例子](#appendix-a-hello-world-完整例子)**
- **[Appendix B: 用户视角（调试 / lifecycle / UI）](#appendix-b-用户视角调试--lifecycle--ui)**
- **[Appendix C: 快速参考卡](#appendix-c-快速参考卡)**

---

## §0 你的任务（一句话总结）

**输入**：
- 用户在 CSM UI 上复制的一段 prompt，里面有一句自然语言需求
  （比如"每周日晚从 feedback_service 意见箱拉意见，逐条评审，需要改代码就改+commit+push，飞书通知我"）
- 你当前的 cwd = 用户想自动化的**目标 repo**（比如 `<TARGET_REPO>`）

**输出**：
- 一份 workflow YAML，落在**强制路径**：
  `<CSM_REPO>/tasks/<workflow_name>.workflow.yaml`
  （注意：**跨 repo 写文件** —— YAML 不放在你当前 cwd 的 tasks 里）
- 最后**输出一行确认**：`wrote <CSM_REPO>/tasks/<name>.workflow.yaml`

**中间流程**（每一步都是必做）：

```
用户一句话 → §1 拆解成 stages + params + outputs
          → §4 挑合适模板 skeleton
          → §2/§3 填字段（避开 §6 F1-F9 坑）
          → §5 加合适 primitive
          → §7 自检 R9-R19 到 0 warn + 0 fail
          → §8 写入并确认
```

**你不需要问用户澄清**（默认场景）——用户希望你能从自然语言 + repo 上下文自动决定。
只有一种情况需要主动澄清：**根据 §1 的拆解规则仍无法确定 stage 边界或产物形式**。

---

## §1 如何拆解用户的一句话需求

**这是你的核心工作**。用户给的是自然语言，你要把它变成有 3-N 个 stages 的
线性数组。以下是方法论，配合末尾 feedback_service 完整案例演示。

### F0（必读，先于所有其它规则）：workflow 只描述 **做什么 / 怎么做**，**不描述什么时候跑**

一个 workflow 是一次可复现的**单次实验定义**。什么时候触发、跑几次、多久跑一次
—— 这些是 **Schedule 层**（cron / one-shot / 手动 launch）的事，跟 workflow YAML
无关。

具体：

- 用户 requirement 里出现 **"每天 / 每周 / 每周日晚 22:00 / 每小时 / 每 N 次 /
  每月第 N 天"** 之类的时间 / 频率触发词，**忽略它们**，不要 bake 到 YAML 里。
- YAML 里**禁止**出现 `cron`、`next_run_at`、`schedule`、`interval`（`poll_interval`
  除外，它是 poll stage 内部的等待节奏，不是 workflow 触发频率）等 schedule 语义字段。
- 数据范围类 param **允许保留**（例如 `days_back: int = 7` 表示"每次跑时取最近 7 天"），
  但**不要**把它跟触发频率绑定 —— 一个 workflow 可以每天跑一次也可以按需 launch，
  用户在 Schedule 层决定。

**判断信号**：如果你在犹豫某个词该不该进 YAML —— 问自己"这句话是描述**每次执行**要
干嘛，还是描述**执行的频率**？"。后者一律丢弃。

### 1.1 找 stages 边界

**规则**：动词切一刀。

用户句子里每个**独立动作**都是一个 stage 候选。
如 "**拉**意见 → **评审** → **改**代码 → **commit+push** → **通知**" —— 5 个动词，5 个候选。

**合并规则**（决定要不要合成一个 stage）：
- 两个动作**必须原子完成**（要么全成要么全失败）→ 合并
  例：`commit + push` 一般合并（push 失败要撤 commit 很麻烦）
- 两个动作**产物形式不同**、下游可能只用其中一个 → 拆
  例：`拉意见（产 json）` + `评审（产 md）` 一定拆 —— evaluator 只读 json
- 中间需要**等外部长任务**（>5 分钟）→ 前后必拆，中间插一个 poll stage
  例：`起训练 → 等训完（poll）→ 分析`

**stage 命名**：
- 用**动词短语**（不是名词）：`fetch_feedback` ✓，`feedback` ✗
- 全小写 + 下划线：`^[a-z][a-z0-9_]*$`

### 1.2 决定每 stage 的 kind（claude vs poll）

| 场景 | kind |
|------|------|
| 起一次 claude session 干活（读文件、跑命令、写产物） | `claude` |
| 每 N 秒 poll 一个文件出现 / 某字符串出现 / 命令 exit 0 | `poll` |
| 用户句子里 "**等** X 完成" / "**直到** X 出现" | `poll` |

**注意**：只有"等某个外部条件成立"这种**结构性等待**才用 poll。"每小时/每天跑一次"
这种**频率语义**不用 poll —— 见 F0，那是 Schedule 层的事。

**注意**：
- claude stage 结尾 session 结束就往下走，不能"半开着"等。
- Poll 本身**不跑任何逻辑**，只做等待。要跑逻辑用 claude stage。
- 如果 "等待" 是几秒到几分钟级（比如网络请求、gh CLI 等待），
  不用 poll —— 让 claude stage 内部自己等。

### 1.3 抽 parameters

用户句子里出现的**可配置量**都提到 `parameters:`：
- "最近 N 天"、"过去 N 天的数据" → `days_back: int, default: 7`（数据范围，非触发频率）
- "飞书通知我" → `notify_target: string, required: true`（用户后面 launch 时填自己的 chat id）
- "阈值 xxx"、"config xxx" → 显式 param

**判断规则**：如果这个量**不同 mission 可能不同**，就当 parameter。
写死到 prompt 里的东西不用当 parameter。

**类型只有 4 种**：`string` / `int` / `float` / `bool`。

### 1.4 设计每 stage 的 outputs

这是最关键的一步 —— 决定了 stage 之间怎么串。

**规则**：
- 每个 claude stage 必须至少 1 个 output（poll stage **不能**有 outputs）
- Outputs 是**相对路径**，用 `{ws}/` 前缀
- 每个 output 都是**具体文件路径**（不是"生成一份报告"这种模糊描述）
- 命名反映内容和阶段：`{ws}/01-fetch/feedback.json`、`{ws}/02-review/reviews.md`
  —— 前面加 `NN-<stage>` 前缀让 workspace 目录一眼看出顺序

**下游 stage 引用上游 outputs**：
```yaml
- name: review_each
  kind: claude
  prompt: |
    读取意见列表 {stages.fetch_feedback.outputs[0]}，逐条评审。
    产物：{ws}/02-review/reviews.md（H2 每条 `## 意见 #N`，写"接受/拒绝"+ 理由）
```

### 1.5 挑 validation primitives

每 claude stage 都**必须**加 validation（虽然 spec 允许省，但 §7 R9 的精神
是"每步都要有可校验产物"）。挑选逻辑：

| 产物是什么 | 用哪个 primitive |
|-----------|------------------|
| JSON 结构化数据 | `file_exists` + `jsonschema` |
| Markdown 报告（多个 section） | `file_exists` + `min_chars` + `required_sections` |
| Markdown 里有关键指标 | 加 `regex_match` scoped by section |
| 纯文本 / patch / diff | `file_exists` + `min_size_bytes` |
| 通知发送成功回执 | `file_exists` + `contains_substring: "ok"` 之类 |

**⚠️ 避开 F1-F9（详见 §6）**：
- `required_sections` 用**逐字 bare heading**（不带括号后缀）—— §6 F4
- `section:` 参数以 `## ` 开头 —— §6 F5
- `regex_match` 用宽松 pattern `metric.*0\.`，禁用 `[:\s]+` —— §6 F6

### 1.6 完整拆解案例：feedback_service 意见箱自动巡检

**用户输入**：
> 每周日晚 22:00，从 feedback_service 意见箱拉最近一周所有意见，
> 你逐条评审给出评审意见，需要改代码的就改完 commit + push，
> 最后飞书通知我。（何时触发、多久一次由 Schedule 层决定，YAML 不写）

**第 1 步 · 找 stages 边界**（1.1）：

| 动词 | stage 候选 | 合并 / 拆分决策 |
|------|-----------|-----------------|
| 拉取 | `fetch_feedback` | 独立 —— 产物 JSON 给下游用 |
| 评审 | `review_each` | 独立 —— 产物 MD 报告 |
| 修改代码 | `apply_changes` | 独立 —— 产物 patch + 变更摘要 |
| commit + push | `commit_push` | 合并（原子）—— 产物 commit hash |
| 飞书通知 | `notify_lark` | 独立 —— 产物 通知回执 |

→ **5 个 stage**。都是 claude（没有外部长任务需要 poll）。

**第 2 步 · 抽 parameters**（1.3）：

- "最近 N 天" → `days_back: int, default: 7`（数据范围参数，与触发频率无关）
- 意见箱源（可能 URL 或 gh repo）→ `feedback_source: string, required: true`
- 飞书通知目标 → `lark_chat_id: string, required: true`
- 分支 → `branch: string, default: "main"`
- Dry run 开关（先看不 push）→ `dry_run: bool, default: false`

**第 3 步 · 设计 outputs**（1.4）：

| Stage | Output 路径 |
|-------|-------------|
| `fetch_feedback` | `{ws}/01-fetch/feedback.json` |
| `review_each` | `{ws}/02-review/reviews.md`, `{ws}/02-review/action_plan.json` |
| `apply_changes` | `{ws}/03-apply/changes.patch`, `{ws}/03-apply/summary.md` |
| `commit_push` | `{ws}/04-commit/commit_info.json` |
| `notify_lark` | `{ws}/05-notify/result.md` |

**第 4 步 · Validation**（1.5）：

- `fetch_feedback` → `file_exists` + `jsonschema: {type: array, minItems: 0}`
- `review_each` reviews.md → `file_exists` + `min_chars: 200` + `required_sections: ["Summary"]`
  action_plan.json → `jsonschema: {type: array, items: {required: [action, file]}}`
- `apply_changes` changes.patch → `file_exists` + `min_size_bytes: {count: 10}`
- `commit_push` commit_info.json → `jsonschema: {required: [hash, pushed]}`
- `notify_lark` result.md → `contains_substring: {text: "ok"}`

**第 5 步 · 选模板**（§4）：全 claude 无 poll → **T1 all-claude linear**。

**第 6 步 · 落 YAML**（skeleton in §4.1, 填字段）→ **完整 YAML 见 Appendix A.2**。

---

## §2 Workflow YAML 骨架

Top-level 字段：

```yaml
name: <workflow_name>          # 必需. ^[a-z][a-z0-9_]*$, 全局唯一
description: <one-liner>       # 必需. UI 上显示
parameters: [...]              # 可选. 用户 launch 时传的输入
workspace: <path template>     # 可选. 默认 ".claude/missions/{mission_id}"
global_timeout: <duration>     # 可选. 默认 604800s (7d)
stages: [...]                  # 必需. 至少 1 个 stage
final_outputs: [...]           # 可选. 声明哪些 outputs 是最终交付物
```

### 2.1 Stage 字段（kind=claude）

```yaml
- name: <stage_name>           # 必需. ^[a-z][a-z0-9_]*$
  kind: claude                 # 必需.
  prompt: |                    # 必需. 会 render 后喂给 claude
    ...
  outputs:                     # 必需. 至少 1 个. 相对路径, 用 {ws}/... 开头
    - "{ws}/..."
  validation:                  # 可选（推荐）. 每 file 一 block.
    - file: "{ws}/..."
      primitives:
        - <primitive>          # 见 §5
  time_budget: <duration>      # 可选. 默认 600s
  depends_on: [...]            # 可选. 只允许指向更早的 stage
```

### 2.2 Stage 字段（kind=poll）

```yaml
- name: <stage_name>
  kind: poll
  poll_interval: <duration>    # 必需. 例 300s
  timeout: <duration>          # 必需. 例 36000s (10h)
  check:                       # 必需. 至少 1 个 entry. 三种形式互斥
    # 形式 A: load-binding —— 从文件读字段绑到 placeholder
    - file: "{stages.X.outputs[0]}"
      load_as: json            # json | text
      extract_field: exp_path
      as: exp_path             # 后续可用 {params.exp_path}
    # 形式 B: primitive validation —— 文件通过若干 primitive
    - file: "{params.exp_path}/train_log/log.txt"
      primitives:
        - file_exists
        - regex_match: {pattern: 'process 0 finish jobs'}
    # 形式 C: shell-exec —— exit code 0 就算通过
    - command: ["squeue", "-j", "12345", "-h"]
```

**Poll stage 不能有**：`prompt`, `outputs`, `validation`。

### 2.3 Duration 语法

`900s` / `30m` / `24h` / `7d` / 裸整数（当秒）。

### 2.4 Workspace

`{ws}` 默认解析为 `.claude/missions/<mission_id>/`。CSM 在 mission 启动时
**自动创建**这个目录，且**每个 stage 的 claude session 的 CWD 就是这里**。
**别在 YAML 里写绝对路径**代替 `{ws}`。

---

## §3 Placeholder 词汇表（封闭）

**只允许**这 5 类 token。任何其他 `{...}` 会让 CSM render 失败。

| Token | 解析为 | 举例 |
|-------|--------|------|
| `{ws}` | 该 mission workspace 绝对路径 | `/data/.../missions/abc123/` |
| `{mission_id}` | mission UUID | `abc12345-...` |
| `{workflow_name}` | workflow.name | `feedback_service_feedback_ops` |
| `{params.<name>}` | 用户 launch 传的参数 | `{params.days_back}` → `7` |
| `{stages.<stage>.outputs[<n>]}` | 前面 stage 的第 n 个 output（0-indexed） | `{stages.fetch_feedback.outputs[0]}` |

**只能引用**已经声明过的更早 stage 的 outputs（stages 数组线性）。

**转义**：输出字面量 `{...}` 用 `{{...}}`。例如给 claude 展示 JSON 模板时：
```
产物：
{{
  "hash": "...",
  "pushed": true
}}
```

---

## §4 三个模板 skeleton

拆解完（§1）就挑一个模板，把 skeleton 里的占位换成你的具体内容。

| 模板 | 用什么时候 | 结构 |
|------|-----------|------|
| **T1** All-claude linear | 每步都是 claude，无外部长任务 | claude → claude → ... |
| **T2** Claude + Poll + Claude | 中间等外部长任务（训练/eval/远端调度）> 5 min | claude → poll → claude |
| **T3** Multi-member fan-out | N 个 seed / 变体并行 + 聚合 | claude(launch N) → poll(master) → claude(读多 member) |

**T2 T3 区别**：T2 只等一个外部任务；T3 一次起 N 个，poll 只看 master，
aggregate 读所有 member。

### 4.1 T1 · All-claude linear skeleton

```yaml
name: <name>
description: <one liner>

parameters:
  - {name: <param>, type: string, required: true, description: "..."}

global_timeout: 3600s

stages:
  - name: <stage1>
    kind: claude
    prompt: |
      <指令 —— 明确产物路径 + 每个 H2 逐字标题>
    outputs:
      - "{ws}/01-<stage1>/<file>"
    validation:
      - file: "{ws}/01-<stage1>/<file>"
        primitives:
          - file_exists
          - <其他 primitives>

  - name: <stage2>
    kind: claude
    prompt: |
      基于 {stages.<stage1>.outputs[0]} 做 X。
      产物：{ws}/02-<stage2>/<file>
    outputs:
      - "{ws}/02-<stage2>/<file>"
    validation:
      - file: "{ws}/02-<stage2>/<file>"
        primitives: [...]

final_outputs:
  - "{ws}/02-<stage2>/<file>"
```

### 4.2 T2 · Claude + Poll + Claude skeleton

```yaml
name: <name>
description: <one liner>

parameters:
  - {name: exp_name, type: string, required: true}

global_timeout: 43200s   # 12h 覆盖 launch + train + analyze

stages:
  - name: setup_experiment
    kind: claude
    prompt: |
      起 tlaunch 训练任务。
      产物：{ws}/02-launch/meta.json —— JSON 含 exp_path / tmux_session / tmux_socket / cluster_job_id
      tmux_session 和 tmux_socket 都必须用 CSM guard 提供的以 `^csm-` 开头的值（不能用短别名）。
    outputs:
      - "{ws}/02-launch/meta.json"
    validation:
      - file: "{ws}/02-launch/meta.json"
        primitives:
          - file_exists
          - jsonschema:
              type: object
              required: [exp_path, tmux_session, tmux_socket]
              properties:
                tmux_session: {type: string, pattern: '^csm-'}
                tmux_socket:  {type: string, pattern: '^csm-'}

  - name: wait_training
    kind: poll
    poll_interval: 300s
    timeout: 36000s
    check:
      - file: "{stages.setup_experiment.outputs[0]}"
        load_as: json
        extract_field: exp_path
        as: exp_path
      # 只 poll 训练**真实产生**的完成 marker（F1）
      - file: "{params.exp_path}/train_log/log.txt"
        primitives:
          - file_exists
          - regex_match: {pattern: 'process 0 finish jobs'}

  - name: analyze
    kind: claude
    prompt: |
      读训练结果 {params.exp_path}/train_log/report.json 生成分析报告。
      产物：{ws}/03-analyze/report.md —— H2 逐字标题:
        `## Result`   （metric 格式：`wauc: 0.NNNN`）
        `## Verdict`  （只写 PROMOTE 或 REJECT）
        `## Risks`    （至少 80 字）
    outputs:
      - "{ws}/03-analyze/report.md"
    validation:
      - file: "{ws}/03-analyze/report.md"
        primitives:
          - file_exists
          - required_sections: ["Result", "Verdict", "Risks"]
          - regex_match: {section: "## Result",  pattern: 'wauc.*0\.[0-9]{4}'}
          - regex_match: {section: "## Verdict", pattern: '\b(PROMOTE|REJECT)\b'}
          - min_chars:   {section: "## Risks",   count: 80}

final_outputs:
  - "{ws}/02-launch/meta.json"
  - "{ws}/03-analyze/report.md"
```

### 4.3 T3 · Multi-member fan-out skeleton

结构：`launch_group` 起 N 个 → `wait_all_train` 只 poll master → `aggregate` 读全部。

```yaml
stages:
  - name: launch_group
    kind: claude
    prompt: |
      起 N-seed 训练组：master = s1，rider = s2/s3（共享 tmux socket，
      独立 exp_path）。每 member 训完写 log.txt 里的 `process 0 finish jobs`。
      产物：{ws}/02-launch/meta.json —— schema:
      {{
        "exp_path": "<MASTER exp 绝对路径>",
        "members": [{{"slug":"...","seed":1,"role":"master","exp_path":"..."}}, ...],
        "tmux_session": "csm-...",
        "tmux_socket": "csm-..."
      }}
    outputs:
      - "{ws}/02-launch/meta.json"
    validation:
      - file: "{ws}/02-launch/meta.json"
        primitives:
          - file_exists
          - jsonschema:
              type: object
              required: [exp_path, members, tmux_session, tmux_socket]
              properties:
                members: {type: array, minItems: 1}
                tmux_session: {type: string, pattern: '^csm-'}
                tmux_socket:  {type: string, pattern: '^csm-'}

  - name: wait_all_train
    kind: poll
    poll_interval: 300s
    timeout: 43200s
    check:
      - file: "{stages.launch_group.outputs[0]}"
        load_as: json
        extract_field: exp_path
        as: exp_path
      - file: "{params.exp_path}/train_log/log.txt"     # master only
        primitives:
          - file_exists
          - regex_match: {pattern: 'process 0 finish jobs'}

  - name: aggregate_analyze
    kind: claude
    prompt: |
      读 {stages.launch_group.outputs[0]} 里所有 member 的
      `<member_exp_path>/train_log/report.json`，做跨 seed 聚合。
      产物：{ws}/03-aggregate/report.md —— H2:
        `## Per-seed metrics`
        `## Mean & variance`
        `## Verdict`
    outputs:
      - "{ws}/03-aggregate/report.md"
    validation:
      - file: "{ws}/03-aggregate/report.md"
        primitives:
          - file_exists
          - required_sections: ["Per-seed metrics", "Mean & variance", "Verdict"]
```

---

## §5 Validation Primitives 全表

**7 个 primitive**，claude stage 的 `validation` 和 poll stage 的 `check.primitives`
共用同一词汇表。

| Primitive | 干什么 | 参数 | YAML 举例 |
|-----------|--------|------|-----------|
| `file_exists` | 文件存在且大小 > 0 | 无 | `- file_exists` |
| `min_chars` | 字符数 ≥ count | `count`, `section?` | `- min_chars: 200` |
| `min_size_bytes` | 字节数 ≥ count | `count`, `section?` | `- min_size_bytes: {count: 4096}` |
| `required_sections` | 所有列出的 markdown 标题都在 | `sections: [str]` | `- required_sections: ["Overview", "Body"]` |
| `regex_match` | `re.search(pattern, content)` 匹配 | `pattern`, `section?` | `- regex_match: {pattern: 'wauc.*0\.'}` |
| `jsonschema` | 文件是有效 JSON 且过 schema | `<inline schema>` | `- jsonschema: {type: object, required: [x]}` |
| `contains_substring` | 子串出现 | `text`, `section?` | `- contains_substring: {text: 'ok'}` |

### 5.1 Section scoping

`min_chars` / `min_size_bytes` / `regex_match` / `contains_substring`
接可选 `section: "## Xxx"` —— 只针对该 H2 章节的内容跑检查。

**⚠️ section 参数必须带 `##`**（不是裸标题）—— **R13** 会 fail 裸标题。

### 5.2 Required_sections 匹配

whole-line regex `^#+\s+<name>\s*$`。含义：
- `## Overview` ✓
- `## Overview (draft)` ✗（后缀让整行不匹配）
- `##Overview` ✗（缺空格）

Prompt 里**必须**要求 claude 输出**逐字 bare heading**，跟 sections 列表字面完全一致。

### 5.3 Poll check 三种形式

见 §2.2。三种互斥。

---

## §6 硬规则 F1-F9（生成时必须避开）

每条都是真实事故沉淀。生成 YAML 时**逐条对照检查**。

### F1. Poll check 不允许引用后续 stage 的产物

**症状**：poll 永远 fail，`file does not exist`；最终 mission timeout。
**根因**：死锁 —— poll 等的文件是下 stage 才写的，poll 不通过下 stage 不启动。
**规则**：
- 只 poll 训练/eval **本身**产生的完成 marker（如 `log.txt` 里 `process 0 finish jobs`）。
- **禁止** poll 后续 stage 的 outputs 路径。

### F2. Poll 路径必须匹配 skill 实际写的位置

**症状**：`file_exists` 永远过不了，虽然文件手工能看到。
**根因**：底层 skill 把文件写在你 YAML 没料到的子路径（如 `train_log/report.json` 而非 `report.json`）。
**规则**：如果不确定 skill 真实产在哪，**先只 `file_exists` 一个大致路径**跑一次 dry run，
看真实产物位置再收紧。

### F3. Poll 的 jsonschema 必须匹配**真实**报告 shape

**症状**：`file_exists` 过了，但 jsonschema fail，`required: [wauc]` missing —— 而报告是
`{"config": {...}, "report": {url: {...}}}`。
**根因**：author 按"理想 shape"写 schema，实际输出 shape 是嵌套的。
**规则**：poll 的 check block **不加 jsonschema**（**R15 会 warn**）。
只 `file_exists`，等 dry run 看到真实 shape 再决定加不加。

### F4. `required_sections` 是**逐行 whole-line regex**

**症状**：产物有 `## Won't-try (do not repropose)`，但 `required_sections: ["Won't-try"]` 报 missing。
**根因**：whole-line 匹配 `^#+\s+Won't-try\s*$`，后缀 `(do not repropose)` 让整行不匹配。
**规则**：
- Prompt 里**必须**写"H2 标题逐字精确、不允许后缀 / 括号 / 副标题"
- `required_sections` 列表跟 prompt 里字面**完全一致**（不带标点也不带 `##`）

### F5. `section:` 参数必须带 `##` 前缀

**症状**：Stage fail `section not found: "Expected wauc"`，虽然文件有 `## Expected wauc`。
**根因**：section slicer 用 `^(#+)\s+(.+?)\s*$` 匹配 arg。裸标题匹配不上。
**规则**：`section: "## Result"`（**不是** `section: "Result"`）。
**R13** 会 fail 这个。

### F6. `regex_match` 对模型自然语言产物要宽松

**症状**：产物有 `wauc = 0.99`，但 pattern `wauc[:\s]+0\.[0-9]{4}` 报 not found（`=` 不在 `[:\s]` 里）。
**根因**：模型写英中随意（`wauc = 0.99` / `wauc | 0.99`），严格分隔符会漏。
**规则**：
- 用 `wauc.*0\.[0-9]{4}`（`.*` 兜住任意分隔）
- 用 `section: "## Result"` scoping 兜住不误配其他数字
- **R17** 会 warn `[:\s]+` 严格分隔符

### F7. tmux 命名必须在 jsonschema 里强制 `^csm-`

**症状**：mission 后半程 guard 挂，`tmux_session` 是 `sim` / `demo` 短别名。
**根因**：Claude 自作聪明用短别名，下游 guard 认 `^csm-` 长字符串。
**规则**：如果 stage 的 prompt 提到 tmux，outputs 的 jsonschema **必须**给
`tmux_session` / `tmux_socket` 加 `pattern: '^csm-'`（**R18** 会 warn 缺）。
外部 tmux（如 eval-batch）不算 —— 只有 CSM 拉起的 tmux 需要。

### F8. Claude stage 必须声明非空 `outputs`

**症状**：Reload 拒绝，R9 fail。
**根因**：outputs 是契约声明 —— 没 outputs 就没有可校验产物，
下游 stage 也没法 `{stages.X.outputs[N]}` 引用。
**规则**：每个 kind=claude 的 stage 至少 1 个 output。Poll stage **不能**有 outputs。

### F9. Placeholder 词汇表封闭

**症状**：mission 一起就 fail，`unknown placeholder {cluster}`。R10 也会拦。
**根因**：CSM 用严格 render —— 不认识的 token 不会静默漏进 prompt。
**规则**：只用 §3 表里的合法 token。字面量 `{...}` 用 `{{...}}` 转义。

---

## §7 自检 R9-R19（返回给用户前必过）

写完 YAML 后，agent **必须**自检以下 11 条 rule，全部到 `pass` 才交付。

**Warn 也算 fail** —— 用户明确要求 warn 清零，因为每条都是真实事故沉淀。

### 7.1 P0 结构（fail）

| Rule | 检查 | 修法 |
|------|------|------|
| R9  | 每 kind=claude stage 有非空 outputs | 加 output 路径 |
| R10 | outputs 里 placeholder 合法 + 相对路径 + 无 `..` | 用 §3 合法 token；改相对；去 `..` |
| R11 | validation 用 §5 已知 primitive | 拼写：`file_exists` 而非 `exists` |
| R12 | poll stage 有非空 `check` | 加 check |
| R13 | primitive 的 `section:` arg 带 `#` 前缀 | 改成 `"## X"` |

### 7.2 P1 契约（warn，但同样清零）

| Rule | 检查 | 修法 |
|------|------|------|
| R14 | poll check file **不**引用后续 stage outputs | 换成训练本身 marker（F1） |
| R15 | poll check 里**不用** jsonschema | 只 `file_exists` 直到 dry run 见真 shape（F3） |
| R16 | `required_sections` 里**不含** `()[]{}\|` 标点 | 改 bare heading，prompt 要求 claude 也 bare（F4） |
| R17 | `regex_match` pattern 不用严格分隔符 `[:\s]+` | 用 `.*`（F6） |
| R18 | prompt 提 tmux 时 outputs 有 `^csm-` schema | 加 pattern，或换成外部 tmux 措辞（F7） |
| R19 | poll 的 `timeout / poll_interval <= 200` | 加大 interval 或减小 timeout |

### 7.3 自检具体做法

**心里过一遍**（生成时逐条对照）：
1. 每 claude stage outputs 有内容？（R9）
2. 所有 `{...}` token 都是 `{ws}` / `{mission_id}` / `{workflow_name}` / `{params.X}` / `{stages.X.outputs[N]}` 或 `{{...}}`？（R10）
3. 所有 primitive 名字都在 §5 的 7 个里？（R11）
4. 每 poll stage 有 check block？（R12）
5. 每个 `section:` 参数以 `## ` 开头？（R13）
6. 每个 poll `check.file` 引用的 stage 是**当前 poll 之前**的？（R14）
7. 有 poll stage 且 check 里用了 jsonschema？→ 删掉，只留 `file_exists` +/- `regex_match`（R15）
8. `required_sections` 每个 entry 都是 `bare heading text`，无 `()`?（R16）
9. `regex_match` pattern 全用 `.*`，无 `[:\s]+`?（R17）
10. Prompt 提 tmux → outputs schema 有 `^csm-` pattern?（R18）
11. Poll 的 `timeout / poll_interval < 200`?（R19）

**如果任何一条不过 —— 改，再自检，直到全过再交付**。

---

## §8 输出格式（写入路径 + 交付确认）

### 8.1 写入路径（强制）

```
<CSM_REPO>/tasks/<workflow_name>.workflow.yaml
```

- `<workflow_name>` = YAML 顶层 `name:` 字段的值，符合 `^[a-z][a-z0-9_]*$`。
- **不允许**写到你当前 cwd 里的 tasks 目录（CSM 只读自己的 tasks）。
- 用绝对路径写 —— 不要依赖 cwd。

### 8.2 交付确认（agent 必输出）

写完后**必须**输出一行（用户会 grep）：

```
wrote <CSM_REPO>/tasks/<name>.workflow.yaml
```

**格式硬要求**：
- 必须以 `wrote ` 开头（后端会文本匹配）
- 后面是完整绝对路径（用户会复制到 UI）
- 单独一行，前后可以有别的解释文字

### 8.3 交付前的最后动作（推荐）

如果时间允许，agent 可以再做：
- `python -c "from csm.modules.workflow.schema import load_workflow_spec; load_workflow_spec(open('<CSM_REPO>/tasks/<name>.workflow.yaml').read())"` —— 确认 schema 通过
- `grep -oE '\{[^}]+\}' <yaml_path>` —— 列出所有 placeholder，眼过一遍是否都合法

不做也行，反正用户点 Reload 时会跑 R9-R19 拦下。

---

## Appendix A: Hello World 完整例子

### A.1 最小 workflow（不依赖 automation repo）

```yaml
name: hello_world
description: 最小 workflow —— 起草 → 扩写，两步纯 claude。

parameters:
  - name: topic
    type: string
    required: true
    description: 想让 claude 写的主题（自由文本）。

global_timeout: 1800s

stages:
  - name: draft
    kind: claude
    prompt: |
      请针对主题 "{params.topic}" 写一份 markdown 提纲。

      产物：
      - {ws}/01-draft/outline.md —— 必须包含以下 H2 标题（**逐字精确匹配**，
        不带任何后缀 / 括号 / 副标题）：
        `## Overview`
        `## Key points`
        `## Open questions`
    outputs:
      - "{ws}/01-draft/outline.md"
    validation:
      - file: "{ws}/01-draft/outline.md"
        primitives:
          - file_exists
          - min_chars: 200
          - required_sections: ["Overview", "Key points", "Open questions"]
    time_budget: 600s

  - name: expand
    kind: claude
    prompt: |
      基于提纲 {stages.draft.outputs[0]} 扩写成正文。

      产物：
      - {ws}/02-final/article.md —— 必须包含以下 H2 标题（**逐字精确**）：
        `## Introduction`
        `## Body`
        `## Conclusion`
    outputs:
      - "{ws}/02-final/article.md"
    validation:
      - file: "{ws}/02-final/article.md"
        primitives:
          - file_exists
          - min_chars: 800
          - required_sections: ["Introduction", "Body", "Conclusion"]
    time_budget: 600s

final_outputs:
  - "{ws}/02-final/article.md"
```

### A.2 §1 拆解案例对应的 YAML（feedback_service 意见箱）

**这是 CSM 里的 flagship workflow**，请把这个当作"good workflow 长什么样"的参考。
以下是**要点摘要**：

**它展示了什么好实践**：

1. **顶部 `# clarifications:` 注释块** —— 保留 clarify agent 问过的 5 个边界问题 + 用户答案。
   这是后来者追溯"当时为什么这样设计"的**唯一线索**，每次生成都要写。
2. **F0 合规** —— 整个 YAML 没有 `每周` `22:00` 这类时间词；触发频率由 Schedule 决定。
3. **参数化数据范围** —— `days_back` = 每次跑取最近 N 天，独立于触发频率。
4. **完整分支覆盖** —— `fetch` 空态 / `review` 全 DEFER / `push` 失败 三条分支都有对应处理。
5. **实际 primitive 组合** —— JSON 用 `file_exists + jsonschema`；MD 用 `file_exists +
   required_sections + regex_match(section:...)`；不误用 `min_chars`（clarification 里
   已经确认空态可能占位）。

**顶部片段**（生成时按这个样式）：

```yaml
# clarifications:
#   1. Q: 最近 7 天 OSS 拉回 0 条意见时，怎么办？
#      A: 跳过 review/apply/push，只发一条 '本周 0 条' 给owner。
#   2. Q: review 结果全是 DEFER 时，怎么办？
#      A: 跳过 apply/push，仍然通知owner + 通知每位提意见人 '已排期'。
#   3. Q: '通知提意见的人' 里，提交人身份从哪拿？
#      A: JSON 里只有姓名/邮箱，用你的通讯录服务解析成聊天 id 再发消息。
#   4. Q: 发给提意见人的飞书消息内容粒度？
#      A: 只说结论：已修复 / 已排期人工评估，附非技术性理由；不带 commit 号。
#   5. Q: git push 失败时怎么处理？
#      A: 保留本地 commit，不 rollback；通知owner手动 push。
#
# CSM 无 stage-level 条件跳过，所有分支逻辑通过下游 stage 读上游 outputs 里的
# `status` 字段自行 no-op（empty | all_defer | has_changes | push_failed）来落地。

name: feedback_service_ops
description: 从意见箱拉最近 N 天用户意见，subagent review，改代码 commit+push，飞书通知owner及提意见人。

parameters:
  - {name: days_back, type: int, default: 7, description: "从 S3 意见箱拉最近多少天。"}
  - {name: owner_chat_id, type: string, default: "oc_placeholder_lark_chat_id"}
  - {name: source_prefix, type: string, default: "./feedback-inbox/"}
  - {name: dry_run, type: bool, default: false, description: "true = 生成变更但不 commit/push/notify"}

global_timeout: 7200s
```

**stage `fetch_feedback` 的 prompt + validation**（真实产品级样式）：

```yaml
  - name: fetch_feedback
    kind: claude
    prompt: |
      从 `{params.source_prefix}` 列出最近 {params.days_back} 天的 JSON 意见文件（本地目录用 `ls`，对象存储用你手边的 CLI/SDK），
      逐个下载解析，聚合成一个数组。若同一 id 出现多次以 created_at 最新为准。

      对照 `<STATE_DIR>/feedback_service_workflow/state.json`
      过滤掉已处理过的意见 id（historical dedup）。

      产物：
      - {ws}/01-fetch/feedback.json —— JSON array. 每项 shape:
        {{
          "id": "<string>", "author_name": "<string>", "author_email": "<string>",
          "content": "<string>", "created_at": "<ISO8601>",
          "category": "bug|feature|question|other", "s3_key": "<原始 S3 key>"
        }}
        若最近 {params.days_back} 天无新意见，输出 `[]`（空数组）。
    outputs:
      - "{ws}/01-fetch/feedback.json"
    validation:
      - file: "{ws}/01-fetch/feedback.json"
        primitives:
          - file_exists
          - jsonschema:
              type: array
              minItems: 0
              items:
                type: object
                required: [id, content, category]
                properties:
                  category: {type: string, enum: [bug, feature, question, other]}
```

**完整的 5-stage 骨架** 还包含 `review_each`、
`apply_changes`、`commit_push`、`notify_lark` 四个后续 stage。值得看的是
特别是 review/notify 阶段是**怎么读上游 outputs 里的 `status` 字段自行 no-op** 来实现
"空态 / all_defer" 分支的（因为 CSM 没有 stage-level 条件跳过，必须靠数据流实现）。

---

### A.3 §1 拆解案例对应的 YAML（教学版）

以下是 §1.6 拆解案例的**教学版**（不含真实 S3 路径、不含生产参数），主要用来配合
§1 方法论说明"从一句话到 YAML 的每步映射"。真跑请看 A.2。

```yaml
name: feedback_service_feedback_ops
description: 每周从意见箱拉意见，逐条评审 + 落码 + 通知。

parameters:
  - {name: feedback_source, type: string, required: true,
     description: "意见箱源 URL / gh repo"}
  - {name: lark_chat_id, type: string, required: true,
     description: "飞书通知目标 chat id"}
  - {name: days_back, type: int, default: 7,
     description: "取最近 N 天的意见"}
  - {name: branch, type: string, default: "main",
     description: "改代码 push 的目标分支"}
  - {name: dry_run, type: bool, default: false,
     description: "true = 生成变更但不 push"}

global_timeout: 7200s   # 2h

stages:
  - name: fetch_feedback
    kind: claude
    prompt: |
      从 {params.feedback_source} 拉取最近 {params.days_back} 天的意见。

      产物：
      - {ws}/01-fetch/feedback.json —— JSON array. 每项 shape:
        {{
          "id": "<string>",
          "author": "<string>",
          "content": "<string>",
          "created_at": "<ISO8601>",
          "category": "bug|feature|question|other"
        }}
    outputs:
      - "{ws}/01-fetch/feedback.json"
    validation:
      - file: "{ws}/01-fetch/feedback.json"
        primitives:
          - file_exists
          - jsonschema:
              type: array
              minItems: 0
              items:
                type: object
                required: [id, content, category]
                properties:
                  category: {type: string, enum: [bug, feature, question, other]}

  - name: review_each
    kind: claude
    prompt: |
      逐条评审 {stages.fetch_feedback.outputs[0]} 里的每个意见。

      产物：
      - {ws}/02-review/reviews.md —— 每条意见一段。H2 逐字标题（不加后缀）：
        `## Summary`     （全局概览：本周共几条、几条要改代码）
        `## Reviews`     （每条评审细节：接受 / 拒绝 + 理由）
      - {ws}/02-review/action_plan.json —— JSON array，只包含需要改代码的意见.
        每项:
        {{
          "id": "<从 feedback.json 里的 id>",
          "action": "modify|create|delete",
          "file": "<相对当前 repo 路径>",
          "rationale": "<一段话>"
        }}
    outputs:
      - "{ws}/02-review/reviews.md"
      - "{ws}/02-review/action_plan.json"
    validation:
      - file: "{ws}/02-review/reviews.md"
        primitives:
          - file_exists
          - min_chars: 200
          - required_sections: ["Summary", "Reviews"]
      - file: "{ws}/02-review/action_plan.json"
        primitives:
          - file_exists
          - jsonschema:
              type: array
              items:
                type: object
                required: [id, action, file]

  - name: apply_changes
    kind: claude
    prompt: |
      按 action plan {stages.review_each.outputs[1]} 修改代码。

      产物：
      - {ws}/03-apply/changes.patch —— `git diff` 输出的完整 patch。
      - {ws}/03-apply/summary.md —— H2 逐字标题:
        `## Changed files`   （文件列表）
        `## Rationale`       （改动理由汇总）
    outputs:
      - "{ws}/03-apply/changes.patch"
      - "{ws}/03-apply/summary.md"
    validation:
      - file: "{ws}/03-apply/changes.patch"
        primitives:
          - file_exists
          - min_size_bytes: {count: 10}
      - file: "{ws}/03-apply/summary.md"
        primitives:
          - file_exists
          - required_sections: ["Changed files", "Rationale"]

  - name: commit_push
    kind: claude
    prompt: |
      commit 上一步的 {stages.apply_changes.outputs[0]} 并推到分支
      {params.branch}。如果 {params.dry_run} 为 true，只 commit 不 push。

      产物：
      - {ws}/04-commit/commit_info.json —— JSON:
        {{
          "hash": "<git commit hash>",
          "branch": "{params.branch}",
          "pushed": true|false,
          "dry_run": <bool>
        }}
    outputs:
      - "{ws}/04-commit/commit_info.json"
    validation:
      - file: "{ws}/04-commit/commit_info.json"
        primitives:
          - file_exists
          - jsonschema:
              type: object
              required: [hash, branch, pushed]
              properties:
                hash: {type: string, minLength: 7}

  - name: notify_lark
    kind: claude
    prompt: |
      向飞书 chat {params.lark_chat_id} 发送本周意见巡检报告。
      模板：
        - 本周共 N 条意见（M 条需改代码）
        - Commit: {stages.commit_push.outputs[0]} 里的 hash
        - 详细：附 {stages.review_each.outputs[0]} 摘要

      产物：
      - {ws}/05-notify/result.md —— 首行 `## Result`，
        正文含字符串 `ok` 表示发送成功。
    outputs:
      - "{ws}/05-notify/result.md"
    validation:
      - file: "{ws}/05-notify/result.md"
        primitives:
          - file_exists
          - required_sections: ["Result"]
          - contains_substring: {text: "ok"}

final_outputs:
  - "{ws}/02-review/reviews.md"
  - "{ws}/04-commit/commit_info.json"
  - "{ws}/05-notify/result.md"
```

---

## Appendix B: 用户视角（调试 / lifecycle / UI）

**Agent 一般不需要读这一节**（除非用户问"我的 mission 挂了/卡住怎么办？"）。

### B.1 Mission 生命周期

```
POST /api/missions/launch → RUNNING → 逐 stage 推进 →
  claude stage: AutomationRunner spawn AUTO session → 跑 prompt → validation → 下一 stage
  poll stage: PollExecutor 每 poll_interval 跑一次 check → 全过 → 下一 stage
  → SUCCEEDED (terminal) / FAILED (retry-able) / CANCELLED (dead)
```

### B.2 Mission 失败时

用户会做的动作：

```bash
# 看失败原因
curl http://127.0.0.1:8000/api/missions/<mid> | jq '.status, .current_stage, .failure_reason'

# 看 workspace 里 stage 实际产出什么
ls -la .claude/missions/<mid>/

# rerun 一个 stage（重起 claude 跑）
curl -X POST 'http://127.0.0.1:8000/api/missions/<mid>/retry?stage=<stage_name>&mode=rerun'

# revalidate（不重跑，只在现有文件上再跑 validation。用户手工改了产物文件时用）
curl -X POST 'http://127.0.0.1:8000/api/missions/<mid>/retry?stage=<stage_name>&mode=revalidate'
```

### B.3 UI 上的位置

- `Automation → Missions` —— 一行一 mission
- `Automation → Runs` —— 一行一 Run（每 claude stage 一个 Run，poll 无 Run）
- `Automation → 🧾 Workflows drawer` —— 一行一 workflow definition + review 结果
- `Sessions 页` —— 正在跑的 claude stage 会显示为 AUTO session（stage 结束自动清理）

---

## Appendix C: 快速参考卡

**写入路径**：`<CSM_REPO>/tasks/<name>.workflow.yaml`（强制）

**交付确认**：`wrote <绝对路径>` 单独一行（后端会 grep）

**Duration**：`900s` / `30m` / `24h` / `7d` / 裸整数（秒）

**Parameter type**：`string` / `int` / `float` / `bool`

**Placeholder**：`{ws}` / `{mission_id}` / `{workflow_name}` /
`{params.X}` / `{stages.X.outputs[N]}`；转义 `{{...}}`

**Primitive**：`file_exists` / `min_chars` / `min_size_bytes` /
`required_sections` / `regex_match` / `jsonschema` / `contains_substring`

**Poll 三种形式**：load-binding / primitives / shell-exec —— 互斥

**Stages 数组线性**：只能引用**前面**已声明的 stage outputs

**Section arg** 必须带 `##`（`"## Result"`）

**required_sections** 逐字 bare（不带括号 / 后缀），prompt 里也这样要求 claude

**regex_match** 用 `.*` 松匹配，禁用 `[:\s]+`

**tmux** 涉及 CSM 拉起的必须 schema `pattern: '^csm-'`

**自检 R9-R19** 到 0 fail + 0 warn 才交付

---

## Change log

- **2026-07-06 (v3.1)** —— 后端 P0-P4 整合完成：M4 TaskDefinition 概念全撤销，
  automation 只剩 **workflow + mission + stage_execution** 三概念。
  UI 上有全新的 **+ New workflow** 表单：用户填 repo 路径 + 一句话需求，
  后端 spawn `claude -p` 自动生成 YAML + 跑 R9-R19 review 并返回结果卡片。
  Schedules 只绑 workflow_def_id；`task_def_id` / `run` 表已从 schema 消失。
- **2026-07-05 (v3)** —— 目标读者从"初次使用的用户"改为**"生成 YAML 的 agent"**。
  重构目录：从"教用户理解"改为"给 agent 一个可执行的生成流程"。
  新增 §1 "如何拆解用户一句话需求"（含 feedback_service 意见箱案例）。
  用户视角内容（原 §3 lifecycle / §6 debug）移到 Appendix B。
  Appendix A 加 feedback_service 完整 5-stage YAML 作对照例。
- **2026-07-05 (v2.2)** —— §0.1 加"两地分工"心智模型；§2.3 区分 authoring-time
  vs runtime CWD；§4 三模板加"什么时候升级"列 + T2/T3 差异说明；
  §7 F8/F9 三段式对齐；§8/§9 warn 政策统一"必须清零"；§9 checklist 拆
  "写之前"vs"写之后"。
- **2026-07-05 (v2.1)** —— 强制 YAML 绝对路径；warn 清零；§10 声明历史清空。
- **2026-07-05 (v2)** —— 初次用户视角重排；补 §0 动机、§1 Hello World、§3 lifecycle、§6 debug；F1-F9 三段式；配 S3 权威副本发布。
- **2026-07-05 (v1)** —— 初版：从首轮真实运行的复盘记录整理。
