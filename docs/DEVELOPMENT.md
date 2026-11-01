# 开发与运行

## 日常开发

项目命令只依赖 Python 3.9+ 标准库：

```bash
python3 scripts/project.py list
python3 scripts/project.py results early-roles
python3 -m unittest discover -s tests -v
```

测试只检查新入口的行为，不运行 Docker、模型 API 或历史实验。

Git 本地协作：每项改动单独开分支；提交代码、配置和需要分享的精简结果；完整运行目录继续保留在本机。之后连接团队远程仓库即可沿用这份目录。

## 实验环境

当前电脑的实验 Python 是 `experiments/m0/CooperBench/.venv/bin/python`，可继续使用，无需重新安装。

新机器先安装 Git、uv、Python 3.12 和 Docker。CooperBench 使用以下固定版本：

```bash
git clone https://github.com/cooperbench/CooperBench.git experiments/m0/CooperBench
git -C experiments/m0/CooperBench checkout 63b9d44d9f39a02fccf5bf0052db48a917a011fd
cd experiments/m0/CooperBench
uv sync --locked --python 3.12
```

当前 upstream checkout 没有本地源码修改。锁定的依赖来自该 checkout 自身的 `uv.lock`。上述步骤提供 benchmark 环境；历史实验还需要对应的 artifacts、image manifests 和 Docker snapshot images，不能只拷贝 checkpoint.json。

模型 key 通过 `OPENAI_API_KEY` 或 `TOGETHER_API_KEY` 环境变量提供。`.env.example` 仅说明变量名，不会自动加载。不要把真实 key 写进配置、脚本或 commit。

## 查看或恢复历史实验

先查看脚本与目录，默认仅打印命令，不执行：

```bash
python3 scripts/project.py show early-roles
python3 scripts/project.py run early-roles
```

确认确实要继续一个中断的已有批次后：

```bash
python3 scripts/project.py run early-roles --execute
```

这是 **resume**：复用已经完成的结果，只继续未完成部分。整批已完成时，不会产生新重复。它不是新实验入口。未完成部分可能调用付费 API；现有协议不设金额或请求次数上限，时间规则由冻结协议定义。

需要检查新改动涉及的集成路径时，才手动调用对应 offline 流程：

```bash
python3 scripts/project.py run early-roles --mode offline --execute
```

Offline 不调用模型 API，但会启动 Docker、恢复历史状态并评分，也需要本机原始资产。平时改 README、查询入口或不相关模块，不必重跑它。

## 新实验约定

- 新实现写进 `src/agent_rsi/`；新任务/模型参数用独立配置，避免再复制整套历史脚本。
- 每次运行使用新目录；不要覆盖已经完成的实验。
- 一个结果必须能对应到任务、模型、分工、实际提交、官方评分和账单。
- 先用小任务跑通新增路径；只有出现新改动、故障或具体疑问，才增加检查。
- 结果分享前移除机器路径和原始对话。当前 `results/early_roles_v1.json` 展示了可分享的最小内容。

