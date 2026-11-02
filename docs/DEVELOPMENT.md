# 开发与运行

## 日常命令

只依赖 Python 3.9+ 标准库：

```bash
python3 scripts/project.py census                # M0 运行的失败原因统计
python3 scripts/project.py census --export       # 用本地 M0 数据重新导出 results/m0_runs.csv
python3 -m unittest discover -s tests            # 测试不运行 Docker 或模型 API
```

## 环境准备

需要 Git、uv、Python 3.12 和 Docker。CooperBench 固定在以下版本：

```bash
git clone https://github.com/cooperbench/CooperBench.git experiments/m0/CooperBench
git -C experiments/m0/CooperBench checkout 63b9d44d9f39a02fccf5bf0052db48a917a011fd
cd experiments/m0/CooperBench
uv sync --locked --python 3.12
```

实验使用 `experiments/m0/CooperBench/.venv/bin/python`。

API key 写在仓库根目录的 `.env` 中（已被 gitignore）：

```text
OPENAI_API_KEY=...
```

不要把 key 写进配置、脚本或 commit。

## 运行 S1

1. 启动 Redis（coop 模式下 agent 之间发消息需要）：

   ```bash
   docker run -d --name cooperbench-redis -p 6379:6379 redis:7
   ```

2. 预览：显示任务数、已完成数和将要执行的命令，不会运行。

   ```bash
   python3 scripts/project.py s1 configs/s1_main.json
   ```

3. 运行（调用付费 API）。`--workers` 是并行数，Docker 内存 8 GB 时建议 4。

   ```bash
   python3 scripts/project.py s1 configs/s1_main.json --execute --workers 4
   ```

4. 重新统计结果，不运行 Docker：

   ```bash
   python3 scripts/project.py s1 configs/s1_main.json --report
   ```

Docker 镜像（`akhatua/cooperbench-<repo>:task<id>`，每个 1–3 GB）首次运行时会自动拉取。

结果写入配置中的 `output` 目录（`runs/` 已被 gitignore），包括 CooperBench 原始日志、每个 agent 的单独评分，以及汇总的 `rows.csv` 和 `summary.json`。已完成的任务会跳过，中断后重新执行即可继续；因 API 报错失败的任务会重试。

预筛完成后，用配置里写好的规则生成主实验任务列表：

```bash
python3 scripts/project.py s1 configs/s1_screen.json --freeze configs/s1_main.json
```

长时间运行时，电脑睡眠会让任务卡住。可以用 `caffeinate -is` 包住运行命令，并保持插电。

## 注意事项

- gpt-6-luna 的调用参数在 `configs/agents/luna_mini_swe.yaml`，与 M0 一致。`reasoning_effort` 必须通过 `extra_body` 传递，否则 litellm 会把它去掉，API 会拒绝带工具的请求。
- CooperBench 的一个问题：coop agent 没提交 PR 就结束时，会被提醒继续提交，但对话里留下的 `exit` 消息会让下一次 API 调用失败。S1 runner 在发送请求前修正这条消息（`s1.api_messages`），没有修改 CooperBench 代码。
- `experiments/m0/` 的代码与已完成的批次绑定，不要修改。新代码写在 `src/agent_coordination/`。

## 恢复 M0 批次

M0 批次依赖本机的历史 artifacts 和 Docker 快照，只能在原机器上恢复。默认只打印命令：

```bash
python3 scripts/project.py show early-roles
python3 scripts/project.py run early-roles
```

确认要继续一个中断的批次时，加 `--execute`。这只会补跑未完成的部分，不会产生新的重复。加 `--mode offline` 可以不调用模型 API，只恢复状态并评分。
