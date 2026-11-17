"""Native, attempt-scoped trajectory capture for interrupted evaluations."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .runtime_log import jsonable


capture_context: ContextVar[dict | None] = ContextVar("atlas_partial_trajectory", default=None)


def snapshot_path(evaluation_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", evaluation_id):
        raise ValueError("Invalid evaluation ID")
    return Path(os.getenv("MCP_RUNTIME_LOG_DIR", "completion_results/runtime_logs")).expanduser() / "interrupted" / f"{evaluation_id}.json"


def save_snapshot(evaluation_id: str, state: dict) -> None:
    """Persist analysis evidence without changing cancellation or scoring."""
    temporary = None
    try:
        history = list(state["history"])
        if state.get("partial"):
            history.append(state["partial"])
        path = snapshot_path(evaluation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(jsonable({"evaluation_id": evaluation_id, "task_id": state["task_id"],
                               "raw_conversation_history": history, "partial": True}), handle, ensure_ascii=False)
        temporary.replace(path)
    except Exception:
        logging.getLogger(__name__).warning("Could not save interrupted trajectory", exc_info=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def capture_partial_response(chunks: list[Any]) -> None:
    """Reconstruct incomplete deltas for analysis only; never feed the agent."""
    state = capture_context.get()
    if state is None:
        return
    def get(value, key, default=None):
        return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)
    content, reasoning, calls = [], [], {}
    for chunk in chunks:
        for choice in get(chunk, "choices", []) or []:
            if get(choice, "index", 0) != 0:
                continue
            delta = get(choice, "delta")
            if delta is None:
                continue
            text = get(delta, "content")
            if isinstance(text, str):
                content.append(text)
            text = get(delta, "reasoning_content") or get(delta, "reasoning")
            if isinstance(text, str):
                reasoning.append(text)
            for call in get(delta, "tool_calls", []) or []:
                index = get(call, "index", 0)
                item = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": []}})
                if get(call, "id"):
                    item["id"] += get(call, "id")
                function = get(call, "function")
                if get(function, "name"):
                    item["function"]["name"] += get(function, "name")
                if isinstance(get(function, "arguments"), str):
                    item["function"]["arguments"].append(get(function, "arguments"))
    message = {"role": "assistant", "content": "".join(content), "reasoning_content": "".join(reasoning)}
    if calls:
        message["tool_calls"] = []
        for index in sorted(calls):
            call = calls[index]
            call["function"]["arguments"] = "".join(call["function"]["arguments"])
            message["tool_calls"].append(call)
    if message["content"] or message["reasoning_content"] or calls:
        state["partial"] = {**message, "original_message": dict(message)}
