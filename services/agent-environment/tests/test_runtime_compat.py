from __future__ import annotations

import json
from pathlib import Path

from agent_environment.oxylabs_mcp_compat import normalize_scraper_payload
from agent_environment.mcp_router import DEFAULT_TOOL_CALL_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[3]
AGENT_ROOT = ROOT / "services" / "agent-environment"


def test_oxylabs_universal_payload_gets_required_source():
    original = {"url": "https://example.com"}
    assert normalize_scraper_payload(original) == {
        "url": "https://example.com",
        "source": "universal",
    }
    assert original == {"url": "https://example.com"}


def test_oxylabs_search_and_explicit_source_are_unchanged():
    search = {"query": "example", "url": "https://example.com"}
    explicit = {"url": "https://example.com", "source": "custom"}
    assert normalize_scraper_payload(search) is search
    assert normalize_scraper_payload(explicit) is explicit


def test_runtime_templates_use_guarded_compatibility_entrypoints():
    shared = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]
    local = json.loads(
        (
            ROOT
            / "services"
            / "task-sandbox"
            / "local_mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]

    assert shared["oxylabs"]["args"] == [
        "-m",
        "agent_environment.oxylabs_mcp_compat",
    ]
    assert shared["ddg-search"]["args"][-1].endswith("ddg_mcp_compat.py")
    assert "duckduckgo-mcp-server[browser]==0.6.0" in shared["ddg-search"]["args"]
    assert shared["osm-mcp-server"]["args"][-1].endswith("osm_mcp_compat.py")
    assert shared["met-museum"]["args"] == [
        "/agent-environment/metmuseum_mcp_compat.mjs",
    ]
    expected_filesystem = [
        "/agent-environment/filesystem_server_compat.mjs",
        "/data",
    ]
    assert shared["filesystem"]["args"] == expected_filesystem
    assert local["filesystem"]["args"] == expected_filesystem
    compat = (
        ROOT / "services" / "agent-environment" / "filesystem_server_compat.mjs"
    ).read_text(encoding="utf-8")
    assert '["list_directory_with_sizes", "directory_tree"]' in compat


def test_osm_has_ordered_fallbacks_inside_a_sufficient_router_budget():
    compat = (
        AGENT_ROOT / "src" / "agent_environment" / "osm_mcp_compat.py"
    ).read_text(encoding="utf-8")
    assert (
        'f"{UPSTREAM_OVERPASS_URL},{SECONDARY_OVERPASS_URL},"' in compat
    )
    assert "OVERPASS_ATTEMPT_TIMEOUT_SECONDS = 45" in compat
    assert '"User-Agent": "mcp-atlas/1.0' in compat
    assert "{406, 429, 500, 502, 503, 504}" in compat
    assert DEFAULT_TOOL_CALL_TIMEOUT_SECONDS >= 3 * 45
    assert "install_neighborhood_optimization()" in compat
    assert "tool.fn = optimized_analyze_neighborhood" in compat


def test_context7_uses_authenticated_schema_compatible_release():
    servers = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]
    context7 = servers["context7"]
    assert context7["args"] == ["-y", "@upstash/context7-mcp@1.0.33"]
    assert context7["env"] == {
        "CONTEXT7_API_KEY": "${CONTEXT7_API_KEY}",
    }


def test_github_uses_prebuilt_pinned_server_instead_of_runtime_npx():
    servers = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]
    github = servers["github"]
    assert github["command"] == "node"
    assert github["args"] == [
        "/mnt/node_modules/@smithery/mcp-github/dist/cli.js",
    ]

    install_script = (
        AGENT_ROOT / "dev_scripts" / "install_mcp_packages.sh"
    ).read_text(encoding="utf-8")
    revision = "68368436034fb0003a6d8ed91afc9d0a64142b84"
    assert f'GITHUB_MCP_REVISION="{revision}"' in install_script
    assert "@smithery/mcp-github/dist/cli.js" in install_script


def test_python_mcp_servers_pin_their_sdk():
    servers = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]
    expected = {
        "arxiv": "mcp==1.28.1",
        "calculator": "mcp==1.28.1",
        "cli-mcp-server": "mcp==1.28.1",
        "ddg-search": "mcp==1.28.1",
        "fetch": "mcp==1.28.1",
        "git": "mcp==1.25.0",
        "osm-mcp-server": "mcp==1.28.1",
        "pubmed": "mcp==1.28.1",
        "twelvedata": "mcp==1.28.1",
        "weather-data": "mcp==1.28.1",
        "wikipedia": "mcp==1.28.1",
    }
    for server, pin in expected.items():
        args = servers[server]["args"]
        index = args.index("--with")
        assert args[index + 1] == pin, server


def test_git_backed_python_servers_pin_commits():
    servers = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]
    for server in ("pubmed", "weather-data"):
        source = servers[server]["args"][
            servers[server]["args"].index("--from") + 1
        ]
        revision = source.rpartition("@")[2]
        assert len(revision) == 40
        assert all(character in "0123456789abcdef" for character in revision)


def test_wikipedia_uses_user_agent_fixed_release():
    servers = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]
    args = servers["wikipedia"]["args"]
    assert "wikipedia-mcp==2.0.1" in args
    assert not any("735590286fbe" in arg for arg in args)
