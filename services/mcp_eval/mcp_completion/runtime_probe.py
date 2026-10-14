"""Model-free connectivity probes owned by the callback-receiving process."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .config import config, _is_loopback_host
from .runtime_identity import runtime_identity
from .runtime_log import write_runtime_event

router = APIRouter()


class RuntimeProbeRequest(BaseModel):
    shared_url: str
    server: str | None = None
    timeout: float = Field(default=180, gt=0, le=600)
    concurrency: int = Field(default=5, ge=1, le=100)
    retries: int = Field(default=2, ge=0, le=10)
    data_only: bool = False
    smoke_only: bool = True
    input_path: str | None = None

    class Config:
        extra = "forbid"


@router.post("/v2/mcp_eval/runtime-probe")
async def runtime_probe(body: RuntimeProbeRequest, request: Request):
    # Keep this model-free control endpoint local even if mounted separately.
    if request.client is None or not _is_loopback_host(request.client.host):
        raise HTTPException(403, "Runtime probe requires a local client")
    if body.shared_url.rstrip("/") != config.MCP_SERVER_URL.rstrip("/"):
        raise HTTPException(409, "Probe gateway does not belong to this completion runtime")
    from mcp_server_probe import run_isolated
    from .task_data import TaskDataError
    from .task_sandbox import TaskSandboxError
    from .runtime_log import _redact_string

    await request.body()
    write_runtime_event("service", "runtime_probe_started", server=body.server)
    probe = asyncio.create_task(run_isolated(
        body.shared_url, body.timeout, body.concurrency, body.server,
        body.data_only, body.smoke_only, body.retries,
        Path(body.input_path) if body.input_path else None,
        completion_owned=True,
    ))

    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({probe, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if probe not in done:
            raise HTTPException(499, "Probe client disconnected")
        results = await probe
        write_runtime_event("service", "runtime_probe_finished", server=body.server,
                            results=[asdict(result) for result in results])
        return {"schema_version": 1, "runtime_identity": runtime_identity(),
                "results": [asdict(result) for result in results]}
    except (TaskDataError, TaskSandboxError) as exc:
        detail = _redact_string(str(exc))
        write_runtime_event("service", "runtime_probe_initialization_failed", server=body.server, detail=detail)
        raise HTTPException(503, {"code": "task_initialization_failed", "detail": detail}) from None
    finally:
        # IsolatedMCPClient shields sandbox/lease teardown on cancellation.
        # Never abandon task containers when the CLI times out or exits.
        for task in (probe, watcher):
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
