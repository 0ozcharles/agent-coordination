# 研究计划：先诊断失败，再按原因选择干预

取代原 Proposal 中"Consolidate 是否优于 StrongShare"这一主问题。原文的形式化定义（V(o,a)、从同一个 checkpoint 配对分叉）保留，在第三阶段使用。M0 的经验见 [M0_RETROSPECTIVE.md](M0_RETROSPECTIVE.md)。

## 研究问题

> Multi-agent 系统失败时，**原因是什么**？**针对不同原因**，哪种组织调整在相同计算预算下最划算？这个原因能否在运行过程中提前识别出来？

拆成三个依次依赖的问题。每一阶段都有独立交付物，下一阶段失败不影响前面的成果：

| 阶段 | 问题 | 交付物 | 继续往下的条件 |
|---|---|---|---|
| **S1 失败普查** | 协作失败分别由什么原因造成，各占多少？ | 带反事实归因的失败分布 | 组织类原因（非能力不足）占失败的 ≥30% |
| **S2 对症干预** | 针对某个原因设计的干预，是否比不对症的干预更能挽回失败？ | 原因 × 干预 的效果矩阵 | 至少一个原因存在显著优于其他干预的对症方案 |
| **S3 在线诊断** | 能否从运行中途可见的信号预测原因并选择干预？ | selector 及其在 held-out 任务上的结果 | — |

即使 S2 或 S3 失败，S1 本身也是一个可发表的结果：在可控条件下，对 multi-agent coding 失败原因做反事实归因。MAST 只给出了 taxonomy，没有做反事实。

## 失败原因的操作化定义

关键做法：**单独评测每个 agent 自己的 patch**。这一步不调用模型，只多跑测试，因此几乎没有额外成本。设 agent i 负责 feature i，对每次 coop 运行评测以下四项：

- `own_i`：agent i **发布的** patch（PR 或 push 的分支）单独应用到 base 上，跑 feature i 的测试
- `local_i`：agent i 在容器里**实际写出的**代码（相对 base 的完整 diff，包括未发布的 commit 和未提交的修改）。CooperBench 只评分已发布的工作，所以要单独记录这一项
- `merged`：两份 patch 合并后跑全部测试（CooperBench 官方方式）
- `solo`：同一任务的 Solo 运行中，feature i 是否通过（同模型，重复 3 次）
- 结构信号：两份 diff 的 hunk 重叠、是否完全相同、消息记录中有没有约定接口

| 原因 | 判定规则（每个失败的 feature 单独判定；一次运行的主标签按 integration > submission > context > capability 取） | 对应你的假设 |
|---|---|---|
| `capability` | `own_i` 失败，**且** Solo 也从未通过 feature i | 任务与 agent 能力不匹配 |
| `context` | `own_i` 失败，**但** Solo 通过过 feature i | 上下文边界：拆分后缺少完成自己部分所需的信息 |
| `integration` | 每个 `own_i` 都通过，`merged` 失败或冲突 | 合并更好：各自都对，合不起来 |
| `submission` | `own_i` 失败，但 `local_i` 通过：写出来了，没有按规则发布 | （新增）流程 / 交付失败 |
| `coordination`（标记，不互斥） | 两份 patch 完全相同、一个 agent 的 patch 通过了对方的 feature（替对方干活），或两份 diff 改了同一批文件 | 协调不足 |

LLM 运行有随机性，单次 `own_i` 失败可能只是噪声。因此 `capability` 和 `context` 按任务汇总重复运行来判定，例如 Solo 3 次里通过 ≥2 次，才算"Solo 能做"。

`context` 和 `coordination` 的判定先人工复核 20 条 trace，再决定是否自动化，可以借用 MAST 的标注流程。`census` 命令只能从 M0 的历史评分记录中粗略判别；S1 的标签由 `s1` 命令基于逐 agent 评测给出。

## S1 失败普查

1. **新 runner 从任务原始状态启动**，不再依赖历史 checkpoint。只跑 CooperBench 原生的 coop（即 Keep）和 solo 两个设置。
2. **任务筛选（运行前固定规则）**：从 CooperBench 中按仓库分层抽 60 个 feature pair，每个做 2 次 coop 和 2 次 solo 的预筛。保留 4 次运行中至少通过过一个 feature、但不是全部通过的任务。选定后冻结列表。
3. **主实验**：预筛保留的任务 × {coop, solo} × 3 次重复，每次 coop 都做逐 agent 评测。实际保留 53 个 pair，见下文预筛结果。
4. **成本估算**：按 gpt-6-luna 的实测费用，预筛约 240 次、主实验约 180 次，合计不到 $15。主要成本在工程实现。
5. **输出**：各原因占比、按仓库和任务类型拆分的分布、人工复核的一致性。

## S2 对症干预

只对 S1 中实际占比高的原因设计干预。干预从任务一开始就作为协议生效，不需要 checkpoint：

| 原因 | 对症干预 | 预期 |
|---|---|---|
| integration | Integrator：一个 agent 负责最终合并产物 | M0 已有初步证据 |
| coordination | 文件 / 函数级认领锁，开工前先约定接口 | |
| context | 加强共享（StrongShare）：开放对方的进度与 diff | |
| capability | 更换为更强的模型，或增加预算；组织调整预计无效 | 作为负对照 |

**核心检验**：对 S1 中被诊断为原因 X 的任务，对症干预 X 的挽回率是否高于其他干预。Solo 是必须超过的基线，所有条件使用相同预算。

## S3 在线诊断与选择

此时才需要 checkpoint 分叉和原 Proposal 的 V(o,a) 框架。从中途可见的信号（文件重叠、自测结果、消息）预测原因，再选择干预。基线包括：最佳固定动作、always-Solo、always-Integrator、直接让 LLM 选择。按 base task 划分训练和测试集。

## 防止再次漂移的规则

- 每个 study 在运行前写下**一句话问题**和**决策规则**，放在它的配置里。
- 一个 study 只用一个模型、一种评分规则。逐 agent 评分和合并评分都记录，主指标事先选定。
- 每个 study 都包含 Solo。
- study 运行中不新增条件；有新想法就等当前 study 结束后另开一个。
- 少于 20 个 base task 时不下结论；重复次数可以少，任务数要多。按任务做聚类置信区间。
- 天花板或地板任务（所有条件全部通过或全部失败）单独报告，不计入比较。

## 工程状态

S1 runner 已实现：[s1.py](../src/agent_coordination/s1.py)，配置示例 [configs/s1_smoke.json](../configs/s1_smoke.json)。

- 直接调用 CooperBench 原生的 coop / solo，从任务原始 base 启动，不依赖 M0 的历史 checkpoint。
- coop 运行结束后，用 CooperBench 的 `test_solo` 单独评测每个 agent 的 patch（`agent_eval.json`），不调用模型。
- 按上表规则打标签，输出 `rows.csv` 和 `summary.json`；`--report` 可以在不跑 Docker 的情况下重新打标签。
- 费用按 gpt-6-luna 定价注册到 litellm，记录在 CooperBench 的 `result.json` 中。

- 每个 agent 发布前，runner 会把它的本地 diff 存为 `local_<agent>.patch`；如果和发布内容不同，再单独评分（`local_eval.json`）。官方评分保持 CooperBench 原样。
- gpt-6-luna 的调用参数与 M0 一致（`configs/agents/luna_mini_swe.yaml`）：`reasoning_effort` 为 `none`、`temperature` 0.7、`top_p` 0.8。`reasoning_effort` 必须通过 `extra_body` 传递，否则会被 litellm 丢掉，导致 API 拒绝带工具的请求。
- 模型 API 报错的运行标为 `infra_error`，并自动重试一次，不计入失败原因。

### Smoke 结果（2026-11-01，llama_index 17070 f1+f3，$0.015）

- coop：两个 agent 发布的 patch 各自通过自己的 feature，合并时冲突，标为 **integration**。负责 feature 3 的 agent 也通过了 feature 1（替对方干活）。
- solo：本地写出 129 行代码，两项都通过，但没有 push 或开 PR，官方评分为空，标为 **submission**。两次 smoke 的 solo 都没有发布。
- 结论：CooperBench 当前版本"只评分已发布的工作"，这会大幅压低官方分数。如果不单独记录本地工作，这类失败会被误判为能力不足。

### 预筛结果（2026-11-01，58 个 pair × {coop, solo} × 2 次，$1.93）

样本：从 `lite.json` 按仓库分层抽样，每仓库最多 6 个 pair，种子 20261101（[scripts/make_s1_screen.py](../scripts/make_s1_screen.py)）。232 次运行全部完成，`infra_error` 为 0。逐行结果见 [results/s1_screen_rows.csv](../results/s1_screen_rows.csv)。

| | 官方通过 | 说明 |
|---|---|---|
| coop | 32 / 116 | 84 次失败：integration 58、capability 15、context 10、submission 1；另有 10 次带 coordination 标记 |
| solo | 25 / 116 | 91 次官方失败中，44 次本地写出了两项都通过的代码但没有发布，47 次确实没写出来；算上本地工作为 69 / 116 |

初步观察（预筛只有 2 次重复，不作为结论）：

1. **integration 占 coop 失败的 69%，其中 55 / 58 是 git 合并冲突**，不是语义上不兼容：两个 agent 各自的 patch 都能通过自己的 feature，只是改了同一段代码。如果合并完美，coop 的上限约为 90 / 116。
2. **solo 近一半的官方失败是没有发布**，不是能力不足。CooperBench 官方分数会明显低估 solo。
3. capability 和 context 合计只占 coop 失败的 30%。

冻结规则（运行前写入 `configs/s1_screen.json`）：保留至少通过过一个 feature、但不是每次都全部通过的 pair。结果保留 53 / 58 个，剔除 3 个从未通过、2 个总是通过的 pair，写入 [configs/s1_main.json](../configs/s1_main.json)（53 个 pair × 2 个设置 × 3 次 = 318 次运行，按预筛单价估计约 $2.7）。

运行中修复的问题：coop agent 没开 PR 就结束时，CooperBench 会提醒它提交，但把 `exit` 消息留在了对话历史中，OpenAI 拒绝这个角色，agent 随即崩溃。这会把 submission 失败误记为 `infra_error`。runner 在发送请求前把 `exit` 转成对应工具调用的结果（`s1.api_messages`），没有修改 `experiments/m0/` 下的代码。

待办：

1. ~~跑通 `s1_smoke`~~ 已完成。
2. ~~预筛并冻结 S1 主实验任务列表~~ 已完成。
3. 跑 S1 主实验：`python3 scripts/project.py s1 configs/s1_main.json --execute --workers 4`。
4. 人工复核 20 条 coop trace，检验 `context` 和 `coordination` 标签是否符合实际。
5. S2 的第一个干预可以先离线做：用更好的合并方式（如按函数合并或让模型解决冲突）重新合并已有的两份 patch，再跑测试。不需要重跑 agent，成本很低，能直接检验 integration 失败能挽回多少。
