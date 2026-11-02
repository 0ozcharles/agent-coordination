# M0 回顾

M0 是 2026-10-29 到 11-01 的探索阶段，共 13 批实验、115 次有效运行。数据见 [results/m0_runs.csv](../results/m0_runs.csv)，可以用 `python3 scripts/project.py census` 重新统计。

## 结论

协作失败的主要原因是合并：两份代码冲突，或者两个 agent 提交了同一份代码。只要由一个 agent 负责最终代码（Consolidate、Integrator），这类失败就会消失，剩下的基本是能力问题。

不过这只基于 10 个“模型 × 任务”组合，其中只有 6 个能区分不同条件，不足以下结论。

## 失败原因统计

| 结果 | 含义 | Consolidate | Integrator | Keep | Solo | StrongShare |
|---|---|---|---|---|---|---|
| 通过 | 两个 feature 都通过 | 20 | 16 | 0 | 16 | 31 |
| 合并冲突 | 两份代码无法合并 | 0 | 0 | 2 | 0 | 6 |
| 重复提交 | 两个 agent 交了同一份代码，且失败 | 0 | 0 | 0 | 0 | 2 |
| 能力不够 | 失败的 feature 在同模型同任务的任何运行中都没通过过 | 2 | 2 | 2 | 4 | 3 |
| 偶然失败 | 失败的 feature 在其他运行中通过过 | 4 | 0 | 0 | 4 | 1 |

M0 的数据没有单独测试每个 agent 的代码，所以无法判断“上下文不够”这类原因。这是 S1 要补上的。

## 问题在哪

1. 每个条件只跑 2 次，2/2 和 1/2 在统计上分不出来。
2. 很多任务要么全过、要么全挂，区分不了条件的好坏。
3. 中途换过 4 种评分规则，同一个条件名在不同批次里衡量的东西不一样。
4. 中途把模型从 Qwen3.5-9B 换成了 gpt-6-luna。
5. 条件是边跑边加的（Integrator、提交提醒、自动选择策略）。
6. 没有先弄清失败原因，就直接测试干预。

费用不是瓶颈，gpt-6-luna 每次运行约 $0.004–0.03。真正拖慢进度的是每个实验都依赖历史 checkpoint 和 Docker 快照。

## 批次记录

| 批次 | 模型 | 要回答的问题 | 结果 |
|---|---|---|---|
| baseline / consolidate smoke | Qwen | 分开写再合并能不能行 | Keep 合并冲突；Consolidate 通过（n=1） |
| expansion_wip_v1 | Qwen | 3 个条件在新任务上的表现 | Keep 0/4；click2800 所有条件都失败 |
| solo_controls_v1 | Qwen | Solo 参照 | click2800 0/2，dirty_equals 2/2 |
| handoff_dirty_v1 | Qwen | 交接代码后 StrongShare 和 Consolidate 谁好 | 4/4 全过，分不出 |
| cross_handoff_v1 | Qwen | 3 个任务上比较 StrongShare / Consolidate / Solo | StrongShare 的失败都是合并冲突或重复提交 |
| luna_solo_v1 | luna | 换模型后的 Solo | 4/4 |
| luna_collaboration_v1 | luna | StrongShare 和 Consolidate | 7/8，唯一的失败是合并冲突 |
| luna_timing_v1 | luna | 早介入还是晚介入 | 只有一组失败，原因同样是合并冲突 |
| luna_integrator_v1 | luna | 指定一个 agent 负责合并 | Integrator 8/8，StrongShare 6/8 |
| luna_selector_v1 | luna | 按改动位置自动选策略 | dspy 全部失败，没有可比较的差异 |
| luna_conflict_v1 | luna | 出现冲突时介入 | 没有出现冲突，无数据 |
| luna_publication_v1 | luna | 提醒 agent 提交 | 6/6 |
| luna_early_roles_v1 | luna | 统一提交规则后比较 StrongShare 和 Integrator | 8/8 全部通过 |

## 保留下来的

- checkpoint 分叉、费用记录、官方评分接入，S3 会继续使用。
- `experiments/m0/` 的代码保持不变，历史批次仍可复现。
