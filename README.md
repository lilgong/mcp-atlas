# MCP-Atlas 部署与评测手册

本文描述如何在一台目标机器上完成以下工作：

1. 获取指定分支代码。
2. 安装运行依赖。
3. 配置模型端点和 MCP 凭证。
4. 准备官方评测所需的文件数据、MongoDB 数据和云端账号数据。
5. 启动 MCP runtime 与 completion 服务。
6. 验证工具连通性、数据内容和任务级隔离。
7. 生成模型轨迹并评分。
8. 查看日志、停止服务和排查常见问题。

全文命令默认在 Linux shell 中执行。除非命令明确写了 `cd services/mcp_eval`，
否则工作目录都是仓库根目录。

---

## 1. 系统结构

一次评测包含三个常驻角色和若干按任务创建的容器：

| 角色 | 作用 | 默认地址 |
| --- | --- | --- |
| 共享 MCP runtime | 提供云端账号读取工具和公开 API 工具 | `http://localhost:1984` |
| completion 服务 | 调用模型、执行多轮工具调用、管理任务容器 | `http://localhost:3000` |
| 轨迹与评分脚本 | 读取任务 CSV，调用 completion，保存轨迹并评分 | 不监听端口 |
| task-local 容器 | 承载 filesystem、Git、Memory、CLI、Desktop Commander、代码执行等工具 | 不发布固定宿主端口 |
| task-network 容器 | 承载 Arxiv、PubMed 等需要联网且会写本地缓存的工具 | Docker 临时分配回环端口 |
| task-Mongo 容器 | 为包含 MongoDB 工具的任务恢复一份独立数据库 | 不发布宿主端口 |

调用路径如下：

```text
mcp_completion_script.py
        |
        v
completion 服务（PORT）
        |
        +--> 共享 MCP runtime（MCP_SHARED_PORT）
        |
        +--> 当前任务的 local/network/Mongo 容器
```

隔离规则：

- 每道题拥有独立的 `/data` 副本，题内写入不会修改 fixture 源目录。
- task-local 容器使用 `network=none`，且不会收到 `.env` 中的云端凭证。
- MongoDB 数据为每道题单独恢复，题目结束后容器和 socket volume 被删除。
- Arxiv/PubMed 容器允许联网，但不会收到共享云端账号凭证。
- Airtable、GitHub、Google Workspace、Lara、Notion、Slack 的共享账号写工具被拒绝。
- filesystem、Git、Memory、MongoDB、Desktop Commander 等任务本地写工具可以在一次性容器中执行。
- `e2b-server_run_code` 不进入评测工具列表。
- `mcp-code-executor_install_dependencies` 不进入评测工具列表。
- 模型请求、工具调用、容器生命周期和容器日志写入 `MCP_RUNTIME_LOG_DIR`。

运行时镜像只包含程序和依赖，不包含评测 `/data`。文件 fixture 和 Mongo fixture
都在部署时显式指定。

---

## 2. 机器要求

### 2.1 必需软件

- 64 位 Linux。
- Git。
- Docker Engine。
- GNU Make。
- Python 3。
- `uv`。
- `curl`、`jq`、`unzip`。

Docker 安装说明：

<https://docs.docker.com/engine/install/>

uv 安装说明：

<https://docs.astral.sh/uv/getting-started/installation/>

Linux 可使用 uv 官方安装器：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装后重新打开 shell，检查命令：

```bash
git --version
docker version
docker info
make --version
python3 --version
uv --version
curl --version
jq --version
unzip -v
```

当前用户必须能够直接执行 Docker 命令：

```bash
docker run --rm hello-world
```

如果这里出现 Docker socket 权限错误，需要先配置当前用户的 Docker 权限。不要依赖
在每一条运行命令前临时添加 `sudo`，因为 completion 服务会在后台调用 Docker CLI。

### 2.2 资源

- Docker 至少需要数 GB 可用磁盘空间；构建 runtime 还会下载 Python、Node 和 MCP 依赖。
- 并发评测会同时存在多个任务容器。并发 20 时应预留足够内存和 CPU。
- 默认每个任务 runtime 的限制为 3 GB、2 CPU；Mongo 为 1 GB、1 CPU。这是容器上限，
  不代表每个容器会持续占满。
- 如果机器资源不足，先减少评测脚本并发做冒烟验证；正式运行再使用计划的并发值。

---

## 3. 获取代码

```bash
git clone \
  --branch codex/task-isolation-runtime \
  https://github.com/lilgong/mcp-atlas.git

cd mcp-atlas
```

确认分支和提交：

```bash
git branch --show-current
git log -1 --oneline
```

预期分支：

```text
codex/task-isolation-runtime
```

不需要执行 `git submodule update`。任务使用的 Git 仓库由文件 fixture 中的
`repos/git_submodule_info.csv` 指定 URL 和 commit，任务启动时按需物化。

---

## 4. 准备部署材料

仅执行 `git clone` 不能得到以下内容，需要单独准备：

| 材料 | 用途 |
| --- | --- |
| `.env` | 模型端点、裁判端点、第三方 MCP 凭证、端口和运行参数 |
| `MCP-Atlas.csv` | 官方任务、claims、enabled tools 和参考轨迹 |
| `mcp-atlas-runtime:latest` | 运行全部 MCP server 的软件镜像 |
| 文件 fixture | 为每道题提供 `/data` |
| Mongo fixture 镜像 | 为每道 Mongo 题提供独立数据库 |
| Airtable/Notion/Calendar/Slack 数据 | 让有状态云端读取任务与官方 claims 对齐 |

### 4.1 任务 CSV

官方任务 CSV 不随当前 Git 分支提交。把可信来源的 CSV 放到：

```text
services/mcp_eval/MCP-Atlas.csv
```

检查文件：

```bash
cd services/mcp_eval

test -f MCP-Atlas.csv
sha256sum MCP-Atlas.csv

uv run python -c '
import pandas as pd
p = "MCP-Atlas.csv"
df = pd.read_csv(p, nrows=2)
required = {"TASK", "PROMPT", "TRAJECTORY", "GTFA_CLAIMS", "ENABLED_TOOLS"}
missing = required - set(df.columns)
print({"columns": list(df.columns), "missing": sorted(missing)})
raise SystemExit(1 if missing else 0)
'

cd ../..
```

记录 CSV 的 SHA256。比较不同机器上的结果时，应使用相同任务 CSV。

### 4.2 复制 `.env`

从模板开始：

```bash
cp env.template .env
chmod 600 .env
```

也可以从可信配置机器传输：

```bash
scp config-user@config-host:/absolute/path/to/mcp-atlas/.env ./.env
chmod 600 .env
```

`.env` 包含真实凭证，不要提交，不要贴进日志或聊天记录。

---

## 5. 配置 `.env`

### 5.1 completion 使用的模型

completion 服务启动时要求 `LLM_API_KEY` 非空。

OpenAI-compatible/LiteLLM 模型配置：

```dotenv
LLM_API_KEY=<completion-api-key>
LLM_BASE_URL=<openai-compatible-base-url>
MCP_COMPLETION_MODEL=openai/<model-name>
```

Pangu 模型配置：

```dotenv
LLM_API_KEY=<non-empty-value>
LLM_BASE_URL=<fallback-openai-compatible-url>

PANGU_API_KEY=<pangu-api-key>
PANGU_API_URL=<pangu-chat-completions-url>
PANGU_TIMEOUT=1800
PANGU_MAX_RETRIES=5
PANGU_RETRY_DELAY=3

MCP_COMPLETION_MODEL=pangu/<checkpoint-name>
```

如果 `PANGU_API_KEY` 或 `PANGU_API_URL` 留空，代码分别回退到 `LLM_API_KEY`
和 `LLM_BASE_URL`。

### 5.2 裁判模型

```dotenv
EVAL_LLM_MODEL=<litellm-model-name>
EVAL_LLM_API_KEY=<evaluator-api-key>
EVAL_LLM_BASE_URL=<evaluator-base-url>
```

`EVAL_LLM_API_KEY` 留空时，评分脚本会回退到 `LLM_API_KEY`。
`EVAL_LLM_BASE_URL` 留空时，LiteLLM 使用对应 provider 的默认地址；它不会读取
`LLM_BASE_URL`。

### 5.3 端口

默认端口：

```dotenv
MCP_SHARED_HOST=0.0.0.0
MCP_SHARED_PORT=1984
MCP_SERVER_URL=http://localhost:1984

HOST=0.0.0.0
PORT=3000
SERVER_URL=http://localhost:3000
```

`MCP_SHARED_PORT` 与 `MCP_SERVER_URL` 必须一致。

`PORT` 与 `SERVER_URL` 也必须一致。比如把 completion 改到 3500：

```dotenv
HOST=0.0.0.0
PORT=3500
SERVER_URL=http://localhost:3500
```

只改 `PORT` 不改 `SERVER_URL` 会导致轨迹脚本继续请求 3000。

如果所有进程都在同一台机器，可以把两个监听 host 设置为 `127.0.0.1`。如果需要从
其他机器访问，使用 `0.0.0.0`，并通过防火墙限制来源。

task-local 和 task-Mongo 容器没有需要配置的宿主端口。

### 5.4 隔离 runtime

```dotenv
MCP_TASK_ISOLATION_ENABLED=true
MCP_SHARED_AGENT_IMAGE=mcp-atlas-runtime:latest
MCP_TASK_AGENT_IMAGE=mcp-atlas-runtime:latest

MCP_TASK_DATA_DIR=<后续生成的文件fixture绝对路径>
MCP_TASK_MONGO_IMAGE=mcp-task-mongo:official-video-game-store-v1

MCP_TASK_SANDBOX_CONCURRENCY=20
MCP_TASK_SANDBOX_STARTUP_TIMEOUT=180
MCP_TASK_SANDBOX_MEMORY=3g
MCP_TASK_SANDBOX_CPUS=2.0
MCP_TASK_MONGO_MEMORY=1g
MCP_TASK_MONGO_CPUS=1.0
MCP_SANDBOX_OWNER=
```

保持：

```dotenv
MCP_TASK_ISOLATION_ENABLED=true
```

`MCP_SANDBOX_OWNER` 可以留空。代码会用 hostname 和 completion 端口生成 owner，
并在服务启动时清理相同 owner 遗留的任务容器。

### 5.5 轨迹参数

```dotenv
MCP_COMPLETION_INPUT=MCP-Atlas.csv
MCP_COMPLETION_OUTPUT=MCP-Atlas-<model-label>.csv
MCP_COMPLETION_NUM_TASKS=
MCP_COMPLETION_CONCURRENCY=20
USE_SYSTEM_PROMPT_IN_COMPLETION=
```

说明：

- `MCP_COMPLETION_INPUT` 相对于 `services/mcp_eval/`。
- `MCP_COMPLETION_OUTPUT` 只写文件名；结果自动进入 `completion_results/`。
- `MCP_COMPLETION_NUM_TASKS` 留空表示运行全部任务。
- `MCP_COMPLETION_CONCURRENCY` 是同时请求 completion 服务的任务数。
- `USE_SYSTEM_PROMPT_IN_COMPLETION` 留空表示不添加项目内置 system prompt。

### 5.6 日志目录

从仓库 Makefile 启动时，以下相对路径最终位于 `services/mcp_eval/`：

```dotenv
TOKEN_LOG_DIR=token_usage_log
EVAL_TOKEN_LOG_DIR=token_usage_log
VERIFY_TOKEN_LOG_DIR=token_usage_log
PANGU_LOG_DIR=completion_results
PANGU_LOG_PATH=
MCP_RUNTIME_LOG_DIR=completion_results/runtime_logs
LOG_LEVEL=INFO
```

也可以使用绝对路径。运行用户必须对这些目录有写权限。

### 5.7 MCP server 选择和凭证

默认：

```dotenv
ENABLED_SERVERS=
```

留空时会启用无需 key 的默认 server，并根据非空凭证自动启用其他 server。

如果设置：

```dotenv
ENABLED_SERVERS=calculator,wikipedia,github
```

则只启动列出的 server，不再自动补充。

主要凭证：

| 服务 | `.env` 变量 |
| --- | --- |
| Airtable | `AIRTABLE_API_KEY` |
| Alchemy | `ALCHEMY_API_KEY` |
| Brave Search | `BRAVE_API_KEY` |
| E2B | `E2B_API_KEY` |
| Exa | `EXA_API_KEY` |
| GitHub | `GITHUB_TOKEN` |
| Google Maps | `GOOGLE_MAPS_API_KEY` |
| Google Workspace | `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_REFRESH_TOKEN` |
| Lara Translate | `LARA_ACCESS_KEY_ID`、`LARA_ACCESS_KEY_SECRET` |
| National Parks | `NPS_API_KEY` |
| Notion | `NOTION_TOKEN` |
| Oxylabs | `OXYLABS_USERNAME`、`OXYLABS_PASSWORD`、`OXYLABS_SCRAPER_URL` |
| Slack | `SLACK_MCP_XOXC_TOKEN`、`SLACK_MCP_XOXD_TOKEN` |
| Twelve Data | `TWELVE_DATA_API_KEY` |
| Weather Data | `WEATHER_API_KEY` |

如果配置 Oxylabs，`OXYLABS_SCRAPER_URL` 不能留空。留空会使 Oxylabs server
启动后不注册工具。

配置 `E2B_API_KEY` 不会开放 `e2b-server_run_code`；该工具仍由评测策略拒绝。

MongoDB 由 task-Mongo 容器提供。官方评测部署中保持：

```dotenv
MONGODB_CONNECTION_STRING=
```

不要为本流程启动宿主机 MongoDB。

修改 `.env` 后，需要重启共享 MCP runtime 和 completion 服务。运行中的进程不会
自动重新读取文件。

---

## 6. 准备云端账号数据

官方任务依赖 Airtable、Notion、Google Calendar 和 Slack 中的确定内容。仅配置
凭证但不导入数据，会出现“工具调用成功但 claims 不匹配”。

### 6.1 Airtable

1. 打开 `data_exports/airtable_database_online_link.txt` 中的共享链接。
2. 登录用于评测的 Airtable 账号。
3. Copy base。
4. 创建能够读取该 base 的 token。
5. 写入 `.env`：

```dotenv
AIRTABLE_API_KEY=<token>
```

### 6.2 Notion

1. 解压并导入：

```text
data_exports/mcp-atlas-notion-data.zip
```

2. 创建 Notion integration。
3. 把导入的页面和数据库 share 给 integration。
4. 写入 `.env`：

```dotenv
NOTION_TOKEN=<token>
```

### 6.3 Google Calendar

1. 解压：

```text
data_exports/calendar_mcp_eval_export.zip
```

2. 将 `.ics` 导入 OAuth 授权账号的主日历 `primary`。
3. 在 Google Cloud 项目启用 Calendar API。
4. 确认 refresh token 对应同一个账号。
5. 写入 `.env`：

```dotenv
GOOGLE_CLIENT_ID=<client-id>
GOOGLE_CLIENT_SECRET=<client-secret>
GOOGLE_REFRESH_TOKEN=<refresh-token>
```

### 6.4 Slack

1. 阅读：

```text
docs/slack_free_method.md
```

2. 使用 `data_exports/slack_mcp_eval_export.zip` 导入评测 workspace。
3. 按文档处理消息时间戳和用户映射。
4. 写入 `.env`：

```dotenv
SLACK_MCP_XOXC_TOKEN=<xoxc-token>
SLACK_MCP_XOXD_TOKEN=<xoxd-token>
```

Slack 免费 workspace 会隐藏超过可见时间窗口的消息，因此必须在评测前做数据探针。

---

## 7. 准备 runtime 镜像

`MCP_SHARED_AGENT_IMAGE` 与 `MCP_TASK_AGENT_IMAGE` 必须指向满足以下契约的镜像：

- label `mcp-atlas.runtime=true`
- label `mcp-atlas.data-contract=external-data-v1`
- label `mcp-atlas.contains-fixture=false`
- 声明 `/data` volume

### 7.1 从镜像文件导入

在镜像来源机器：

```bash
docker save mcp-atlas-runtime:latest -o mcp-atlas-runtime.tar
sha256sum mcp-atlas-runtime.tar
```

传输 `mcp-atlas-runtime.tar`，目标机器执行：

```bash
sha256sum mcp-atlas-runtime.tar
docker load -i mcp-atlas-runtime.tar
```

检查镜像：

```bash
docker image inspect mcp-atlas-runtime:latest \
  --format '{{.Id}} {{json .Config.Labels}} {{json .Config.Volumes}}'
```

多台机器复测同一批任务时，应记录并比较镜像 ID。

### 7.2 从当前分支构建

如果没有镜像文件：

```bash
make build-atlas-runtime \
  ATLAS_RUNTIME_IMAGE=mcp-atlas-runtime:latest
```

构建需要访问系统软件源、npm 和 Python package index。

构建完成后执行同样的 inspect 命令：

```bash
docker image inspect mcp-atlas-runtime:latest \
  --format '{{.Id}} {{json .Config.Labels}} {{json .Config.Volumes}}'
```

---

## 8. 准备文件 fixture

官方文件数据已经以普通文件形式保存在：

```text
services/agent-environment/data
```

选择一个仓库外、当前用户可写的绝对目录，例如：

```text
/home/your-user/mcp-atlas-fixtures
```

把 `your-user` 替换为目标机器的实际用户名。

创建父目录：

```bash
mkdir -p /home/your-user/mcp-atlas-fixtures
```

将仓库内的官方文件数据打包为 fixture-v2：

```bash
uv run --project services/mcp_eval python \
  scripts/prepare_task_data_fixture.py \
  --source services/agent-environment/data \
  --output /home/your-user/mcp-atlas-fixtures/official-data-v2 \
  --fixture-id official-data-v2
```

注意：

- `--output` 指向的目录必须尚不存在。
- 重新打包时使用另一个输出目录和 fixture ID。
- packager 会生成 `.atlas-fixture.json`。
- packager 会计算内容 SHA256。
- packager 会拒绝源目录中的 symlink。
- Git 仓库按 `repos/git_submodule_info.csv` 中的 URL/commit 物化，不需要 clone submodule。

检查 manifest：

```bash
python3 -m json.tool \
  /home/your-user/mcp-atlas-fixtures/official-data-v2/.atlas-fixture.json
```

把实际绝对路径写入 `.env`：

```dotenv
MCP_TASK_DATA_DIR=/home/your-user/mcp-atlas-fixtures/official-data-v2
```

任务启动时，代码会：

1. 校验 fixture contract、ID 和 SHA。
2. 为当前任务创建独立工作目录。
3. 复制 fixture。
4. 按 manifest 物化需要的 Git 仓库。
5. 只信任 manifest 中精确列出的 Git 路径。
6. 把任务副本挂载为 `/data:rw`。
7. 任务结束后删除副本。

---

## 9. 准备 Mongo fixture 镜像

官方 Mongo dump 位于：

```text
data_exports/mongo_dump_video_game_store-UNZIP-FIRST.zip
```

### 9.1 解压

```bash
unzip \
  data_exports/mongo_dump_video_game_store-UNZIP-FIRST.zip \
  -d data_exports
```

检查 BSON：

```bash
find \
  data_exports/mongo_dump_video_game_store-UNZIP-FIRST/mongo_dump_video_game_store/video_game_store \
  -maxdepth 1 -type f -name '*.bson' -print
```

### 9.2 构建镜像

```bash
make build-task-mongo \
  MONGO_FIXTURE_DUMP=data_exports/mongo_dump_video_game_store-UNZIP-FIRST/mongo_dump_video_game_store \
  MONGO_FIXTURE_DB=video_game_store \
  MONGO_FIXTURE_ID=official-video-game-store-v1 \
  TASK_MONGO_IMAGE=mcp-task-mongo:official-video-game-store-v1
```

构建器会：

1. 定位 dump 中的 `video_game_store`。
2. 检查至少存在一个 BSON collection。
3. 计算 BSON/metadata 内容 SHA。
4. 把数据库规范化为任务内逻辑库 `store`。
5. 写入 fixture ID、逻辑库和内容 SHA label。

检查镜像：

```bash
docker image inspect mcp-task-mongo:official-video-game-store-v1 \
  --format '{{.Id}} {{json .Config.Labels}}'
```

把镜像名写入 `.env`：

```dotenv
MCP_TASK_MONGO_IMAGE=mcp-task-mongo:official-video-game-store-v1
```

宿主机不运行 MongoDB 服务，也不开放 27017。Mongo 工具调用时会自动创建容器并
恢复 `store`，题目结束后自动删除。

如果评测自有数据，把 `MONGO_FIXTURE_DUMP`、`MONGO_FIXTURE_DB`、
`MONGO_FIXTURE_ID` 和镜像 tag 换成对应值即可。

---

## 10. 安装 Python 依赖

同步评测环境：

```bash
uv sync --project services/mcp_eval --frozen
```

运行 router 单元测试或在宿主机开发 agent-environment 时，再同步：

```bash
uv sync --project services/agent-environment --frozen
```

runtime 镜像自身的 Python/Node/MCP 依赖已经在镜像内，不依赖宿主机虚拟环境。

---

## 11. 启动服务

需要两个长期运行的终端。第三个终端用于检查和评测。

### 11.1 终端 1：共享 MCP runtime

仓库根目录：

```bash
make run-docker-host
```

该命令读取根目录 `.env`，使用 host network 启动
`MCP_SHARED_AGENT_IMAGE`，并监听 `MCP_SHARED_PORT`。

保持终端运行。

检查：

```bash
curl -sS http://localhost:1984/ | jq
curl -sS http://localhost:1984/enabled-servers | jq
curl -sS -X POST http://localhost:1984/list-tools | jq 'length'
```

如果修改了 `MCP_SHARED_PORT`，把命令中的 1984 换成实际端口。

### 11.2 终端 2：completion 服务

仓库根目录：

```bash
make run-mcp-completion
```

该服务监听 `.env` 中的 `HOST`/`PORT`，并通过 Docker CLI 为每个任务创建和销毁
task-local、task-network 和 task-Mongo 容器。

保持终端运行。

检查默认端口：

```bash
curl -sS http://localhost:3000/health | jq
```

如果使用 3500：

```bash
curl -sS http://localhost:3500/health | jq
```

health 输出中应看到：

- `status: healthy`
- `task_isolation_enabled: true`
- 正确的 `shared_mcp_url`

### 11.3 端口检查

```bash
ss -ltnp | grep -E ':(1984|3000|3500)\b'
```

只检查实际配置的端口。端口被占用时，先确认占用进程，再选择未使用端口并同步修改
`.env` 中的成对配置。

---

## 12. 部署验收

### 12.1 共享 server 状态

```bash
curl -sS http://localhost:1984/enabled-servers | jq
```

检查计划使用的 server 是否为 `OK`。server 不在列表通常表示：

- 对应凭证为空。
- `ENABLED_SERVERS` 显式列表没有包含它。
- server 启动失败。

### 12.2 公开 API 和云端读取工具

进入评测目录：

```bash
cd services/mcp_eval
```

单独检查一个 server：

```bash
uv run python test_server_v1.py \
  --base-url http://localhost:1984 \
  --server brave-search
```

对准备参与评测的共享 server 逐项执行。示例：

```bash
for server in \
  airtable alchemy brave-search calculator clinicaltrialsgov-mcp-server context7 \
  ddg-search exa fetch github google-maps google-workspace lara-translate \
  met-museum national-parks notion open-library osm-mcp-server oxylabs \
  slack twelvedata weather weather-data whois wikipedia
do
  uv run python test_server_v1.py \
    --base-url http://localhost:1984 \
    --server "$server"
done
```

没有配置凭证、没有计划参与评测的 server 可以从循环中去掉。不要把 API 失败的
server 保留为已启用状态后直接跑正式任务。

### 12.3 云端账号数据

分别检查：

```bash
for server in airtable notion slack google-workspace
do
  uv run python test_server_v1.py \
    --base-url http://localhost:1984 \
    --server "$server" \
    --data-only
done
```

期望每项输出 `DATA OK`。`API FAIL` 表示连通性或凭证问题；`DATA BAD` 表示 API
可调用但账号数据与 claims 需要的内容不一致。

回到仓库根目录：

```bash
cd ../..
```

### 12.4 任务级隔离

确保以下配置均已生效：

- `MCP_TASK_DATA_DIR`
- `MCP_TASK_MONGO_IMAGE`
- `MCP_TASK_ISOLATION_ENABLED=true`
- Airtable 和 Notion 读取凭证
- 共享 MCP runtime 正在运行

执行：

```bash
make check-task-isolation
```

该检查会验证：

- 所有 task-local server 能加载。
- Arxiv/PubMed task-network server 能加载。
- 云端写工具不可见、不可调用。
- local 容器没有云端凭证。
- local 容器使用 `network=none`。
- filesystem 写入只在当前任务可见。
- Mongo 写入只在当前任务可见。
- 20 个 task sandbox 可以并发启动。

期望最后输出：

```text
PASS: all isolated servers loaded; cloud writes blocked; filesystem and Mongo writes destroyed; 20 task sandboxes started concurrently
```

### 12.5 Mongo 官方内容

下面的检查通过隔离客户端启动 task-Mongo，查询官方 dump 中
`Delivery Logistics` 的确定内容，并在退出时销毁容器：

```bash
cd services/mcp_eval

uv run python - <<'PY'
import asyncio
from mcp_completion.mcp_client import IsolatedMCPClient


async def main():
    async with IsolatedMCPClient(
        task_id="mongo-official-content-check",
        shared_url="http://localhost:1984",
        enabled_tools=["mongodb_count"],
    ) as client:
        result = await client.call_tool(
            "mongodb_count",
            {
                "database": "store",
                "collection": "Delivery Logistics",
                "query": {
                    "Delivery Status": "Delivered",
                    "Order Date": {
                        "$gte": {"$date": "2022-06-01T00:00:00.000Z"},
                        "$lt": {"$date": "2022-07-01T00:00:00.000Z"},
                    },
                },
            },
        )
        print(result)
        if result.is_error or "Found 10 documents" not in str(result):
            raise SystemExit("Mongo official content check failed")


asyncio.run(main())
PY

cd ../..
```

期望结果包含：

```text
Found 10 documents
```

### 12.6 completion 端到端冒烟

下面使用默认 completion 端口 3000；如果 `.env` 配置为 3500，就把 URL 中的
3000 换成 3500。把 JSON 中的模型名换成实际模型：

```bash
curl -sS -X POST \
  http://localhost:3000/v2/mcp_eval/run_agent \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "pangu/your-checkpoint",
    "taskId": "deployment-filesystem-smoke",
    "messages": [
      {
        "role": "user",
        "content": "What is the first word of the file at /data/Barber Shop.csv?"
      }
    ],
    "enabledTools": ["filesystem_read_text_file"],
    "maxTurns": 20
  }' | jq
```

期望：

- HTTP 请求成功。
- 返回中出现 filesystem 工具调用。
- 最终答案包含 `Customer`。
- `MCP_RUNTIME_LOG_DIR` 中出现本任务的模型、工具和 sandbox 事件。

---

## 13. 生成模型轨迹

进入评测目录：

```bash
cd services/mcp_eval
```

### 13.1 少量任务冒烟

```bash
uv run python mcp_completion_script.py \
  --model "<model-name>" \
  --input "MCP-Atlas.csv" \
  --output "MCP-Atlas-smoke.csv" \
  --num-tasks 2 \
  --concurrency 2
```

结果：

```text
services/mcp_eval/completion_results/MCP-Atlas-smoke.csv
```

先检查：

```bash
uv run python -c '
import pandas as pd
p = "completion_results/MCP-Atlas-smoke.csv"
df = pd.read_csv(p)
print(df[["TASK", "errors", "trajectory_time", "num_retry"]].to_string(index=False))
'
```

### 13.2 正式运行

命令行方式：

```bash
uv run python mcp_completion_script.py \
  --model "<model-name>" \
  --input "MCP-Atlas.csv" \
  --output "MCP-Atlas-<model-label>.csv" \
  --concurrency 20
```

也可以只设置 `.env` 后运行：

```bash
uv run python mcp_completion_script.py
```

脚本启动时会打印最终生效的：

- 模型。
- 输入文件。
- 输出文件。
- 任务数。
- 并发。
- completion URL。
- MCP URL。
- system prompt 开关。
- retry 次数。

默认会根据共享 runtime 的 enabled server 过滤任务。被过滤的任务不会进入模型请求；
排除信息写入 `excluded_tasks.txt`。只有明确要测试不可用 server 的失败行为时才使用
`--no-filter`。

如果输出文件已经存在，脚本会读取其中的 `TASK` 并跳过已完成任务。因此：

- 进程中断后用同一个输出文件名可继续跑。
- 想完整重跑时使用另一个输出文件名。
- 不要把两组不同模型、fixture 或参数写进同一个输出文件。

完成后输出 CSV 同时包含：

- 官方任务字段。
- 模型最终回答。
- 完整对话。
- 实际工具轨迹。
- 错误。
- 耗时。
- retry 次数。

---

## 14. 评分

确保 `.env` 已配置裁判：

```dotenv
EVAL_LLM_MODEL=<litellm-model-name>
EVAL_LLM_API_KEY=<evaluator-api-key>
EVAL_LLM_BASE_URL=<evaluator-base-url>
```

在 `services/mcp_eval` 执行：

```bash
uv run python mcp_evals_scores.py \
  --input-file "completion_results/MCP-Atlas-<model-label>.csv" \
  --model-label "<model-label>" \
  --concurrency 20 \
  --pass-threshold 0.75
```

可选参数：

- `--evaluator-model`：覆盖 `EVAL_LLM_MODEL`。
- `--output-dir`：评分输出目录，默认 `evaluation_results`。
- `--num-tasks`：只评前 N 条。
- `--concurrency`：裁判并发。
- `--pass-threshold`：通过率阈值。

输出：

```text
evaluation_results/
├── scored_<model-label>.csv
├── coverage_stats_<model-label>.csv
└── coverage_histogram_<model-label>.png
```

比较模型时至少记录：

- Git commit。
- 任务 CSV SHA256。
- runtime image ID。
- 文件 fixture contract、ID、SHA。
- Mongo image ID 和 fixture SHA。
- completion 模型及参数。
- system prompt 开关。
- 轨迹并发。
- 裁判模型。
- pass threshold。
- 被 server filter 排除的任务数。

---

## 15. 日志和产物

默认路径均在：

```text
services/mcp_eval/
```

轨迹：

```text
completion_results/MCP-Atlas-<model-label>.csv
```

runtime 日志：

```text
completion_results/runtime_logs/<YYYY-MM>/
├── model_calls_<YYYYMMDD>.jsonl
├── tools_<YYYYMMDD>.jsonl
├── sandbox_<YYYYMMDD>.jsonl
├── service_<YYYYMMDD>.jsonl
└── containers/<task-id>/*.log
```

Pangu provider 原始响应：

```text
completion_results/pangu_response_<YYYYMMDD>.jsonl
```

评分：

```text
evaluation_results/
```

日志包含：

- 模型请求开始、完成和失败。
- provider retry。
- 工具名、参数、耗时和错误。
- 任务路由。
- runtime image ID。
- fixture ID 和 SHA。
- 任务容器启动、ready、停止。
- 容器 stdout/stderr。

配置中的 token、key、secret、password 会在 runtime JSONL 中替换为
`<redacted>`。仍然不要把完整日志提交到 Git。

检查容器是否正常回收：

```bash
docker ps -a --filter label=mcp-atlas.task-sandbox=true
docker volume ls --filter label=mcp-atlas.task-sandbox=true
```

正常完成后不应持续积累任务容器和 volume。

---

## 16. 停止、重启与清理

### 16.1 正常停止

在 completion 终端按 `Ctrl-C`。

在共享 MCP runtime 终端按 `Ctrl-C`。共享容器使用 `--rm`，停止后自动删除。

### 16.2 清理孤儿任务容器

仓库根目录：

```bash
make cleanup-task-sandboxes
```

该命令只清理与当前 `MCP_SANDBOX_OWNER` 匹配的 MCP-Atlas task sandbox。

### 16.3 修改配置后重启

修改以下任意内容后，停止并重新启动两个常驻服务：

- `.env` 凭证。
- `ENABLED_SERVERS`。
- 端口。
- runtime image tag。
- 文件 fixture 路径。
- Mongo fixture image。
- completion 模型端点。

修改任务 CSV不需要重启服务，但应为结果选择不同输出文件名并记录新的 CSV SHA。

---

## 17. 故障排查

### 17.1 completion 请求仍发往 3000

现象：

```text
connection refused http://localhost:3000
```

检查 `.env`：

```dotenv
PORT=3500
SERVER_URL=http://localhost:3500
```

修改后重启 completion，并重新启动轨迹脚本。

### 17.2 共享 MCP 端口不一致

检查：

```dotenv
MCP_SHARED_PORT=1984
MCP_SERVER_URL=http://localhost:1984
```

检查监听：

```bash
curl -sS http://localhost:1984/enabled-servers | jq
```

### 17.3 runtime image 不满足契约

现象包含：

```text
does not implement the fixture-free external-data-v1 contract
```

检查：

```bash
docker image inspect mcp-atlas-runtime:latest \
  --format '{{json .Config.Labels}} {{json .Config.Volumes}}'
```

确认 `.env` 的共享和任务镜像名都指向正确 runtime。

### 17.4 缺少文件 fixture

现象包含：

```text
MCP_TASK_DATA_DIR is required
```

或：

```text
digest mismatch
```

检查：

```bash
python3 -m json.tool \
  /absolute/path/to/fixture/.atlas-fixture.json
```

确保 `.env` 使用绝对路径，目录没有在打包后被修改。

### 17.5 缺少 Mongo fixture

现象包含：

```text
MCP_TASK_MONGO_IMAGE is required for MongoDB tasks
```

检查：

```bash
docker image inspect "$MCP_TASK_MONGO_IMAGE"
```

注意：普通 shell 不会自动读取 `.env`。如果命令中的变量为空，直接写镜像名：

```bash
docker image inspect mcp-task-mongo:official-video-game-store-v1
```

### 17.6 Mongo 查询数据库名不一致

任务 runtime 会把 Mongo 工具参数中的 `database` 强制改写为 `store`。fixture
构建时的 `MONGO_FIXTURE_DB=video_game_store` 只表示 dump 中的源目录名。

检查 runtime 日志中的实际 arguments 和 `task_mongo_fixture_restored` 事件。

### 17.7 Git 报 dubious ownership

确保文件 fixture 包含：

```text
repos/git_submodule_info.csv
```

任务启动时会生成 `/data/.atlas-gitconfig`，只信任 manifest 中物化的仓库。不要在
容器中设置 `safe.directory=*`。

### 17.8 Oxylabs 显示 OK 但没有工具

检查：

```dotenv
OXYLABS_USERNAME=<value>
OXYLABS_PASSWORD=<value>
OXYLABS_SCRAPER_URL=<queries-endpoint>
```

修改后重启共享 MCP runtime。

### 17.9 server 显示 OK，但调用返回 401/403/429

- 401：检查 token 是否有效、是否发给正确端点。
- 403：检查账号权限、API 是否启用、资源是否 share 给 integration。
- 429：检查配额和同账号并发。
- server 已启用但凭证不可用时，依赖它的任务仍可能进入评测并降低分数。修复凭证或从
  `ENABLED_SERVERS` 中去掉该 server。

### 17.10 云端工具能调用但 `DATA BAD`

凭证可用不代表账号数据正确。重新执行第 6 节导入，并用第 12.3 节逐项探测。

### 17.11 输出文件没有重新运行任务

`mcp_completion_script.py` 会跳过输出 CSV 中已存在的 `TASK`。使用另一个
`--output` 文件名。

### 17.12 task container 持续累积

检查：

```bash
docker ps -a --filter label=mcp-atlas.task-sandbox=true
docker volume ls --filter label=mcp-atlas.task-sandbox=true
```

确认 completion 正常退出，并执行：

```bash
make cleanup-task-sandboxes
```

如果仍然累积，检查 `completion_results/runtime_logs` 中的 sandbox stop/reaper 事件。

### 17.13 Docker 权限错误

completion 必须能直接运行：

```bash
docker ps
docker run --rm hello-world
```

修复当前用户的 Docker socket 权限后，重启 completion。

---

## 18. 完整执行清单

首次部署按顺序执行：

```text
[ ] 安装并验证 Git、Docker、Make、Python、uv、curl、jq、unzip
[ ] clone codex/task-isolation-runtime
[ ] 放置 services/mcp_eval/MCP-Atlas.csv 并记录 SHA256
[ ] 创建并填写 .env
[ ] 导入 Airtable、Notion、Calendar、Slack 数据
[ ] docker load 或 make build-atlas-runtime
[ ] 生成 official-data-v2 文件 fixture
[ ] 解压 Mongo dump 并构建 task-Mongo fixture 镜像
[ ] 在 .env 填入 MCP_TASK_DATA_DIR 和 MCP_TASK_MONGO_IMAGE
[ ] uv sync --project services/mcp_eval --frozen
[ ] 终端 1：make run-docker-host
[ ] 终端 2：make run-mcp-completion
[ ] 检查 /enabled-servers 和 /health
[ ] 验证云端 API 和四个云端账号数据
[ ] make check-task-isolation
[ ] 验证 Mongo 官方内容
[ ] completion filesystem 端到端冒烟
[ ] 运行 2 条轨迹冒烟
[ ] 运行正式轨迹
[ ] 运行评分
[ ] 记录 commit、CSV SHA、image ID、fixture ID/SHA 和参数
```

运行中的三个终端：

```text
终端 1：make run-docker-host
终端 2：make run-mcp-completion
终端 3：检查、mcp_completion_script.py、mcp_evals_scores.py
```

---

## 19. 相关文件

| 文件 | 作用 |
| --- | --- |
| `env.template` | 环境变量模板 |
| `Makefile` | runtime 构建、服务启动、隔离检查和清理入口 |
| `scripts/build_atlas_runtime.py` | 构建 fixture-free MCP runtime |
| `scripts/prepare_task_data_fixture.py` | 生成内容寻址文件 fixture |
| `scripts/build_task_mongo_fixture.py` | 从 mongodump 构建 task-Mongo fixture 镜像 |
| `scripts/run_shared_mcp.py` | 启动共享 MCP runtime |
| `services/mcp_eval/mcp_completion_script.py` | 生成模型轨迹 |
| `services/mcp_eval/mcp_evals_scores.py` | claim coverage 评分 |
| `services/mcp_eval/test_server_v1.py` | 公开 API 和云端账号数据探针 |
| `services/mcp_eval/scripts/check_task_isolation.py` | 任务容器隔离验收 |
| `services/mcp_eval/scripts/cleanup_task_sandboxes.py` | 清理同 owner 孤儿容器 |
| `data_exports/` | 官方云端账号数据和 Mongo dump |
| `docs/slack_free_method.md` | Slack 导入和可见窗口处理 |
