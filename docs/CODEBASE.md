# 代码地图

## 当前流程

```text
任务 / 原始 feature 要求
        ↓
agent1 + agent2：各自 workspace、消息通信、共享 Git
        ↓
checkpoint：代码、完整 history、工具状态和通信状态
        ↓
组织调整 + 完整交接信息
        ↓
继续执行 → 实际发表的代码 → 官方测试 → 结果与费用
```

`experiments/m0/` 里的代码按研究批次逐步增加，因此同一个功能有多个历史版本。当前先保留这些文件，把统一入口和后续开发放到 `src/agent_rsi/`，不通过移动文件破坏旧实验路径。

## 一次最新实验的调用关系

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 调度 | `run_luna_early_roles.py` | 固定任务、顺序、共同起点、预算、执行和评分 |
| 分工 | `early_role_runtime.py` | StrongShare / Integrator 指令，恢复两名 agent，相同最终交付规则 |
| 执行循环 | `luna_integrator_worker.py` | 原生 agent loop，工具执行与并行工作 |
| 容器与通信 | `baseline_runtime.py` | workspace、共享 Git、消息、snapshot/restore |
| 交接 | `cross_task_packet.py` | 从 checkpoint 构造完整代码 diff 和允许的 scratch 信息 |
| 模型 | `luna_model.py` | API 调用、脱敏响应与错误 |
| 费用 | `luna_accounting.py` | 记录已知用量、费用及未知用量请求 |
| 提交 | `publication_guard.py` | 提交前提醒，不替 agent 改代码或发布 |
| 评分 | `early_role_evaluator.py` | 仅评分 agent1 实际发表的完整代码，运行官方两项测试 |

以上路径都相对于 `experiments/m0/`。benchmark 原生 agent 和评分工具来自固定版本的 CooperBench。

## 已完成和待开发

| 组件 | 当前状态 | 后续改动位置 |
| --- | --- | --- |
| 任务定义 | 已有，分散在3个 registry | 提取为 `src/agent_rsi/tasks.py`，由配置选择 |
| 两 agent 执行 | 已跑通，仍与历史实验耦合 | 提取可独立使用的 runner |
| 固定动作 | Keep、StrongShare、Consolidate、Integrator 已有历史实现 | 从当前对照使用的语义开始，逐个接入新 runner |
| 自动选择动作 | 只有简单重叠规则，未显示优势 | 等具体问题明确后再实现 manager |
| 评分与费用 | 已跑通 | 提取接口时保留交付规则和 unknown usage 的含义 |
| 可分享结果 | 最新8次有精简快照 | 新批次完成后导出同样格式 |

这里列出的后续模块是开发安排，不表示它们已经实现。

## 结果如何读取

- `summary.json`：一批实验的汇总。
- `result.json`：一个已完成条件的结果。
- `evaluation_summary.json`：官方测试结果。
- `usage_ledger.json`：真实请求账单，包括失败和未知用量。
- `checkpoint.json` / `shared_git.tar`：恢复状态；还需要对应 Docker snapshot images。
- `source_snapshot/` / `protocol.json`：该批实验绑定的代码和配置。

同一物理账单只算一次。`failed_attempts/` 中的账单副本不能再加一遍。成本比较中加入共享前期工作，不代表本批真的再次支付了前期费用。

## 当前边界

旧实验入口依赖本机的历史 `artifacts/` 和 Docker images；其中 `run_solo_smoke.configure()` 还固定了 macOS Docker socket。复制 Git 源码并不能直接在 Linux 服务器上复现旧 checkpoint 实验。

新 fresh runner 的第一个完成标准：任务、模型、输出路径、Docker 连接均来自配置或环境；从 benchmark 原始 base 启动；两名 agent 都能正常结束并留下可评分的 published patches。用一个小任务验证一次即可，不需要把所有历史实验重新跑一遍。

