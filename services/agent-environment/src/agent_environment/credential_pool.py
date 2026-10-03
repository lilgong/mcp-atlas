"""Read-only MCP credential pools; public tool schemas and arguments stay fixed."""
import asyncio
import json
import re
import logging
import os
from pathlib import Path
import time
from copy import deepcopy

import mcp.types

logger = logging.getLogger(__name__)
EXHAUSTED = re.compile(
    r"monthly.{0,50}(?:quota|limit)|(?:quota|credit|balance).{0,50}(?:exceeded|exhausted|insufficient)|"
    r"insufficient.{0,20}(?:quota|credit|balance)|invalid.{0,15}(?:key|token)|unauthorized|"
    r"余额不足|额度.{0,10}(?:不足|耗尽|用尽)", re.I)
TWELVEDATA_EXHAUSTED = re.compile(
    r"twelvedata_daily_credits_exhausted|run out of api credits for the day|"
    r"daily.{0,40}(?:credit|quota|limit).{0,40}(?:reached|exceeded|exhausted)|"
    r"(?:invalid|incorrect|expired).{0,15}(?:api.?key|token)|"
    r"api.?key.{0,15}(?:invalid|incorrect|expired)|unauthorized|"
    r"TwelveData upstream HTTP 401", re.I)


def record_attempt(slot, tool, status, service="context7"):
    directory = os.getenv("MCP_USAGE_LOG_DIR")
    if not directory:
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    row = json.dumps({"service": service, "status": status, "key_slot": slot + 1,
                      "tool": tool, "timestamp": time.time(), "measurement": "mcp_attempt"}) + "\n"
    # The gateway runs as root in the container; the host-owned enclosing
    # directory is private, but its usage reader must read this secret-free log.
    fd = os.open(root / f"{service}-pool.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, row.encode())
    finally:
        os.close(fd)


def configured_keys(primary, raw, variable="CONTEXT7_API_KEYS"):
    extra = json.loads(raw) if raw.strip().startswith("[") else raw.split(",")
    if not isinstance(extra, list) or any(not isinstance(k, str) for k in extra):
        raise ValueError(f"{variable} must contain strings")
    return list(dict.fromkeys(k.strip() for k in [primary, *extra] if k.strip()))


class CredentialPool:
    server_name = "context7"
    key_env = "CONTEXT7_API_KEY"
    exhausted_pattern = EXHAUSTED
    all_exhausted_message = "Context7: monthly quota reached or invalid API key for all configured credentials."

    def _bind_key(self, config, key):
        config.setdefault("env", {})[self.key_env] = key

    def __init__(self, server_config, keys, factory):
        if not keys:
            raise ValueError("MCP credential pool requires at least one key")
        self.clients = []
        for key in keys:
            config = deepcopy(server_config)
            self._bind_key(config, key)
            self.clients.append(factory(self.server_name, config))
        self.locks = [asyncio.Lock() for _ in keys]
        self.entered = set()
        self.cursor = 0
        self.selection_lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def _client(self, slot):
        async with self.locks[slot]:
            if slot not in self.entered:
                await self.clients[slot].__aenter__()
                self.entered.add(slot)
        return self.clients[slot]

    async def list_tools(self):
        return await (await self._client(0)).list_tools()

    async def call_tool_mcp(self, name, arguments, timeout=None):
        # Credential failure belongs to this request, not the lifetime of the
        # gateway. Daily/monthly quotas can reset while this process stays up.
        # Try every configured key at most once; only a fresh all-key failure
        # may produce the account-stop marker.
        attempted = set()
        while True:
            async with self.selection_lock:
                choices = [(self.cursor + i) % len(self.clients) for i in range(len(self.clients))]
                slot = next((i for i in choices if i not in attempted), None)
                if slot is not None:
                    self.cursor = slot + 1
            if slot is None:
                return mcp.types.CallToolResult(isError=True, content=[mcp.types.TextContent(
                    type="text", text=self.all_exhausted_message)])
            attempted.add(slot)
            try:
                client = await self._client(slot)
                result = await client.call_tool_mcp(name, arguments, timeout=timeout)
                text = " ".join(getattr(c, "text", "") for c in result.content)
                error_surface = result.isError or (len(text) < 1500 and re.match(
                    r"\s*(?:error|monthly quota|you (?:have )?(?:reached|exceeded|run out)|invalid|unauthorized|twelvedata|额度|余额)", text, re.I))
                exhausted = bool(error_surface and self.exhausted_pattern.search(text))
            except Exception as error:
                record_attempt(slot, name, 429 if self.exhausted_pattern.search(str(error)) else 500, self.server_name)
                if not self.exhausted_pattern.search(str(error)):
                    raise
                exhausted = True
            else:
                record_attempt(slot, name, 429 if exhausted else 500 if result.isError else 200, self.server_name)
            if not exhausted:
                logger.info("%s key_slot=%d status=ok", self.server_name, slot + 1)
                return result
            logger.warning("%s key_slot=%d status=unavailable_for_request retry_next=true", self.server_name, slot + 1)

    async def close(self):
        await asyncio.gather(*(self.clients[i].close() for i in self.entered), return_exceptions=True)
        self.entered.clear()


class Context7Pool(CredentialPool):
    pass


class TwelveDataPool(CredentialPool):
    server_name = "twelvedata"
    key_env = "TWELVE_DATA_API_KEY"
    exhausted_pattern = TWELVEDATA_EXHAUSTED
    all_exhausted_message = "TWELVEDATA_DAILY_CREDITS_EXHAUSTED: all configured credentials exhausted or invalid."

    def _bind_key(self, config, key):
        super()._bind_key(config, key)
        # The template's -k argument has already been expanded before routing.
        # Merely replacing env would make every slot use the same original key.
        args = config.get("args") or []
        positions = [i for i, value in enumerate(args) if value == "-k"]
        if len(positions) != 1 or positions[0] + 1 >= len(args):
            raise ValueError("TwelveData credential pool requires the pinned -k transport binding")
        args[positions[0] + 1] = key
