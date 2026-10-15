#!/usr/bin/env python3
"""Rebuild monthly model usage summaries without loading prompt logs into memory."""

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re


def summarize(root: Path, month: str | None = None) -> None:
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not re.fullmatch(r"\d{4}-\d{2}", directory.name):
            continue
        if month and directory.name != month:
            continue
        totals = defaultdict(lambda: [0, 0, 0, 0])
        invalid = 0
        missing_usage = 0
        files = sorted(directory.glob("*token_usage_*.jsonl"))
        for path in files:
            kind = "评分模型" if path.name.startswith("eval_") else "生成模型"
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("Expected a JSON object")
                        values = [record.get(key) for key in (
                            "prompt_tokens", "completion_tokens", "total_tokens"
                        )]
                        if any(value is not None and (
                            not isinstance(value, int) or isinstance(value, bool) or value < 0
                        ) for value in values):
                            raise ValueError("Invalid token count")
                    except (ValueError, TypeError):
                        invalid += 1
                        continue
                    missing_usage += any(value is None for value in values)
                    row_kind = "测试探针" if record.get("probe") else kind
                    row = totals[(row_kind, str(record.get("model") or "unknown"))]
                    row[0] += 1
                    for index, value in enumerate(values, 1):
                        row[index] += value or 0
            print(f"Read {directory.name}/{path.name}", flush=True)
        total = [sum(row[index] for row in totals.values()) for index in range(4)]
        lines = [
            f"# MCP-Atlas API 用量：{directory.name}", "",
            f"更新时间：{datetime.now().astimezone().isoformat(timespec='seconds')}", "",
            "调用次数 = 原始 token 日志的响应记录数；包含已记录的重试响应，"
            "不包含失败请求或未写入 token 日志的响应。不是完整 HTTP 请求计数。",
            "月份按原始文件所在月份统计；缺失 token 字段按 0 汇总。", "",
            "| 类型 | 模型 | 调用次数 | 输入 tokens | 输出 tokens | 总 tokens |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for (kind, model), row in sorted(totals.items()):
            model = model.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {kind} | {model} | " + " | ".join(f"{v:,}" for v in row) + " |")
        lines.extend([
            "| 合计 | 全部（含测试探针） | " + " | ".join(f"{v:,}" for v in total) + " |",
            "", f"原始文件：{len(files)} 个；无效记录：{invalid} 条；"
            f"缺失 token 字段的记录：{missing_usage} 条。", "",
            "此汇总是生成时的快照；新增调用后重新运行汇总脚本即可更新。", "",
        ])
        temporary = directory / "summary.md.tmp"
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(directory / "summary.md")
        print(f"Wrote {directory / 'summary.md'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("api_usage_log/mcp-atlas"))
    parser.add_argument("--month", help="Only rebuild this month (YYYY-MM)")
    args = parser.parse_args()
    summarize(args.root, args.month)
