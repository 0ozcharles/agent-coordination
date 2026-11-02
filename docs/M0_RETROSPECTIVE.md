# M0 回顾：13 批实验实际告诉了我们什么

时间 2026-10-29 ~ 11-01。数据来自 `python3 scripts/project.py census`（快照 [results/m0_runs.csv](../results/m0_runs.csv)）。

## 一句话结论

在这些 CooperBench 任务上，**两名 agent 协作失败的主要可见原因是集成失败**：两份 patch 合并冲突，或者两人重复提交了同一份代码。凡是让一个人负责最终产物的做法（Consolidate、Integrator、统一交付规则）都能消除这类失败。消除之后各条件几乎全部通过，剩下的失败基本是模型能力不够。不过，这个结论只建立在 **10 个 model×task 组合**上，其中 6 个能区分条件。

## 失败原因分布（115 次已评分运行）

| label | 含义 | consolidate | integrator | keep | solo | strongshare |
|---|---|---|---|---|---|---|
| passed | 两项通过 | 20 | 16 | 0 | 16 | 31 |
| merge_conflict | 两份 patch 无法合并 | 0 | 0 | 2 | 0 | 6 |
| duplicate_patch | 两人交了同一份 patch 且失败 | 0 | 0 | 0 | 0 | 2 |
| capability_gap | 失败的 feature 在同模型同任务的任何运行中都没通过过 | 2 | 2 | 2 | 4 | 3 |
| recoverable | 失败的 feature 在其他运行中通过过 | 4 | 0 | 0 | 4 | 1 |

对照你最初列的四类原因：

| 你的假设 | 现有数据能否回答 | 证据 |
|---|---|---|
| 合并 / 集成更好 | **能，而且是主因** | StrongShare 的 12 次失败中 8 次是 merge_conflict 或 duplicate_patch |
| 协调不足 | 部分能 | duplicate_patch 算一种；"没有约定接口"需要读 trace 才能判断 |
| 任务与 agent 能力不匹配 | 能 | dspy feature 3、Qwen 上的 click2800 feature 2 任何条件都没过 |
| 上下文边界不好 | **不能** | 评分记录里没有这个信号，需要逐 agent 评分或读 trace |

## 为什么感觉乱

1. **样本量**：每个格子 n=2。2/2 对 1/2 在统计上无法区分。原计划是 20 个 base task × 4 个条件 × 3 次重复，从来没有执行。
2. **任务多数在天花板或地板上**：gpt-6-luna 上的 pillow 全部通过，dspy 全部失败。这类任务分不出组织方式的好坏，却占了大量运行。
3. **评分规则中途变了**：先后用过 naive merge、lead patch fallback、identical skip、只评 agent1 交付物四种规则。不同批次的同一个条件名，实际衡量的东西不一样。
4. **中途换了模型**：前半程是 Qwen3.5-9B，后半程是 gpt-6-luna。
5. **条件中途新增**：Integrator、发布提醒、selector 都是在上一批暴露问题之后临时加的。计划中的 Serial StrongShare（D1）一次也没跑，Keep 只在 Qwen 上跑过 4 次。
6. **跳过了诊断这一步**：每一批都直接测某个干预有没有用，没有先回答"失败是什么原因"。所以每批的结果只能引出下一个临时补丁。
7. **钱不是瓶颈**：gpt-6-luna 每次运行约 $0.004–0.03。真正的瓶颈是每个实验都绑定了历史 checkpoint 和 Docker snapshot。

## 批次时间线

| 批次 | 模型 | 当时想回答的问题 | 实际得到 |
|---|---|---|---|
| baseline / consolidate smoke | Qwen | 分开写再合并能不能行 | Keep 合并冲突失败；Consolidate 通过（n=1） |
| expansion_wip_v1 | Qwen | 3 个条件在新任务上的表现 | Keep 0/4；click2800 所有条件都失败（能力不足） |
| solo_controls_v1 | Qwen | Solo 参照 | click2800 0/2，dirty_equals 2/2 |
| handoff_dirty_v1 | Qwen | 交接源码后 SS 和 Cons 谁好 | 4/4 全过，分不出 |
| cross_handoff_v1 | Qwen | 3 个任务上比较 SS / Cons / Solo | SS 的失败是合并冲突或重复 patch；Cons 在 pillow 上 0/2 |
| luna_solo_v1 | luna | 换模型后的 Solo | 4/4 |
| luna_collaboration_v1 | luna | SS 和 Cons | 7/8，唯一失败是 SS 合并冲突 |
| luna_timing_v1 | luna | 早介入还是晚介入 | 只有"晚 + SS + llama"失败，原因同样是合并冲突 |
| luna_integrator_v1 | luna | 指定整合人 | Integrator 8/8，SS 6/8（合并冲突） |
| luna_selector_v1 | luna | 按改动位置自动选策略 | dspy 全部失败；没有可比较的差异 |
| luna_conflict_v1 | luna | 冲突出现时介入 | 一个冲突都没收集到，0 行数据 |
| luna_publication_v1 | luna | 提醒 agent 提交 | 6/6 |
| luna_early_roles_v1 | luna | 统一交付规则后 SS 和 Integrator 对比 | 8/8，全部通过 |

## 可以保留下来的东西

- **从同一个 checkpoint 分叉、配对比较**的做法，以及成本账本、官方评分接入。这套基础设施是对的。
- **一个可以写进论文的初步观察**：在补齐信息共享之后，剩下的协作损失主要来自"谁对最终产物负责"，而不是信息不足。这与 CooperBench 报告的 coordination deficit 方向一致。不过要在 20 个以上的任务上确认，才能当作结论。
- `experiments/m0/` 的代码保持原样，以便历史批次继续可以复验。
