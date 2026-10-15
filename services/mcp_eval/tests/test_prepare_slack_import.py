import copy
import csv
import datetime as dt
import json
import os
import time
from decimal import Decimal
import tempfile
import unittest
from pathlib import Path

from prepare_slack_import import (
    fix_claims,
    read_export,
    message_files,
    derive_legacy_offset,
    shift_message,
    shift_export,
    repair_year_arithmetic,
    shift_bound_dates,
    trajectory_uses_slack,
)


class PrepareSlackImportTests(unittest.TestCase):
    def test_reviewed_marketing_claim_survives_derivation(self):
        import ast
        from decimal import Decimal
        origin = Path(__file__).resolve().parents[1] / "MCP-Atlas-origin.csv"
        with origin.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 500)
        task = "688a441c1a0d2e5c93853e93"
        row = next(value for value in rows if value["TASK"] == task)
        claims = ast.literal_eval(row["GTFA_CLAIMS"])
        difference = Decimal(65540) - Decimal(39261)
        self.assertEqual(difference, 26279)
        self.assertEqual(round(difference / Decimal(39261) * 100, 2), Decimal("66.93"))
        self.assertIn("$26,279", claims[2])
        self.assertNotIn("$27,279", row["GTFA_CLAIMS"])
        source = Path(__file__).resolve().parents[3] / "data_exports/slack_mcp_eval_export.zip"
        messages = message_files(read_export(source))
        self.assertEqual(derive_legacy_offset(origin, messages), 161)
        with tempfile.TemporaryDirectory() as raw:
            derived = Path(raw) / "MCP-Atlas.csv"
            for _ in range(2):
                fix_claims(origin, derived, messages, 161, 259, apply=True)
                with derived.open(newline="", encoding="utf-8") as handle:
                    converted = list(csv.DictReader(handle))
                self.assertEqual(len(converted), len(rows))
                for source_row, converted_row in zip(rows, converted):
                    self.assertEqual(source_row["TASK"], converted_row["TASK"])
                    for field in source_row.keys() - {"PROMPT", "GTFA_CLAIMS"}:
                        self.assertEqual(source_row[field], converted_row[field])
                actual = next(value for value in converted if value["TASK"] == task)
                self.assertEqual(actual["GTFA_CLAIMS"], row["GTFA_CLAIMS"])
                self.assertEqual(actual["PROMPT"], row["PROMPT"])
                self.assertEqual(actual["TRAJECTORY"], row["TRAJECTORY"])

    def test_detects_actual_slack_tool_calls_not_slackr_source_text(self):
        actual = json.dumps([
            {
                "role": "assistant",
                "tool_calls": [{
                    "function": {
                        "name": "slack_conversations_history",
                        "arguments": "{}",
                    }
                }],
            }
        ])
        repository_only = json.dumps([
            {
                "role": "assistant",
                "content": "The source changed slackr_msg().",
                "tool_calls": [{
                    "function": {
                        "name": "git_git_log",
                        "arguments": "{}",
                    }
                }],
            }
        ])

        self.assertTrue(trajectory_uses_slack(actual))
        self.assertFalse(trajectory_uses_slack(repository_only))

    def test_shifts_slack_dates_in_all_supported_forms(self):
        # The official export moved 2025-06-27 to 2025-12-05 (+161 days),
        # and this local run moves the export another 259 days.
        msg_dates = {dt.date(2025, 12, 5)}
        cases = {
            "2025-06-27 at 16:38:56.421649+00:00": (
                "2026-08-21 at 16:38:56.421649+00:00"
            ),
            "June 27, 2025": "August 21, 2026",
            "27 June 2025": "21 August 2026",
            "june 2025": "august 2026",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                actual, years = shift_bound_dates(source, msg_dates, 161, 259)
                self.assertEqual(expected, actual)
                self.assertEqual({(2025, 2026)}, years)

    def test_preserves_unrelated_and_ambiguous_dates(self):
        msg_dates = {
            dt.date(2025, 1, 1),
            dt.date(2025, 1, 31),
        }
        text = "The film opened January 21, 1992; activity was in January 2025."

        actual, years = shift_bound_dates(text, msg_dates, 0, 31)

        self.assertEqual(text, actual)
        self.assertEqual(set(), years)

    def test_repairs_only_verified_year_arithmetic(self):
        text = "4 times 2025 equals 8100; 3 * 2025 = 9999."

        actual = repair_year_arithmetic(text, {(2025, 2026)})

        self.assertEqual("4 times 2026 equals 8104; 3 * 2025 = 9999.", actual)

    def test_fix_claims_rewrites_prompt_and_claims_together(self):
        fields = ["TASK", "ENABLED_TOOLS", "PROMPT", "GTFA_CLAIMS", "TRAJECTORY"]
        row = {
            "TASK": "slack-date-task",
            "ENABLED_TOOLS": "[]",
            "PROMPT": "On 27 June 2025, who posted first?",
            "GTFA_CLAIMS": "June 27, 2025; 4 times 2025 equals 8100.",
            "TRAJECTORY": json.dumps([{
                "role": "assistant",
                "tool_calls": [{
                    "function": {
                        "name": "slack_conversations_search_messages",
                        "arguments": "{}",
                    }
                }],
            }]),
        }
        timestamp = dt.datetime(
            2025, 12, 5, 16, 38, tzinfo=dt.timezone.utc
        ).timestamp()
        msg_files = {"channel/2025-12-05.json": [{"ts": str(timestamp)}]}

        with tempfile.TemporaryDirectory() as raw:
            origin = Path(raw) / "origin.csv"
            output = Path(raw) / "derived.csv"
            with origin.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)

            changes = fix_claims(
                origin, output, msg_files, legacy=161, new_shift_days=259, apply=True
            )
            with output.open(newline="", encoding="utf-8") as handle:
                derived = next(csv.DictReader(handle))

        self.assertEqual({"PROMPT", "GTFA_CLAIMS"}, {item[1] for item in changes})
        self.assertEqual("On 21 August 2026, who posted first?", derived["PROMPT"])
        self.assertEqual(
            "August 21, 2026; 4 times 2026 equals 8104.",
            derived["GTFA_CLAIMS"],
        )

    def test_reviewed_baseline_already_has_runtime_tool_catalog(self):
        from mcp_completion.tool_policy import OFFICIAL_RETIRED_SERVERS

        origin = Path(__file__).resolve().parents[1] / "MCP-Atlas-origin.csv"
        with origin.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        tools = [item if isinstance(item, str) else item["name"]
                 for row in rows for item in json.loads(row["ENABLED_TOOLS"])]
        self.assertIn("clinicaltrialsgov-mcp-server_clinicaltrials_search_studies", tools)
        self.assertNotIn("clinicaltrialsgov-mcp-server_clinicaltrials_list_studies", tools)
        retired = tuple(f"{name}_" for name in OFFICIAL_RETIRED_SERVERS)
        self.assertFalse(any(name.startswith(retired) for name in tools))

    def test_derivation_never_rewrites_tool_catalog(self):
        row = {
            "TASK": "custom-baseline", "PROMPT": "unchanged",
            "GTFA_CLAIMS": "unchanged", "TRAJECTORY": "[]",
            "ENABLED_TOOLS": json.dumps([
                {"name": "clinicaltrialsgov-mcp-server_clinicaltrials_list_studies",
                 "requiredParams": ["query"]},
                "rijksmuseum-server_search_artwork",
            ]),
        }
        with tempfile.TemporaryDirectory() as raw:
            origin, output = Path(raw) / "origin.csv", Path(raw) / "derived.csv"
            with origin.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(fix_claims(origin, output, {}, 161, 259, True), [])
            with output.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(next(csv.DictReader(handle)), row)

    def test_shifts_attachment_and_thread_timestamps_exactly(self):
        message = {
            "ts": "1764952736.421649", "thread_ts": "1764952735.999999",
            "latest_reply": "1764952737.000001",
            "edited": {"ts": "1764952738.123456"},
            "replies": [{"user": "unchanged", "ts": "1764952737.000001"}],
            "files": [{"created": 1764623085, "timestamp": 1764623085,
                       "updated": 1764630515, "update_notification": 1764623086,
                       "size": 123}],
            "text": "Keep 2025-12-05 and every other part of the message.",
        }
        for days in (-306, 0, 306):
            with self.subTest(days=days):
                shifted = copy.deepcopy(message)
                seconds = days * 86400
                shift_message(shifted, seconds)
                for key in ("ts", "thread_ts", "latest_reply"):
                    self.assertEqual(Decimal(shifted[key]) - Decimal(message[key]), seconds)
                self.assertEqual(Decimal(shifted["edited"]["ts"]) - Decimal(message["edited"]["ts"]), seconds)
                self.assertEqual(Decimal(shifted["replies"][0]["ts"]) - Decimal(message["replies"][0]["ts"]), seconds)
                for key in ("created", "timestamp", "updated", "update_notification"):
                    self.assertEqual(shifted["files"][0][key] - message["files"][0][key], seconds)
                self.assertEqual(shifted["files"][0]["size"], 123)
                self.assertEqual(shifted["text"], message["text"])
                self.assertEqual(shifted["replies"][0]["user"], "unchanged")

    @unittest.skipUnless(hasattr(time, "tzset"), "requires process timezone support")
    def test_export_uses_utc_at_midnight_and_dst_boundaries(self):
        source = {
            "channel/2025-03-09.json": [
                {"ts": str(int(dt.datetime(2025, 3, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp())) + ".000001"},
                {"ts": str(int(dt.datetime(2025, 3, 9, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp())) + ".999999"},
            ],
        }
        previous = os.environ.get("TZ")
        results = []
        try:
            for zone in ("UTC", "Asia/Shanghai", "America/New_York"):
                os.environ["TZ"] = zone
                time.tzset()
                result = shift_export(copy.deepcopy(source), 86400)
                self.assertEqual(list(result), ["channel/2025-03-10.json"])
                results.append(result)
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0], results[2])
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()


if __name__ == "__main__":
    unittest.main()
