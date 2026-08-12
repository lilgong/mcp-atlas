"""MCP evaluation functionality."""

import json
import logging
import os
import uuid
from typing import AsyncGenerator, Dict, List, Union, Any, Optional, Tuple

from .mcp_client import IsolatedMCPClient, MCPClient
from .llm import create_completion, _transform_tool_calls
from .account_guard import FatalAccountError
from .schema import (
    RunAgentAPIRequestBody,
    Message,
    AssistantMessage,
    ToolCallOutputMessage,
    TextContent,
    ImageContent,
    ResourceContent,
    Content,
    CallToolResponse,
    SystemMessage,
    UserMessage,
)
from .errors import MCPClientToolExecutionError
from .config import config
from .runtime_log import write_runtime_event

logger = logging.getLogger(__name__)


def _tool_result_char_limit() -> int:
    """Per-tool-call character budget; 0 or less disables clamping."""
    return int(os.getenv("MAX_TOOL_RESULT_CHARS", "120000"))


def _turn_result_char_limit() -> int:
    """Combined budget for every tool call in one turn.

    A per-call cap alone is trivially defeated by parallel tool calls: seven
    calls each clipped to the per-call limit still add seven times that much
    to the context in a single turn.
    """
    return int(os.getenv("MAX_TURN_TOOL_RESULT_CHARS", "150000"))


def _call_budget(turn_budget: int, calls_left: int, per_call: int) -> int:
    """Share what is left of the turn budget across the remaining calls.

    Splitting evenly keeps one greedy call from starving its siblings, while
    unspent share rolls forward because the budget is recomputed per call.
    """
    if turn_budget <= 0:
        return per_call
    share = max(1, turn_budget // max(1, calls_left))
    return share if per_call <= 0 else min(per_call, share)


# A model that emits malformed calls tends to keep doing it, so bound the
# corrective retries instead of burning the whole turn budget on them.
_MAX_MALFORMED_TURNS = int(os.getenv("MAX_MALFORMED_TOOL_CALL_TURNS", "2"))


_TRUNCATION_NOTE = (
    "\n\n[Tool result truncated: {kept} of {total} characters shown. Narrow the "
    "query with a filter, a smaller page size, or a more specific search term "
    "to see the rest.]"
)


def _clamp_tool_result(
    content: List[Content], limit: int
) -> Tuple[List[Content], int, int]:
    """Clip a tool result so a single call cannot swamp the context window.

    An unbounded result (an unfiltered search can return hundreds of KB) is
    re-sent on every subsequent turn, which is what pushes a task past the
    caller's request timeout. Returns the clamped content, how many characters
    were dropped, and how many were kept.
    """
    total = sum(
        len(part.text) for part in content if isinstance(part, TextContent)
    )
    if limit <= 0 or total <= limit:
        return content, 0, total

    clamped: List[Content] = []
    budget = limit
    for part in content:
        if not isinstance(part, TextContent):
            clamped.append(part)
            continue
        if budget <= 0:
            continue
        clamped.append(TextContent(type="text", text=part.text[:budget]))
        budget -= min(budget, len(part.text))

    clamped.append(
        TextContent(
            type="text",
            text=_TRUNCATION_NOTE.format(kept=limit, total=total),
        )
    )
    return clamped, total - limit, limit


class AgentOutput:
    """MCP eval output wrapper."""

    def __init__(self, output_type: str, data: Any):
        self.type = output_type
        self.data = data


async def run_mcp_eval(
    mcp_client: MCPClient,
    model: str,
    messages: List[Message],
    max_turns: int,
    extra_body: Optional[Dict[str, Any]] = None,
    retry_thinking_contract_violations: bool = False,
    task_id: str = "unknown",
) -> AsyncGenerator[AgentOutput, None]:
    """
    Simple MCP evaluation loop that keeps calling tools until the model decides there are no more tools to call.
    """
    tools = await mcp_client.list_tools()
    transformed_tools = _transform_tool_calls([tool.model_dump() for tool in tools])

    all_messages: List[Message] = list(messages)
    malformed_turns = 0

    for i in range(max_turns):
        assistant_message = None
        original_content = None

        try:
            # Use unified LiteLLM completion for all models
            result = await create_completion(
                model=model,
                messages=all_messages,
                tools=transformed_tools,
                extra_body=extra_body,
                retry_thinking_contract_violations=retry_thinking_contract_violations,
                task_id=task_id,
                turn=i + 1,
            )

            assistant_message = result.message
            original_content = result.original_content

        except FatalAccountError:
            raise
        except Exception as error:
            logger.error(f"Model create completion or parsing failed: {error}")
            # Re-raise as server error instead of graceful handling
            raise Exception(f"LLM completion failed: {error}")

        all_messages.append(assistant_message)

        yield AgentOutput("message", assistant_message.model_dump())

        tool_calls = assistant_message.tool_calls or []

        if tool_calls:
            malformed_turns = 0
            per_call_limit = _tool_result_char_limit()
            turn_budget = _turn_result_char_limit()

            for call_index, tool_call in enumerate(tool_calls):
                # Recomputed per call so a failed call does not skew the split.
                calls_left = len(tool_calls) - call_index
                try:
                    # Parse tool arguments
                    args = json.loads(tool_call.function["arguments"])

                    # Call the tool
                    response = await mcp_client.call_tool(
                        tool_call.function["name"],
                        args,
                    )

                    limit = _call_budget(
                        turn_budget, calls_left, per_call_limit
                    )
                    content, dropped, kept = _clamp_tool_result(
                        response.content, limit
                    )
                    turn_budget -= kept
                    if dropped:
                        write_runtime_event(
                            "tools",
                            "tool_result_truncated",
                            task_id=task_id,
                            turn=i + 1,
                            tool=tool_call.function["name"],
                            dropped_chars=dropped,
                            limit=limit,
                            parallel_calls=len(tool_calls),
                        )

                    # Create tool call message
                    tool_call_message = ToolCallOutputMessage(
                        role="tool",
                        content=content,
                        tool_call_id=tool_call.id,
                    )

                    all_messages.append(tool_call_message)
                    yield AgentOutput("message", tool_call_message.model_dump())

                except FatalAccountError:
                    raise
                except Exception as error:
                    logger.error(
                        f"Tool call failed: {error}, tool: {tool_call.function['name']}"
                    )
                    # 不再因单个工具失败而中断整条轨迹：把错误作为 tool result
                    # 回灌给模型，让它有机会换工具/换参数恢复。
                    tool_call_message = ToolCallOutputMessage(
                        role="tool",
                        content=[
                            TextContent(
                                type="text",
                                text=f"Tool execution failed - tool: {tool_call.function['name']}, error: {error}",
                            )
                        ],
                        tool_call_id=tool_call.id,
                    )
                    all_messages.append(tool_call_message)
                    yield AgentOutput("message", tool_call_message.model_dump())
        elif result.dropped_tool_calls:
            # Every tool call this turn was malformed (a null function.name).
            # Treat it like a failed tool call: tell the model what went wrong
            # and let it retry, rather than ending the task as if it were done.
            malformed_turns += 1
            if malformed_turns > _MAX_MALFORMED_TURNS:
                write_runtime_event(
                    "model_calls",
                    "malformed_tool_calls_gave_up",
                    task_id=task_id,
                    turn=i + 1,
                    consecutive_turns=malformed_turns,
                )
                break
            retry_message = UserMessage(
                role="user",
                content=(
                    f"{result.dropped_tool_calls} tool call(s) were rejected "
                    "because the function name was missing. Reissue them with a "
                    "valid tool name from the provided list, or answer directly "
                    "if no tool is needed."
                ),
            )
            all_messages.append(retry_message)
            yield AgentOutput("message", retry_message.model_dump())
        else:
            # No more tool calls, agent is done
            break


async def handle_run_mcp_eval(
    body: RunAgentAPIRequestBody,
) -> AsyncGenerator[AgentOutput, None]:
    """
    Shared handler for running MCP eval that can be used by different routers.

    Args:
        body: Request body matching RunAgentAPIRequestBodySchema format

            Yields:
        AgentOutput: Generator that yields either successful messages or errors during MCP eval execution
    """
    task_id = body.task_id or f"request-{uuid.uuid4().hex[:14]}"
    mcp_client = IsolatedMCPClient(
        task_id=task_id,
        shared_url=config.MCP_SERVER_URL,
        enabled_tools=body.enabled_tools,
    )

    async with mcp_client:
        async for output in run_mcp_eval(
            mcp_client=mcp_client,
            model=body.model,
            messages=body.messages,
            max_turns=body.max_turns,
            extra_body=body.extra_body,
            retry_thinking_contract_violations=body.retry_thinking_contract_violations,
            task_id=task_id,
        ):
            yield output
