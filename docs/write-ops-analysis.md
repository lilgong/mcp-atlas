# MCP Atlas 测试用例写操作分析

## 分析方法

两轮分析均采用"规则初筛 + LLM 二次校验"的方式：

1. **规则初筛**：根据工具名称模式，将工具调用分为"确定写操作"和"疑似写操作"两类
2. **LLM 二次校验**：对于 `cli-mcp-server_run_command`、`mcp-code-executor_execute_code`、`e2b-server_run_code` 等模糊工具，提取实际的命令/代码内容，交由 LLM 判断是否存在写操作

---

## 场景一：本地 /data 目录写操作分析

### 规则设定

| 类型 | 直接判定为写操作的工具 |
|------|----------------------|
| 文件系统 | `filesystem_write_file`、`filesystem_create_directory`、`filesystem_move_file`、`filesystem_delete_*` |
| Memory | `memory_create_entities`、`memory_create_relations`、`memory_add_observations`、`memory_delete_*` |
| MongoDB | `mongodb_insert*`、`mongodb_update*`、`mongodb_delete*`、`mongodb_drop*` |
| GitHub | `github_create_*`、`github_update_*`、`github_push_*`、`github_delete_*` |
| Git | `git_git_commit`、`git_git_push`、`git_git_add`、`git_git_merge` |
| Desktop Commander | `desktop-commander_write_file`、`desktop-commander_create_directory` |

| 类型 | 需 LLM 校验的工具 |
|------|-----------------|
| Shell 命令 | `cli-mcp-server_run_command`（提取实际命令判断） |
| 代码执行 | `mcp-code-executor_execute_code`、`e2b-server_run_code`（提取实际代码判断） |

### 分析结果

| 指标 | 数量 |
|------|------|
| 总用例数 | 500 |
| 规则初步标记为疑似写操作 | 128 |
| LLM 校验确认为写操作 | **0** |
| LLM 确认为误判（实际是读操作）| 128 |

### 被误判的工具调用实际内容

| 工具 | 规则误判原因 | LLM 实际确认 |
|------|------------|-------------|
| `cli-mcp-server_run_command` | 命令执行可能写文件 | 实际全是 `ls`、`cat`、`find` 等读命令 |
| `mcp-code-executor_execute_code` | 代码执行可能写文件 | 实际全是数值计算、pandas 数据分析 |
| `e2b-server_run_code` | 代码执行可能写文件 | 实际全是数值计算 |

### 结论

**500 条用例对本地 `/data` 目录无任何写操作。**

---

## 场景二：云端及远程服务写操作分析

### 规则设定

| 服务 | 判定为写操作的工具模式 |
|------|----------------------|
| GitHub | `github_create_*`、`github_update_*`、`github_delete_*`、`github_push_*`、`github_merge_*`、`github_fork_*` |
| Airtable | `airtable_create_*`、`airtable_update_*`、`airtable_delete_*` |
| Notion | `notion_API-patch-*`（PATCH 请求）、页面创建类 POST |
| Slack | `slack_send_*`、`slack_post_*`、`slack_create_*`、`slack_invite_*` |
| Memory | `memory_create_*`、`memory_add_*`、`memory_delete_*` |
| MongoDB | `mongodb_insert*`、`mongodb_update*`、`mongodb_delete*`、`mongodb_create*` |
| Google Workspace | `google-workspace_create_*`、`google-workspace_update_*`、`google-workspace_send_*` |
| Git | `git_git_commit`、`git_git_push`、`git_git_init`、`git_git_merge` |
| 非 /data 文件写入 | `filesystem_write_file`（路径不在 /data 下）|

### 各服务实际出现的工具调用

| 服务 | 实际出现的工具调用 | 是否有写操作 |
|------|-----------------|:----------:|
| GitHub | 仅 `search_repositories`、`get_repository`、`list_*`、`get_commit` | 无 |
| Notion | 仅 `API-post-search`、`API-post-database-query`（POST 为查询语义，非写入）| 无 |
| Airtable | 仅 `list_bases`、`list_tables`、`list_records`、`search_records` | 无 |
| Slack | 仅 `channels_list`、`conversations_history` | 无 |
| MongoDB | 仅 `find`、`aggregate`、`list-collections`、`list-databases` | 无 |
| Git | 仅 `git_log`、`git_show`、`git_status`、`git_diff` | 无 |
| Filesystem | 仅 `list_directory`、`read_file`、`read_text_file` | 无 |
| 代码执行（LLM 校验） | 203 条，全为 `ls`/`cat`/数值计算/读 CSV | 无 |

### 分析结果

| 指标 | 数量 |
|------|------|
| 总用例数 | 500 |
| 规则初步标记为疑似云端写操作 | 128 |
| LLM 校验确认为写操作 | **0** |

### 结论

**500 条用例对任何云端或远程服务均无写操作。**

---

## 综合结论

MCP Atlas 的 500 条测试用例经过两轮分析（本地 + 云端），均未发现任何写操作：

- **设计原则**：Agent 只负责查询数据并计算结果，不对任何环境产生副作用
- **测试安全性**：多条用例并发共享同一容器是安全的，`/data` 内容在整个测试过程中保持不变
- **无需隔离机制**：不需要用例级别的数据重置，容器级别的隔离已经足够

| 分析维度 | 有写操作的用例数 |
|---------|:--------------:|
| 本地 /data 目录 | 0 / 500 |
| 其他本地目录 | 0 / 500 |
| 云端/远程服务 | 0 / 500 |
| **总计** | **0 / 500** |
