"""data-syn P2 synthesis agent loop on top of the isolated MCP client.

Ported from mcp-atlas-data-syn ``services/synthesis_agent_service.py`` @ 0dd37a1
(``_budget_status``, ``_repeat_tool_warning``, ``normalize_tool_arguments``,
the per-result/context character limits, timeout quarantine and the
call-budget accounting of ``_stream_agent``).  Only used when a request sends
``synthesisControl``; evaluation and teacher rollouts keep ``run_mcp_eval``.

The model sees each tool result truncated to ``per_result_max_chars``; the
yielded message keeps the complete result, exactly as data-syn stored the
sanitized raw content while sending a bounded string to the next turn.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, AsyncGenerator, Dict, List, Optional

from .account_guard import FatalAccountError
from .failure_protocol import classify_model_exception, failure_receipt
from .llm import _transform_tool_calls, create_completion
from .runtime_log import write_runtime_event
from .schema import (
    Message,
    SynthesisControl,
    TextContent,
    ToolCallOutputMessage,
    UserMessage,
)

logger = logging.getLogger(__name__)

TIMEOUT_RE = re.compile(r"\b504\b|timed?[ -]?out|TimeoutError", re.IGNORECASE)


def budget_status(remaining_calls: int, remaining_turns: int) -> str:
    """Tell the model the usable budget while reserving room to finish."""
    if remaining_calls <= 1 or remaining_turns <= 1:
        return (
            "BUDGET STATUS: return the final evidence summary now. "
            "Do not call any more tools."
        )
    return (
        f"BUDGET STATUS: {remaining_calls} tool-call slots and "
        f"{remaining_turns} model turns remain, including this response. "
        f"Use at most {remaining_calls - 1} additional tool calls and keep at "
        "least one slot unused for a clean finish."
    )


def repeat_tool_warning(tool: str, count: int, limit: int) -> tuple[int, str] | None:
    """Return a controller warning tier when one tool is being over-explored."""
    if count >= limit:
        return 3, (
            f"Execution controller: {tool} has been called {count} times and "
            f"has reached the per-tool validity limit ({limit}). Do not call "
            "this tool again. Use existing evidence, switch to a different "
            "viable source, or finish honestly."
        )
    if count >= max(2, limit - 2):
        return 2, (
            f"Execution controller: {tool} has already been called {count} "
            f"times; only {limit - count} calls remain before the trajectory "
            "is invalid. Stop varying the same lookup and choose another path "
            "or finish."
        )
    if count >= max(2, limit // 2):
        return 1, (
            f"Execution controller: {tool} has already been called {count} "
            "times. Repeated successful searches are still repetition. Reuse "
            "the evidence already returned; only call it again when one exact, "
            "untried lookup is essential."
        )
    return None


def _resolved_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    current: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(current, dict):
            return schema
        current = current.get(part.replace("~1", "/").replace("~0", "~"))
    return current if isinstance(current, dict) else schema


def normalize_tool_arguments(
    value: Any, schema: dict[str, Any], *, root: dict[str, Any] | None = None,
) -> Any:
    """Decode JSON-stringified object/array fields only when the schema expects it."""
    root = root or schema
    schema = _resolved_schema(schema, root)
    candidates = schema.get("anyOf") or schema.get("oneOf") or []
    if candidates:
        structured = [
            _resolved_schema(candidate, root)
            for candidate in candidates
            if isinstance(candidate, dict)
            and _resolved_schema(candidate, root).get("type") in {"object", "array"}
        ]
        if structured:
            schema = structured[0]
    expected = schema.get("type")
    if expected in {"object", "array"} and isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value
        if (
            expected == "object" and isinstance(decoded, dict)
        ) or (
            expected == "array" and isinstance(decoded, list)
        ):
            value = decoded
    if expected == "object" and isinstance(value, dict):
        properties = schema.get("properties") or {}
        return {
            key: normalize_tool_arguments(
                child,
                properties.get(key, {}) if isinstance(properties, dict) else {},
                root=root,
            )
            for key, child in value.items()
        }
    if expected == "array" and isinstance(value, list):
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        return [
            normalize_tool_arguments(child, item_schema, root=root)
            for child in value
        ]
    return value


def model_visible_text(text: str, max_chars: int) -> str:
    """``sanitize_tool_result``'s bounded string for the next LLM turn."""
    if max_chars > 0 and len(text) > max_chars:
        suffix = f"...[truncated, total {len(text)} chars]"
        text = text[: max(0, max_chars - len(suffix))] + suffix
    return text


def _limit_tool_calls(message: Any, allowed: int) -> None:
    """Keep only the calls the budget admits, in both message copies."""
    message.tool_calls = list(message.tool_calls or [])[:allowed] or None
    original = getattr(message, "original_message", None)
    raw_calls = getattr(original, "tool_calls", None) if original is not None else None
    if raw_calls:
        try:
            original.tool_calls = list(raw_calls)[:allowed] or None
        except (AttributeError, TypeError, ValueError):
            pass


def _estimated_request_chars(messages: List[Any], tools: List[Any]) -> int:
    def dump(value: Any) -> Any:
        return value.model_dump() if hasattr(value, "model_dump") else value

    return len(json.dumps(
        {"messages": [dump(item) for item in messages], "tools": [dump(item) for item in tools]},
        ensure_ascii=False, default=str, separators=(",", ":"),
    ))


def _server_of(tool_name: str, servers_by_tool: Dict[str, str], candidates: List[str]) -> str | None:
    server = servers_by_tool.get(tool_name)
    if server:
        return server
    return next((name for name in candidates if tool_name.startswith(f"{name}_")), None)


class _Output:
    def __init__(self, output_type: str, data: Any) -> None:
        self.type = output_type
        self.data = data


async def run_synthesis_eval(
    mcp_client: Any,
    model: str,
    messages: List[Message],
    max_turns: int,
    max_tool_calls: int,
    control: SynthesisControl,
    extra_body: Optional[Dict[str, Any]] = None,
    retry_thinking_contract_violations: bool = False,
    task_id: str = "unknown",
    include_telemetry: bool = False,
    prompt_cache_key: Optional[str] = None,
    output_factory: Any = _Output,
) -> AsyncGenerator[Any, None]:
    tools = await mcp_client.list_tools()
    tool_rows = [tool.model_dump() for tool in tools]
    transformed_tools = _transform_tool_calls(tool_rows)
    schemas = {
        str(row["name"]): row.get("input_schema") or {"type": "object"}
        for row in tool_rows
    }
    servers_by_tool = {
        str(row["name"]): str(row["server"]) for row in tool_rows if row.get("server")
    }
    conversation: List[Any] = list(messages)
    calls_seen = 0
    turns = 0
    calls_by_tool: Counter[str] = Counter()
    warning_tier: dict[str, int] = {}
    timed_out_servers: set[str] = set()
    aborted: str | None = None
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    usage_seen = False
    cache_reported = True

    while turns < max_turns:
        turns += 1
        remaining = max_tool_calls - calls_seen
        if remaining <= 0:
            aborted = "max_tool_calls_reached"
            break
        conversation.append(UserMessage(
            role="user", content=budget_status(remaining, max_turns - turns + 1),
        ))
        if _estimated_request_chars(conversation, transformed_tools) > control.context_max_chars:
            aborted = "context_budget_exhausted"
            break
        try:
            result = await create_completion(
                model=model, messages=conversation, tools=transformed_tools,
                extra_body=extra_body,
                retry_thinking_contract_violations=retry_thinking_contract_violations,
                task_id=task_id, turn=turns, prompt_cache_key=prompt_cache_key,
            )
        except FatalAccountError:
            raise
        except Exception as error:
            logger.error("Synthesis completion failed: %s", error)
            yield output_factory("error", {
                **classify_model_exception(error, model=model),
                "message": str(error),
                "serverResponse": None,
            })
            return
        if isinstance(result.usage, dict):
            usage_seen = True
            for key in total_usage:
                total_usage[key] += int(result.usage.get(key) or 0)
            cache_reported = cache_reported and bool(result.usage.get("cache_reported"))
        message = result.message
        raw_calls = list(message.tool_calls or [])
        allowed = min(len(raw_calls), remaining)
        _limit_tool_calls(message, allowed)
        selected = list(message.tool_calls or [])
        has_text = bool((message.content or "").strip())
        if not selected and not has_text:
            yield output_factory("message", message.model_dump())
            aborted = (
                "malformed_tool_call" if result.dropped_tool_calls
                else "empty_model_response"
            )
            break
        conversation.append(message)
        yield output_factory("message", message.model_dump())
        if not selected:
            break
        calls_seen += len(selected)
        warnings: list[str] = []
        for call in selected:
            name = str(call.function.get("name") or "")
            calls_by_tool[name] += 1
            warning = repeat_tool_warning(name, calls_by_tool[name], control.max_repeat_per_tool)
            if warning is not None and warning[0] > warning_tier.get(name, 0):
                warning_tier[name] = warning[0]
                warnings.append(warning[1])
            server = _server_of(name, servers_by_tool, control.no_retry_timeout_servers)
            if server in timed_out_servers:
                content = [TextContent(type="text", text=(
                    f"Tool server {server} already timed out in this run. Do not "
                    "retry that server; use a different source or finish honestly."
                ))]
            else:
                try:
                    arguments = json.loads(call.function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    arguments = normalize_tool_arguments(
                        arguments, schemas.get(name, {"type": "object"}),
                    )
                    response = await mcp_client.call_tool(name, arguments)
                    content = list(response.content)
                except FatalAccountError:
                    raise
                except Exception as error:
                    lines = str(error).splitlines()
                    content = [TextContent(
                        type="text", text=f"Error: {lines[0] if lines else type(error).__name__}",
                    )]
                    if (
                        server in control.no_retry_timeout_servers
                        and TIMEOUT_RE.search(str(error) + type(error).__name__)
                    ):
                        timed_out_servers.add(server)
                        warnings.append(
                            f"Execution controller: {server} timed out and is "
                            "unavailable for the rest of this run. Do not call "
                            "another tool from that server."
                        )
            stored = ToolCallOutputMessage(role="tool", tool_call_id=call.id, content=content)
            stored_dump = stored.model_dump()
            yield output_factory("message", stored_dump)
            conversation.append(ToolCallOutputMessage(
                role="tool", tool_call_id=call.id,
                content=[TextContent(
                    type="text",
                    text=model_visible_text(
                        str(stored_dump["content"]), control.per_result_max_chars,
                    ),
                )],
            ))
        if warnings:
            notice = UserMessage(role="user", content="\n".join(warnings))
            conversation.append(notice)
            yield output_factory("message", notice.model_dump())
        if len(raw_calls) > allowed:
            aborted = "max_tool_calls_reached"
            break
    else:
        aborted = "max_turns_reached"

    if aborted is not None:
        data = {
            **failure_receipt(
                failure_class="quality", reason_code=aborted,
                retryable=False, source_kind="agent",
            ),
            "maxToolCalls": max_tool_calls,
            "totalToolCalls": calls_seen,
            "maxTurns": max_turns,
            "reason": aborted,
        }
        write_runtime_event("model_calls", aborted, task_id=task_id, **data)
        yield output_factory("error", data)
    if include_telemetry:
        yield output_factory("telemetry", {
            "usage": ({**total_usage, "cache_reported": cache_reported} if usage_seen else None),
        })
