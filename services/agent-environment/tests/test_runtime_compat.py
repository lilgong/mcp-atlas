from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from agent_environment.oxylabs_mcp_compat import normalize_scraper_payload
from agent_environment import pubmed_mcp_compat as pubmed
from agent_environment.twelvedata_mcp_compat import (
    daily_credit_error,
    safe_upstream_error,
)
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
    assert shared["pubmed"]["command"] == "/agent-environment/.venv/bin/python"
    assert shared["pubmed"]["args"] == [
        "-m",
        "agent_environment.pubmed_mcp_compat",
    ]
    assert shared["pubmed"]["env"] == {
        "PUBMED_RELAY_URL": "${PUBMED_RELAY_URL}",
        "PUBMED_RELAY_TOKEN": "${PUBMED_RELAY_TOKEN}",
    }
    assert shared["twelvedata"]["args"] == [
        "run",
        "--python",
        "3.13",
        "--no-project",
        "--with",
        "mcp==1.28.1",
        "--with",
        "mcp-server-twelve-data==0.2.5",
        "python",
        "/agent-environment/src/agent_environment/twelvedata_mcp_compat.py",
        "-k",
        "${TWELVE_DATA_API_KEY}",
    ]


def test_twelvedata_daily_credit_error_is_fatal_but_minute_limit_is_not():
    class Response:
        status_code = 429

        def __init__(self, message):
            self.message = message

        def json(self):
            return {"code": 429, "message": self.message, "status": "error"}

    assert daily_credit_error(Response(
        "You have run out of API credits for the day. Wait for the next day."
    )).startswith("TWELVEDATA_DAILY_CREDITS_EXHAUSTED:")
    assert daily_credit_error(Response(
        "You have run out of API credits for the current minute."
    )) is None


def test_twelvedata_http_error_does_not_expose_apikey():
    response = httpx.Response(
        400,
        request=httpx.Request(
            "GET",
            "https://api.twelvedata.com/time_series?apikey=secret-value",
        ),
        json={"message": "Invalid symbol"},
    )

    error = safe_upstream_error(response)
    assert error == "TwelveData upstream HTTP 400: Invalid symbol"
    assert "secret-value" not in error


def test_twelvedata_minute_limit_remains_a_rate_limit_error():
    response = httpx.Response(
        429,
        request=httpx.Request(
            "GET",
            "https://api.twelvedata.com/time_series?apikey=secret-value",
        ),
        json={
            "message": "You have run out of API credits for the current minute."
        },
    )

    error = safe_upstream_error(response)
    assert error.startswith("TwelveData upstream HTTP 429:")
    assert "TWELVEDATA_DAILY_CREDITS_EXHAUSTED" not in error


def test_pubmed_search_batches_all_metadata_into_one_efetch(monkeypatch):
    calls = []
    esearch = pubmed.ET.fromstring(
        b"<eSearchResult><IdList><Id>1</Id><Id>2</Id></IdList></eSearchResult>"
    )
    efetch = pubmed.ET.fromstring(
        b"""<PubmedArticleSet>
        <PubmedArticle><MedlineCitation><PMID>1</PMID><Article>
          <ArticleTitle>First <i>paper</i></ArticleTitle>
          <AuthorList><Author><LastName>Alpha</LastName></Author></AuthorList>
          <Journal><Title>Journal A</Title><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue></Journal>
          <Abstract><AbstractText>First abstract.</AbstractText></Abstract>
        </Article></MedlineCitation></PubmedArticle>
        <PubmedArticle><MedlineCitation><PMID>2</PMID><Article>
          <ArticleTitle>Second paper</ArticleTitle>
          <Journal><Title>Journal B</Title><JournalIssue><PubDate><MedlineDate>2024 Winter</MedlineDate></PubDate></JournalIssue></Journal>
        </Article></MedlineCitation></PubmedArticle>
        </PubmedArticleSet>"""
    )

    def fake_request_xml(endpoint, params):
        calls.append((endpoint, params))
        return esearch if endpoint == "esearch" else efetch

    monkeypatch.setattr(pubmed, "_request_xml", fake_request_xml)
    rows = pubmed.search_key_words("diabetes", 10)

    assert [row["PMID"] for row in rows] == ["1", "2"]
    assert rows[0]["Title"] == "First paper"
    assert rows[1]["Publication Date"] == "2024"
    assert calls == [
        ("esearch", {"db": "pubmed", "term": "diabetes", "retmax": 10}),
        ("efetch", {"db": "pubmed", "id": "1,2"}),
    ]


def test_pubmed_detects_ncbi_abuse_redirect():
    response = type("Response", (), {
        "headers": {"Location": "https://misuse.ncbi.nlm.nih.gov/error/abuse.shtml"},
        "content": b"",
        "text": "",
    })()
    assert pubmed._is_blocked(response)


def test_pubmed_relay_reconstructs_upstream_response(monkeypatch):
    class RelayResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "status_code": 200,
                "headers": {"Content-Type": "application/xml; charset=utf-8"},
                "body_base64": pubmed.base64.b64encode(b"<ok/>").decode(),
                "url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            }

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return RelayResponse()

    monkeypatch.setattr(pubmed, "RELAY_TOKEN", "secret")
    monkeypatch.setattr(pubmed._SESSION, "post", fake_post)
    response = pubmed._request_via_relay(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed"},
        allow_redirects=False,
    )

    assert response.status_code == 200
    assert response.content == b"<ok/>"
    assert calls[0][0].endswith("/v1/fetch")
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}


def test_pubmed_relay_preserves_ipwo_auth_failure(monkeypatch):
    class RelayResponse:
        status_code = 402

        def json(self):
            return {
                "code": "relay_account_error",
                "error": "IPWO_PROXY_AUTH_FAILED: proxy credential rejected",
            }

    monkeypatch.setattr(pubmed, "RELAY_TOKEN", "secret")
    monkeypatch.setattr(pubmed._SESSION, "post", lambda *args, **kwargs: RelayResponse())

    with pytest.raises(
        pubmed.PubMedUpstreamError,
        match="IPWO_PROXY_AUTH_FAILED",
    ):
        pubmed._request_via_relay(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed"},
            allow_redirects=False,
        )


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
    for server in ("weather-data",):
        source = servers[server]["args"][
            servers[server]["args"].index("--from") + 1
        ]
        revision = source.rpartition("@")[2]
        assert len(revision) == 40
        assert all(character in "0123456789abcdef" for character in revision)


def test_weather_data_keeps_official_tools_and_uses_optional_yibu_transport():
    servers = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]
    weather = servers["weather-data"]
    assert weather["args"][-1] == "weather-mcp-server"
    assert weather["env"]["PYTHONPATH"] == (
        "/agent-environment/src/agent_environment/weatherapi_preload"
    )
    # The preload reads these two out of its own process environment, and the
    # subprocess only receives what this block declares.  Without the Yibu key
    # ``_install`` returns early, the official server never gets a key, and
    # every tool call fails with "Weather API key not set."
    assert weather["env"]["WEATHER_YIBU_API_KEY"] == "${WEATHER_YIBU_API_KEY}"
    assert weather["env"]["MCP_USAGE_LOG_DIR"] == "${MCP_USAGE_LOG_DIR}"
    preload = (
        AGENT_ROOT
        / "src"
        / "agent_environment"
        / "weatherapi_preload"
        / "sitecustomize.py"
    )
    compile(preload.read_text(encoding="utf-8"), str(preload), "exec")
    text = preload.read_text(encoding="utf-8")
    assert "httpx.AsyncClient.get" in text
    assert 'params.pop("key", None)' in text


def test_special_cased_servers_receive_every_env_var_their_gating_reads():
    """A server enabled on the strength of an env var must also be given it.

    ``mcp_client`` special-cases a few servers so an optional Yibu key can
    stand in for the official credentials.  That decision reads the key from
    the service's own environment, but the MCP subprocess inherits nothing
    beyond PATH/HOME/..., so it only sees what the template's ``env`` block
    declares.  When the two disagree the server is started and then fails
    every single call -- weather-data returned "Weather API key not set." for
    a whole 500-task run because its Yibu key was gated on but never passed.
    """
    client_source = (
        AGENT_ROOT / "src" / "agent_environment" / "mcp_client.py"
    ).read_text(encoding="utf-8")
    servers = json.loads(
        (
            AGENT_ROOT
            / "src"
            / "agent_environment"
            / "mcp_server_template.json"
        ).read_text(encoding="utf-8")
    )["mcpServers"]

    special_cases = re.findall(
        r'if name == "([^"]+)":(.*?)continue', client_source, re.DOTALL
    )
    assert special_cases, "expected mcp_client to special-case at least one server"

    for name, block in special_cases:
        gating_vars = set(re.findall(r'os\.getenv\(\s*"([A-Z0-9_]+)"', block))
        declared = set(servers[name].get("env") or {})
        missing = sorted(gating_vars - declared)
        assert not missing, (
            f"{name}: enablement is gated on {missing}, but the template never "
            "passes them to the subprocess, so the server starts unusable"
        )


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
    assert servers["wikipedia"]["env"]["PYTHONPATH"] == (
        "/agent-environment/src/agent_environment/wikipedia_preload"
    )
    preload = (
        AGENT_ROOT
        / "src"
        / "agent_environment"
        / "wikipedia_preload"
        / "sitecustomize.py"
    )
    compile(preload.read_text(encoding="utf-8"), str(preload), "exec")
    text = preload.read_text(encoding="utf-8")
    assert "Session.request" in text
    assert "SyncHTTPClient._do_get" in text
