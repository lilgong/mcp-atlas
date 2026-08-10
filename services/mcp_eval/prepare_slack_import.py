#!/usr/bin/env python3
"""
把官方 Slack 导出的时间戳整体平移到"最近 90 天内"，产出可直接导入的 zip。

为什么需要：
    免费版 Slack 只显示最近 90 天的消息。官方导出 slack_mcp_eval_export.zip 里的消息是
    2025-12-01~10，早就超期，导进去也查不到 —— 依赖 slack 的评测任务会全部拿不到分。

为什么安全：
    - 33 条 slack 任务的题面里没有任何一条提到日期（已核查）。
    - 平移量取整天数，时分秒/微秒/相对间隔全部原样保留，
      所以"谁先发的"、"某人当天发了几条"这类相对/计数类 claim 不受影响。
    - ScaleAI 自己就这么干过：GT claims 记的是 2025-06-27 16:38:56.421649，
      而发布的导出里同一条消息是 2025-12-05 16:38:56.421649 —— 正好 +161 天，
      时分秒与微秒完全一致。也就是说官方平移过一次，只是忘了同步改 claims。

--fix-claims 就是补上官方漏掉的那一步：把绑定 slack 消息日期的 claim 一起平移。
只改能对上导出消息的日期，git commit 日期、电影上映日期等一律不碰。
官方 MCP-Atlas.csv 始终只读；派生结果写到 MCP-Atlas.slack-aligned.csv。

两个输入都是官方原版、只读、永不修改，每次运行都从它们重新派生：

    data_exports/slack_mcp_eval_export.zip  →  ..._<MMDD>.zip  （平移时间戳）
    services/mcp_eval/MCP-Atlas.csv  →  MCP-Atlas.slack-aligned.csv（平移 claim 日期）

不在上一轮结果上叠加，所以重复运行幂等：跑几次 md5 都一样，不会二次平移。

导入这一步没法自动化：Slack 的 workspace 导入是管理员浏览器流程，没有公开 API。
脚本打好包后会打印手动导入指引。

Usage:
    uv run prepare_slack_import.py                 # dry-run：产出 zip，只打印 claim 会怎么改
    uv run prepare_slack_import.py --fix-claims    # 同时把 MCP-Atlas.csv 从原版派生出来
    uv run prepare_slack_import.py --days-ago 3    # 让最新消息落到 3 天前（默认 3）
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parents[1]
SRC_ZIP = REPO_ROOT / "data_exports/slack_mcp_eval_export.zip"
# 产出带当天日期（MMDD），这样一眼看出手里/传上去的是哪一轮生成的
OUT_ZIP = REPO_ROOT / f"data_exports/slack_mcp_eval_export_{dt.date.today():%m%d}.zip"
# 和 zip 同样的路数：Git 中的官方原版只读，每次从它派生出目标文件，
# 绝不在改过的结果上再叠加。这样重复运行是幂等的。
ORIGIN_CSV = SCRIPT_DIR / "MCP-Atlas.csv"
CSV_PATH = SCRIPT_DIR / "MCP-Atlas.slack-aligned.csv"

DAY = 86400
UTC = dt.timezone.utc

# TRAJECTORY 单个字段就有几百 KB，远超 csv 模块默认的 128KB 上限
csv.field_size_limit(sys.maxsize)

# 导出里 <频道目录>/<YYYY-MM-DD>.json
DATE_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
# claims 里的两种日期写法。时间部分的分隔符可能是空格/T，也可能是 " at "
# （实际 claim 长这样：2025-06-27 at 16:38:56.421649+00:00）
ISO_DT = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})(?:(?:[ T]|\s+at\s+)(\d{2}):(\d{2}):(\d{2})(\.\d+)?)?"
)
PROSE_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),\s*(\d{4})\b"
)
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


# ── 读导出 ────────────────────────────────────────────────────────────────────
def read_export(zip_path: Path) -> dict[str, list]:
    """返回 {zip内路径: json内容}，跳过 __MACOSX/.DS_Store 等垃圾。"""
    out = {}
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.startswith("__MACOSX/") or name.endswith("/") or ".DS_Store" in name:
                continue
            if not name.endswith(".json"):
                continue
            with z.open(name) as f:
                try:
                    out[name] = json.load(f)
                except json.JSONDecodeError:
                    pass
    return out


def message_files(files: dict) -> dict[str, list]:
    """只取 <频道>/<日期>.json —— 顶层的 channels/users/... 不含消息。"""
    return {n: v for n, v in files.items()
            if DATE_FILE.match(Path(n).name) and isinstance(v, list)}


def all_ts(msg_files: dict) -> list[float]:
    return [float(m["ts"]) for msgs in msg_files.values() for m in msgs if m.get("ts")]


# ── 平移 ──────────────────────────────────────────────────────────────────────
def shift_message(m: dict, secs: int) -> None:
    """就地平移一条消息里所有时间字段。整天平移 → 时分秒/微秒天然不变。"""
    if m.get("ts"):
        m["ts"] = f"{float(m['ts']) + secs:.6f}"
    if isinstance(m.get("edited"), dict) and m["edited"].get("ts"):
        m["edited"]["ts"] = f"{float(m['edited']['ts']) + secs:.6f}"
    for f in m.get("files") or []:
        for k in ("created", "timestamp"):
            if isinstance(f.get(k), (int, float)):
                f[k] = int(f[k]) + secs


def shift_export(files: dict, secs: int) -> dict[str, list]:
    """返回平移后的 {新路径: 内容}；按日期命名的文件同步改名到新日期。"""
    out = {}
    for name, content in files.items():
        p = Path(name)
        if DATE_FILE.match(p.name) and isinstance(content, list):
            for m in content:
                shift_message(m, secs)
            # 文件名跟着消息的新 UTC 日期走（原导出就是按 UTC 日期命名的）
            if content and content[0].get("ts"):
                d = dt.datetime.fromtimestamp(float(content[0]["ts"]), UTC).date()
                name = str(p.with_name(f"{d.isoformat()}.json"))
        out[name] = content
    return out


def write_zip(files: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in sorted(files.items()):
            z.writestr(name, json.dumps(content, ensure_ascii=False, indent=1))


# ── 推导官方那次平移的偏移量 ──────────────────────────────────────────────────
def derive_legacy_offset(csv_path: Path, msg_files: dict) -> int | None:
    """推导原版 claim 的日期与导出消息日期之间差多少天，用"微秒指纹"把两边对上。

    这就是官方那次平移量（+161 天）：claims 停留在 2025-06-27，而发布的导出里同一条
    消息已是 2025-12-05，时分秒和微秒分毫不差。因为永远读原版，这个值恒定，不写死，
    让数据自己说话。"""
    fingerprints = {}  # (H,M,S,micros) -> 导出里那条消息的日期
    for msgs in msg_files.values():
        for m in msgs:
            if not m.get("ts"):
                continue
            # 微秒直接从 ts 字符串切，不走 float —— 否则 .421649 可能被舍成 .421648
            sec_str, _, frac_str = str(m["ts"]).partition(".")
            micro = int(frac_str.ljust(6, "0")[:6]) if frac_str else 0
            d = dt.datetime.fromtimestamp(int(sec_str), UTC)
            fingerprints[(d.hour, d.minute, d.second, micro)] = d.date()

    offsets = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if "slack_" not in (row.get("TRAJECTORY") or ""):
                continue
            for mt in ISO_DT.finditer(row.get("GTFA_CLAIMS") or ""):
                y, mo, da, hh, mi, se, frac = mt.groups()
                if hh is None or frac is None:
                    continue
                micro = int(frac[1:].ljust(6, "0")[:6])  # ".421649" -> 421649
                hit = fingerprints.get((int(hh), int(mi), int(se), micro))
                if hit:
                    offsets.add((hit - dt.date(int(y), int(mo), int(da))).days)
    if len(offsets) == 1:
        return offsets.pop()
    return None


# ── 改 claims ─────────────────────────────────────────────────────────────────
def fix_claims(origin_path: Path, out_path: Path, msg_files: dict, legacy: int,
               new_shift_days: int, apply: bool) -> list[tuple[str, str, str]]:
    """从官方原版派生出目标 CSV：把绑定 slack 消息的日期平移 legacy+new_shift_days 天。

    始终读 origin_path、写 out_path，不在上一轮的结果上叠加，所以重复运行幂等。
    判据：claim 日期 + legacy 必须正好落在导出的某个消息日期上 —— 这样 git commit
    日期、电影上映日期等一律不会被误伤。返回 [(task, 原文, 改后)]。"""
    msg_dates = {dt.datetime.fromtimestamp(t, UTC).date() for t in all_ts(msg_files)}
    total = legacy + new_shift_days
    changes: list[tuple[str, str, str]] = []

    def is_slack_date(d: dt.date) -> bool:
        return (d + dt.timedelta(days=legacy)) in msg_dates

    def sub_iso(mt: re.Match) -> str:
        y, mo, da = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
        try:
            d = dt.date(y, mo, da)
        except ValueError:
            return mt.group(0)
        if not is_slack_date(d):
            return mt.group(0)
        return mt.group(0).replace(d.isoformat(), (d + dt.timedelta(days=total)).isoformat(), 1)

    def sub_prose(mt: re.Match) -> str:
        mon, da, y = mt.group(1), int(mt.group(2)), int(mt.group(3))
        try:
            d = dt.date(y, MONTHS.index(mon) + 1, da)
        except ValueError:
            return mt.group(0)
        if not is_slack_date(d):
            return mt.group(0)
        n = d + dt.timedelta(days=total)
        return f"{MONTHS[n.month - 1]} {n.day}, {n.year}"

    with open(origin_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        rows = list(reader)

    for row in rows:
        if "slack_" not in (row.get("TRAJECTORY") or ""):
            continue
        old = row.get("GTFA_CLAIMS") or ""
        new = PROSE_DATE.sub(sub_prose, ISO_DT.sub(sub_iso, old))
        if new != old:
            changes.append((row.get("TASK", "?"), old, new))
            row["GTFA_CLAIMS"] = new

    if apply:
        backup_note = ""
        if out_path.exists():
            shutil.copy2(out_path, out_path.with_suffix(out_path.suffix + ".bak"))
            backup_note = f"（上一版备份为 {out_path.name}.bak）"
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"  {origin_path.name} → {out_path.name}{backup_note}")
    return changes


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="平移官方 Slack 导出的时间戳到 90 天窗口内")
    ap.add_argument("--days-ago", type=int, default=3,
                    help="让最新一条消息落到几天前（默认 3，尽量吃满 90 天窗口）")
    ap.add_argument("--fix-claims", action="store_true",
                    help="从官方 CSV 派生 Slack 对齐版并同步修正绑定消息日期的 claim")
    ap.add_argument("--src", type=Path, default=SRC_ZIP, help="官方原版导出 zip（只读）")
    ap.add_argument("--out", type=Path, default=OUT_ZIP, help="产出的 zip，用它导入 Slack")
    ap.add_argument("--origin", type=Path, default=ORIGIN_CSV,
                    help="官方原版 CSV（只读）；每次都从它派生，保证幂等")
    ap.add_argument("--csv", type=Path, default=CSV_PATH,
                    help="产出的 Slack 对齐版 CSV，免费 Slack 评测使用")
    a = ap.parse_args()

    if not a.src.exists():
        print(f"找不到导出文件: {a.src}", file=sys.stderr)
        return 1

    files = read_export(a.src)
    msgs = message_files(files)
    ts = all_ts(msgs)
    if not ts:
        print("导出里没有消息？", file=sys.stderr)
        return 1

    newest = dt.datetime.fromtimestamp(max(ts), UTC)
    oldest = dt.datetime.fromtimestamp(min(ts), UTC)
    today = dt.datetime.now(UTC)
    target = today - dt.timedelta(days=a.days_ago)
    shift_days = (target.date() - newest.date()).days   # 整天 → 时分秒/微秒不变
    secs = shift_days * DAY

    print("=" * 74)
    print(f"源文件      : {a.src.name}")
    print(f"消息        : {len(ts)} 条，{len(msgs)} 个频道文件")
    print(f"原始范围    : {oldest.date()} ~ {newest.date()}  (最新距今 {(today - newest).days} 天)")
    print(f"平移        : +{shift_days} 天")
    print(f"平移后范围  : {(oldest + dt.timedelta(days=shift_days)).date()} ~ "
          f"{(newest + dt.timedelta(days=shift_days)).date()}")
    expire = (oldest + dt.timedelta(days=shift_days + 90)).date()
    print(f"⚠️ 免费版 90 天窗口：最早的消息将于 {expire} 再次隐藏，届时需重跑本脚本并重导")
    print("=" * 74)

    if not a.origin.exists():
        print(f"\n⚠️ 找不到官方原版 {a.origin.name}，跳过 claim 处理。"
              f"\n   它是 claim 平移的唯一基准（每次都从它派生，保证幂等）。")
        legacy = None
    else:
        legacy = derive_legacy_offset(a.origin, msgs)

    if legacy is not None:
        print(f"\n官方那次平移量: +{legacy} 天（微秒指纹把原版 claim 与导出消息对上得到，非写死）")
        print(f"claim 总平移 = {legacy} + {shift_days} = {legacy + shift_days} 天")
        changes = fix_claims(a.origin, a.csv, msgs, legacy, shift_days, apply=a.fix_claims)
        if changes:
            head = "已修正的 claim" if a.fix_claims else "将会修正的 claim（加 --fix-claims 才真正写入）"
            print(f"\n{head}: {len(changes)} 条任务")
            for task, old, new in changes:
                print(f"\n  [task {task[:24]}]")
                for o, n in zip(old.split("', '"), new.split("', '")):
                    if o != n:
                        print(f"    - {o.strip(chr(39))[:96]}")
                        print(f"    + {n.strip(chr(39))[:96]}")
        else:
            print("\n没有需要修正的 claim（原版里没有绑定 slack 消息日期的）")
    elif a.fix_claims:
        print("\n⚠️ 无法推导偏移量，跳过 claim 修正（--fix-claims 未生效）")

    shifted = shift_export(files, secs)
    write_zip(shifted, a.out)
    print(f"\n✅ 已生成: {a.out}")

    print(f"""
下一步（导入必须手动做，Slack 没有导入 API）：
  1. 下载到本地:
       scp <server>:{a.out} .
  2. 浏览器打开 https://<你的workspace>.slack.com/services/import
     选 "Slack" 导入方式，上传这个 zip
  3. ⚠️ 若之前导入过旧数据，先清掉频道里的旧消息，否则会重复
  4. 重启 MCP 容器（--env-file 只在启动时读一次），然后验收:
       uv run test_server_v2.py --server slack --base-url http://localhost:1984
  5. 免费 Slack 评测把 .env 设为:
       MCP_COMPLETION_INPUT={a.csv.name}
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
