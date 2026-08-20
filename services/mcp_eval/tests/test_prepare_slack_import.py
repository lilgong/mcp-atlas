import json
import unittest

from prepare_slack_import import adapt_enabled_tools


class PrepareSlackImportTests(unittest.TestCase):
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
