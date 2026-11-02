# M0 代码地图

本文说明 `experiments/m0/` 的结构，供恢复或参考历史批次时使用。当前的代码在 `src/agent_coordination/`：`census.py` 统计 M0 结果，`s1.py` 是 S1 的实验 runner。

## M0 实验流程

```text
任务（两个 feature）
        ↓
agent1 + agent2：各自的工作区、消息通信、共享 Git
        ↓
checkpoint：保存代码、对话历史、工具和通信状态
        ↓
切换组织方式，交接信息
        ↓
继续执行 → 提交的代码 → 官方测试 → 结果与费用
```

M0 的代码是随批次逐步加出来的，同一功能可能有多个历史版本。为了不破坏旧批次的复现，这些文件保持原样。

## 最后一批实验的调用关系

路径相对于 `experiments/m0/`。

| 层 | 文件 | 作用 |
| --- | --- | --- |
| 调度 | `run_luna_early_roles.py` | 任务、顺序、起点、预算、执行和评分 |
| 分工 | `early_role_runtime.py` | StrongShare / Integrator 的指令和提交规则 |
| 执行循环 | `luna_integrator_worker.py` | agent 循环和工具执行 |
| 容器与通信 | `baseline_runtime.py` | 工作区、共享 Git、消息、快照与恢复 |
| 交接 | `cross_task_packet.py` | 从 checkpoint 生成代码 diff 和交接信息 |
| 模型 | `luna_model.py` | API 调用和错误处理 |
| 费用 | `luna_accounting.py` | 记录用量和费用 |
| 提交 | `publication_guard.py` | 提交前提醒 agent |
| 评分 | `early_role_evaluator.py` | 对 agent1 提交的代码运行官方测试 |

## 结果文件

- `summary.json`：一批实验的汇总
- `result.json`：一个条件的结果
- `evaluation_summary.json`：官方测试结果
- `usage_ledger.json`：请求账单，包括失败和用量未知的请求
- `checkpoint.json` / `shared_git.tar`：恢复状态用，还需要对应的 Docker 快照
- `source_snapshot/` / `protocol.json`：该批实验使用的代码和配置

计算费用时，同一笔请求只算一次，`failed_attempts/` 里的账单副本不要重复计入。

## 限制

旧批次依赖本机的 `artifacts/` 和 Docker 快照，部分脚本还固定了 macOS 的 Docker socket，因此不能在其他机器上直接复现。
