# Agent Coordination

> 原名 Agent RSI（2026-11 更名）。本项目研究的是 **multi-agent 协作失败的诊断与干预**，不是 recursive self-improvement。

当多个 LLM agents 一起完成任务却失败时，原因是什么？是任务超出了 agent 的能力，是协调不足导致重复劳动，是拆分后的上下文边界让某个 agent 缺少必要信息，还是各自都做对了却合不起来？**针对不同原因，哪种组织调整在相同计算预算下最划算？这个原因能否在运行过程中提前识别？**

实验场景是 coding tasks，任务和官方测试来自 [CooperBench](https://github.com/cooperbench/CooperBench)。

## 研究路线

| 阶段 | 问题 | 状态 |
|---|---|---|
| M0 探索 | 跑通两 agent 执行、checkpoint 分叉、评分与计费 | **已完成**，见 [回顾](docs/M0_RETROSPECTIVE.md) |
| S1 失败普查 | 协作失败分别由哪些原因造成，各占多少？ | **进行中**：预筛完成（232 次运行），主实验任务列表已冻结（53 个 pair） |
| S2 对症干预 | 对症的干预是否比不对症的更能挽回失败？ | 待 S1 |
| S3 在线诊断 | 能否从中途信号预测原因并选择干预？ | 待 S2 |

完整设计、失败原因的操作化定义和防漂移规则见 [研究计划](docs/RESEARCH_PLAN.md)。

**S1 预筛初步结果**（gpt-6-luna，58 个 pair × 2 次，$1.93，[逐行数据](results/s1_screen_rows.csv)）：coop 官方通过 32 / 116。84 次失败中 58 次是 integration，其中 55 次是 git 合并冲突：两个 agent 各自都写对了，只是改了同一段代码。solo 官方通过 25 / 116，另有 44 次写对了但没有发布。预筛只有 2 次重复，结论以主实验为准。详见 [研究计划](docs/RESEARCH_PLAN.md) 的“预筛结果”一节。

## M0 学到了什么

115 次已评分运行中，StrongShare 的 12 次失败有 8 次是**两份 patch 合并冲突或重复提交**。凡是让一个 agent 负责最终产物的做法都能消除这类失败；消除之后，剩下的失败基本是模型能力不足。但这只覆盖 10 个 model×task 组合，样本太少，不能当作结论。M0 的主要教训在流程上：没有先做诊断、每格 n=2、评分规则和模型中途变更。

```bash
python3 scripts/project.py census     # 全部历史运行 + 失败原因标签 + 任务×条件矩阵
```

没有本地 artifacts 时，该命令读取提交在仓库里的 [results/m0_runs.csv](results/m0_runs.csv)。

## 快速开始

只需要 Python 3.9+，不需要 Docker 或 API key：

```bash
python3 scripts/project.py census                 # M0 历史运行的失败标签
python3 scripts/project.py list                   # 历史 study 入口
python3 scripts/project.py results early-roles    # 某个 study 的成功次数与费用
python3 -m unittest discover -s tests -v
```

恢复历史批次需要 CooperBench 环境、Docker、模型 key，以及未纳入 Git 的 checkpoint 和镜像，见 [开发与运行](docs/DEVELOPMENT.md)。

## 代码结构

```text
agent-coordination/
├── src/agent_coordination/  新代码：CLI、census（M0 历史标签）、s1（失败普查 runner）
├── experiments/m0/      M0 的 runner、分工策略、评分实现（冻结，供历史批次复验）
├── results/             可分享的结果快照
├── configs/             study 入口与 benchmark 版本
├── docs/                研究计划、M0 回顾、代码地图、环境准备
└── tests/
```

M0 代码的调用关系见 [代码地图](docs/CODEBASE.md)。`experiments/m0/` 中的已运行批次通过 source hash 绑定实现，不要修改；新功能写在 `src/agent_coordination/`。

## 运行 S1

```bash
python3 scripts/project.py s1 configs/s1_smoke.json            # 预览：任务数、已完成数、命令
python3 scripts/project.py s1 configs/s1_smoke.json --execute  # 实际运行（调用付费 API）
python3 scripts/project.py s1 configs/s1_smoke.json --report   # 重新打标签，不跑 Docker
python3 scripts/project.py s1 configs/s1_screen.json --execute --workers 4   # 并行预筛
python3 scripts/project.py s1 configs/s1_screen.json --freeze configs/s1_main.json  # 按预设规则冻结任务
```

需要 Docker、CooperBench venv、`OPENAI_API_KEY`（写在仓库根目录的 `.env` 中，已被 gitignore），coop 模式还需要 Redis。准备步骤见 [开发与运行](docs/DEVELOPMENT.md#s1-runner)。runner 从任务原始状态启动 CooperBench 的 coop 和 solo，再**单独评测每个 agent 发布的 patch 和它本地实际写出的代码**，据此区分 integration / submission / context / capability。规则见 [研究计划](docs/RESEARCH_PLAN.md#失败原因的操作化定义)。

## 相关文档

- [Research Proposal](https://docs.google.com/document/d/1h7dLtewjGbIDzCLL7Z0KvKv-oVhIs5JGf4IEvXnY6ts)：原始提案，S3 沿用其中的形式化定义
- [Experiment Design and Pilot Plan](https://docs.google.com/document/d/1h_IDHwaXVPa3zsxrINIExhJQNe_LJIBMeqhS7lZ7Z0U)：原始 pilot 计划
- [实验记录](https://docs.google.com/document/d/1ZnL8_nTPCkAt11x4yFNqmnVelbU9Cyy2HC3gb45sDlI)：M0 各批次的详细结果
