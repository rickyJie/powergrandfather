# 完成判据

> agent 怎么知道这次 Run "做完了"？写**机器能验证**的条件，不要写主观判断。
> R4 规则会严卡这一点。

## 必须满足的条件（ALL）

下面的条件必须**全部满足**，否则视为未完成。

- [ ] `<path>/final_report.md` 文件存在
- [ ] `<path>/final_report.md` 包含字符串 `"status: ok"`
- [ ] `<path>/metrics.json` 文件存在且为合法 JSON
- [ ] `metrics.json` 中 `accuracy` 字段 >= 0.85
- [ ] `<某个命令>` 退出码为 0

## 可选标识（ANY）

下面任一条件出现，视为完成（有些流程提前出来也算）：

- [ ] 日志末尾出现 `"DONE"` 字样
- [ ] PID 文件被清理

## 反例（不要这样写）

❌ "agent 觉得做得差不多"
❌ "report 看起来正常"
❌ "应该是 ok 的"

这些都会被 R4 判 fail。

## 失败判据

下面任一出现，视为失败：

- [ ] `<path>/error.log` 文件存在且非空
- [ ] 退出码非 0 且非 None
- [ ] `metrics.json` 中 `accuracy` 字段 < 0.5

（CSM 的 runner 只看 exit_code，更精细的失败判定靠你这边产出 + 后续 supervisor agent。）
