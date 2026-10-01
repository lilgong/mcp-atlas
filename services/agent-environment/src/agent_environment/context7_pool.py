"""Read-only Context7 calls rotate credentials without changing public tools."""
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


def record_attempt(slot, tool, status):
    directory = os.getenv("MCP_USAGE_LOG_DIR")
    if not directory:
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    row = json.dumps({"service": "context7", "status": status, "key_slot": slot + 1,
                      "tool": tool, "timestamp": time.time(), "measurement": "mcp_attempt"}) + "\n"
    fd = os.open(root / "context7-pool.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, row.encode())
    finally:
        os.close(fd)


def configured_keys(primary, raw):
    extra = json.loads(raw) if raw.strip().startswith("[") else raw.split(",")
    if not isinstance(extra, list) or any(not isinstance(k, str) for k in extra):
        raise ValueError("CONTEXT7_API_KEYS must contain strings")
    return list(dict.fromkeys(k.strip() for k in [primary, *extra] if k.strip()))


class Context7Pool:
    def __init__(self, server_config, keys, factory):
        self.clients = []
        for key in keys:
            config = deepcopy(server_config)
            config.setdefault("env", {})["CONTEXT7_API_KEY"] = key
            self.clients.append(factory("context7", config))
        self.locks = [asyncio.Lock() for _ in keys]
        self.entered = set()
        self.disabled = set()
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
        attempted = set()
        while True:
            async with self.selection_lock:
                choices = [(self.cursor + i) % len(self.clients) for i in range(len(self.clients))]
                slot = next((i for i in choices if i not in self.disabled and i not in attempted), None)
                if slot is not None:
                    self.cursor = slot + 1
            if slot is None:
                return mcp.types.CallToolResult(isError=True, content=[mcp.types.TextContent(
                    type="text", text="Context7: monthly quota reached or invalid API key for all configured credentials.")])
            attempted.add(slot)
            try:
                client = await self._client(slot)
                result = await client.call_tool_mcp(name, arguments, timeout=timeout)
                text = " ".join(getattr(c, "text", "") for c in result.content)
                error_surface = result.isError or (len(text) < 1500 and re.match(
                    r"\s*(?:error|monthly quota|you (?:have )?(?:reached|exceeded)|invalid|unauthorized|额度|余额)", text, re.I))
                exhausted = bool(error_surface and EXHAUSTED.search(text))
            except Exception as error:
                record_attempt(slot, name, 429 if EXHAUSTED.search(str(error)) else 500)
                if not EXHAUSTED.search(str(error)):
                    raise
                exhausted = True
            else:
                record_attempt(slot, name, 429 if exhausted else 500 if result.isError else 200)
            if not exhausted:
                logger.info("context7 key_slot=%d status=ok", slot + 1)
                return result
            async with self.selection_lock:
                self.disabled.add(slot)
            logger.warning("context7 key_slot=%d status=disabled retry_next=true", slot + 1)

    async def close(self):
        await asyncio.gather(*(self.clients[i].close() for i in self.entered), return_exceptions=True)
        self.entered.clear()
