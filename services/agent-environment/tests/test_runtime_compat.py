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


def test_oxylabs_vendor_patch_accepts_synchronous_responses_and_preserves_http_errors():
    source = (
        AGENT_ROOT
        / "vendor"
        / "yibu-patched"
        / "oxylabs_mcp"
        / "utils.py"
    ).read_text(encoding="utf-8")
    scrape = source[source.index("    async def scrape("):source.index(
        "\n\n\n@asynccontextmanager"
    )]

    assert scrape.index("response.raise_for_status()") < scrape.index(
        "response_json: dict[str, typing.Any] = response.json()"
    )
    assert 'job = response_json.get("job")' in scrape
    assert "response_json['job']" not in scrape


def test_runtime_templates_use_only_required_compatibility_entrypoints():
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
    assert shared["ddg-search"]["args"][-1] == "duckduckgo-mcp-server"
    assert "duckduckgo-mcp-server[browser]==0.6.1" in shared["ddg-search"]["args"]
    assert shared["osm-mcp-server"]["args"][-1].endswith("osm_mcp_compat.py")
    assert shared["met-museum"]["args"] == [
        "/agent-environment/src/agent_environment/run_node_mcp.cjs",
        "metmuseum-mcp@1.0.0",
        "dist/index.js",
    ]
    expected_filesystem = [
        "/agent-environment/src/agent_environment/run_node_mcp.cjs",
        "@modelcontextprotocol/server-filesystem@2026.7.10",
        "dist/index.js",
        "/data",
    ]
    assert shared["filesystem"]["args"] == expected_filesystem
    assert local["filesystem"]["args"] == expected_filesystem


def test_cli_uses_read_only_flag_allowlist_and_git_matches_official_release():
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
    for servers in (shared, local):
        flags = set(servers["cli-mcp-server"]["env"]["ALLOWED_FLAGS"].split(","))
        assert "-al" in flags
        assert "-maxdepth" in flags
        assert "-exec" not in flags
        assert "-delete" not in flags
        assert flags != {"ALL"}
    assert "mcp-server-git==2026.7.10" in shared["git"]["args"]
    install_script = (
        AGENT_ROOT / "dev_scripts" / "install_mcp_packages.sh"
    ).read_text(encoding="utf-8")
    assert (
        "mcp-server-git==2026.7.10 --with mcp==1.25.0"
        in install_script
    )


def test_osm_has_ordered_fallbacks_inside_a_sufficient_router_budget():
    compat = (
        AGENT_ROOT / "src" / "agent_environment" / "osm_mcp_compat.py"
    ).read_text(encoding="utf-8")
    assert (
        'f"{UPSTREAM_OVERPASS_URL},{SECONDARY_OVERPASS_URL},"' in compat
    )
    assert (
        'SECONDARY_OVERPASS_URL = "https://overpass.private.coffee/api/interpreter"'
        in compat
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


def test_clinicaltrials_uses_fixed_upstream_release_without_wrapper():
    servers = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]
    clinicaltrials = servers["clinicaltrialsgov-mcp-server"]
    assert clinicaltrials["command"] == "npx"
    assert clinicaltrials["args"] == [
        "clinicaltrialsgov-mcp-server@1.9.3",
    ]
    assert not any("compat" in arg for arg in clinicaltrials["args"])


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
        "/agent-environment/src/agent_environment/run_node_mcp.cjs",
        "@smithery/mcp-github#68368436034fb0003a6d8ed91afc9d0a64142b84",
        "dist/cli.js",
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

    arxiv = servers["arxiv"]
    assert arxiv == {
        "command": "/usr/local/bin/arxiv-mcp-server",
        "args": [],
    }
    installer = (
        AGENT_ROOT / "dev_scripts/install_mcp_packages.sh"
    ).read_text(encoding="utf-8")
    assert (
        "uv tool install arxiv-mcp-server==0.2.11 --with mcp==1.28.1"
        in installer
    )


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
