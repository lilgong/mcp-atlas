#!/usr/bin/env python3
"""
MCP 服务器检查（v1）：区分"API 通不通"和"数据对不对"。

与 test_servers.py 的区别：
    test_servers.py 只要调用不报错就算 PASS —— 返回空数据也会全绿，验不出官方数据是否导入。
    本脚本对 5 个有状态服务（airtable / google-workspace / mongodb / notion / slack）
    回放 MCP-Atlas.csv 里的**真实 GT 调用**并断言返回含 GT 的关键事实，
    从而真正验证"官方数据是否已正确导入"。其余服务沿用原有连通性冒烟测试。

断言只用**内容**（库名/表名/频道名/事件名/文档数），不用 ID：
    GT 里的 ID 属于录制基准时用的账号，你用自己的账号导入后 ID 必然不同（Airtable base、
    Notion database、Slack channel 都会变）。所以有 ID 的地方一律先动态发现、再断言内容。
    评测时模型也是这么做的（先 list_bases/search 再查），因此 ID 不同不影响跑分。

结果分四类：
    DATA OK    API 通，且数据与 GT 一致
    DATA BAD   API 通，但数据对不上（多半是没导入 / 导错账号）
    API FAIL   调用本身失败（key 失效、服务没起、网络不通）
    SKIP       该服务未启用（.env 缺 key）

Usage:
    uv run test_server_v1.py
    uv run test_server_v1.py --data-only
    uv run test_server_v1.py --server mongodb
    uv run test_server_v1.py --base-url http://localhost:2984
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from test_servers import (
    DEFAULT_MCP_SERVER_URL,
    ENV_PATH,
    TEST_CALLS,
    load_env_keys,
    load_servers,
    read_env_value,
)


class ApiError(RuntimeError):
    """调用层面的失败（HTTP 错误或 MCP 工具报错），区别于"数据对不上"。"""


class DataMismatch(RuntimeError):
    """API 通，但返回的数据和 GT 对不上。"""


# ── 调用封装 ──────────────────────────────────────────────────────────────────
def make_caller(client: httpx.AsyncClient, base_url: str, timeout: float):
    async def call(tool: str, args: dict[str, Any]) -> str:
        try:
            resp = await client.post(
                base_url, json={"tool_name": tool, "tool_args": args}, timeout=timeout
            )
        except Exception as exc:
            raise ApiError(f"{tool}: {exc}") from exc
        body = resp.text
        if resp.status_code >= 300:
            raise ApiError(f"{tool}: HTTP {resp.status_code}: {_flat(body)[:150]}")
        if _tool_errored(body):
            raise ApiError(f"{tool}: {_flat(body)[:150]}")
        return body

    return call


def _flat(s: str) -> str:
    return " ".join(s.split())


def _tool_errored(body: str) -> bool:
    """MCP 工具级错误：[{type:text, text:"Error: ..."}] 或整体含 error。"""
    try:
        data = json.loads(body)
    except Exception:
        return False
    if isinstance(data, dict) and "error" in str(data).lower():
        return True
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                text = item.get("text", "")
                if isinstance(text, str) and text.startswith("Error:"):
                    return True
    return False


def _texts(body: str) -> str:
    """把 MCP 的 [{type:text,text:...}] 拼成一段纯文本，便于做内容断言。"""
    try:
        data = json.loads(body)
    except Exception:
        return body
    if isinstance(data, list):
        return "\n".join(
            str(i.get("text", "")) for i in data if isinstance(i, dict)
        )
    return body


def _need(haystack: str, needle: str, what: str) -> None:
    if needle not in haystack:
        raise DataMismatch(f"{what}：返回里找不到 {needle!r}；实际 {_flat(haystack)[:120]}")


# ── 数据探针 ──────────────────────────────────────────────────────────────────
# 每个探针对应一个有状态服务：先发现 ID，再断言 GT 的内容事实。
Probe = Callable[[Callable[[str, dict], Awaitable[str]]], Awaitable[str]]


async def probe_mongodb(call) -> str:
    """GT task 68a398aa2f58036e8d45ee2e：2022-06 已送达订单恰好 10 单。
    库名/集合名/文档值都由 mongorestore 原样保留，可直接断言。"""
    body = await call("mongodb_count", {
        "database": "video_game_store",
        "collection": "Delivery Logistics",
        "query": {
            "Delivery Status": "Delivered",
            "Order Date": {
                "$gte": {"$date": "2022-06-01T00:00:00.000Z"},
                "$lt": {"$date": "2022-07-01T00:00:00.000Z"},
            },
        },
    })
    _need(_texts(body), "Found 10 documents", "2022-06 已送达订单数应为 10")
    return "video_game_store 已恢复，Delivery Logistics 文档数与 GT 一致"


async def probe_airtable(call) -> str:
    """GT task 689bd255c0422b257e7dfcf4：Car Dealership base 的 Digital Analytics 表。
    base_id 因 Copy base 而变，先按 base 名发现。"""
    bases = json.loads(_texts(await call("airtable_list_bases", {})))
    base = next((b for b in bases if b.get("name") == "Car Dealership"), None)
    if base is None:
        raise DataMismatch(
            f"没找到名为 'Car Dealership' 的 base；现有: {[b.get('name') for b in bases]}"
        )
    recs = json.loads(_texts(await call("airtable_list_records", {
        "base_id": base["id"], "table_name": "Digital Analytics",
    })))
    if not recs:
        raise DataMismatch("Digital Analytics 表存在但没有记录")
    hit = any(r.get("fields", {}).get("Page Name") == "Inventory" for r in recs)
    if not hit:
        raise DataMismatch(f"Digital Analytics 有 {len(recs)} 条记录，但找不到 Page Name='Inventory' 的行")
    return f"Car Dealership({base['id']}) 的 Digital Analytics 有 {len(recs)} 条记录，含 GT 的 Inventory 行"


async def probe_notion(call) -> str:
    """GT task 689cd6f8522029b7ad7b200a：Real Estate 库里 YearBuilt=2010 且未装修的房源。
    database_id 因重新导入而变，先按库标题发现。"""
    res = json.loads(_texts(await call(
        "notion_API-post-search", {"filter": {"property": "object", "value": "database"}}
    )))
    dbs = {}
    for r in res.get("results", []):
        title = "".join(t.get("plain_text", "") for t in (r.get("title") or []))
        if title:
            dbs[title] = r.get("id")
    if "Real Estate" not in dbs:
        raise DataMismatch(f"没找到 'Real Estate' 库；现有: {sorted(dbs)}")
    rows = json.loads(_texts(await call("notion_API-post-database-query", {
        "database_id": dbs["Real Estate"],
        "filter": {"and": [
            {"property": "YearBuilt", "number": {"equals": 2010}},
            {"property": "FurnishingStatus", "select": {"equals": "Unfurnished"}},
        ]},
    })))
    n = len(rows.get("results", []))
    if n == 0:
        raise DataMismatch("Real Estate 库存在，但 GT 的过滤条件(YearBuilt=2010/Unfurnished)查不到任何行")
    return f"6 张库在位；Real Estate 按 GT 条件命中 {n} 行（共 {len(dbs)} 张库）"


async def probe_slack(call) -> str:
    """GT task 689bd255c0422b257e7dfcc4：#movie-suggestions 频道的历史消息。
    channel_id 因重建 workspace 而变，先按频道名发现。

    除消息文本外还断言用户名能解析出来：conversations_history 返回
    UserID,UserName,RealName,...，而 GT claims 里有 'The user "mcpdumple" made a
    recommendation'、'@mcpdumle sent 4 messages' 这类按名字判定的断言。导入 Slack
    时若把用户整个排除掉，名字会解析不出来，这些任务就白跑了 —— 正确的导入选项是
    "请勿导入这些用户，但仅导入其消息"（保留名字、不发邀请、不占席位）。"""
    csv = _texts(await call("slack_channels_list", {"channel_types": "public_channel"}))
    chan_id = None
    for line in csv.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1].strip() == "#movie-suggestions":
            chan_id = parts[0].strip()
            break
    if not chan_id:
        found = [l.split(",")[1] for l in csv.splitlines()[1:] if len(l.split(",")) >= 2]
        raise DataMismatch(f"没找到 #movie-suggestions 频道（Slack 导出未导入？）；现有: {found}")
    hist = _texts(await call("slack_conversations_history", {"channel_id": chan_id}))
    _need(hist, "Akira", "#movie-suggestions 应含 GT 的历史消息")
    if "Omari West" not in hist and "hiphopluvr1989" not in hist:
        raise DataMismatch(
            "消息在，但发送者名字解析不出来（GT 里这条是 hiphopluvr1989 / Omari West）。"
            "导入时应选『请勿导入这些用户，但仅导入其消息』，不要整个排除用户"
        )
    return f"#movie-suggestions({chan_id}) 历史消息在位，且用户名可解析"


async def probe_google_workspace(call) -> str:
    """GT task 68993ef3cf3e953b8ab83fa3：2025-07 下半月的日历事件。
    事件标题由 .ics 导入原样保留，可直接断言。"""
    body = _texts(await call("google-workspace_list_events", {
        "timeMin": "2025-07-15T00:00:00Z",
        "timeMax": "2025-07-31T23:59:59Z",
        "maxResults": 1000,
    }))
    if body.strip() in ("[]", ""):
        raise DataMismatch("GT 日期范围(2025-07-15~31)内没有任何事件——.ics 未导入或导入到了别的账号")
    _need(body, "Virtual Jazz Workshop", "2025-07 应含 GT 事件")
    return "日历含 GT 的 2025-07 事件"


PROBES: dict[str, tuple[Probe, str]] = {
    "mongodb": (probe_mongodb, "video_game_store 的 mongorestore 是否成功"),
    "airtable": (probe_airtable, "Car Dealership base 是否已 Copy 且有数据"),
    "notion": (probe_notion, "Notion 导入的 6 张库是否在位"),
    "slack": (probe_slack, "Slack 导出的频道/消息是否已导入"),
    "google-workspace": (probe_google_workspace, "Google Calendar 的 .ics 是否已导入"),
}


# ── 结果 ──────────────────────────────────────────────────────────────────────
OK, BAD, FAIL, SKIP = "DATA OK", "DATA BAD", "API FAIL", "SKIP"


@dataclass
class Result:
    server: str
    kind: str  # "data" | "smoke"
    status: str
    elapsed: float = 0.0
    detail: str = ""


async def run_probe(call, server: str, timeout: float) -> Result:
    t0 = time.monotonic()
    try:
        detail = await PROBES[server][0](call)
        return Result(server, "data", OK, time.monotonic() - t0, detail)
    except DataMismatch as e:
        return Result(server, "data", BAD, time.monotonic() - t0, str(e))
    except ApiError as e:
        return Result(server, "data", FAIL, time.monotonic() - t0, str(e))
    except Exception as e:  # 解析失败等，按数据问题报，附类型便于排查
        return Result(server, "data", BAD, time.monotonic() - t0,
                      f"{type(e).__name__}: {str(e)[:150]}")


async def run_smoke(call, server: str, tool: str, args: dict) -> Result:
    t0 = time.monotonic()
    try:
        await call(tool, args)
        return Result(server, "smoke", OK, time.monotonic() - t0)
    except ApiError as e:
        return Result(server, "smoke", FAIL, time.monotonic() - t0, str(e))


async def main(base_url: str, timeout: float, concurrency: int,
               only: str | None, data_only: bool, smoke_only: bool) -> None:
    servers, required_vars = load_servers()
    env_keys = load_env_keys(ENV_PATH)

    def missing_keys(name: str) -> list[str]:
        if not servers.get(name, False):
            return []
        return [v for v in required_vars.get(name, []) if v not in env_keys]

    probes, smokes, skipped = [], [], []
    for name in servers:
        if only and name != only:
            continue
        if name in PROBES and not smoke_only:
            kind = "data"
        elif name in TEST_CALLS and not data_only:
            kind = "smoke"
        else:
            continue
        lack = missing_keys(name)
        if lack:
            skipped.append(Result(name, kind, SKIP, detail=f".env 缺: {', '.join(lack)}"))
        elif kind == "data":
            probes.append(name)
        else:
            smokes.append(name)

    if not probes and not smokes and not skipped:
        print("没有可跑的检查（--server 名字写错？）")
        return

    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        call = make_caller(client, base_url, timeout)

        async def guard(coro_fn, *a):
            async with sem:
                return await coro_fn(*a)

        tasks = [guard(run_probe, call, n, timeout) for n in probes]
        tasks += [guard(run_smoke, call, n, *TEST_CALLS[n]) for n in smokes]
        results = list(await asyncio.gather(*tasks))
    results += skipped

    icon = {OK: "✅", BAD: "❌", FAIL: "💥", SKIP: "⏭️"}
    data_res = [r for r in results if r.kind == "data"]
    smoke_res = [r for r in results if r.kind == "smoke"]

    if data_res:
        print(f"\n{'='*78}\n数据校验（回放 MCP-Atlas GT 调用，验证官方数据是否已导入）\n{'='*78}")
        for r in sorted(data_res, key=lambda x: x.server):
            print(f"{icon[r.status]} {r.status:9s} {r.server:18s} {r.elapsed:5.1f}s")
            if r.server in PROBES:
                print(f"       └─ 验的是: {PROBES[r.server][1]}")
            if r.detail:
                print(f"       └─ {r.detail}")

    if smoke_res:
        print(f"\n{'='*78}\n连通性冒烟（无专属数据的服务，只验 API 能否调通）\n{'='*78}")
        for r in sorted(smoke_res, key=lambda x: x.server):
            print(f"{icon[r.status]} {'OK' if r.status == OK else r.status:9s} {r.server:18s} {r.elapsed:5.1f}s")
            if r.detail:
                print(f"       └─ {r.detail}")

    n = {s: sum(1 for r in results if r.status == s) for s in (OK, BAD, FAIL, SKIP)}
    print(f"\n{'='*78}")
    print(f"合计 {len(results)} 项：✅ {n[OK]}   ❌ 数据不符 {n[BAD]}   💥 API 失败 {n[FAIL]}   ⏭️ 跳过 {n[SKIP]}")
    if n[BAD]:
        print("→ ❌ API 通但数据对不上：该服务的官方数据没导入（或导到了别的账号）。")
        print("   依赖它的评测任务会照跑但拿不到分——注意这类失败会压低分数。")
    if n[FAIL]:
        print("→ 💥 调用本身失败：检查 key、服务是否启动、MCP_SERVER_URL 端口。")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MCP 服务器数据+连通性检查")
    p.add_argument("--base-url", default=None,
                   help="MCP 服务地址（默认取 .env 的 MCP_SERVER_URL，否则 http://localhost:1984）")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--server", default=None, help="只测某一个服务")
    p.add_argument("--data-only", action="store_true", help="只跑 5 个有状态服务的数据校验")
    p.add_argument("--smoke-only", action="store_true", help="只跑连通性冒烟")
    a = p.parse_args()

    mcp_url = a.base_url or read_env_value(ENV_PATH, "MCP_SERVER_URL") or DEFAULT_MCP_SERVER_URL
    print(f"MCP 服务: {mcp_url.rstrip('/')}/call-tool")
    asyncio.run(main(f"{mcp_url.rstrip('/')}/call-tool", a.timeout, a.concurrency,
                     a.server, a.data_only, a.smoke_only))
