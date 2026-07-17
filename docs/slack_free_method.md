# Slack 免费版数据维护指南

官方 Slack 导出的时间戳早已超出免费版的 90 天可见期。本文说明**每次**要跑什么、怎么导入、怎么验证。

> 一句话：跑 `uv run prepare_slack_import.py --fix-claims` → 浏览器手动导入生成的 zip → 跑 `test_server_v1.py --server slack` 验收。
> 免费版下每 ~3 个月要重复一次。

---

## 1. 为什么需要这么做

`data_exports/slack_mcp_eval_export.zip` 里是 6 个频道、160 条消息，时间戳为 **2025-12-01 ~ 2025-12-10**。

免费版 Slack **只显示最近 90 天的消息**。直接导入官方 zip 的后果：

- 频道能建出来，但**消息全部不可见**
- `slack_conversations_history` 返回空
- **27 条 GT 轨迹里调用了 slack 工具的任务全部拿不到分**（占基准 5.4%）

而且这类失败**不会被任务过滤器排除**——因为 key 有效、服务在线，`/enabled-servers` 认为 slack 可用，任务照跑照失败。

> ⚠️ **反直觉但重要**：启用了 slack 却没有数据，比**不启用**更糟。不启用时这 27 条会被自动排除（不进分母）；启用后它们变成 27 个零分进分母。
> 所以如果你**不打算**维护 Slack 数据，正确做法是把 `SLACK_MCP_XOXC_TOKEN` / `SLACK_MCP_XOXD_TOKEN` 从 `.env` 里拿掉。

### 为什么平移时间戳是安全的

- **33 条**含 slack 的任务，题面（PROMPT）里**没有任何一条**提到日期（已全量核查）
- 平移量取**整天数** → 时分秒、微秒、相对间隔、消息顺序**全部原样保留**
  → "谁先发的"、"某人当天发了几条"这类相对/计数类 claim 不受影响
- **官方自己就这么干过**：GT claims 记的是 `2025-06-27 16:38:56.421649`，而发布的导出里同一条消息是 `2025-12-05 16:38:56.421649` —— 正好 **+161 天**，时分秒与微秒完全一致

### 顺带修好官方的一个 bug

官方平移了导出，却**忘了同步更新 GT claims**。所以有 2 条任务在**原始状态下就是不可能拿分的**：

```
claim 说 : The user "mcpdumple" posted on 2025-06-27 ...
数据实际 : 2025-12-05 ...          ← 模型如实回答 12-05，裁判拿 06-27 比对 → 判不通过
```

`--fix-claims` 会把这类日期一起平移修正。它**只改能对上导出消息日期的日期**，同一条 claim 里的 git commit 日期、电影上映日期一律不碰。

---

## 2. 每次要跑的：一条命令

```bash
cd services/mcp_eval
uv run prepare_slack_import.py --fix-claims
```

它会：

1. 读 `data_exports/slack_mcp_eval_export.zip`
2. 自动算平移量：让**最新一条消息落到今天前 3 天**（吃满 90 天窗口）
3. 平移所有 `ts` / `edited.ts` / `files[].created|timestamp`，并把按日期命名的 JSON 改名到新日期
4. 产出 → **`data_exports/slack_mcp_eval_export_shifted.zip`**
5. `--fix-claims`：修正 `MCP-Atlas.csv` 里绑定 slack 日期的 claim（**自动备份为 `MCP-Atlas.csv.bak`**）
6. 打印**下次到期日**

常用参数：

| 参数 | 说明 |
|---|---|
| `--fix-claims` | 同步修正 GT claims。不加则只 dry-run 打印会改什么，不写入 |
| `--days-ago N` | 让最新消息落到 N 天前（默认 3） |
| `--src` / `--out` / `--csv` | 覆盖默认路径 |

### 原版只读，每次全新派生（幂等）

两个输入都是**官方原版、只读、永不修改**，每次运行都从它们重新派生出目标文件：

```
data_exports/slack_mcp_eval_export.zip   →  _shifted.zip      （平移时间戳 + 改邮箱）
services/mcp_eval/MCP-Atlas.origin.csv   →  MCP-Atlas.csv     （平移 claim 日期）
```

**不在上一轮的结果上叠加**，所以重复运行是幂等的：跑几次 md5 都一样，不会二次平移。
偏移量也始终是官方那次的 **+161 天**（脚本用微秒指纹从原版推导，不写死）。

> `MCP-Atlas.origin.csv` 是这条链的基准，**别动它**。md5 应为 `28edad761f29`。
> 它丢了的话，`--fix-claims` 会跳过 claim 处理并提示。

### ⚠️ `--fix-claims` 会改动基准数据集

- `MCP-Atlas.csv` **不在 git 里**（53MB 未跟踪），所以**改动不会随 git 同步**
- 改完后**必须手动同步到另一台/另一份**，否则两边跑分不可比：
  ```bash
  cp /home/lny/mcp-atlas/services/mcp_eval/MCP-Atlas.csv \
     /mnt/hzp/mcp-atlas/services/mcp_eval/MCP-Atlas.csv
  # 同步后两边 md5 应一致
  md5sum /home/lny/mcp-atlas/services/mcp_eval/MCP-Atlas.csv \
         /mnt/hzp/mcp-atlas/services/mcp_eval/MCP-Atlas.csv
  ```
- 改完就和**官方基准分叉**了，跨团队/论文对比时要说明
- 实际改动极小：500×5=2500 格里**只有 2 格**（均在 `GTFA_CLAIMS`），`TASK`/`PROMPT`/`TRAJECTORY`/`ENABLED_TOOLS` 100% 未变

---

## 3. 导入：必须手动

**Slack 没有 workspace 导入 API**，`/services/import` 是管理员浏览器流程，无法脚本化。

### 3.1 清理旧消息（**重复导入前必做**）

**首次导入可跳过这一步**（workspace 里本来就没有 eval 数据）。

但每 3 个月刷新时**必须先清理**：Slack 导入是**追加**语义，同一批消息导两次就会**出现两份**，
`@mcpdumle sent 4 messages` 这类计数型 claim 会直接算错（变成 8 条）。

清理方式：**删掉这 6 个 eval 频道**（删频道会连同其中的消息一起删除），然后重新导入即可重建：

```
all-dumle-servers   social   new-channel   gaming-suggestions   tv-show-suggestions   movie-suggestions
```

操作：进频道 → 频道名称 → **设置 / Settings** → **删除频道 / Delete channel**（需 workspace 管理员权限）。

> - 这 6 个都是导入时创建的普通频道，可以删。workspace 自带的默认频道（`#所有-xxx` / `#general`）
>   删不掉，但那不是 eval 频道，不用管。
> - **更省事的替代**：直接新建一个 workspace 从头导。代价是要重新取 `SLACK_MCP_XOXC_TOKEN` /
>   `SLACK_MCP_XOXD_TOKEN` 并更新 `.env`（还要重启容器）。
> - 逐条删 160 条消息不现实，不要考虑。

### 3.2 上传

```bash
# 把 zip 下载到你本地（导入是浏览器上传）
scp <server>:/home/lny/mcp-atlas/data_exports/slack_mcp_eval_export_shifted.zip .
```

1. 浏览器打开 `https://<你的workspace>.slack.com/services/import`
2. 选择 **Slack** 导入方式，上传 `slack_mcp_eval_export_shifted.zip`

### 3.3 频道导入方式：选「**创建同样隐私设置的新频道**」

| 选项 | 选它吗 | 原因 |
|---|:---:|---|
| **创建同样隐私设置的新频道** | ✅ **选这个** | 忠实还原导出的原始设置，不会有意外 |
| 创建新的公共频道 | 🟡 可接受 | 结果和上面一样（6 个频道本来就都是公共的），但不如上面来得"照原样" |
| 创建新的私人频道 | ❌ | 会偏离导出的原始状态；私人频道还要求 token 对应的用户是成员才看得见 |
| 将频道与现有 Slack 频道合并 | ❌ **危险** | 见下 |

**这 6 个频道本来就都是公共的**：Slack 导出把私人频道放在 `groups.json`、公共频道放在 `channels.json`，
而这份导出**只有 `channels.json`、没有 `groups.json`**，6 个频道全在里面。

> GT 有条 claim 写着 "in the #movie-suggestions **private** Slack channel" —— 那是**标注员写得不准**，
> 以数据为准，它是公共频道。不要因为这句话去选「创建新的私人频道」。

**为什么绝不能选「合并」**：评测和验证脚本都是**按频道名查**的。合并会把消息塞进**已有频道、
并沿用已有的名字** —— 你的 workspace 现在是 `#所有-travel` / `#社交` / `#新频道` 这套中文默认频道，
一合并，消息是进去了、名字对不上，任务照样零分，而且这种失败很难排查。

#### 选完顶层，下面每个频道还能单独选 —— 全部保持「创建新的公共频道」

顶层选项只是默认值，列表里 6 个频道**每个都能单独覆盖**成公共/私人/合并。**一个都别改，尤其别选合并。**

评测实际只用到其中 **3 个**（GT 轨迹里按 channel_id 查询的次数）：

| 频道 | GT 查询次数 | 要紧吗 |
|---|:---:|---|
| `movie-suggestions` | 15 | 🔴 **关键** |
| `gaming-suggestions` | 9 | 🔴 **关键** |
| `tv-show-suggestions` | 3 | 🔴 **关键** |
| `all-dumle-servers` | 0 | 无所谓 |
| `new-channel` | 0 | 无所谓 |
| `social` | 0 | 无所谓 |

- 上面 3 个 **suggestions 频道承载了全部 27 条任务**，必须新建、绝不能合并
- 下面 3 个从未被任何 GT 轨迹查询过，理论上怎么选都不影响跑分

> ⚠️ **注意 `all-dumle-servers`**：它 `is_general=True`（是源 workspace 的 #general），
> Slack 很可能**默认建议把它合并进你的 `#所有-travel`**。它本身无关紧要、合并了也不掉分，
> 但别让这个默认值把你带偏、顺手把其他几个也合并了。稳妥起见 6 个统一选「创建新的公共频道」。

### 3.4 用户映射：选「**导入为已注销账户**」

导入过程中 Slack 会让你决定导出里的 20 个用户怎么处理。**选「导入为已注销账户」**
（import as deactivated accounts）。它会建立**真实成员记录**（有 user ID、标记已注销），
**不发邀请邮件、不占席位**，正是我们要的。

| 选项 | 选它吗 | 后果 |
|---|:---:|---|
| **导入为已注销账户** | ✅ **选这个** | 有真实 user ID、不发邀请、不占席位、名字可解析 |
| 邀请为新成员 | ❌ | **给 20 个陌生人发邮件**（见下），还白占席位 |
| 请勿导入这些用户，但仅导入其消息 | ❌ **有毒** | 消息全变 `bot_message`，见下 |
| 合并到现有成员 | 🟡 | 仅当该用户已经是你 workspace 成员时才用（见 3.4.1）|

**为什么绝不能选「邀请为新成员」**：导出里这 20 个用户带的是**真实邮箱**（gmail / proton / yahoo），
是 ScaleAI 那边真人的地址：

```
mcpdumle@gmail.com、hiphopluvr1989@proton.me、shinsplints7070@proton.me ...
```

**为什么「请勿导入这些用户，但仅导入其消息」是个陷阱**

它听起来正合需求（不发邀请、还导消息），实际后果是**灾难性的**，而且症状极具迷惑性。
用户在 workspace 里不存在，Slack 只好把消息归给 bot：

```json
{
  "subtype": "bot_message",                       ← 被 MCP 默认当 activity 消息过滤掉
  "text": "I always liked the anime movie Akira",
  "username": "Omari West",                       ← 名字只是个字符串
  "ts": "1784046986.026929"
}                                                 ← 没有 "user" 字段，没有 ID
```

于是：

- **UI 里一切正常**——你能看到每条消息、每个人的名字（因为 `username` 字段在），
  Slack 也显示"导入完成"
- **API 里几乎什么都没有**——`slack_conversations_history` 默认过滤 `bot_message`，
  28 条消息只返回 1 条（唯一那条真实成员发的）
- **就算绕过过滤**（`include_activity_messages=true` 能拿到全部），**用户归属也丢了**
  （`user: None`），`@mcpdumle sent 4 messages` 这类按名字判定的 claim 照样全废

「导入为已注销账户」建立的是真实成员记录，消息带 `user: Uxxxx`、没有 `bot_message`
子类型，MCP 默认就能返回，名字也解析得出来（已注销用户仍在 `users.list` 里，带 `deleted:true`）。

> 选没选对不用猜：第 4 节的验证脚本会**专门断言用户名可解析**，选错会直接报出来。

#### 3.4.1 邮箱冲突：用 `--rewrite-emails`

如果 Slack 提示某些用户的邮箱**已对应现有账号**，就不再让你选「导入为已注销账户」，只给合并。
常见原因：**上一次导入被撤销后，它创建的账号会残留**（撤销只删消息和频道，不删账号）。

合并也能用（有真实 ID），但残留账号的 `real_name` 会**退化成用户名**
（`lucas.t.medina1994` 而不是 `Lucas Medina`），而有 claim 是按真名判定的。

干净的做法是给这些用户换个不冲突的邮箱，让 Slack 当新用户、以已注销账户导入：

```bash
uv run prepare_slack_import.py --fix-claims \
  --rewrite-emails ivansalazar0003,lucas.t.medina1994
```

它只改 `profile.email`（→ `<用户名>@example.com`），**`name` 和 `real_name` 一个字不动**。

**改邮箱不影响评测**（已全量核查）：

- 33 条 slack 任务的题面和 claims 里，**出现字面邮箱 0 处**
- slack 的三个工具（`channels_list` / `conversations_history` / `search_messages`）
  **没有一个读 email 字段**——邮箱只是 Slack 导入时的账户匹配钥匙
- 唯一那条靠邮箱推用户名的任务（`6888e207a34beb25cfedda70`），邮箱取自 **Notion**
  的 `get-users`，不是这里；它需要的只是 slack 里存在用户名 `mcpdumle`

代价：workspace 里会多出几个孤儿已注销账号（不占席位、无影响）。

### 3.5 导入后

如果改过 `.env`（比如换了 token），**重启 MCP 容器**——`--env-file` 只在容器启动时读一次：

```bash
make run-docker          # 或 make run-docker-host
```

---

## 4. 验证

```bash
cd services/mcp_eval
uv run test_server_v1.py --server slack --base-url http://localhost:1984
```

期望：

```
✅ DATA OK   slack                0.7s
       └─ 验的是: Slack 导出的频道/消息是否已导入
       └─ #movie-suggestions(C...) 历史消息在位，且用户名可解析
```

它依次断言三件事：

1. `slack_channels_list` 里能找到 **`#movie-suggestions`** 频道 → 频道导进来了
2. 该频道的 `slack_conversations_history` 里含 GT 的消息文本 **`Akira`** → 消息可见（没被 90 天窗口挡掉）
3. 同一条消息的发送者能解析出 **`hiphopluvr1989` / `Omari West`** → 用户映射选对了（见 3.4）

**断言的全是频道名、消息文本、用户名，不含任何时间戳**，所以平移时间戳后**不需要改这个脚本**。

顺带把 5 个有状态服务一起验：

```bash
uv run test_server_v1.py --data-only --base-url http://localhost:1984
```

常见失败：

| 现象 | 原因 |
|---|---|
| `❌ DATA BAD ... 没找到 #movie-suggestions 频道` | zip 没导入，或导到了别的 workspace |
| `❌ DATA BAD ... 返回里找不到 'Akira'` | 频道建出来了但消息不可见 → 多半是**时间戳超期**，重跑第 2 节的平移脚本 |
| `❌ DATA BAD ... 发送者名字解析不出来` | 导入时**用户映射选错了**（把用户整个排除了）→ 见 3.4，应选「请勿导入这些用户，但仅导入其消息」 |
| `💥 API FAIL ... channel_not_found` | 同上，或 token 指向了别的 workspace |
| `💥 API FAIL` 且 token 刚换过 | **忘了重启容器** |

---

## 5. 周期性维护

免费版的宿命：**消息会再次过期**。

- 平移后最早的那条消息，**90 天后**会重新变得不可见
- `prepare_slack_import.py` 每次运行都会打印下次到期日，例如：
  ```
  ⚠️ 免费版 90 天窗口：最早的消息将于 2026-10-03 再次隐藏，届时需重跑本脚本并重导
  ```
- 到期后：**重跑第 2 节 → 清旧消息 → 重做第 3 节导入 → 第 4 节验证**

### 要不要直接上付费

| 方案 | 代价 | 维护 |
|---|---|---|
| **免费 + 平移** | 0 | 每 ~3 个月重跑+重导一次，且每次要同步 CSV |
| **付费 $9/月** | $9/月 | **一劳永逸**：直接导原始 zip，不用平移、不用改 claims、不用同步 CSV |

如果这个基准要长期反复跑，**付费更省事**——顺带还能避免 `--fix-claims` 带来的基准分叉问题。

---

## 6. 回滚

```bash
# 还原 MCP-Atlas.csv（--fix-claims 每次都会自动生成 .bak）
cd services/mcp_eval
cp MCP-Atlas.csv.bak MCP-Atlas.csv
```

平移产生的 `slack_mcp_eval_export_shifted.zip` 是**新文件**，原始的 `slack_mcp_eval_export.zip` 从不被修改，可随时重新生成。

---

## 相关文件

| 路径 | 作用 |
|---|---|
| `data_exports/slack_mcp_eval_export.zip` | 官方原始导出（只读，不修改） |
| `data_exports/slack_mcp_eval_export_shifted.zip` | 脚本产出，用它导入 |
| `services/mcp_eval/prepare_slack_import.py` | 平移 + 修 claims |
| `services/mcp_eval/test_server_v1.py` | 验证数据是否真的导入了 |
| `services/mcp_eval/MCP-Atlas.csv` | 基准数据集（未跟踪，需手动同步） |
| `data_exports/README.md` | 官方的 5 个有状态服务数据设置说明 |
