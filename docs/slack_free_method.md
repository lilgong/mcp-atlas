# Slack 免费版数据维护指南

官方 Slack 导出的时间戳早已超出免费版的 90 天可见期。本文说明**每次**要跑什么、怎么导入、怎么验证。

> 一句话：跑 `uv run prepare_slack_import.py --fix-claims` → 浏览器手动导入生成的 zip → 跑 `test_server_v2.py --server slack` 验收。
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
4. 产出 → **`data_exports/slack_mcp_eval_export_<MMDD>.zip`**
5. `--fix-claims`：从官方 `MCP-Atlas-origin.csv` 派生 Git 忽略的
   `MCP-Atlas.csv`，同步修正绑定 slack 日期的 claim
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
data_exports/slack_mcp_eval_export.zip → ..._<MMDD>.zip             （平移时间戳）
services/mcp_eval/MCP-Atlas-origin.csv → MCP-Atlas.csv（平移 claim 日期）
```

**不在上一轮结果上叠加**，所以同一天用相同参数重复运行是幂等的，不会二次平移。
偏移量也始终是官方那次的 **+161 天**（脚本用微秒指纹从原版推导，不写死）。

> Git 中的 `MCP-Atlas-origin.csv` 是这条链的只读基准，SHA256 应为
> `065f423ffd1425185d23ed01a1d1ad8ed8c6355749868521a07faaa13ec4c0ad`。
> 不要手改它；文件缺失或 hash 不符时先恢复 Git 文件。

### 官方原版和免费 Slack 对齐版不会混在一起

- `MCP-Atlas-origin.csv`：Git 跟踪的官方原版，严格官方复测使用。
- `MCP-Atlas.csv`：本机生成、Git 忽略的免费 Slack 对齐版。
- 对齐版与官方原版相比只改 2 个 `GTFA_CLAIMS` 单元格；另外四列完全相同。
- 免费 Slack 运行时在 `.env` 设置
  `MCP_COMPLETION_INPUT=MCP-Atlas.csv`。
- 严格官方原版运行时设置 `MCP_COMPLETION_INPUT=MCP-Atlas-origin.csv`。
- 跨机器比较免费 Slack 结果时，两台机器要在同一天用相同 `--days-ago`
  参数生成，并记录对齐版 SHA256。

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
scp <server>:/home/lny/mcp-atlas/data_exports/slack_mcp_eval_export_0717.zip .   # 换成实际日期
```

1. 浏览器打开 `https://<你的workspace>.slack.com/services/import`
2. 选择 **Slack** 导入方式，上传 `slack_mcp_eval_export_<MMDD>.zip`

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
| 合并到现有成员 | 🟡 | Slack 只给这个选项时才用（残留账号，见 3.4.1）—— 代价极小 |

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

#### 3.4.1 有些用户只能选「合并」——顺着它就好

Slack 可能对某几个用户提示「已有用户使用此电子邮件」，于是不给你「导入为已注销账户」，只剩合并。

原因：**撤销导入只删消息和频道，不删它创建的账号**。上一轮导入残留的已注销账号还在
workspace 里，Slack 认出了它们。

**实测结论：Slack 不是按邮箱匹配的。** 我们把导出里的邮箱改成 `@example.com` 重新上传，
Slack 依然报同一个 `@gmail.com` 被占用——而那个地址只存在于残留账号上，新 zip 里根本没有。
它按用户名（或导出里的原始 user ID）匹配，把**残留账号的**邮箱显示给你看。所以**改邮箱绕不开**，
别在这上面浪费时间。

**直接合并，代价极小**：合并会沿用残留账号的 `real_name`，而它退化成了用户名
（`lucas.t.medina1994` 而不是 `Lucas Medina`）。全量核查后，这只可能影响 1 条任务
（`689af4e653c3905e7b5b2581`，3 条 claim 里有 1 条是 `Lucas Medina recommended the game
"This War of Mine"`）：

- 题面只说 "the game **Lucas** recommended"，模型看到 `lucas.t.medina1994` 照样能锁定人
- 裁判大概率认得出 `lucas.t.medina1994` 就是 `Lucas Medina`，判 fulfilled 或 partial
- 即便只给 partial：coverage = (2+0.5)/3 = **0.833 > 0.75，仍然通过**
- 最坏情况（判 0）才丢这 1 条 = **0.2%**

为这 0.2% 去跟 Slack 的账号残留搏斗（联系支持、或新建 workspace 从零来过）不划算。

> **消息导入不受影响**：用户映射和消息解析是两回事。消息按 `ts` 索引，平移后是全新时间戳
> = Slack 眼里的全新消息，一定会导进去。所以 3 个月后重导同样可行——那时这些账号仍会提示
> 合并，顺着选即可。

### 3.5 导入后

如果改过 `.env`（比如换了 token），**重启 MCP 容器**——`--env-file` 只在容器启动时读一次：

```bash
make run-docker          # 或 make run-docker-host
```

---

## 4. 验证

```bash
cd services/mcp_eval
uv run test_server_v2.py --server slack --base-url http://localhost:1984
```

脚本默认读取仓库根 `.env` 的 `MCP_COMPLETION_INPUT`；未配置时回退到当前仓库的
`services/mcp_eval/MCP-Atlas.csv`。也可用 `--input /绝对路径/MCP-Atlas.csv`
临时指定另一份测试集。

期望：

```
✅ DATA OK   slack                0.7s
       └─ 验的是: Slack 导出的频道/消息是否已导入
       └─ #movie-suggestions(C...) 历史消息在位，且用户名可解析；测试集 ... 与 Slack 时间锚点一致
```

它依次断言四件事：

1. `slack_channels_list` 里能找到 **`#movie-suggestions`** 频道 → 频道导进来了
2. 该频道的 `slack_conversations_history` 里含 GT 的消息文本 **`Akira`** → 消息可见（没被 90 天窗口挡掉）
3. 同一条消息的发送者能解析出 **`hiphopluvr1989` / `Omari West`** → 用户映射选对了（见 3.4）
4. 当前评测 CSV 中 Napoleon Dynamite 的精确 UTC claim 与 Slack 云端消息时间戳一致
   → 导入的 zip 与实际使用的测试集属于同一轮平移。

顺带把 5 个有状态服务一起验：

```bash
uv run test_server_v2.py --data-only --base-url http://localhost:1984
```

常见失败：

| 现象 | 原因 |
|---|---|
| `❌ DATA BAD ... 没找到 #movie-suggestions 频道` | zip 没导入，或导到了别的 workspace |
| `❌ DATA BAD ... 返回里找不到 'Akira'` | 频道建出来了但消息不可见 → 多半是**时间戳超期**，重跑第 2 节的平移脚本 |
| `❌ DATA BAD ... 发送者名字解析不出来` | 导入时**用户映射选错了**（把用户整个排除了）→ 见 3.4，应选“导入为已注销账户” |
| `❌ DATA BAD ... 测试集与 Slack 时间不对应` | `.env` 的 `MCP_COMPLETION_INPUT` 与本次导入的 Slack zip 不配套；重新生成/导入并使用同轮派生 CSV |
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
| **免费 + 平移** | 无 Slack 订阅费 | 每 ~3 个月重跑、重导并重新生成对齐版 CSV |
| **支持完整历史的付费方案** | 以 Slack 当前价格为准 | 可直接导原始 zip，不需要周期性平移 |

如果这个基准要长期反复跑，支持完整历史的方案维护成本更低。无论采用哪种方案，
都要记录实际使用的任务 CSV SHA256。

---

## 6. 切回官方原版

```bash
# .env
MCP_COMPLETION_INPUT=MCP-Atlas-origin.csv
```

不需要复制或还原文件。平移产生的 `MCP-Atlas.csv` 和
`slack_mcp_eval_export_<MMDD>.zip` 都是新文件；官方 CSV 和原始 zip 从不修改。

---

## 相关文件

| 路径 | 作用 |
|---|---|
| `data_exports/slack_mcp_eval_export.zip` | 官方原始导出（只读，不修改） |
| `data_exports/slack_mcp_eval_export_<MMDD>.zip` | 脚本产出，用它导入 |
| `services/mcp_eval/prepare_slack_import.py` | 平移 + 修 claims |
| `services/mcp_eval/test_server_v2.py` | 按任务隔离正式路由验证数据是否真的导入 |
| `services/mcp_eval/MCP-Atlas-origin.csv` | Git 跟踪的官方原版评测集（只读） |
| `services/mcp_eval/MCP-Atlas.csv` | 免费 Slack 对齐版（脚本生成、Git 忽略） |
| `data_exports/README.md` | 官方的 5 个有状态服务数据设置说明 |
