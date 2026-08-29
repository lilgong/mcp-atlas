import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from prepare_slack_import import (
    adapt_enabled_tools,
    fix_claims,
    repair_year_arithmetic,
    shift_bound_dates,
    trajectory_uses_slack,
)


class PrepareSlackImportTests(unittest.TestCase):
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

            changes, _, _ = fix_claims(
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

    def test_adapts_runtime_tool_catalog_without_touching_other_fields(self):
        rows = [
            {
                "TASK": "task-1",
                "PROMPT": "unchanged",
                "ENABLED_TOOLS": json.dumps(
                    [
                        "clinicaltrialsgov-mcp-server_clinicaltrials_list_studies",
                        "rijksmuseum-server_search_artwork",
                        "brave-search_brave_web_search",
                    ]
                ),
            }
        ]

        renamed, removed = adapt_enabled_tools(rows)

        self.assertEqual(
            json.loads(rows[0]["ENABLED_TOOLS"]),
            [
                "clinicaltrialsgov-mcp-server_clinicaltrials_search_studies",
                "brave-search_brave_web_search",
            ],
        )
        self.assertEqual("unchanged", rows[0]["PROMPT"])
        self.assertEqual(1, len(renamed))
        self.assertEqual([("task-1", "rijksmuseum-server_search_artwork")], removed)

    def test_preserves_dict_tool_metadata_when_renaming(self):
        rows = [
            {
                "TASK": "task-2",
                "ENABLED_TOOLS": json.dumps(
                    [
                        {
                            "name": "clinicaltrialsgov-mcp-server_clinicaltrials_list_studies",
                            "requiredParams": ["query"],
                        }
                    ]
                ),
            }
        ]

        adapt_enabled_tools(rows)

        self.assertEqual(
            json.loads(rows[0]["ENABLED_TOOLS"]),
            [
                {
                    "name": "clinicaltrialsgov-mcp-server_clinicaltrials_search_studies",
                    "requiredParams": ["query"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
