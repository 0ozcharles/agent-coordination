# Agent Coordination

多个 LLM agent 合作写代码时，为什么会失败？每种失败原因，用什么办法修最划算？

> 项目原名 Agent RSI，2026-11 更名。研究内容是多 agent 协作，与 recursive self-improvement 无关。

## 研究内容

两个 agent 各负责一个 feature，最后把代码合并。失败可能来自四类原因：

- **能力不够**：任务超出了模型的能力
- **上下文不够**：任务拆开后，agent 缺少完成自己那部分所需的信息
- **协调不够**：重复劳动，或者不知道对方在改什么
- **合并失败**：各自都写对了，但合不到一起

不同原因需要不同的修法。我们先测出每类原因占多少，再针对主要原因比较干预方法，最后研究能否在运行中途提前判断原因。任务和测试来自 [CooperBench](https://github.com/cooperbench/CooperBench)。

| 阶段 | 问题 | 状态 |
|---|---|---|
| M0 探索 | 搭建实验流程 | 已完成，见 [M0 回顾](docs/M0_RETROSPECTIVE.md) |
| S1 失败普查 | 失败由哪些原因造成，各占多少 | 进行中：预筛完成，主实验待运行 |
| S2 对症干预 | 对症的干预是否比其他干预更有效 | 待 S1 完成 |
| S3 在线诊断 | 能否在运行中途判断原因并选择干预 | 待 S2 完成 |

详细设计见 [研究计划](docs/RESEARCH_PLAN.md)。

## 目前的发现

S1 预筛：gpt-6-luna，58 个任务，coop 和 solo 各跑 2 次，共 232 次运行，费用 $1.93。

- coop 通过 32 / 116。84 次失败中有 58 次是合并失败，其中 55 次是 git 冲突：两个 agent 都写对了，只是改了同一段代码。
- 能力不够 15 次，上下文不够 10 次。
- solo 官方只通过 25 / 116，但另有 44 次其实写对了，只是没有提交 PR。CooperBench 只给已提交的代码打分。

每个任务只跑了 2 次，结论以主实验为准。逐行数据见 [results/s1_screen_rows.csv](results/s1_screen_rows.csv)。M0 的结果方向一致，但样本太小，只能作为线索。

## 快速开始

以下命令只需要 Python 3.9+，不需要 Docker 或 API key：

```bash
python3 scripts/project.py census              # M0 运行的失败原因统计
python3 scripts/project.py s1 configs/s1_screen.json --report   # S1 预筛结果（需要本地运行日志）
python3 -m unittest discover -s tests
```

运行 S1 实验需要 Docker、Redis 和 OpenAI API key，步骤见 [开发与运行](docs/DEVELOPMENT.md)。

## 目录

```text
src/agent_coordination/   当前代码：CLI、M0 统计（census）、S1 runner（s1）
configs/                  实验配置
results/                  可分享的结果数据
experiments/m0/           M0 阶段的代码（冻结，不再修改）
docs/                     研究计划、M0 回顾、开发说明
tests/
```

## 文档

- [研究计划](docs/RESEARCH_PLAN.md)：问题、失败原因的判断方法、各阶段设计、当前进展
- [M0 回顾](docs/M0_RETROSPECTIVE.md)：探索阶段做了什么、学到了什么
- [开发与运行](docs/DEVELOPMENT.md)：环境准备、运行 S1、恢复 M0 批次
- [M0 代码地图](docs/CODEBASE.md)：`experiments/m0/` 的结构
- 原始文档：[Research Proposal](https://docs.google.com/document/d/1h7dLtewjGbIDzCLL7Z0KvKv-oVhIs5JGf4IEvXnY6ts) · [Pilot Plan](https://docs.google.com/document/d/1h_IDHwaXVPa3zsxrINIExhJQNe_LJIBMeqhS7lZ7Z0U) · [M0 实验记录](https://docs.google.com/document/d/1ZnL8_nTPCkAt11x4yFNqmnVelbU9Cyy2HC3gb45sDlI)
