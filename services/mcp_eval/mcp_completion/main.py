"""Main FastAPI application for MCP eval."""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Header, Request, Response

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
        validate_isolated_control_plane(config.HOST, config.MCP_SERVER_URL)
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
    while not await request.is_disconnected():
        await asyncio.sleep(0.25)


async def _collect_until_disconnect(
    body: RunAgentAPIRequestBody,
    request: Request,
) -> List[Dict[str, Any]]:
    """Cancel this request's eval when its HTTP client stops waiting."""
    evaluation = asyncio.create_task(_collect_agent_outputs(body))
    disconnect = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            {evaluation, disconnect}, return_when=asyncio.FIRST_COMPLETED
        )
        if evaluation in done:
            return await evaluation

        logger.warning(
            "Client disconnected; cancelling evaluation for task_id=%s",
            body.task_id or "generated",
        )
        write_runtime_event(
            "service",
            "evaluation_cancelled_after_client_disconnect",
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


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log requests with their actual response status codes."""
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time

    logger.info(
        f"{request.client.host}:{request.client.port} - "
        f'"{request.method} {request.url.path} HTTP/1.1" {response.status_code} '
        f"- {process_time:.3f}s"
    )

    return response


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
                "error": str(error),
                "source_kind": error.source_kind,
                "source_name": error.source_name,
                "credential_envs": list(error.credential_envs),
            },
        )

    except MCPClientToolExecutionError as error:
        logger.error(f"MCP client tool execution error: {error}")
        raise HTTPException(status_code=500, detail={"error": str(error)})

    except Exception as error:
        logger.error(f"Error during MCP eval execution: {error}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Unknown error during mcp_eval: {str(error)}",
            },
        )


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
        access_log=False,  # Disable default access logs (we have custom middleware)
    )


if __name__ == "__main__":
    main()
