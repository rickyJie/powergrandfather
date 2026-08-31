# 预期产出清单

> 这个流程跑完后，会产出哪些文件？放在哪里？什么格式？
> R6 规则会按这份清单判，越具体越好。

## 主产出

| 路径（相对 cwd） | 类型 | 说明 |
|---|---|---|
| `final_report.md` | markdown | 流程结论摘要，supervisor agent 会读 |
| `metrics.json` | json | 关键指标（accuracy / latency / ... ） |
| `progress.json` | json | 流程进度快照，跑一半也会有 |

## 附产出

| 路径 | 类型 | 说明 |
|---|---|---|
| `logs/run-{run_id}.log` | log | 详细执行日志 |
| `outputs/*.png` | image | 可视化图 |

## 注意

- CSM runner 自动扫的"convention globs"包括：`progress.json` / `final_report.md` / `summary.md` / `report.json` / `journal*.jsonl` 等。在上面这些路径会被自动入库。
- TaskDefinition YAML 里也可以声明 `output_globs`，会被一起扫，**但仅限 cwd 内**（绝对路径、`..`、symlink 跳出 cwd 都会被拒）。
- 大文件 CSM 只存前 4KB 的 preview，原文件留在磁盘上。
