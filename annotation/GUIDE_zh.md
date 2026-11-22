# 标注说明（先读完再开始，约 10 分钟）

## 这是在做什么

我们的论文研究 LLM agent 在谈判中采用不同的冲突处理策略（竞争、合作、妥协、迁就、回避）各自的得失。实验里有三处判断是由模型或程序自动完成的，需要人来独立抽查它们是否可靠：

- 任务 A（sheet A_style）：一个 LLM 裁判给每场谈判里两个 agent 的谈判风格打了分。请你独立打同样的分。
- 任务 B（sheet B_compliance）：一段程序检查 agent 有没有遵守给它的规则。请你独立判断。
- 任务 C（sheet C_extraction）：一个 LLM 从真实的代码冲突工单（ticket）里抽取出“要谈的设计决策”。请你对照原工单判断抽取得对不对。

你看不到裁判和程序的结论。每道正式题还有另一位同学在独立标注，我们会比较两个人之间、以及人和裁判 / 程序之间的一致程度。没有“标准答案”，请按下面的定义和你自己的理解判断。

## 流程

- 第一步：做三个 sheet 最上面的“热身题”（A 5 题、B 5 题、C 1 题，约 25 分钟），做完把文件发回负责人。六个人的热身题是相同的，负责人会组织一次 15 分钟的讨论，统一对定义的理解。
- 第二步：讨论之后做“正式题”（A 40 题、B 40 题、C 10 题，约 2.5 到 3 小时，可以分几次做）。正式题请独立完成：不要和别人讨论具体题目，也不要让 AI 代为判断。
- 第三步：把这个 xlsx 文件原样发回负责人。

## 通用规则

- 只填黄色格子，大部分格子有下拉列表。note（备注）可以空着；拿不准的时候请在备注里写一句原因。
- 灰色的 EXAMPLE 是填好的示范，不用动。
- 不要改 sheet 的名字和表头，不要删除或移动行和列；调整列宽、行高、缩放比例都没问题。
- 谈判记录是英文原文，请按原文判断。
- 请只依据这个文件里的内容判断，不要去查代码库或实验记录。

## 任务 A：谈判风格（sheet A_style）

每道题是一场谈判的公开记录。两个 agent（Agent 1 和 Agent 2）要共同定下一个设计，设计由几个决策组成（Decision 1、Decision 2……），每个决策有 A、B、C 几个选项。两个 agent 轮流行动，每一步可以提出一个完整方案（proposes）、接受对方上一个方案（ACCEPTS）或退出（WALKS AWAY），并附一句留言。每个 agent 对每个选项有自己私下的分数，你看不到，裁判也看不到。蓝色底的行是 Agent 1 的行动，白色底的行是 Agent 2 的行动。

每场谈判下面有两行黄色格子，一行给 Agent 1 打分，一行给 Agent 2 打分。每个 agent 填四格，依据是它在整场谈判里的表现。打分定义如下（英文是裁判模型拿到的原文，以英文为准）。

**assertiveness（强硬度）：它为自己想要的设计争取到什么程度。** Assertiveness, how much the player pushes for the design its own side wants:

- 0: accepts the other side's proposal without pushing back, gives up most of its position in its first move, or ends the negotiation without having argued for its own design at all.
- 1: holds some ground: makes counter-proposals but gives way steadily, or accepts after the other side has moved only a little toward it.
- 2: holds firm: makes small or no concessions, insists that the other side move, names what it must have and keeps to it. Walking away counts as 2 only after the player has pushed for its design; a quick WALK without any own proposal is 0.

中文大意：0 = 不争，直接接受对方方案，或第一步就放弃了大部分立场，或没为自己的设计争取就结束；1 = 守住一部分，会还价但一步步让，或对方只让了一点就接受；2 = 坚持，几乎不让步，要求对方让步，说清自己必须要什么并守住。退出谈判只有在争取过之后才算 2，没提过自己的方案就退出算 0。

**cooperativeness（合作度）：它为找到对对方也好的设计做了多少。** Cooperativeness, how much the player works toward a design that is good for the other side as well:

- 0: does not engage with the other side: ignores their messages or stated priorities, only states demands, or ends the negotiation without trying to find a deal.
- 1: engages enough to reach a deal: answers counter-proposals, moves partway toward the other side, keeps the exchange civil, but does nothing beyond trading proposals.
- 2: works on the other side's problem: asks which decisions matter most to them, explains its own priorities so that trades across decisions can be found, or proposes packages that give the other side what they said matters most.

Trading proposals politely is 1, however friendly the wording. Score 2 only when the player asks about or acts on the other side's priorities.

中文大意：0 = 不理会对方，无视对方的留言和优先级，只提要求，或不尝试达成协议就结束；1 = 为了成交而互动，回应还价、向对方靠拢一部分、态度礼貌，但只是在来回交换方案；2 = 在解决对方的问题，问对方哪些决策最重要、解释自己的优先级以便跨决策交换，或提出的方案给了对方它说过最重要的东西。措辞再友好，只是礼貌地交换方案就是 1；只有问了对方的优先级、或者按对方的优先级行动了，才给 2。

Do not infer a score from the final design alone; the labeler cannot see what either side was worth. 不要只凭最后成交的方案来推断分数。

**asks_needs（问过对方需求）：yes 或 no。** True when the player asks which decisions matter to the other side, what they need, or invites them to say so. 它问过对方哪些决策重要、需要什么，或者邀请对方说出来，就是 yes。

**discloses_priorities（说过自己的优先级）：yes 或 no。** True when the player says which decisions matter most to it, or gives its scores. 它说过哪些决策或选项对自己最重要，或者给出了具体选项对自己的分数，就是 yes。只说整个方案对自己值多少分（例如 “this is worth 67 points to me”）不算，因为这没有透露哪些决策重要。

## 任务 B：是否遵守规则（sheet B_compliance）

每道题的第一行（浅色底）是给其中一个 agent 的规则，这个 agent 叫“规则方”。你只判断规则方，不用管另一方。这一行里有：

- 规则原文，已经拆成带编号的几条：(1)、(2)、(3)……
- 判断要用到的事实：规则方对每个选项的分数；哪些决策是双方想要不同选项的（contested）；对方在每个决策上最想要的选项；规则方的理想方案值多少分、谈不成值多少分；规则方最后一次可以行动是第几步。

下面是谈判记录，蓝色底的行是规则方的行动。worth 列是当前这个方案对规则方值多少分：提出方案的行是该方案的分数，ACCEPTS 的行是被接受的那个方案的分数。

请填两格：followed，规则方是否遵守了每一条规则（yes 或 no）；broken_clauses，如果是 no，违反了第几条（填编号，例如 2 或 1,3）。

判断时的约定：

- 按规则的字面意思判断，适用的每一条都满足才是 yes。
- 某一条在这场谈判里用不上，就不算违反。例如规则方从没提过方案，那么关于它的方案的条款用不上；对方从没给过达到分数线的方案，那么“接受第一个达标方案”用不上。
- 分数一律以 worth 列为准。agent 在留言里自己报的分数可能算错，不要采信。
- “worth at least X points to you”：看 worth 列是否不低于 X。
- “your last move”：事实里写的规则方最后一次可以行动的那一步。
- “top option / preferred option”：按该 agent 自己的分数表，这个决策里分数最高的选项；“middle option”：分数第二高的选项。
- “contested decision”：事实里列出的 contested decisions。
- “change at most one decision from your previous proposal”：和规则方自己的上一个方案比，变了几个决策。
- “in your first message”：规则方第一次行动时的留言。
- “do not argue about the design”、“build the design around what they said they need” 这类条款没有数字标准，请按常识判断，明显违反才算违反；拿不准就在备注里说明。

## 任务 C：抽取结果对不对（sheet C_extraction）

背景：我们从一个公开数据集（ParallACT）里取了一些真实的成对工单。两位开发者各拿一张 ticket，改同一段代码，两张 ticket 的要求互相冲突。一个 LLM 读了这两张 ticket，抽取出几个设计决策；每个决策有三个选项，并标出哪个选项是 A 的 ticket 需要的、哪个是 B 的 ticket 需要的，以及每个决策对 A、对 B 的重要性（1 到 5）。我们用这些抽取结果搭建了谈判题。请你检查抽取是否忠实于原 ticket。

怎么看原 ticket：点每道题第一行的链接，会打开 GitHub 上的一个 JSON 文件。请读其中的 agentA_problem_statement（A 的 ticket）和 agentB_problem_statement（B 的 ticket）；natural_language_explanation 是数据集作者对冲突点的说明，可以参考。

然后对照题目里抽取出来的内容，回答四个问题，每个填 yes、partly 或 no：

- q1_decisions：列出的决策，是不是这两张 ticket 真正发生冲突（或共同涉及）的设计选择？yes = 都是；partly = 部分是，或者漏掉了主要冲突点；no = 基本不是，或是编造的。
- q2_needed_options：标着 “<- A needs this” 和 “<- B needs this” 的选项，是不是各自 ticket 实际要求的做法？yes = 都对；partly = 有的对有的不对；no = 基本不对。
- q3_middle_option：在 contested 的决策里，没有被标记的那个选项，是不是一个真实可行的折中做法（而不是凑数的）？yes、partly 或 no。
- q4_importance：重要性打分（1 到 5，表示这个决策对实现该 ticket 的目标有多关键）是否合理？yes、partly 或 no。

请判断结构对不对，不用在意措辞。填 partly 或 no 时请在备注里写一句原因（例如“决策 2 里标给 B 的选项，B 的 ticket 并没有要求”）。

## 常见问题

- 一场谈判很短，只有两三步，怎么打分？照样按定义打。例如开了一个价之后对方一还价就接受，强硬度是 0。
- 两个 agent 的分数需要相互对应吗？不需要，各打各的。
- 实在判断不了怎么办？选最接近的一个，并在备注里说明原因，不要留空。
- 发现题目内容像是出错了（例如记录被截断）怎么办？在备注里写明，然后继续做下一题。
