import os
import tempfile
import unittest
from pathlib import Path

from mcp_completion.runtime_log import write_runtime_event
from mcp_completion.tool_policy import (
    ToolRoute,
    effective_enabled_servers,
    is_cloud_data_write,
    route_for_tool,
    server_for_tool,
    servers_for_enabled_tools,
    shared_routable_servers,
)


class ToolPolicyTests(unittest.TestCase):
    def test_local_mutations_are_task_isolated(self):
        for name in (
            "filesystem_write_file",
            "git_git_commit",
            "memory_create_entities",
            "mongodb_insert-many",
            "desktop-commander_start_process",
            "mcp-code-executor_execute_code",
        ):
            self.assertEqual(route_for_tool(name), ToolRoute.TASK_LOCAL, name)

    def test_download_caches_are_task_isolated_with_network(self):
        self.assertEqual(
            route_for_tool("arxiv_download_paper"), ToolRoute.TASK_NETWORK
        )
        self.assertEqual(
            route_for_tool("pubmed_download_pubmed_pdf"),
            ToolRoute.TASK_NETWORK,
        )

    def test_cloud_reads_remain_available(self):
        for name in (
            "airtable_list_records",
            "github_get_repository",
            "google-workspace_list_events",
            "lara-translate_translate",
            "notion_API-post-database-query",
            "notion_API-post-search",
            "slack_conversations_history",
        ):
            self.assertFalse(is_cloud_data_write(name), name)
            self.assertEqual(route_for_tool(name), ToolRoute.CLOUD, name)

    def test_cloud_writes_fail_closed(self):
        for name in (
            "airtable_create_record",
            "github_push_files",
            "google-workspace_send_email",
            "lara-translate_create_memory",
            "notion_API-post-page",
            "slack_conversations_add_message",
            "github_future_mutation_tool",
        ):
            self.assertTrue(is_cloud_data_write(name), name)
            self.assertEqual(
                route_for_tool(name), ToolRoute.BLOCKED_CLOUD_WRITE, name
            )

    def test_dependency_install_matches_official_tool_surface(self):
        self.assertEqual(
            route_for_tool("mcp-code-executor_install_dependencies"),
            ToolRoute.TASK_LOCAL,
        )

    def test_e2b_remains_available_for_official_evaluation(self):
        self.assertEqual(
            route_for_tool("e2b-server_run_code"),
            ToolRoute.CLOUD,
        )

    def test_longest_server_prefix_and_legacy_mongo(self):
        self.assertEqual(
            server_for_tool("mcp-server-code-runner_run-code"),
            "mcp-server-code-runner",
        )
        self.assertEqual(server_for_tool("MongoDB_find"), "mongodb")

    def test_enabled_tool_servers_cover_full_task_surface(self):
        servers = servers_for_enabled_tools(
            '["context7_resolve-library-id", "pubmed_search_pubmed_advanced"]'
        )
        self.assertEqual(["context7", "pubmed"], servers)

    def test_official_retired_server_does_not_exclude_task(self):
        servers = servers_for_enabled_tools(
            '["rijksmuseum-server_search_artwork", "brave-search_brave_web_search"]'
        )
        self.assertEqual(["brave-search"], servers)

    def test_enabled_tool_servers_reject_unknown_tool(self):
        with self.assertRaisesRegex(ValueError, "unknown tool"):
            servers_for_enabled_tools('["unknown-server_tool"]')

    def test_isolated_availability_ignores_shared_local_status(self):
        enabled = effective_enabled_servers(
            ["airtable", "git"],
            isolation_enabled=True,
            task_data_configured=True,
            task_mongo_configured=True,
        )
        self.assertIn("airtable", enabled)
        self.assertIn("git", enabled)
        self.assertIn("mongodb", enabled)
        self.assertIn("arxiv", enabled)

    def test_isolated_availability_fails_closed_without_fixtures(self):
        enabled = effective_enabled_servers(
            ["airtable", "git", "mongodb", "arxiv"],
            isolation_enabled=True,
            task_data_configured=False,
            task_mongo_configured=False,
        )
        self.assertEqual(["airtable"], enabled)

    def test_legacy_availability_preserves_shared_status(self):
        enabled = effective_enabled_servers(
            ["airtable", "git"],
            isolation_enabled=False,
            task_data_configured=False,
            task_mongo_configured=False,
        )
        self.assertEqual(["airtable", "git"], enabled)

    def test_explicit_allowlist_can_exclude_task_network_server(self):
        enabled = effective_enabled_servers(
            ["airtable"],
            isolation_enabled=True,
            task_data_configured=True,
            task_mongo_configured=True,
            allowed_servers=["airtable", "arxiv", "git"],
        )
        self.assertEqual(["airtable", "arxiv", "git"], enabled)
        self.assertNotIn("pubmed", enabled)

    def test_transiently_degraded_discovered_server_remains_routable(self):
        routable, reconnectable, online_count = shared_routable_servers(
            {
                "servers": [
                    ["airtable", "OK"],
                    ["oxylabs", "ERROR_NOT_ONLINE"],
                    ["never-started", "ERROR_NOT_ONLINE"],
                ],
                "details": [
                    {"name": "airtable", "tool_count": 5},
                    {"name": "oxylabs", "tool_count": 4},
                    {"name": "never-started", "tool_count": 0},
                ],
            }
        )
        self.assertEqual(["airtable", "oxylabs"], routable)
        self.assertEqual(["oxylabs"], reconnectable)
        self.assertEqual(1, online_count)

    def test_old_health_format_remains_supported(self):
        self.assertEqual(
            (["airtable", "github"], [], 2),
            shared_routable_servers(
                {"enabled_servers": ["github", "airtable"]}
            ),
        )


class RuntimeLogTests(unittest.TestCase):
    def test_runtime_log_redacts_configured_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            old_dir = os.environ.get("MCP_RUNTIME_LOG_DIR")
            old_secret = os.environ.get("TEST_SECRET_TOKEN")
            try:
                os.environ["MCP_RUNTIME_LOG_DIR"] = directory
                os.environ["TEST_SECRET_TOKEN"] = "secret-value-123456"
                path = write_runtime_event(
                    "test",
                    "redaction_probe",
                    payload="before secret-value-123456 after",
                )
                text = Path(path).read_text(encoding="utf-8")
                self.assertNotIn("secret-value-123456", text)
                self.assertIn("<redacted>", text)
            finally:
                if old_dir is None:
                    os.environ.pop("MCP_RUNTIME_LOG_DIR", None)
                else:
                    os.environ["MCP_RUNTIME_LOG_DIR"] = old_dir
                if old_secret is None:
                    os.environ.pop("TEST_SECRET_TOKEN", None)
                else:
                    os.environ["TEST_SECRET_TOKEN"] = old_secret


if __name__ == "__main__":
    unittest.main()
