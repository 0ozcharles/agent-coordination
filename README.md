# Agent RSI

Agent RSI 研究 **multi-agent 系统在任务执行过程中如何调整分工**。当多个 agents 出现重复工作、信息缺失或代码整合困难时，我们希望系统能够根据当前进度决定是否干预，以及采用哪种协作方式。

项目目前以 coding tasks 为实验场景，使用 [CooperBench](https://github.com/cooperbench/CooperBench) 提供任务和测试。目标是实现一个 runtime manager，并验证它能否在相同资源约束下提高任务完成率，或在完成率相近时减少成本。

## 当前实现

现有代码能够启动两个 coding agents，保存执行进度，从同一个 checkpoint 尝试不同分工，最后用官方测试评分并记录 API 费用。

```text
两个 agents 分别实现功能
          │
      保存 checkpoint
          │
     ┌────┴────┐
 StrongShare  Integrator
 自行协商分工  指定一人负责整合
     └────┬────┘
      测试结果与费用
```

在最新对照中，两个条件都保留两名 agents，并且只评分 agent1 发布的完整代码。两组获得相同的起始进度和交接信息，比较的是额外指定整合职责是否有帮助。

**当前阶段是研究原型。** 固定分工的执行和评分已经跑通，动态 manager 仍待开发。核心实验代码目前在 `experiments/m0/`；`src/agent_rsi/` 目前只有统一命令入口。

## 快速开始

### 浏览实验与结果

在仓库根目录执行，需要 Python 3.9+：

```bash
# 查看有哪些实验
python3 scripts/project.py list

# 查看最新实验的入口和结果目录
python3 scripts/project.py show early-roles

# 查看成功次数和费用
python3 scripts/project.py results early-roles
```

这些命令不需要 Docker 或 API key。新 checkout 没有原始实验数据时，`results early-roles` 会读取仓库中的结果快照。

### 继续已有实验

实际执行需要 CooperBench 环境、Docker、模型 API key，以及该批实验的 checkpoint 和镜像。准备步骤见 [开发与运行](docs/DEVELOPMENT.md)。

```bash
# 预览运行命令
python3 scripts/project.py run early-roles

# 恢复该批实验中尚未完成的部分
python3 scripts/project.py run early-roles --execute
```

`run` 当前用于恢复已有批次；已经完成的结果会被复用。**它还不能从配置创建一批全新的实验。** 历史 checkpoint 和 Docker images 未纳入 Git，需要与实验负责人单独同步。

## 代码结构

```text
Agent RSI/
├── src/agent_rsi/       项目 CLI，后续通用组件的开发位置
├── configs/            实验入口与 benchmark 版本索引
├── experiments/m0/     现有 runner、分工策略、模型调用和评分实现
├── results/            可共享的实验结果快照
├── tests/              项目 CLI 的测试
├── scripts/project.py  无需安装的命令入口
└── docs/               代码说明与环境准备
```

阅读当前实现，可以沿着下面三个文件往下看：

| 文件 | 负责什么 |
| --- | --- |
| [run_luna_early_roles.py](experiments/m0/run_luna_early_roles.py) | 选择任务，安排对照实验，调用执行与评分 |
| [early_role_runtime.py](experiments/m0/early_role_runtime.py) | 恢复两个 agents，注入分工指令，继续执行 |
| [early_role_evaluator.py](experiments/m0/early_role_evaluator.py) | 对实际提交的代码运行官方测试 |

底层容器、通信、模型和 checkpoint 的对应关系见 [代码地图](docs/CODEBASE.md)。

## 共同开发

接下来先把现有执行流程提取成一个 **配置驱动的 runner**：给定任务、模型和分工方式，就能从任务原始状态启动，完成代码提交、评分和费用记录。这样新同事不需要拿到历史 checkpoint，也能开始实验。

开发分成两个相接的部分：

- **执行与评估：** 整理任务配置、模型接口、运行入口和结果格式，让一个任务可以独立运行。
- **组织调整：** 接入固定分工方式，明确 manager 能观察什么、能采取什么动作，再实现状态驱动的选择策略。

新代码放在 `src/agent_rsi/`，每个改动单独开分支。已有实验的代码与结果通过版本和文件 hash 关联；提取组件时保留旧实现，新实验使用独立版本和输出目录。具体约定见 [开发说明](docs/DEVELOPMENT.md#新实验约定)。

## 实验记录

最新的 LlamaIndex、Click 对照中，StrongShare 和 Integrator 均为每个任务 **2/2 通过**。这批结果没有显示提前指定 Integrator 的成功率优势；Click 上的费用差异仍需要更多独立任务验证。

- [最新结果快照](results/early_roles_v1.json)：任务、模型、评分和费用。
- [实验文档](https://docs.google.com/document/d/1ZnL8_nTPCkAt11x4yFNqmnVelbU9Cyy2HC3gb45sDlI)：各批次结果、结论和研究方向。
- 本机 `experiments/m0/artifacts/`：完整 trace、patch、checkpoint 和账单，不随 Git 分发。
