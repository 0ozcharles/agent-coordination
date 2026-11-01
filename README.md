# Agent RSI

研究 multi-agent 执行过程中，何时调整分工、调整成什么方式，才能改善任务完成率或降低成本。

当前已有 CooperBench coding tasks 的执行、checkpoint、几种固定干预、官方评分和费用记录。**动态 manager 尚未验证有效。** 最近统一交付方式的8次对照全部通过，未发现提前指定 Integrator 的成功率优势。

## 从这里开始

需要 Python 3.9+。以下命令只读本地文件，不需要 API key 或 Docker：

```bash
python3 scripts/project.py list
python3 scripts/project.py show early-roles
python3 scripts/project.py results early-roles
```

`results` 优先读取本机实验结果。新同事没有原始数据时，会读取 Git 中的精选结果快照。

也可以在自己的开发虚拟环境中安装命令：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/agent-rsi list
```

这个轻量开发环境不包含模型或 benchmark 的运行依赖。实验环境见 [开发说明](docs/DEVELOPMENT.md)。

## 目录

```text
src/agent_rsi/          共同维护的项目入口；后续提取的通用模块放这里
configs/project.json   实验名称、脚本入口、结果路径和 benchmark 版本
experiments/m0/         已有实验实现，保留原路径以便追溯
results/               可以随 Git 分享的精选结果，不含完整 trace
tests/                 新开发入口的轻量测试
docs/                  代码地图、开发方式和下一步工作
scripts/project.py     无需安装即可使用的统一命令
```

本机还有 `experiments/m0/artifacts/`、CooperBench checkout、Docker images 和生成的报告，它们不进入 Git。旧 `environment_setup.md` 是2026-10-29的过程记录，当前开发请看 `docs/DEVELOPMENT.md`。

## 要改什么，去哪里找

| 工作 | 现有实现 |
| --- | --- |
| 任务与 feature pair | `experiments/m0/task_specs.py`、`cross_task_specs.py`、`selector_task_specs.py` |
| 两个 agent、通信、容器与恢复 | `experiments/m0/baseline_runtime.py` |
| 模型调用与费用 | `experiments/m0/luna_model.py`、`luna_accounting.py` |
| checkpoint 与完整交接信息 | `experiments/m0/wip_checkpoint.py`、`cross_task_packet.py` |
| 最新分工对照 | `experiments/m0/early_role_runtime.py` |
| 相同交付方式下的官方评分 | `experiments/m0/early_role_evaluator.py` |
| 简单动作选择规则 | `experiments/m0/selector_rule.py` |

各模块如何连接、哪些实现仍需提取，见 [代码地图](docs/CODEBASE.md)。

## 已有结果

最近一批使用 `gpt-6-luna`，两个 agent 从相同的早期 checkpoint 继续，只评分 agent1 实际发布的完整代码。

| 任务 | StrongShare | Integrator |
| --- | --- | --- |
| LlamaIndex：NDCG 修正 + graded relevance | 2/2通过 | 2/2通过 |
| Click：editor_args + working_dir | 2/2通过 | 2/2通过 |

新增已知实验 API 费用 $0.13321271，另有2笔超时请求用量未知。费用包含中断尝试；已有共同起点没有重复收费。两个条件各重复两次，共享两个任务起点，不能视为8个独立任务。

完整进展：[共同编辑的实验文档](https://docs.google.com/document/d/1ZnL8_nTPCkAt11x4yFNqmnVelbU9Cyy2HC3gb45sDlI)。

## 两个人怎么一起改

1. 每个改动开一个分支，例如 `feature/task-config` 或 `feature/fresh-runner`。
2. 把新实现放进 `src/agent_rsi/`，新实验配置放进 `configs/`；一次改动解决一个问题。
3. 只运行与改动有关的测试，在提交说明里写清改了什么、如何检查。
4. 历史实验通过 source hash 绑定实现。改进这些组件时，提取到新模块并使用新的实验版本，保留旧结果可解释性。

**下一步共同开发的具体任务：提取一个配置驱动、从原始任务启动的 runner。** 它应能在没有本机历史 checkpoint 的情况下启动两个 agents、收集已发表的代码并评分。先复用现有容器、通信和评分实现；完成后再接动态 manager。

本次整理没有增加模型实验，没有安装新依赖，也没有改变历史模型结果。

