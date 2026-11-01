# CooperBench 首次试跑：环境准备方案

更新日期：2026-10-29  
适用环境：当前 Apple Silicon Mac；首轮使用本地 Docker，模型通过 Together API 调用。

## 1. 本阶段要完成什么

把一个真实任务的运行环境和评分流程准备好：**Docker 可用 → CooperBench 可导入 → 任务容器可启动 → 官方参考实现通过测试**。完成这些检查后，再开始一次 agent 试跑，记录 usage 并在结束后汇总成本。

当前进度（2026-10-29）：**首个 solo 端到端流程已完成：API 调用 → agent 修改代码 → 提交 patch → 官方评分 → 费用汇总。** 使用 Together 的 Qwen3.5-9B，一名 agent 同时处理两个 features。Feature 1 通过，Feature 6 有一项测试失败；流程已跑通，任务尚未全部解决。

| 本轮结果 | 记录 |
| --- | --- |
| Run ID | `solo_smoke_20261029T215741Z` |
| Agent 提交 | `Submitted`，非空 patch |
| Feature 1：备用搜索目录 | 47/47 tests passed |
| Feature 6：路径转换 | 48/49 tests passed，1 failed |
| 失败原因 | `FileSystemLoader` 漏接 `path_transform` 参数，调用报 `TypeError`；不是环境或评分故障 |
| 用时 | Agent 阶段约 214 秒；官方评分约 8.6 秒 |
| API 用量 | 89 次请求（含 1 次 preflight）；input 2,675,151 tokens，output 17,521 tokens |
| 本轮估算费用 | **$0.45915592，约 $0.46**；无缺失 usage 请求 |
| 包含此前调试的累计已知费用 | **$0.68084954，约 $0.68**；另有 6 次早期失败或中断请求未返回 usage，未计入此数 |

费用按 API usage 和当时记录的价率估算，最终以 provider 账单为准。本轮不设金额和调用次数上限。结果与累计成本保存在 [smoke_summary.json](experiments/m0/artifacts/smoke_summary.json)；[官方评分全文](experiments/m0/artifacts/solo_runs/solo_smoke_20261029T215741Z/evaluation.json)与[模型提交的 patch](experiments/m0/artifacts/solo_runs/solo_smoke_20261029T215741Z/solo/pallets_jinja_task/1621/f1_f6/solo.patch)均已保存。模型提交后未人工修改答案。

环境准备与 reference 评分也已完成。Docker Desktop 4.97.0、Python 3.12.15 和 CooperBench 锁定依赖均已安装；Docker CLI 与 Python SDK 已连通同一引擎；官方 ARM64 任务镜像正常运行。

| 已执行检查 | 结果 |
| --- | --- |
| Docker `hello-world` 与任务容器启动 | 均正常完成 |
| Feature 1 reference patch | 47/47 tests passed，其中新增功能测试 7/7 |
| Feature 6 reference patch | 49/49 tests passed，其中新增功能测试 9/9 |
| 任务级 `combined.patch` | 分别通过 feature 1 的 47 项和 feature 6 的 49 项测试 |

完整输出保存在 [reference 检查目录](experiments/m0/artifacts/reference_checks_20261029T204006Z/)，版本与验收摘要见 [environment_status.json](experiments/m0/artifacts/environment_status.json)。这些 reference 记录验证了 `tests/test_loader.py` 的评分流程。上表另行记录了模型的真实提交结果；checkpoint、双 agent 和 intervention 尚未验证。下文保留环境复现步骤，无需重新安装已完成的组件。

已创建本地环境配置 `experiments/m0/env.sh` 和可重复运行的 `experiments/m0/reference_checks.py`。框架配置保存在项目内，避免导入时写入用户全局配置目录。Docker 启动后，可在终端加载路径配置：

```bash
source "/Users/charleszheng/Documents/ChatGPT/Agent RSI/experiments/m0/env.sh"
```

首个任务固定为：

| 项目 | 选择 |
| --- | --- |
| Benchmark | CooperBench |
| Repository | `pallets_jinja_task` |
| Task ID | `1621` |
| Feature pair | `1,6` |
| Feature 1 | 为模板加载增加备用搜索目录 `fallback_searchpath` |
| Feature 6 | 在模板加载前支持路径转换 `path_transform` |
| 执行设置 | `solo`：一个 agent 完成两个 features |
| 当前 smoke 模型 | Together 的 `Qwen/Qwen3.5-9B`；原 GPT-OSS 请求的服务端解析错误单独保留 |
| 执行并发 | `1` |

该 pair 位于官方 flash 开发子集中，两项功能都涉及 `src/jinja2/loaders.py`，具有对应评分测试。选它用于首轮流程调试；之后仍需检查其他任务，才能判断 benchmark 是否适合正式实验。[任务目录](https://github.com/cooperbench/CooperBench/tree/main/dataset/pallets_jinja_task/task1621) · [flash 子集](https://github.com/cooperbench/CooperBench/blob/main/dataset/subsets/flash.json)

## 2. 环境组成与当前状态

| 组件 | 用途 | 当前状态 / 本轮选择 |
| --- | --- | --- |
| macOS ARM64 | 运行控制程序 | 已确认架构 |
| Homebrew、uv | 安装工具及管理 Python 环境 | 已找到命令；版本在执行时记录 |
| Docker Desktop | 在 Mac 上提供 Linux 容器引擎 | Desktop 4.97.0 / Engine 29.9.2；CLI、SDK 和容器检查均通过 |
| 宿主机 Python | 运行 CooperBench 控制程序 | uv 管理的 Python 3.12.15；依赖安装已完成 |
| 任务镜像 | 提供 Jinja 代码、Python 和测试环境 | 官方 ARM64 镜像已拉取；容器内 Python 3.11.15；reference 评分通过 |
| 模型 API | agent 生成代码与工具调用 | Together Qwen3.5-9B 已完成一次 solo 提交；usage 与响应均已记录 |

本任务不需要 GPU、CUDA 或本地模型权重。`solo` 设置不需要 Redis；首次不接入 Modal、GCP 或额外云服务。[官方运行要求与设置](https://github.com/cooperbench/CooperBench#settings-solo--coop--team)

宿主机 Python 3.12 与容器内 Python 3.11 用于不同部分，无需强行统一。任务镜像应保留官方运行环境。[任务 Dockerfile](https://github.com/cooperbench/CooperBench/blob/main/dataset/pallets_jinja_task/task1621/Dockerfile)

## 3. 安装并检查 Docker

### 3.1 安装 Docker Desktop

从 [Docker 官方 Mac 安装页](https://docs.docker.com/desktop/setup/install/mac-install/) 下载 **Apple silicon** 版本，安装到 Applications，然后打开 Docker Desktop，完成首次启动界面，等待引擎运行。

首次使用默认资源配置，保持串行运行。若任务出现内存不足，再依据日志调整分配；此时不预设需要大规模机器资源。

重新打开一个终端，执行：

```bash
docker version
docker info
docker run --rm hello-world
```

**通过标准：**`docker version` 同时显示 Client 和 Server，`docker info` 无连接错误，`hello-world` 正常完成。只安装 Python 的 `docker` 包不能替代 Docker Desktop。

若安装后仍提示找不到命令，先检查 Docker Desktop 的 CLI 安装设置；使用默认用户目录安装时，可在当前终端执行：

```bash
export PATH="$HOME/.docker/bin:$PATH"
docker version
```

### 3.2 让命令行和 Python 使用同一个引擎

CooperBench 的 agent 使用 Docker CLI，评分器使用 Python Docker SDK。当前上游 lockfile 中的 SDK 版本不能仅依靠 CLI context 自动找到 Docker Desktop socket，因此显式设置 `DOCKER_HOST`。

先查看 context，确认当前 context 指向本机 Docker Desktop：

```bash
docker context ls
docker context show
```

如果当前不是本机 Docker Desktop，而列表中存在 `desktop-linux`，再执行：

```bash
docker context use desktop-linux
```

然后在同一终端中设置并检查：

```bash
CB_ENV_CONTEXT="$(docker context show)"
export DOCKER_HOST="$(docker context inspect "$CB_ENV_CONTEXT" --format '{{.Endpoints.docker.Host}}')"
unset DOCKER_CONTEXT
docker info --format '{{.ID}}'
```

此配置仅用于已经确认的本机 `unix://` socket；若 context 指向远程或 TLS 引擎，先停止核对。保存显示的 daemon ID，稍后与 Python SDK 的输出比较。`DOCKER_HOST` 只在当前终端及其子进程有效；换终端后需要重新设置。[评分器连接方式](https://github.com/cooperbench/CooperBench/blob/main/src/cooperbench/eval/backends/docker.py) · [Docker SDK 7.1.0 环境变量处理](https://github.com/docker/docker-py/blob/7.1.0/docker/utils/utils.py)

## 4. 建立独立的实验目录和 Python 环境

目录安排如下，已有 proposal、实验 plan 和报告保持在原目录：

```text
Agent RSI/
├── environment_setup.md
└── experiments/
    └── m0/
        ├── CooperBench/        # 官方源码、同版本 dataset、项目 .venv
        ├── env.sh              # 本地路径与 Docker socket 配置
        ├── reference_checks.py # 官方参考实现评分入口
        ├── config/             # 框架本地配置
        └── artifacts/          # 版本信息、环境检查和评分结果
```

在终端执行以下命令。若 `CooperBench/` 已存在，先确认它是本实验的 checkout，再从该目录继续；不要覆盖现有工作。

```bash
cd "/Users/charleszheng/Documents/ChatGPT/Agent RSI"
mkdir -p experiments/m0/artifacts
cd experiments/m0
git clone https://github.com/cooperbench/CooperBench.git CooperBench
cd CooperBench
git checkout --detach 63b9d44d9f39a02fccf5bf0052db48a917a011fd
git rev-parse HEAD > ../artifacts/cooperbench_commit.txt

uv --version > ../artifacts/uv_version.txt
uv python install 3.12
uv sync --locked --python 3.12
uv run --locked python --version > ../artifacts/python_version.txt
uv run --locked cooperbench --help
```

从这里起，后续代码块都在 `experiments/m0/CooperBench/` 目录执行。

选择源码安装，是为了同时保留官方 dataset 和 vendored agent 依赖。本轮固定 commit 为 `63b9d44d9f39a02fccf5bf0052db48a917a011fd`，检查后源码没有改动。`--locked` 要求沿用现有依赖锁文件：若提示 lockfile 与项目不一致，保留错误并先排查，不直接删掉该参数或升级全部依赖。[项目依赖](https://github.com/cooperbench/CooperBench/blob/main/pyproject.toml) · [uv 的锁定与同步规则](https://docs.astral.sh/uv/concepts/projects/sync/)

检查 Python SDK 能否访问同一个 Docker 引擎：

```bash
uv run --locked python - <<'PY'
import docker

client = docker.from_env()
assert client.ping(), "Docker SDK 无法连接引擎"
print("Docker SDK daemon ID:", client.info()["ID"])
client.close()
PY
```

**通过标准：**环境安装成功、CLI 帮助可以打开、SDK ping 成功，且 daemon ID 与上一节一致。Python 依赖安装会联网下载软件包，但不会调用模型。

## 5. 检查任务文件并拉取镜像

使用此次 Git checkout 自带的 `dataset/`，使任务文件与 harness 一起记录版本。此路径下不再另行执行 `cooperbench prepare` 覆盖数据。

```bash
uv run --locked python - <<'PY'
from pathlib import Path

task = Path("dataset/pallets_jinja_task/task1621")
required = [task / "combined.patch", task / "runner.sh"]
for feature in (1, 6):
    required.extend(task / f"feature{feature}" / name
                    for name in ("feature.md", "feature.patch", "tests.patch"))
missing = [str(p) for p in required if not p.is_file()]
assert not missing, f"缺少任务文件: {missing}"
print("任务文件齐全：task1621，features 1 和 6")
PY

docker pull --platform linux/arm64 akhatua/cooperbench-pallets-jinja:task1621
docker image inspect akhatua/cooperbench-pallets-jinja:task1621 \
  --format '{{.Os}}/{{.Architecture}} {{json .RepoDigests}}' \
  > ../artifacts/task_image.txt
cat ../artifacts/task_image.txt

docker run --rm --platform linux/arm64 --entrypoint /bin/sh \
  akhatua/cooperbench-pallets-jinja:task1621 \
  -lc 'python --version && test -d /workspace/repo && test -x /usr/local/bin/runner.sh'
```

官方 tag 已核查提供 `linux/arm64` 和 `linux/amd64`。本机选择 ARM64，记录实际拉取的 digest。本轮完成前不重新拉取同名 tag，每次实验启动前复核 digest 与记录一致。原生 runner 仍按 tag 找镜像，仅保存 digest 不等于已经强制锁定；跨机器复现时需另行落实镜像版本固定。[镜像 manifest](https://hub.docker.com/v2/repositories/akhatua/cooperbench-pallets-jinja/tags/task1621) · [镜像命名方式](https://github.com/cooperbench/CooperBench/blob/main/src/cooperbench/utils.py)

**通过标准：**任务文件存在，镜像显示 `linux/arm64`，容器检查退出码为 0。此时只证明容器能启动，还没有证明评分器正常。

本次实际拉取记录见 [task_image.json](experiments/m0/artifacts/task_image.json)：`linux/arm64`，RepoDigest 为 `sha256:b51b95853994225afb5050ab32439561032b0e2f69c2fddea779fcd88f33da5c`。任务容器使用 Jinja base commit `a2920752fd111c2d52c88eb57487667b3cf0ea7b`。手动检查镜像时需要覆盖 ENTRYPOINT；镜像默认会把参数交给评分脚本。

## 6. 用官方参考实现验证评分流程

先运行两个 feature 各自的 reference patch，再运行任务级 `combined.patch` 对两套测试的评分。后者是整个任务的综合参考实现，不是我们生成的 agent 结果，也不代表研究实验的成功率。

以下调用只启动本地测试容器，不调用模型。评分 API 中 `agent_patch=None` 会自动选用该 feature 的参考实现，不能把它当作“未修改代码”的负例。[官方评分函数](https://github.com/cooperbench/CooperBench/blob/main/src/cooperbench/eval/sandbox.py)

已将检查保存为独立脚本，重复运行时执行：

```bash
source "/Users/charleszheng/Documents/ChatGPT/Agent RSI/experiments/m0/env.sh"
"$CB_M0_ROOT/CooperBench/.venv/bin/python" -u "$CB_M0_ROOT/reference_checks.py"
```

脚本使用自身所在目录定位 dataset，每次把完整 JSON 写入新的 `reference_checks_<UTC时间>/` 目录。参考评分阶段会跳过 LiteLLM 的远程价格表查询；这个设置仅用于不调用模型的检查，正式模型运行需重新核查价格与费用统计。

**通过标准：**两项单 feature 检查均为 `passed=true`，综合检查为 `both_passed=true`，无运行错误，JSON 中包含实际测试输出。保留全部 JSON，而不只记录“通过”。若接口报错，优先核对安装的 commit 与函数参数。

这里的 `timeout=600` 是传给上游评分器的参数，不能当作已经验证的严格整轮超时保护。若评分长时间没有进展，检查本轮测试容器及日志，不反复开启重复评分。

两个单 feature 的参考 patch 可能修改相同代码，不要求它们直接 merge 成功；用官方 `combined.patch` 检查综合参考结果。`test_solo` 对同一 patch 分别运行两套测试，没有把两份 tests.patch 同时叠加。其 JSON 中的 `feature2` 指 pair 的第二个 feature，本轮对应 feature ID 6。[参考 patch 的含义](https://github.com/cooperbench/CooperBench/blob/main/dataset/README.md)

## 7. 环境验收与下一步

环境准备完成时应能勾选：

- [x] Docker CLI 与 Python SDK 连接同一个正常运行的引擎。
- [x] CooperBench commit、Python/uv 版本和镜像 digest 已保存。
- [x] `task1621` 的 features 1、6 文件齐全。
- [x] 两个 feature 的参考实现评分通过。
- [x] `combined.patch` 对两套测试均通过，完整日志已保存。
- [x] 以上检查均未使用模型 API。

通过后，再准备模型接入：由本地环境变量读取 `TOGETHER_API_KEY`，先做一次小型工具调用检查，然后运行首个 `solo` 任务。API key 不写入本文、代码、Git 或结果文件；参考答案与评分测试不放进 agent 的任务输入。

按最新决定，`experiments/m0/run_solo_smoke.py` 使用仅记账模式：不设美元预算或模型调用次数上限，运行后汇总费用。`budget_guard.py` 保留此前的限制模式用于复现，当前入口传入 `enforce_limits=False`。21 项离线测试通过。

| 限制 | 实现方式 |
| --- | --- |
| 并发 1 | 整轮文件锁由 supervisor 和模型子进程共同持有 |
| API 请求记录 | preflight、solver 与重试全部计入；不设调用次数上限；SDK 自动重试关闭，500/502/503/504 最多显式重试 1 次，compaction 关闭 |
| 每次最多 16,384 output tokens | 请求传入 `max_tokens=16384`，避免较长代码修改被截断；提示分段修改；Qwen 本次关闭 thinking，temperature 0.7、top_p 0.8 |
| Agent 阶段最多 20 分钟 | 外层进程 watchdog；单 API 请求 timeout 90 秒；容器命令 timeout 120 秒 |
| 费用仅记账 | 有效 usage 按对应模型价率换算；失败或无 usage 的请求单独标记，不算作免费；不因美元额度停止 |
| 输入与执行隔离 | 只发送公开任务说明、公开代码及容器输出；agent 无宿主机挂载、无外网、无 API key；评分在提交后独立运行 |

价格于 2026-10-29 核查。Qwen3.5-9B 每百万 input tokens $0.17、output tokens $0.25。费用按 API 返回的 input/output token 数计算，属于价格表估算，最终以 provider 账单为准。未知 usage 请求保留完整 context 加最大 output 的估计值以供核对，不据此中断任务。[Together 官方模型与价格表](https://docs.together.ai/docs/serverless/models)

首轮 Qwen 试跑 `solo_smoke_20261029T213836Z` 成功完成 21 次 API 请求，已知费用为 $0.06002029；旧实现累计所有历史预留，在提交前提前停止，因此没有可评分 patch。该问题已修复。随后的 `solo_smoke_20261029T214702Z` 在取消预算前已启动，最终触及旧 30 次调用限制，未提交 patch，已知费用 $0.09743885；tracked 工作区 diff 已留作诊断，未当作正式提交评分。新配置从下一次启动生效。每次运行的 `run_config.json` 记录其实际设置。

`solo_smoke_20261029T215052Z` 已取消金额和调用次数限制，但多次整文件输出触及旧 4,096-token 输出上限，工具调用被截断。观察到重复后人工停止该轮，已知费用 $0.06409063，另有 1 次中断请求未返回 usage；容器已清理。之后提高单次输出上限至 16,384 tokens，并加入小段修改的提示。

`known_cost_usd` 为有 usage 请求的估算费用；`unknown_usage_requests` 为费用未确认的请求数。`budget_occupied_usd` 包括已知费用和未知请求预留；为兼容保留的 `reserved_upper_bound_usd` 只是历史预留之和，不能作为实际费用。运行会提醒 agent 预留提交时间，并保存 tracked 工作区 diff 供诊断；官方评分仅使用 runner 收集的提交 patch。

此前 GPT-OSS 使用 input $0.15 / output $0.60 每百万 tokens、每次预留 $0.0221184 的记录仍保留。各请求各自记录模型和价率，切换模型不重置账本。当前仅记账模式会记录 usage 超界等异常；缺失有效 usage 时会报错，避免把未知费用当作零。

首次从截图读取凭据的检查返回 HTTP 401。改用本机输入后，preflight 成功；进一步保存错误详情后确认 GPT-OSS 的正式请求返回 `harmony_parsing_failed`，包括一次重试。这个错误属于服务端生成输出解析失败，不是 key 无效，也不是 benchmark 评分失败。Qwen3.5-9B 使用同一 key 已正常完成多轮 tool calls。

运行下列命令后粘贴有效 Together key 并回车，key 只通过进程环境传递，不写入文件。若运行停止，脚本会保留 key 在该终端进程的内存中，等检查原因后按回车重试；输入 `q` 并回车退出：

```bash
bash "/Users/charleszheng/Documents/ChatGPT/Agent RSI/experiments/m0/run_with_together_key.sh"
```

每次运行的配置、逐次响应、trajectory 和 usage 保存在 `experiments/m0/artifacts/solo_runs/solo_smoke_<UTC时间>/`；跨运行预算记录在 `experiments/m0/artifacts/model_budget_ledger.json`。官方 runner 原有 cost/token 字段可能漏记格式错误响应，费用报告以独立账本为准。

非空 patch 正常提交后，脚本自动调用 `evaluate_solo_smoke.py` 进行官方评分。该阶段不调用模型、不继承 key，最多运行 660 秒。查看 `evaluation_summary.json` 的 `passed/failed/error/timeout`，以及 `pipeline_summary.json` 的 `pipeline_completed` 和 `task_solved`，区分流程完成与任务通过。

目前已有可复用的环境、reference 评分记录以及一次真实模型提交与评分。接下来可以在同一任务上建立双 agent 对照；checkpoint、intervention 和跨模型扩展仍需分别验证。

## 8. 常见问题

| 现象 | 优先检查 |
| --- | --- |
| `docker: command not found` | Docker Desktop 是否安装；CLI 路径是否进入当前终端 PATH |
| CLI 只有 Client，没有 Server | Docker Desktop 引擎是否已经启动 |
| CLI 可用，但 Python SDK 无法连接 | 当前终端的 `DOCKER_HOST` 是否指向相同 socket；比较 daemon ID |
| `uv sync --locked` 失败 | 保留完整报错，检查 Python 版本、vendored 依赖和 lockfile；不先绕过锁定 |
| 镜像架构不符或 `exec format error` | 镜像是否为 `linux/arm64`；拉取命令是否指定正确平台 |
| 参考实现的评分失败 | 先看 patch 应用、依赖安装和 pytest 输出，再判断数据/镜像版本是否匹配 |
| 测试超时或容器退出 | 查看本轮容器日志、可用内存与 Docker 资源配置；串行排查 |

整个准备阶段只清理能够明确对应本轮实验的临时容器，不使用全局 `docker system prune`，避免影响其他项目。
