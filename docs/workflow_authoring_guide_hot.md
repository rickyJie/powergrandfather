# CSM Workflow Authoring Guide — HOT VERSION

> **本文是精简版**（~10-15KB）：只留每次生成都要看的关键规则 + 一个真实例子。
> 完整方法论（§1）/ primitive 全表（§5）/ Appendix B & C 请用 `Read` 工具读
> `<CSM_REPO>/docs/workflow_authoring_guide.md`。

---

## §0 你的任务

**输入**：
- 用户在 CSM UI 上给一句话需求（比如"从 feedback_service 意见箱拉意见，逐条评审..."）
- 你当前的 cwd = 用户想自动化的目标 repo

**输出**：
- 一份 workflow YAML 写到强制路径：
  `<CSM_REPO>/tasks/<workflow_name>.workflow.yaml`
- 最后一行输出：`wrote <CSM_REPO>/tasks/<name>.workflow.yaml`

---

## F0（先于所有规则）：workflow 只写 what / how，**不写 when / how-often**

workflow 是**单次实验定义**。什么时候跑、多久一次是 **Schedule 层**的事，跟 YAML 无关。

- 用户 requirement 里出现 "每天 / 每周 / 每周日晚 22:00 / 每小时 / 每 N 次" 等
  **时间 / 频率触发词**：**忽略它们**，不要 bake 到 YAML 里。
- YAML **禁止**出现 `cron` / `schedule` / `interval`（`poll_interval` 除外，是 poll
  内部节奏）/ `next_run_at` 等 schedule 语义字段。
- 数据范围类 param（如 `days_back` = 每次跑取最近 N 天）**允许**，但独立于触发频率。

**判断信号**：如果犹豫某词该不该进 YAML —— 问自己"这是描述**每次执行**要干嘛，还是
**多久执行一次**？"，后者一律丢弃。

---

## §3 Placeholder 词汇表（封闭）

**只允许**这 5 类 token。其他 `{...}` 会让 CSM render 失败（R10 会拦）。

| Token | 解析为 | 例 |
|-------|--------|------|
| `{ws}` | 该 mission 的 workspace 绝对路径 | `/data/.../missions/abc/` |
| `{mission_id}` | mission UUID | `abc12345-...` |
| `{workflow_name}` | workflow.name | `ci_failure_triage` |
| `{params.<name>}` | 用户 launch 时传的参数 | `{params.days_back}` → `7` |
| `{stages.<stage>.outputs[<n>]}` | 前一 stage 第 n 个 output | `{stages.fetch.outputs[0]}` |

**只能引用已声明过的更早 stage** 的 outputs（stages 数组线性）。

**转义**：输出字面量 `{...}` 用 `{{...}}`。例如给 claude 展示 JSON 模板：
```
产物：
{{
  "id": "...",
  "pushed": true
}}
```

---

## §4 三个模板 skeleton

拆解完就挑一个模板，把 skeleton 里的占位换成具体内容。

| 模板 | 用什么时候 | 结构 |
|------|-----------|------|
| **T1** All-claude linear | 每步都是 claude，无外部长任务 | claude → claude → ... |
| **T2** Claude + Poll + Claude | 中间等外部长任务（训练/eval）> 5 min | claude → poll → claude |
| **T3** Multi-member fan-out | N 个变体并行 + 聚合 | claude(launch N) → poll(master) → claude(读多 member) |

**T2 vs T3**：T2 等一个外部任务；T3 一次起 N 个，poll 只看 master，aggregate 读所有。

### 4.1 T1 · All-claude linear

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
      <指令 —— 明确产物路径 + 每 H2 逐字标题>
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
    validation: [...]

final_outputs:
  - "{ws}/02-<stage2>/<file>"
```

### 4.2 T2 · Claude + Poll + Claude（等训练/eval）

```yaml
global_timeout: 43200s   # 12h 覆盖 launch + train + analyze

stages:
  - name: setup_experiment
    kind: claude
    prompt: |
      起 tlaunch 训练任务。
      产物：{ws}/02-launch/meta.json 含 exp_path / tmux_session / tmux_socket
      tmux_* 必须用 `^csm-` 开头值（不能用短别名）。
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
      # 只 poll 训练本身产的 marker（F1）
      - file: "{params.exp_path}/train_log/log.txt"
        primitives:
          - file_exists
          - regex_match: {pattern: 'process 0 finish jobs'}

  - name: analyze
    kind: claude
    prompt: |
      读 {params.exp_path}/train_log/report.json 生成分析报告。
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
```

### 4.3 T3 · Multi-member fan-out（N seed 并行 + 聚合）

```yaml
stages:
  - name: launch_group
    kind: claude
    prompt: |
      起 N-seed 训练组：master=s1，rider=s2/s3（共享 tmux socket，独立 exp_path）。
      产物 meta.json shape:
      {{
        "exp_path": "<MASTER exp 路径>",
        "members": [{{"slug":"...","seed":1,"role":"master","exp_path":"..."}}, ...],
        "tmux_session": "csm-...",
        "tmux_socket": "csm-..."
      }}
    outputs: ["{ws}/02-launch/meta.json"]
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
      产物：{ws}/03-aggregate/report.md
    outputs: ["{ws}/03-aggregate/report.md"]
    validation:
      - file: "{ws}/03-aggregate/report.md"
        primitives:
          - file_exists
          - required_sections: ["Per-seed metrics", "Mean & variance", "Verdict"]
```

---

## §5 Primitive 快速参考（完整版看 full guide §5）

| Primitive | 用途 | 常见形式 |
|-----------|------|---------|
| `file_exists` | 文件必须存在 | 无参 |
| `min_chars` | 最少字符数 | `{count: 200}` 或 `{section: "## Result", count: 80}` |
| `min_size_bytes` | 最少字节数 | `{count: 100}` |
| `required_sections` | 必须存在的 H2 headings | `["Summary", "Verdict"]` |
| `regex_match` | 匹配正则 | `{pattern: 'wauc.*0\.[0-9]{4}'}` 或 `{section: "## X", pattern: '...'}` |
| `jsonschema` | JSON 结构约束 | `{type: object, required: [x, y]}` |
| `contains_substring` | 包含子串 | `{text: "ok"}` |

**用哪个的经验规则**：
- JSON → `file_exists` + `jsonschema`
- Markdown 报告 → `file_exists` + `required_sections` + `regex_match(section:...)`
- 空态可能占位 → **不要设 `min_chars`**（会卡死；改用 `required_sections` 兜底）
- 通知回执 → `contains_substring: "ok"` 类

---

## §6 硬规则 F1-F9（生成时必须避开）

每条都是真实事故沉淀。生成 YAML 时**逐条对照检查**。

### F1. Poll check 不允许引用后续 stage 的产物
**症状**：poll 永远 fail，最终 timeout。
**根因**：死锁 —— poll 等的文件是下 stage 才写的。
**规则**：只 poll 训练/eval 本身产的完成 marker（如 `log.txt` 里 `process 0 finish jobs`）。
**禁止** poll 后续 stage 的 outputs 路径。

### F2. Poll 路径必须匹配 skill 实际写的位置
**症状**：`file_exists` 永远过不了，虽然文件手工能看到。
**根因**：底层 skill 写在你 YAML 没料到的子路径（`train_log/report.json` 而非 `report.json`）。
**规则**：不确定就**先只 `file_exists` 一个大致路径**跑 dry run，看真实位置再收紧。

### F3. Poll 的 jsonschema 必须匹配**真实**报告 shape
**症状**：`file_exists` 过了，jsonschema fail `required: [wauc] missing` —— 真实报告是嵌套的。
**根因**：按"理想 shape"写 schema，实际输出 shape 不一样。
**规则**：poll 的 check block **不加 jsonschema**（**R15** 会 warn）。
只 `file_exists`，等 dry run 看到真实 shape 再决定加不加。

### F4. `required_sections` 是**逐行 whole-line regex**
**症状**：产物有 `## Won't-try (do not repropose)`，但 `required_sections: ["Won't-try"]` 报 missing。
**根因**：whole-line 匹配，后缀 `(do not repropose)` 让整行不匹配。
**规则**：
- Prompt 里**必须**写"H2 标题逐字精确、不允许后缀/括号/副标题"
- `required_sections` 列表跟 prompt 字面完全一致（不带标点也不带 `##`）

### F5. `section:` 参数必须带 `##` 前缀
**症状**：Stage fail `section not found: "Expected wauc"`，虽然文件有 `## Expected wauc`。
**根因**：section slicer 用 `^(#+)\s+(.+?)\s*$` 匹配 arg，裸标题匹配不上。
**规则**：`section: "## Result"`（**不是** `section: "Result"`）。**R13** 会 fail 这个。

### F6. `regex_match` 对模型自然语言产物要宽松
**症状**：产物 `wauc = 0.99`，pattern `wauc[:\s]+0\.[0-9]{4}` 报 not found（`=` 不在 `[:\s]`）。
**根因**：模型写英中随意（`wauc = 0.99` / `wauc | 0.99`），严格分隔符会漏。
**规则**：用 `wauc.*0\.[0-9]{4}`（`.*` 兜住任意分隔）+ `section:` scoping 兜住不误配。
**R17** warn `[:\s]+` 严格分隔符。

### F7. tmux 命名必须在 jsonschema 里强制 `^csm-`
**症状**：mission 后半程 guard 挂，`tmux_session` 是 `sim` 短别名。
**根因**：Claude 自作聪明用短别名，下游 guard 认 `^csm-` 长字符串。
**规则**：stage prompt 提 tmux 时，outputs 的 jsonschema **必须**给 `tmux_session` /
`tmux_socket` 加 `pattern: '^csm-'`（**R18** warn 缺）。外部 tmux（如 eval-batch）不算。

### F8. Claude stage 必须声明非空 `outputs`
**症状**：Reload 拒绝，R9 fail。
**根因**：outputs 是契约声明，没 outputs 就没有可校验产物，下游也无法 `{stages.X.outputs[N]}` 引用。
**规则**：每个 kind=claude stage 至少 1 个 output。Poll stage **不能**有 outputs。

### F9. Placeholder 词汇表封闭
**症状**：mission 一起就 fail `unknown placeholder {cluster}`。R10 也会拦。
**根因**：CSM 用严格 render，不认识的 token 不静默漏进 prompt。
**规则**：只用 §3 表里的合法 token。字面量 `{...}` 用 `{{...}}` 转义。

---

## §7 自检 R9-R19（返回给用户前必过）

写完 YAML 后**必须**自检以下 11 条 rule。**Warn 也算 fail** —— 每条都是真实事故沉淀。

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

### 7.3 自检具体做法（生成时逐条对照心里过一遍）

1. 每 claude stage outputs 有内容？（R9）
2. 所有 `{...}` token 都是 §3 里 5 类合法 token 或 `{{...}}`？（R10）
3. 所有 primitive 名字都在 §5 的 7 个里？（R11）
4. 每 poll stage 有 check block？（R12）
5. 每个 `section:` 参数以 `## ` 开头？（R13）
6. 每个 poll `check.file` 引用的 stage 是**当前 poll 之前**的？（R14）
7. 有 poll stage 且 check 里用了 jsonschema？→ 删掉（R15）
8. `required_sections` 每 entry 是 bare heading text，无 `()`?（R16）
9. `regex_match` pattern 全用 `.*`，无 `[:\s]+`?（R17）
10. Prompt 提 tmux → outputs schema 有 `^csm-` pattern?（R18）
11. Poll 的 `timeout / poll_interval < 200`?（R19）

**任何一条不过 —— 改，再自检，直到全过再交付**。

---

## §8 输出格式

**写入路径**（强制）：`<CSM_REPO>/tasks/<workflow_name>.workflow.yaml`

**交付确认**（最后一行必输出）：
```
wrote <CSM_REPO>/tasks/<name>.workflow.yaml
```

格式硬要求：以 `wrote ` 开头（后端会文本匹配），后面完整绝对路径，单独一行。

---

## Appendix — 产品级 workflow 举例

一个 5-stage 的 CI 失败巡检 workflow。挑它当范例是因为它把下面这些**同时**用上了，
而不是因为它复杂：
- **顶部 `# clarifications:` 注释块**（保留 clarify agent 问过的问题 + 用户答案）
- **F0 合规**：整个 YAML 没有 `每周` `22:00` 这类时间词；触发频率由 Schedule 决定
- **参数化数据范围**：`days_back` = 每次跑取最近 N 天，与频率解耦
- **完整分支覆盖**：`collect` 空态 / `triage` 全 DEFER / `push` 失败 三条分支都处理
- **实际 primitive 组合**：`file_exists + jsonschema` for JSON；
  `file_exists + required_sections + regex_match(section:...)` for MD

**顶部注释块的样式**（每次生成 YAML 都要跟着写）：
```yaml
# clarifications:
#   1. Q: 最近 7 天没有新的失败报告时，怎么办？
#      A: 跳过 triage/fix/push，只发一条 '本周 0 条' 通知。
#   2. Q: triage 结果全是 DEFER（没有任何可自动修的）时，怎么办？
#      A: 跳过 fix/push，仍然发通知并附 defer 原因。
#   3. Q: git push 失败时怎么办？
#      A: 保留本地 commit，不 rollback，通知里注明需要人工 push。
```

**头两个 stage 摘要**（真实工业级 prompt 样式）：
```yaml
name: ci_failure_triage
description: 从 CI 报告目录拉最近 N 天的失败用例，逐条评审归类，能自动修的改掉并 commit+push，最后汇总通知。

parameters:
  - {name: days_back, type: int, default: 7, description: "取最近多少天的 CI 报告。与调度频率解耦。"}
  - {name: reports_dir, type: string, default: "./ci-reports", description: "CI 失败报告落盘目录。"}
  - {name: dry_run, type: bool, default: false, description: "true = 生成变更但不 commit / push / 通知"}

stages:
  - name: collect_failures
    kind: claude
    prompt: |
      列出 `{params.reports_dir}` 下最近 {params.days_back} 天修改过的 JSON 报告，
      逐个解析，聚合成一个数组。同一 test_id 出现多次时以 last_seen 最新的为准。

      对照 `{ws}/../state.json` 过滤掉已处理过的 test_id（historical dedup）——
      没有这个文件就当作空集，不要报错。

      产物：
      - {ws}/01-collect/failures.json —— JSON array. 每项 shape:
        {{
          "test_id": "<string>", "suite": "<string>", "message": "<string>",
          "last_seen": "<ISO8601>", "category": "flaky|regression|env|other"
        }}
        若最近 {params.days_back} 天无新失败，输出 `[]`（空数组）。
    outputs:
      - "{ws}/01-collect/failures.json"
    validation:
      - file: "{ws}/01-collect/failures.json"
        primitives:
          - file_exists
          - jsonschema:
              type: array
              minItems: 0
              items:
                type: object
                required: [test_id, suite, category]
                properties:
                  category: {type: string, enum: [flaky, regression, env, other]}
```

注意 `minItems: 0` —— 空态是**合法产物**，不是失败。把它写成 `minItems: 1`
是最常见的一个错：巡检类 workflow 有一半的运行本来就该是空的。


需要**方法论细节**（§1 拆解思路）、**primitive 全表**（§5）或 **Appendix B 用户视角**，
用 `Read` 工具打开完整版：`<CSM_REPO>/docs/workflow_authoring_guide.md`。
