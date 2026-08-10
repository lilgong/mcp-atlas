"use strict";

// Transport-only compatibility for Yibu-backed Node MCP servers. This file is
// loaded before the upstream package, so tool registration and schemas remain
// entirely upstream-owned.
const {
  appendFileSync,
  mkdirSync,
} = require("node:fs");

const SERVICE_RULES = {
  brave: {
    upstreamOrigin: "https://api.search.brave.com",
    gatewayOrigin: "https://yibuapi.com",
    gatewayPrefix: "/brave",
    stripPathPrefix: "/res",
    credentialHeader: "x-subscription-token",
  },
  exa: {
    upstreamOrigin: "https://api.exa.ai",
    gatewayOrigin: "https://yibuapi.com",
    gatewayPrefix: "/exa",
    credentialHeader: "x-api-key",
  },
};

function rewriteUrl(rawUrl, service) {
  const rule = SERVICE_RULES[service];
  if (!rule) return String(rawUrl);
  const url = new URL(String(rawUrl));
  if (url.origin !== rule.upstreamOrigin) return url.toString();
  const gateway = new URL(rule.gatewayOrigin);
  url.protocol = gateway.protocol;
  url.host = gateway.host;
  const pathname = rule.stripPathPrefix && url.pathname.startsWith(rule.stripPathPrefix)
    ? url.pathname.slice(rule.stripPathPrefix.length)
    : url.pathname;
  url.pathname = rule.gatewayPrefix + pathname;
  return url.toString();
}

function rewriteHeaders(source, service) {
  const headers = new Headers(source || {});
  const rule = SERVICE_RULES[service];
  if (!rule) return headers;
  const key = headers.get(rule.credentialHeader);
  if (key) {
    headers.delete(rule.credentialHeader);
    headers.set("authorization", `Bearer ${key}`);
  }
  return headers;
}

function isUpstreamRequest(rawUrl, service) {
  const rule = SERVICE_RULES[service];
  return Boolean(rule) && new URL(String(rawUrl)).origin === rule.upstreamOrigin;
}

function logUsage(service, key, url, status, durationMs, errorName) {
  try {
    const value = String(key || "").replace(/^Bearer\s+/i, "");
    const suffix = value ? value.slice(-8) : "no-key";
    const now = new Date();
    const month = now.toISOString().slice(0, 7);
    const day = now.toISOString().slice(0, 10).replace(/-/g, "");
    const directory = `${process.env.MCP_USAGE_LOG_DIR || "mcp_usage_log"}/${month}`;
    const parsed = new URL(url);
    mkdirSync(directory, { recursive: true });
    appendFileSync(
      `${directory}/${service}_${suffix}_${day}.jsonl`,
      JSON.stringify({
        ts: now.toISOString(),
        service,
        key_suffix: suffix,
        host: parsed.hostname,
        path: parsed.pathname,
        status: status || 0,
        duration_ms: durationMs,
        error: errorName || null,
        task_id: process.env.MCP_TASK_ID || null,
      }) + "\n",
    );
  } catch (error) {
    process.stderr.write(`[mcp-usage-log] ${error?.message || error}\n`);
  }
}

function install(service) {
  if (!SERVICE_RULES[service]) {
    throw new Error(`unsupported MCP_YIBU_SERVICE: ${service}`);
  }
  const originalFetch = globalThis.fetch;
  if (typeof originalFetch !== "function") {
    throw new Error("Yibu preload requires Node.js global fetch");
  }
  globalThis.fetch = async function yibuFetch(input, init = {}) {
    const originalUrl = input instanceof Request ? input.url : String(input);
    if (!isUpstreamRequest(originalUrl, service)) {
      return originalFetch(input, init);
    }
    const rewrittenUrl = rewriteUrl(originalUrl, service);
    const sourceHeaders = init.headers || (input instanceof Request ? input.headers : {});
    const headers = rewriteHeaders(sourceHeaders, service);
    const key = headers.get("authorization") || "";
    const requestInit = { ...init, headers };
    const requestInput = input instanceof Request
      ? new Request(rewrittenUrl, input)
      : rewrittenUrl;
    const startedAt = Date.now();
    let status = 0;
    let errorName = null;
    try {
      const response = await originalFetch(requestInput, requestInit);
      status = response.status;
      return response;
    } catch (error) {
      errorName = error?.name || "Error";
      throw error;
    } finally {
      logUsage(
        service,
        key,
        rewrittenUrl,
        status,
        Date.now() - startedAt,
        errorName,
      );
    }
  };
}

module.exports = { install, isUpstreamRequest, rewriteHeaders, rewriteUrl };

if (require.main !== module && process.env.MCP_YIBU_SERVICE) {
  install(process.env.MCP_YIBU_SERVICE || "");
}
