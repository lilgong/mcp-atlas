"""Instrument Yibu-backed MCP clients with per-request JSONL accounting.

Brave uses ``fetch``, Exa uses Axios, and Oxylabs uses HTTPX.  Each request is
logged after completion (including transport failures) without exposing the
credential itself.  Run this after ``scripts/setup_yibu.py`` reinstalls and
patches the upstream packages.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "services/agent-environment/vendor/yibu-patched"
NODE_MODULES = VENDOR / "node_modules"

MARKER = "__MCP_USAGE_LOG_V2__"
LEGACY_MARKER = "__mcpLogApiCall"
PY_MARKER = "_MCP_USAGE_LOG_V2"
LEGACY_PY_MARKER = "_mcp_log_api_call"


def oxylabs_root() -> Path:
    return VENDOR / "oxylabs_mcp"


JS_LOG_HELPER = r'''
// ── MCP API usage logging v2 (per-key request accounting) ───────────────────
const __MCP_USAGE_LOG_V2__ = true;
import { appendFileSync as __mcpAppend, mkdirSync as __mcpMkdir } from "node:fs";
function __mcpLogApiCall(service, key, url, status, durationMs, errorName) {
    try {
        const dir = process.env.MCP_USAGE_LOG_DIR || "mcp_usage_log";
        const parsed = new URL(url);
        const keyText = String(key || "").replace(/^Bearer\s+/i, "");
        const suffix = keyText ? keyText.slice(-8) : "no-key";
        const now = new Date();
        const month = now.toISOString().slice(0, 7);
        const day = now.toISOString().slice(0, 10).replace(/-/g, "");
        const outDir = dir + "/" + month;
        __mcpMkdir(outDir, { recursive: true });
        __mcpAppend(
            outDir + "/" + service + "_" + suffix + "_" + day + ".jsonl",
            JSON.stringify({
                ts: now.toISOString(), service, key_suffix: suffix,
                host: parsed.hostname, path: parsed.pathname,
                status: status || 0, duration_ms: durationMs,
                error: errorName || null,
                task_id: process.env.MCP_TASK_ID || null,
            }) + "\n"
        );
    } catch (logError) {
        process.stderr.write(`[mcp-usage-log] ${logError?.message || logError}\n`);
    }
}
'''

BRAVE_FETCH_SNIPPET = JS_LOG_HELPER + r'''
const __mcpOrigFetch = globalThis.fetch;
globalThis.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || String(input);
    if (!/yibuapi\.com|api\.search\.brave\.com/.test(url)) {
        return __mcpOrigFetch(input, init);
    }
    const startedAt = Date.now();
    let status = 0;
    let errorName = null;
    try {
        const response = await __mcpOrigFetch(input, init);
        status = response.status;
        return response;
    } catch (error) {
        errorName = error?.name || "Error";
        throw error;
    } finally {
        const headers = new Headers((init && init.headers) || {});
        const key = headers.get("authorization") ||
            headers.get("x-api-key") || headers.get("x-subscription-token") || "";
        __mcpLogApiCall("brave", key, url, status, Date.now() - startedAt, errorName);
    }
};
'''

EXA_REQUEST_FROM = (
    "            const response = await axiosInstance.post("
    "API_CONFIG.ENDPOINTS.SEARCH, searchRequest, { timeout: 25000 });\n"
)
EXA_REQUEST_TO = r'''            let response;
            const __mcpStartedAt = Date.now();
            let __mcpStatus = 0;
            let __mcpError = null;
            const __mcpUrl = API_CONFIG.BASE_URL.replace(/\/$/, "") + "/" +
                API_CONFIG.ENDPOINTS.SEARCH.replace(/^\//, "");
            try {
                response = await axiosInstance.post(API_CONFIG.ENDPOINTS.SEARCH, searchRequest, { timeout: 25000 });
                __mcpStatus = response.status;
            } catch (__mcpExc) {
                __mcpStatus = __mcpExc?.response?.status || 0;
                __mcpError = __mcpExc?.name || "Error";
                throw __mcpExc;
            } finally {
                __mcpLogApiCall(
                    "exa", process.env.EXA_API_KEY || "", __mcpUrl,
                    __mcpStatus, Date.now() - __mcpStartedAt, __mcpError
                );
            }
'''

OXY_REQUEST_FROM = (
    "        response = await self._client.post("
    "settings.OXYLABS_SCRAPER_URL, json=payload)\n"
)
OXY_REQUEST_TO = '''        _mcp_started_at = _mcp_time.monotonic()
        _mcp_status = 0
        _mcp_error = None
        try:
            response = await self._client.post(settings.OXYLABS_SCRAPER_URL, json=payload)
            _mcp_status = response.status_code
        except Exception as _mcp_exc:
            _mcp_error = type(_mcp_exc).__name__
            raise
        finally:
            _mcp_log_api_call(
                settings.OXYLABS_SCRAPER_URL,
                _mcp_status,
                int((_mcp_time.monotonic() - _mcp_started_at) * 1000),
                _mcp_error,
            )
'''
PY_SNIPPET = r'''

# ── MCP API usage logging v2 (per-key request accounting) ───────────────────
_MCP_USAGE_LOG_V2 = True
def _mcp_log_api_call(url, status, duration_ms, error_name=None):
    import sys as _sys
    try:
        import json as _json
        import os as _os
        from datetime import datetime as _dt, timezone as _tz
        from urllib.parse import urlparse as _urlparse

        parsed = _urlparse(str(url))
        key = str(_os.environ.get("OXYLABS_PASSWORD", "") or "")
        suffix = key[-8:] if key else "no-key"
        now = _dt.now(_tz.utc)
        out_dir = _os.path.join(
            _os.environ.get("MCP_USAGE_LOG_DIR", "mcp_usage_log"),
            now.strftime("%Y-%m"),
        )
        _os.makedirs(out_dir, exist_ok=True)
        path = _os.path.join(out_dir, f"oxylabs_{suffix}_{now.strftime('%Y%m%d')}.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_json.dumps({
                "ts": now.isoformat().replace("+00:00", "Z"),
                "service": "oxylabs", "key_suffix": suffix,
                "host": parsed.hostname, "path": parsed.path,
                "status": status or 0, "duration_ms": duration_ms,
                "error": error_name,
                "task_id": _os.environ.get("MCP_TASK_ID"),
            }, ensure_ascii=False) + "\n")
    except Exception as log_exc:
        print(f"[mcp-usage-log] {log_exc}", file=_sys.stderr)
'''


def _remove_legacy_js(text: str) -> str:
    """Remove the v1 fetch wrapper so an already-patched tree can be upgraded."""
    if LEGACY_MARKER not in text or MARKER in text:
        return text
    start = text.index("// ── MCP API usage logging")
    function_start = text.index("async function __mcpLogApiCall", start)
    brace_start = text.index("{", function_start)
    depth = 0
    end = None
    for index in range(brace_start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise RuntimeError("could not remove legacy JS usage logger")
    return text[:start] + text[end:].lstrip("\n")


def instrument_brave(path: Path, anchor: str) -> str:
    if not path.is_file():
        return f"MISSING {path}"
    text = _remove_legacy_js(path.read_text(encoding="utf-8"))
    if MARKER in text:
        return f"already instrumented: {path.name}"
    if anchor not in text:
        raise RuntimeError(f"Brave anchor not found in {path}")
    path.write_text(
        text.replace(anchor, anchor + "\n" + BRAVE_FETCH_SNIPPET, 1),
        encoding="utf-8",
    )
    return f"instrumented: {path.name}"


def instrument_exa(path: Path, anchor: str) -> str:
    if not path.is_file():
        return f"MISSING {path}"
    text = _remove_legacy_js(path.read_text(encoding="utf-8"))
    if MARKER in text and EXA_REQUEST_TO.strip() in text:
        return f"already instrumented: {path.name}"
    if anchor not in text or EXA_REQUEST_FROM not in text:
        raise RuntimeError(f"audited Exa Axios request layout not found in {path}")
    text = text.replace(anchor, anchor + "\n" + JS_LOG_HELPER, 1)
    text = text.replace(EXA_REQUEST_FROM, EXA_REQUEST_TO, 1)
    path.write_text(text, encoding="utf-8")
    return f"instrumented: {path.name}"


def instrument_oxylabs(path: Path) -> str:
    if not path.is_file():
        return f"MISSING {path}"
    text = path.read_text(encoding="utf-8")
    if LEGACY_PY_MARKER in text and PY_MARKER not in text:
        if OXY_REQUEST_TO not in text:
            raise RuntimeError("legacy Oxylabs request logger layout changed")
        text = text.replace(OXY_REQUEST_TO, OXY_REQUEST_FROM, 1)
        text = text.removeprefix("import time as _mcp_time\n")
        legacy_start = text.find("\n# ── MCP API usage logging (per-key request accounting)")
        if legacy_start < 0:
            raise RuntimeError("legacy Oxylabs logger body not found")
        text = text[:legacy_start].rstrip() + "\n"
    if PY_MARKER in text:
        return f"already instrumented: {path.name}"
    if OXY_REQUEST_FROM not in text:
        raise RuntimeError(f"audited Oxylabs request site not found in {path}")
    text = text.replace(OXY_REQUEST_FROM, OXY_REQUEST_TO, 1)
    text = "import time as _mcp_time\n" + text + PY_SNIPPET
    path.write_text(text, encoding="utf-8")
    return f"instrumented: {path.name}"


def main() -> int:
    try:
        results = [instrument_brave(
            NODE_MODULES / "@modelcontextprotocol/server-brave-search/dist/index.js",
            'import { CallToolRequestSchema, ListToolsRequestSchema, } from "@modelcontextprotocol/sdk/types.js";',
        )]
        for name in ("webSearch.js", "webSearchExa.js"):
            path = NODE_MODULES / f"exa-mcp-server/build/tools/{name}"
            if path.is_file():
                results.append(instrument_exa(path, path.read_text(encoding="utf-8").splitlines()[0]))
        results.append(instrument_oxylabs(oxylabs_root() / "utils.py"))
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    for result in results:
        print("  " + result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
