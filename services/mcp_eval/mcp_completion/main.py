"""Main FastAPI application for MCP eval."""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Header, Request

from .agent_eval import handle_run_mcp_eval
from .schema import RunAgentAPIRequestBody
from .errors import MCPClientToolExecutionError
from .config import config
from .config import validate_isolated_control_plane
from .runtime_log import write_runtime_event
from .account_guard import FatalAccountError, describe_fatal_account_error
from .task_sandbox import (
    DEFAULT_RUNTIME_IMAGE,
    reap_owned_task_sandboxes,
    run_orphan_sweeper,
)
from .failure_protocol import failure_receipt
from .runtime_identity import runtime_identity

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    isolation_enabled = (
        os.getenv("MCP_TASK_ISOLATION_ENABLED", "true").lower()
        not in {"0", "false", "no"}
    )
    sweeper: Optional[asyncio.Task] = None
    if isolation_enabled:
        validate_isolated_control_plane(
            config.HOST,
            config.MCP_SERVER_URL,
            os.getenv("MCP_ATLAS_RUN_RUNTIME_HOST"),
        )
        # Do not synchronously delete old Docker resources during startup. A
        # large backlog on a busy shared daemon can otherwise keep /health
        # unavailable for N serial teardown timeouts. The age-gated sweeper
        # handles runtime orphans; shutdown performs an immediate owner-scoped
        # compensation pass after Uvicorn has drained active requests.
        sweeper = asyncio.create_task(
            run_orphan_sweeper(
                interval_seconds=float(
                    os.getenv("MCP_SANDBOX_SWEEP_INTERVAL", "300")
                ),
                min_age_seconds=float(
                    os.getenv("MCP_SANDBOX_ORPHAN_MAX_AGE", "1800")
                ),
            )
        )
    write_runtime_event(
        "service",
        "completion_service_started",
        host=config.HOST,
        port=config.PORT,
        shared_mcp_url=config.MCP_SERVER_URL,
        task_isolation_enabled=isolation_enabled,
        task_agent_image=os.getenv(
            "MCP_TASK_AGENT_IMAGE", DEFAULT_RUNTIME_IMAGE
        ),
    )
    try:
        yield
    finally:
        if sweeper is not None:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
        if isolation_enabled:
            try:
                cleanup = await reap_owned_task_sandboxes()
            except Exception as exc:
                logger.warning("Shutdown sandbox reaping failed: %s", exc)
                write_runtime_event(
                    "sandbox",
                    "shutdown_orphan_reap_failed",
                    error=str(exc),
                )
            else:
                if (
                    cleanup["containers_remaining"]
                    or cleanup["volumes_remaining"]
                    or cleanup["networks_remaining"]
                    or cleanup["listing_failures"]
                ):
                    logger.warning(
                        "Shutdown left Atlas sandbox resources: %s", cleanup
                    )
                    write_runtime_event(
                        "sandbox",
                        "shutdown_orphans_remaining",
                        **cleanup,
                    )
        write_runtime_event(
            "service",
            "completion_service_stopped",
            host=config.HOST,
            port=config.PORT,
        )


app = FastAPI(
    title="MCP Eval",
    description="Standalone MCP evaluation environment",
    version="0.1.0",
    lifespan=lifespan,
)

# Every HTTP attempt has its own ID.  This lets the benchmark client cancel the
# exact evaluation it timed out on and wait for its model call and sandbox to
# finish tearing down before it retries.
_ACTIVE_EVALUATIONS: Dict[str, asyncio.Task] = {}


async def _collect_agent_outputs(
    body: RunAgentAPIRequestBody,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    async for agent_output in handle_run_mcp_eval(body):
        results.append(
            {
                "type": agent_output.type,
                "data": agent_output.data,
            }
        )
    return results


async def _wait_for_disconnect(request: Request) -> None:
    # Read the ASGI disconnect event directly. Starlette's is_disconnected()
    # performs a non-blocking probe which can miss the event when receive has
    # been wrapped by BaseHTTPMiddleware.
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _collect_until_disconnect(
    body: RunAgentAPIRequestBody,
    request: Request,
) -> List[Dict[str, Any]]:
    """Cancel this request's eval when its HTTP client stops waiting."""
    evaluation_id = body.evaluation_id or uuid.uuid4().hex
    evaluation = asyncio.create_task(_collect_agent_outputs(body))
    existing = _ACTIVE_EVALUATIONS.get(evaluation_id)
    if existing is not None and not existing.done():
        evaluation.cancel()
        with suppress(asyncio.CancelledError):
            await evaluation
        raise HTTPException(status_code=409, detail="evaluationId is already active")
    _ACTIVE_EVALUATIONS[evaluation_id] = evaluation
    disconnect = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            {evaluation, disconnect}, return_when=asyncio.FIRST_COMPLETED
        )
        if evaluation in done:
            return await evaluation

        logger.warning(
            "Client disconnected; cancelling evaluation_id=%s task_id=%s",
            evaluation_id,
            body.task_id or "generated",
        )
        write_runtime_event(
            "service",
            "evaluation_cancelled_after_client_disconnect",
            evaluation_id=evaluation_id,
            task_id=body.task_id or "generated",
        )
        evaluation.cancel()
        with suppress(asyncio.CancelledError):
            await evaluation
        raise HTTPException(status_code=499, detail="Client disconnected")
    finally:
        if not evaluation.done():
            evaluation.cancel()
        with suppress(asyncio.CancelledError):
            await evaluation
        if not disconnect.done():
            disconnect.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect
        if _ACTIVE_EVALUATIONS.get(evaluation_id) is evaluation:
            _ACTIVE_EVALUATIONS.pop(evaluation_id, None)


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"message": "MCP Eval is running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "task_isolation_enabled": (
            os.getenv("MCP_TASK_ISOLATION_ENABLED", "true").lower()
            not in {"0", "false", "no"}
        ),
        "shared_mcp_url": config.MCP_SERVER_URL,
        "runtime_identity": runtime_identity(),
    }


@app.post("/v2/mcp_eval/run_agent")
async def run_agent(
    body: RunAgentAPIRequestBody,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """
    MCP evaluation endpoint. The main entrypoint. For simplicity, no authentication or rate limiting is used.
    """
    logger.info(
        "v2 API /run_agent called with model=%s task_id=%s",
        body.model,
        body.task_id or "generated",
    )

    try:
        return await _collect_until_disconnect(body, request)

    except HTTPException:
        raise

    except FatalAccountError as error:
        logger.critical(
            "Stopping request for fatal account failure: %s",
            describe_fatal_account_error(error),
        )
        raise HTTPException(
            status_code=402,
            detail={
                "code": "fatal_account_error",
                **failure_receipt(
                    failure_class="account_fatal",
                    reason_code="fatal_account_error",
                    retryable=False,
                    source_kind=error.source_kind,
                    source_name=error.source_name,
                ),
                "error": str(error),
                "source_kind": error.source_kind,
                "source_name": error.source_name,
                "credential_envs": list(error.credential_envs),
            },
        )

    except MCPClientToolExecutionError as error:
        logger.error(f"MCP client tool execution error: {error}")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "mcp_tool_execution_error",
                **failure_receipt(
                    failure_class="infrastructure",
                    reason_code="mcp_tool_execution_error",
                    retryable=False,
                    source_kind="mcp",
                    detail_code=type(error).__name__,
                ),
            },
        )

    except Exception as error:
        logger.error(f"Error during MCP eval execution: {error}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "runtime_internal_error",
                **failure_receipt(
                    failure_class="infrastructure",
                    reason_code="runtime_internal_error",
                    retryable=False,
                    source_kind="runtime",
                    detail_code=type(error).__name__,
                ),
            },
        )


@app.post("/v2/mcp_eval/cancel/{evaluation_id}")
async def cancel_evaluation(evaluation_id: str):
    """Cancel one attempt and return only after its cleanup has completed."""
    evaluation = _ACTIVE_EVALUATIONS.get(evaluation_id)
    if evaluation is None:
        return {
            "evaluationId": evaluation_id,
            "found": False,
            "cleanupCompleted": True,
        }

    evaluation.cancel()
    await asyncio.gather(evaluation, return_exceptions=True)
    if _ACTIVE_EVALUATIONS.get(evaluation_id) is evaluation:
        _ACTIVE_EVALUATIONS.pop(evaluation_id, None)
    write_runtime_event(
        "service",
        "evaluation_cancelled_by_client",
        evaluation_id=evaluation_id,
    )
    return {
        "evaluationId": evaluation_id,
        "found": True,
        "cleanupCompleted": True,
    }


def main():
    # Validate required configuration at startup
    config.validate_required_config()

    logger.info(f"Starting MCP Eval server on {config.HOST}:{config.PORT}")

    uvicorn.run(
        "mcp_completion.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,  # Set to True for development
        log_level=config.LOG_LEVEL.lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
