# MCP-Atlas 部署与评测

在真实 MCP server 上评测模型的工具使用能力。本文只讲**如何部署、如何评测、如何排障**。

> 一句话流程：`git clone` → 配 `.env` → 构建无数据运行时 → 准备外部 fixture → 启动共享 MCP → 生成轨迹 → 打分。

---

## 0. 组件与端口

| 组件 | 说明 | 端口 |
| --- | --- | --- |
| **MCP runtime** | 跑共享 MCP server 的常驻容器（docker） | `MCP_SHARED_PORT`（默认 1984） |
| **completion 服务** | 连接 LLM 与 MCP server，跑 agentic loop | `PORT`（默认 3000） |
| **评测脚本** | 生成轨迹 / 打分 / 验收（`services/mcp_eval/`） | — |

数据流：`mcp_completion_script.py` → completion 服务(`SERVER_URL`) → 隔离路由 → 共享云端只读容器 / 按任务一次性容器 → 各 MCP server。

依赖：`docker`、`uv`、`jq`、`python3.10+`。completion 服务运行用户必须有 Docker 权限。给 docker 至少 8GB 内存（并发 20 建议更多）。

### 0.1 当前隔离边界

- `1984` 的常驻 `mcp-atlas-runtime:<version>` 只承载云端/公开读取工具，保留 Yibu 适配和云端凭证。
- filesystem、Git、Memory、CLI、Desktop Commander、代码执行和 MongoDB 按任务启动一次性容器；不向这些容器传入 `.env` 或云端凭证，而且容器使用 `network=none`。
- Arxiv/PubMed 有本地下载缓存且需要联网，使用另一类无云凭证的按任务容器。
- Airtable、GitHub、Google Workspace、Lara Memory、Notion、Slack 的云端写工具在工具展示和实际调用两层都被拒绝；这些服务的读取工具仍开放。
- 宿主机通过 `docker exec` 访问只监听容器 loopback 的本地 MCP 服务；Mongo 走仅挂载给该题两个容器的私有 volume 内 Unix socket，不需要给代码容器开放 Docker bridge。
- 每题结束后容器、匿名数据层和私有 Mongo socket volume 都会销毁。服务异常退出留下的同 owner 容器和 volume 会在下次启动时回收。
- 模型每轮请求/响应/失败/token、工具调用、容器生命周期和容器 stdout/stderr 都写到 `MCP_RUNTIME_LOG_DIR`。
- 运行时镜像不含任何 `/data` fixture。外部数据先校验内容 SHA，再为每题复制一份并挂载到 `/data:rw`；写入只影响该题副本。

这不是把 36 个 server 全复制进一个带凭证的任务容器。那样 CLI/代码执行可以直接读取云端 token，不构成安全隔离。

---

## 1. Clone

```bash
git clone https://github.com/lilgong/mcp-atlas.git
cd mcp-atlas
git submodule update --init --recursive   # clone 时若没带 --recursive
```

> 子模块非必需。Git 任务按外部 fixture 的
> `repos/git_submodule_info.csv` 固定 SHA，并在任务副本中物化仓库。

---

## 2. 配置 `.env`

```bash
cp env.template .env
```

**最省事的做法是从已有机器直接拷 `.env`**（里面有 20+ 个第三方 key）：

```bash
scp <老机器>:/path/to/mcp-atlas/.env ./.env
```

拷过来后按新机器核对这几项：

| 变量 | 说明 |
| --- | --- |
| `PANGU_API_URL` / `LLM_BASE_URL` | 模型推理端点，新机器必须**能连通**（先 `curl` 测） |
| `MONGODB_CONNECTION_STRING` | 本机 mongo 用 `mongodb://localhost:27017`（配合 host 网络，见 §4） |
| `MCP_SHARED_HOST` / `MCP_SHARED_PORT` | 常驻 MCP runtime 的监听地址和宿主端口 |
| `MCP_SERVER_URL` | completion 访问常驻 MCP runtime 的地址；应与上面的端口一致 |
| `PORT` / `SERVER_URL` | completion 服务端口；改了 `PORT` 必须同步改 `SERVER_URL` |
| `OXYLABS_SCRAPER_URL` | **必填**，见下方警告 |
| `EVAL_LLM_MODEL` | 打分用的裁判模型（如 `openai/gpt-5.4`） |
| `MCP_SHARED_AGENT_IMAGE` / `MCP_TASK_AGENT_IMAGE` | 共享/按任务容器使用的无数据版本化镜像，默认 `mcp-atlas-runtime:20260724` |
| `MCP_TASK_DATA_DIR` | 带 `.atlas-fixture.json` 的外部任务数据目录；本地/下载型任务必填 |
| `MCP_TASK_MONGO_IMAGE` | 一次性 synthetic Mongo fixture 镜像；Mongo 任务必填，不配置时 fail closed |
| `MCP_RUNTIME_LOG_DIR` | 完整模型调用与隔离运行日志目录；跨机器部署时可设绝对路径 |

> ⚠️ **`OXYLABS_SCRAPER_URL` 必须设**（如 `https://yibuapi.com/oxylabs/v1/queries`）。
> 模板会把它透传给 oxylabs server；**留空**会让 `envsubst` 写入空值、覆盖掉包里的默认地址，
> 于是 oxylabs **一个工具都注册不了**（`/enabled-servers` 仍显示它 OK，调用却报 `Unknown tool`）。

### 2.1 凭证申请（只填要用的服务）

这些 key 被 `mcp_server_template.json` 以 `${VAR}` 引用。**绝不要把真实 key 写进任何文档或提交到 git**（`.env` 已在 `.gitignore` 里）。

| 服务 | 变量 | 申请地址 |
| --- | --- | --- |
| Airtable | `AIRTABLE_API_KEY` | https://github.com/felores/airtable-mcp?tab=readme-ov-file#obtaining-an-airtable-api-key |
| Alchemy | `ALCHEMY_API_KEY` | https://www.alchemy.com/docs/ |
| Brave Search | `BRAVE_API_KEY` | https://brave.com/search/api/ |
| E2B | `E2B_API_KEY` | https://e2b.dev/ |
| Exa | `EXA_API_KEY` | https://exa.ai/ |
| GitHub | `GITHUB_TOKEN` | https://github.com/settings/tokens |
| Google Maps | `GOOGLE_MAPS_API_KEY` | https://www.npmjs.com/package/@modelcontextprotocol/server-google-maps#setup |
| Google Workspace | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | https://github.com/epaproditus/google-workspace-mcp-server#prerequisites |
| Lara Translate | `LARA_ACCESS_KEY_ID` / `LARA_ACCESS_KEY_SECRET` | https://developers.laratranslate.com/docs/introduction |
| National Parks | `NPS_API_KEY` | https://www.nps.gov/subjects/developer/get-started.htm |
| MongoDB | `MONGODB_CONNECTION_STRING` | 本机 mongo 见 §4；云端 Atlas 建议连接串加 `?tls=true` |
| Notion | `NOTION_TOKEN` | https://github.com/makenotion/notion-mcp-server?tab=readme-ov-file#installation |
| Oxylabs | `OXYLABS_USERNAME` / `OXYLABS_PASSWORD` / `OXYLABS_SCRAPER_URL` | https://oxylabs.io/products/scraper-api/web |
| Slack | `SLACK_MCP_XOXC_TOKEN` / `SLACK_MCP_XOXD_TOKEN` | https://github.com/korotovsky/slack-mcp-server/blob/master/docs/01-authentication-setup.md |
| Twelve Data | `TWELVE_DATA_API_KEY` | https://twelvedata.com/docs |
| Weather | `WEATHER_API_KEY` | https://www.weatherapi.com/ |

> 默认启用 20 个不需要 key 的 server。需要 key 的 server 只有在 `.env` 里配了对应 key 才会自动启用。
> **一个反直觉的坑**：某个 server 启用了但数据/key 是坏的，比**不启用**更糟——不启用时依赖它的任务会被过滤器自动排除（不计分），启用后变成一批零分进分母。所以要么配好，要么把该 server 的 key 从 `.env` 拿掉。

---

## 3. 准备有状态服务数据

5 个服务需要往账号里导入数据，否则依赖它们的任务拿不到分。详见 `data_exports/README.md`。

| 服务 | 数据来源 | 备注 |
| --- | --- | --- |
| MongoDB | `data_exports/mongo_dump_video_game_store-UNZIP-FIRST.zip` | 唯一需要在本机导入的（见下） |
| Airtable | 云端 base，Copy base 即可 | 数据在云端，配好 key 即用 |
| Notion | `data_exports/mcp-atlas-notion-data.zip` 导入 | 云端账号 |
| Google Calendar | `data_exports/calendar_mcp_eval_export.zip` 里的 `.ics` | **必须导入主日历**（MCP 写死查 `primary`）；OAuth 授权账号 = 导入账号 |
| Slack | `data_exports/slack_mcp_eval_export.zip` | 免费版有 90 天限制，见 `docs/slack_free_method.md` |

**MongoDB 导入**：

```bash
cd data_exports && unzip mongo_dump_video_game_store-UNZIP-FIRST.zip
mongorestore --uri="mongodb://localhost:27017" mongo_dump_video_game_store-UNZIP-FIRST
```

**Slack**（免费版最麻烦）：官方导出的消息时间戳超过 90 天窗口，且用户/频道导入有坑。
**务必先读 `docs/slack_free_method.md`**——它讲了如何用 `prepare_slack_import.py` 平移时间戳、
频道/用户映射怎么选（用户选「导入为已注销账户」，别选「仅导入消息」）、以及验证方式。

> 多台机器共用同一套云账号是安全的：500 条任务经核查**零写操作**（见 `docs/write-ops-analysis.md`），
> 不会互相污染数据；共用的真正风险只是第三方 API 限流。

---

## 4. 构建运行时、准备外部数据并启动共享容器

构建版本化、无 fixture 的运行时镜像：

```bash
make build-atlas-runtime \
  ATLAS_RUNTIME_IMAGE=mcp-atlas-runtime:20260724
```

该构建从独立基础镜像和显式源码清单生成，包含固定 MCP 依赖、code-executor
只读 venv 与 Yibu patch，但构建上下文明确排除 `data/` 和 `.env*`。它不会
build、retag 或修改 `agent-environment:latest`。

再从你选择的数据目录生成内容寻址 fixture。合成数据必须使用 synthetic
源；复测同一批题时，两边必须使用同一个 fixture ID 和 SHA：

```bash
uv run --project services/mcp_eval python \
  scripts/prepare_task_data_fixture.py \
  --source /path/to/synthetic-data \
  --output /srv/mcp-fixtures/my-synthetic-v1 \
  --fixture-id my-synthetic-v1
```

在 `.env` 中设置：

```dotenv
MCP_SHARED_AGENT_IMAGE=mcp-atlas-runtime:20260724
MCP_TASK_AGENT_IMAGE=mcp-atlas-runtime:20260724
MCP_TASK_DATA_DIR=/srv/mcp-fixtures/my-synthetic-v1
```

若生成和复测在不同机器上，不要分别用同一个 tag 各自重建后假设内容相同。
在构建机一次构建，再用现有离线传输方式复制镜像，并核对 image ID：

```bash
docker save mcp-atlas-runtime:20260724 -o mcp-atlas-runtime-20260724.tar
# 复制 tar 到目标机后：
docker load -i mcp-atlas-runtime-20260724.tar
docker image inspect mcp-atlas-runtime:20260724 --format '{{.Id}}'
```

fixture 目录也一并复制；两端运行日志中的 image ID、fixture ID 和 SHA 必须一致。

启动共享容器：

```bash
make run-docker-host
```

端口直接读取 `.env` 的 `MCP_SHARED_PORT`。等约 1 分钟，日志出现对应端口后：

```bash
curl -s http://localhost:<MCP_SHARED_PORT>/enabled-servers | jq -c
```

共享容器使用 host 网络访问云端服务和现有本机依赖；逐题 local/Mongo 容器仍是
独立的 `network=none`，不会继承共享容器的凭证或数据。

---

## 5. 起 completion 服务（新终端）

第一次启用 Mongo 任务前，从自己的 synthetic mongodump 构建一次性 fixture
镜像。源数据库名可以任意指定，构建器会统一规范化为任务内的 `store`。这个命令
**不会修改或覆盖**任何 MCP runtime 镜像：

```bash
make build-task-mongo \
  MONGO_FIXTURE_DUMP=/path/to/mongodump \
  MONGO_FIXTURE_DB=my_seed_database \
  MONGO_FIXTURE_ID=my-synthetic-v1 \
  TASK_MONGO_IMAGE=mcp-task-mongo:my-synthetic-v1
```

随后在 `.env` 设置
`MCP_TASK_MONGO_IMAGE=mcp-task-mongo:my-synthetic-v1`；data-synthesis 必须设置
同一个 image tag。未设置时 Mongo 工具不会退回官方 fixture，而是在创建任务时
明确失败。

这里的 `27017` 只是每题 Mongo 容器内部的固定端口，不会发布到宿主机，也不是
常驻服务：有 Mongo 工具的任务开始时创建容器、恢复 fixture，任务结束就删除。
`MONGO_FIXTURE_DB` 可以选择 mongodump 中任意源数据库；运行时统一恢复为
`store`，并把模型传入的 `database` 强制改写为 `store`。这样可以换自种数据，
但不能让模型借数据库名越过任务边界。

宿主机真正需要协调的常驻端口只有 `.env` 中的 `MCP_SHARED_PORT`（共享 MCP）
和 `PORT`（completion）。network 任务容器的宿主端口由 Docker 临时分配；
local/Mongo 任务不发布宿主端口，因此这些内部端口不需要、也不应该配置。

completion 服务本身运行在宿主机上，通过 Docker CLI按题创建和销毁一次性容器：

```bash
make run-mcp-completion        # 监听 .env 的 PORT
```

启动时会清理由相同 `MCP_SANDBOX_OWNER`（默认 hostname + PORT）遗留的孤儿任务容器。手工清理命令：

```bash
make cleanup-task-sandboxes
```

隔离验收会检查全部本地/下载型 MCP 能启动、云端写工具不可见/不可调用、本地文件与 Mongo 写入不会跨题泄漏、本地容器没有云端凭证且 `network=none`，以及 20 个任务容器并发启动：

```bash
make check-task-isolation
```

冒烟测试（期望答案 `Customer`）：

```bash
curl -X POST http://localhost:${PORT:-3000}/v2/mcp_eval/run_agent \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-5.1","messages":[{"role":"user","content":"What is the first word of the file at /data/Barber Shop.csv?"}],"enabledTools":["filesystem_read_text_file"],"maxTurns":20}' | jq
```

---

## 6. 验收：数据到底导进去没有

评测前务必跑一遍。`test_server_v1.py` 会**回放数据集里真实的 GT 调用并断言数据内容**，
而不是像 `test_servers.py` 那样只看「调用没报错」（后者对空数据也全绿）。

```bash
cd services/mcp_eval
uv run test_server_v1.py --base-url http://localhost:1984              # 全部 36 项
uv run test_server_v1.py --base-url http://localhost:1984 --data-only  # 只验 5 个有状态服务
```

期望 5 个有状态服务全 `✅ DATA OK`。常见失败：

| 现象 | 原因 |
| --- | --- |
| `❌ DATA BAD ... 没找到频道` / `找不到 'Akira'` | Slack 没导入 / 超 90 天窗口 → 重跑 `prepare_slack_import.py` |
| `❌ DATA BAD ... 发送者名字解析不出来` | Slack 用户映射选错（见 `docs/slack_free_method.md`） |
| `💥 google-workspace ... API has not been used` | 该 GCP 项目没启用 Calendar API（项目级开关，≠ OAuth scope） |
| `💥 oxylabs ... Unknown tool` | `.env` 缺 `OXYLABS_SCRAPER_URL`（见 §2） |
| `💥 ... quota` / `401` / `SUBSCRIPTION_TOKEN_INVALID` | 第三方 key 配额用尽或失效 |

---

## 7. 生成轨迹（completion）

脚本是 **env 驱动**的，模型/输入/输出/并发都从 `.env` 读：

```bash
cd services/mcp_eval
uv run python mcp_completion_script.py         # 读 MCP_COMPLETION_MODEL / _INPUT / _OUTPUT / _CONCURRENCY
```

也可命令行显式指定：

```bash
uv run python mcp_completion_script.py \
  --model "pangu/<checkpoint>" --input "MCP-Atlas.csv" --output "MCP-Atlas-<label>.csv"
```

结果写入 `completion_results/`。脚本会自动跳过输出文件里已有的行；要重跑先删/改名。

每个请求现在会把 `TASK` 作为 `taskId` 传给 completion 服务。完整日志默认位于：

```text
completion_results/runtime_logs/<YYYY-MM>/
├── model_calls_<YYYYMMDD>.jsonl
├── tools_<YYYYMMDD>.jsonl
├── sandbox_<YYYYMMDD>.jsonl
├── service_<YYYYMMDD>.jsonl
└── containers/<task-id>/*.log
```

其中 `model_call_started` 在实际请求模型前落盘，因此即使上游超时或进程被杀，也能看到该次调用。配置中的 token/key/secret/password 会在 JSONL 中替换为 `<redacted>`。

---

## 8. 打分（evaluation）

```bash
uv run mcp_evals_scores.py \
  --input-file "completion_results/MCP-Atlas-<label>.csv" \
  --model-label "<label>"
```

- `--evaluator-model` 覆盖裁判模型（默认取 `EVAL_LLM_MODEL`）
- `--pass-threshold` 通过阈值（coverage，默认 0.75）

输出到 `evaluation_results/`：`scored_<label>.csv`（每任务分数）、`coverage_stats_<label>.csv`（汇总）、`coverage_histogram_<label>.png`。

> 裁判打分的可复现性：本项目的第三方裁判端点**不理会 `temperature`**（temp=0 也非确定），
> 所以逐条分数会有轻微抖动（实测重跑差异在 1 分内）。要比就比**聚合通过率**。

---

## 9. 启动顺序小结

```
① make build-atlas-runtime                 —— 构建无数据运行时
② scripts/prepare_task_data_fixture.py      —— 准备外部 fixture
③ make run-docker-host                      —— 起共享 MCP
④ make run-mcp-completion                   —— 起 completion
⑤ uv run test_server_v1.py                  —— 验收
⑥ uv run mcp_completion_script.py           —— 生成轨迹
⑦ uv run mcp_evals_scores.py                —— 打分
```

改了 `.env` 后 **必须重启容器**（`--env-file` 只在启动时读一次）。

---

## 相关文档

| 文件 | 内容 |
| --- | --- |
| `docs/slack_free_method.md` | Slack 免费版数据维护（时间戳平移、导入选项、验收）——**导 Slack 前必读** |
| `data_exports/README.md` | 5 个有状态服务的数据设置 |
| `docs/write-ops-analysis.md` | 500 条任务的写操作分析（结论：零写操作，可共用账号） |
| `docs/custom-npm-package-guide.md` / `docs/windows-local-package-guide.md` | 自定义/本地 MCP npm 包 |

---

## 组成

- **36 个 MCP server**（filesystem、Git、Wikipedia、GitHub、weather、Airtable、Notion、Slack、MongoDB…）
- **completion 服务**：跑多轮 LLM + 工具调用
- **docker 化的 fixture-free MCP runtime**：一致的 server 运行环境，数据按题注入
- **评测脚本**：轨迹生成（`mcp_completion_script.py`）、打分（`mcp_evals_scores.py`）、数据验收（`test_server_v1.py`）
