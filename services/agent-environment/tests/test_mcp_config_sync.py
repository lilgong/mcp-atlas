"""Verify that every Node MCP entrypoint is backed by a repository lock."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
SHARED_TEMPLATE = ROOT / "src/agent_environment/mcp_server_template.json"
LOCAL_TEMPLATE = REPO_ROOT / "services/task-sandbox/local_mcp_server_template.json"
NODE_ROOT = "/agent-environment/node_modules/"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def package_from_path(path: str) -> str | None:
    if not path.startswith(NODE_ROOT):
        return None
    relative = path.removeprefix(NODE_ROOT)
    parts = relative.split("/")
    return "/".join(parts[:2]) if relative.startswith("@") else parts[0]


def package_from_spec(spec: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"(@[^/]+/[^@]+|[^@/]+)@([^@]+)", spec)
    return (match.group(1), match.group(2)) if match else None


def configured_node_packages() -> tuple[set[str], dict[str, str]]:
    path_packages: set[str] = set()
    explicit_specs: dict[str, str] = {}
    for template in (SHARED_TEMPLATE, LOCAL_TEMPLATE):
        servers = load_json(template)["mcpServers"]
        for server in servers.values():
            for argument in server.get("args", []):
                package = package_from_path(argument)
                if package:
                    path_packages.add(package)
                spec = package_from_spec(argument)
                if spec:
                    explicit_specs[spec[0]] = spec[1]
    return path_packages, explicit_specs


def test_every_node_entrypoint_has_one_exact_repository_pin():
    package_json = load_json(ROOT / "package.json")
    package_lock = load_json(ROOT / "package-lock.json")
    dependencies = package_json["dependencies"]
    path_packages, explicit_specs = configured_node_packages()

    assert all(version and not version.startswith(("^", "~", ">", "<", "*"))
               for version in dependencies.values())

    # GitHub is built from a checked commit because the published package does
    # not expose the Atlas schema. Every other absolute Node path must resolve
    # to package.json and package-lock.json instead of bypassing validation.
    assert path_packages - {"@smithery/mcp-github"} <= dependencies.keys()
    for package in path_packages - {"@smithery/mcp-github"}:
        locked = package_lock["packages"][f"node_modules/{package}"]["version"]
        assert locked == dependencies[package], package

    for package, version in explicit_specs.items():
        assert dependencies[package] == version, package
        locked = package_lock["packages"][f"node_modules/{package}"]["version"]
        assert locked == version, package


def test_node_dependency_set_matches_templates_exactly():
    dependencies = set(load_json(ROOT / "package.json")["dependencies"])
    path_packages, explicit_specs = configured_node_packages()
    configured = (path_packages - {"@smithery/mcp-github"}) | set(explicit_specs)
    assert configured == dependencies


def test_git_node_entrypoint_is_commit_pinned():
    install_script = (ROOT / "dev_scripts/install_mcp_packages.sh").read_text(
        encoding="utf-8"
    )
    revision = re.search(
        r'^GITHUB_MCP_REVISION="([0-9a-f]{40})"$',
        install_script,
        re.MULTILINE,
    )
    assert revision
    assert (
        "/agent-environment/node_modules/@smithery/mcp-github/dist/cli.js"
        in install_script
    )


def test_templates_do_not_use_external_node_module_copies():
    for template in (SHARED_TEMPLATE, LOCAL_TEMPLATE):
        text = template.read_text(encoding="utf-8")
        assert "/mnt/node_modules" not in text
        assert "/usr/lib/node_modules" not in text


def test_node_runner_and_yibu_transport_contracts():
    runner = ROOT / "src/agent_environment/run_node_mcp.cjs"
    preload = ROOT / "src/agent_environment/yibu_fetch_preload.cjs"
    subprocess.run(["node", "--check", str(runner)], check=True)
    subprocess.run(["node", "--check", str(preload)], check=True)
    script = f"""
const adapter = require({json.dumps(str(preload))});
const runner = require({json.dumps(str(runner))});
const headers = adapter.rewriteHeaders({{'x-api-key': 'secret'}}, 'exa');
console.log(JSON.stringify({{
  source: runner.parseSource('exa-mcp-server@3.2.1'),
  url: adapter.rewriteUrl('https://api.exa.ai/search', 'exa'),
  brave: adapter.rewriteUrl('https://api.search.brave.com/res/v1/web/search', 'brave'),
  lara: adapter.rewriteUrl('https://api.laratranslate.com/v2/languages', 'lara'),
  laraTokenParts: adapter.laraSessionToken().split('.').length,
  laraAuthorization: adapter.rewriteHeaders({{'authorization': 'Lara signature'}}, 'lara', 'lara-secret').get('authorization'),
  unrelated: adapter.rewriteUrl('https://example.com/x', 'exa'),
  interceptUnrelated: adapter.isUpstreamRequest('https://example.com/x', 'exa'),
  authorization: headers.get('authorization'),
  oldHeader: headers.get('x-api-key'),
}}));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    output = json.loads(result.stdout)
    assert output["source"] == {
        "name": "exa-mcp-server",
        "expectedVersion": "3.2.1",
    }
    assert output["url"] == "https://yibuapi.com/exa/search"
    assert output["brave"] == "https://yibuapi.com/brave/v1/web/search"
    assert output["lara"] == "https://yibuapi.com/lara/v2/languages"
    assert output["laraTokenParts"] == 3
    assert output["laraAuthorization"] == "Bearer lara-secret"
    assert output["unrelated"] == "https://example.com/x"
    assert output["interceptUnrelated"] is False
    assert output["authorization"] == "Bearer secret"
    assert output["oldHeader"] is None
