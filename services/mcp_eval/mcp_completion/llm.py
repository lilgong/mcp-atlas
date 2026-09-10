"""LLM completion functionality using LiteLLM."""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import os
import datetime
import time
import uuid

import litellm
from pydantic import BaseModel

from .schema import Message, ToolCallSchema, AssistantMessage
from .config import config
from .runtime_log import jsonable, write_runtime_event
from .account_guard import FatalAccountError, is_fatal_account_error

logger = logging.getLogger(__name__)

# Configure LiteLLM - suppress verbose logging
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
litellm.ssl_verify = False


def month_log_root(base_root_path: str) -> str:
    leaf = os.path.basename(os.path.normpath(base_root_path))
    try:
        datetime.datetime.strptime(leaf, "%Y-%m")
        return base_root_path
    except ValueError:
        return os.path.join(base_root_path, datetime.date.today().strftime("%Y-%m"))


def build_token_log_path(api_key: str, env_name: str = "TOKEN_LOG_DIR") -> str:
    base_root_path = os.getenv(env_name, "token_usage_log")
    root_path = month_log_root(base_root_path)
    key_suffix = api_key[-8:] if api_key else "no-key"
    log_file_name = (
        f"token_usage_{key_suffix}_{str(datetime.date.today()).replace('-', '')}.jsonl"
    )
    os.makedirs(root_path, exist_ok=True)
    return os.path.join(root_path, log_file_name)


TOKEN_LOG_PATH = build_token_log_path(config.LLM_API_KEY)

THINKING_CONTRACT_MAX_ATTEMPTS = 3
RETRYABLE_MODEL_STATUS_CODES = frozenset({429, 500, 502, 503})
_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)


class ThinkingContractViolation(RuntimeError):
    """A provider returned reasoning after thinking was explicitly disabled."""


def _thinking_is_disabled(extra_body: Dict[str, Any]) -> bool:
    thinking = extra_body.get("thinking")
    return (
        isinstance(thinking, dict)
        and str(thinking.get("type") or "").strip().casefold() == "disabled"
    )


def _contains_nonempty_think_block(content: Any) -> bool:
    text = "" if content is None else str(content)
    return any(match.group(1).strip() for match in _THINK_BLOCK_RE.finditer(text))


def _response_message(response: Any) -> Any:
    if isinstance(response, dict):
        return response["choices"][0]["message"]
    return response.choices[0].message


def _message_value(message: Any, field: str) -> Any:
    if isinstance(message, dict):
        return message.get(field)
    return getattr(message, field, None)


def _reasoning_and_content(response: Any) -> Tuple[Any, str]:
    message = _response_message(response)
    reasoning = _message_value(message, "reasoning_content")
    raw_content = _message_value(message, "content")
    content = "" if raw_content is None else str(raw_content)
    return reasoning, content


def _message_for_provider(message: Message) -> Dict[str, Any]:
    """Serialize conversation history without framework-only fields.

    ``original_message`` is retained for result fidelity, but it is not part of
    the chat-completions protocol.  Reasoning-capable providers need the
    previous assistant turn's ``reasoning_content`` alongside its tool calls in
    order to continue an interleaved reasoning trajectory.
    """

    if isinstance(message, AssistantMessage):
        return message.model_dump(
            exclude={"original_message"},
            exclude_none=True,
        )
    return message.model_dump(exclude_none=True)


def _write_token_usage(
    response: Any,
    *,
    task_id: str,
    turn: int,
    call_id: str,
    model: str,
    messages: List[Message],
    answer: str,
) -> None:
    if isinstance(response, dict):
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    token_usage = {
        "task_id": task_id,
        "turn": turn,
        "call_id": call_id,
        "model": model,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "prompt": [item.role + str(item.content) for item in messages],
        "answer": answer,
    }
    os.makedirs(os.path.dirname(TOKEN_LOG_PATH) or ".", exist_ok=True)
    with open(TOKEN_LOG_PATH, "a+", encoding="utf-8") as log_out:
        log_out.write(json.dumps(token_usage, ensure_ascii=False) + "\n")


class LLMResponse(BaseModel):
    """Response from LLM completion."""

    message: AssistantMessage
    original_content: Optional[str] = None
    dropped_tool_calls: int = 0
    repaired_tool_calls: int = 0
    usage: Optional[Dict[str, Any]] = None


def _normalized_usage(usage: Any) -> Optional[Dict[str, Any]]:
    """Normalize provider usage without turning absent cache telemetry into zero."""
    value = jsonable(usage)
    if not isinstance(value, dict):
        return None
    details = value.get("prompt_tokens_details") or value.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    cache_reported = (
        "cached_tokens" in details
        or "cache_read_input_tokens" in value
        or "cached_tokens" in value
    )
    return {
        "input_tokens": int(value.get("prompt_tokens") or value.get("input_tokens") or 0),
        "output_tokens": int(value.get("completion_tokens") or value.get("output_tokens") or 0),
        "cached_tokens": int(
            details.get("cached_tokens")
            or value.get("cache_read_input_tokens")
            or value.get("cached_tokens")
            or 0
        ),
        "cache_reported": cache_reported,
    }


# Only braces are ever restored, shortest first. The observed defect drops
# closing braces when a response is split between calls. A missing quote or
# bracket means the text itself was cut short instead, and inventing one would
# fabricate arguments the model never produced.
_ARGUMENT_CLOSERS = ("}", "}}", "}}}")


def _repair_arguments(arguments: str) -> Optional[str]:
    """Rebalance arguments JSON that lost its outermost closing brace.

    Some providers split a multi-tool-call response by scanning for the first
    closing brace instead of balancing them, so every call except the last
    loses one '}' whenever its final value is a nested object. The prefix is
    otherwise intact, which makes appending the missing closers a faithful
    reconstruction rather than a guess. Returns None if it cannot be repaired.
    """
    for closer in _ARGUMENT_CLOSERS:
        candidate = arguments + closer
        try:
            json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        return candidate
    return None


def _sanitize_tool_calls(
    raw_calls: List[Dict[str, Any]],
) -> Tuple[Optional[List[Dict[str, Any]]], int, int]:
    """Make tool calls representable, and count what was repaired or dropped.

    Two provider defects are handled. A null function.name cannot satisfy
    ToolCall's Dict[str, str] and the call is unusable, so it is dropped.
    Arguments that are not valid JSON are worse than unusable: the malformed
    assistant message lands in the history and every later turn is rejected
    upstream with a 400, so the task can never recover and each rerun
    reproduces it. Those are repaired when possible, dropped otherwise.
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    repaired = 0
    for call in raw_calls:
        function = call.get("function") or {}
        name = function.get("name")
        if not (isinstance(name, str) and name):
            dropped += 1
            continue

        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            dropped += 1
            continue

        try:
            json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            fixed = _repair_arguments(arguments)
            if fixed is None:
                dropped += 1
                continue
            call = {
                **call,
                "function": {**function, "arguments": fixed},
            }
            repaired += 1

        kept.append(call)
    return (kept or None), dropped, repaired


def configure_litellm():
    litellm.api_base = config.LLM_BASE_URL  # could also be just openai url
    litellm.api_key = config.LLM_API_KEY


# Configure LiteLLM once at module level
configure_litellm()


def _model_error_status_code(error: Exception) -> Optional[int]:
    """Extract an HTTP status without depending on one LiteLLM exception type."""

    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


async def _create_openai_compatible_completion(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    extra_body: Dict[str, Any],
    task_id: str,
    turn: int,
    call_id: str,
    prompt_cache_key: Optional[str] = None,
) -> Any:
    """Call the configured OpenAI-compatible endpoint with bounded retries.

    SDK retries are disabled so retry count is controlled in exactly one
    place. A 504, a client timeout, and connection errors are deliberately not
    retried because the upstream generation may still be running.
    """

    for provider_attempt in range(1, config.LLM_MAX_ATTEMPTS + 1):
        try:
            return await litellm.acompletion(
                model=model,
                custom_llm_provider="openai",
                messages=messages,
                tools=tools,
                api_key=config.LLM_API_KEY,
                api_base=config.LLM_BASE_URL,
                timeout=config.DEFAULT_TIMEOUT,
                max_retries=0,
                **({"prompt_cache_key": prompt_cache_key} if prompt_cache_key else {}),
                **({"extra_body": extra_body} if extra_body else {}),
            )
        except Exception as error:
            if isinstance(
                error,
                (litellm.APIConnectionError, litellm.Timeout, asyncio.TimeoutError),
            ):
                raise

            status_code = _model_error_status_code(error)
            should_retry = (
                status_code in RETRYABLE_MODEL_STATUS_CODES
                and provider_attempt < config.LLM_MAX_ATTEMPTS
            )
            if not should_retry:
                raise

            delay = config.LLM_RETRY_DELAY * (2 ** (provider_attempt - 1))
            write_runtime_event(
                "model_calls",
                "model_request_retry_scheduled",
                task_id=task_id,
                turn=turn,
                call_id=call_id,
                model=model,
                provider_attempt=provider_attempt,
                max_provider_attempts=config.LLM_MAX_ATTEMPTS,
                status_code=status_code,
                delay_seconds=delay,
            )
            if delay:
                await asyncio.sleep(delay)


def strip_all_additional_properties(schema: any) -> any:
    """Recursively remove all `additionalProperties` keys from the schema."""
    if isinstance(schema, dict):
        # Remove 'additionalProperties' if it exists
        schema.pop("additionalProperties", None)

        # Recurse into all values
        for key, value in schema.items():
            strip_all_additional_properties(value)

    elif isinstance(schema, list):
        for item in schema:
            strip_all_additional_properties(item)

    return schema


async def create_completion(
    model: str,
    messages: List[Message],
    tools: List[ToolCallSchema],
    extra_body: Optional[Dict[str, Any]] = None,
    retry_thinking_contract_violations: bool = False,
    task_id: str = "unknown",
    turn: int = 0,
    prompt_cache_key: Optional[str] = None,
) -> LLMResponse:
    """Create a completion using LiteLLM."""

    # Convert our schema to provider form.  Gemini retains its historical raw
    # assistant-message path; other providers receive the normalized message,
    # including reasoning_content when the provider returned it on a previous
    # turn, but never the framework-only original_message wrapper.
    if "gemini" in model.lower():
        litellm_messages = [
            (
                msg.model_dump()
                if not isinstance(msg, AssistantMessage)
                else msg.original_message.model_dump()
            )
            for msg in messages
        ]
        litellm_tools = [
            strip_all_additional_properties(tool.model_dump()) for tool in tools
        ]
    else:
        litellm_messages = [_message_for_provider(msg) for msg in messages]
        litellm_tools = [tool.model_dump() for tool in tools]

    # Copy the caller-owned mapping, but preserve its provider-specific fields
    # exactly.  In particular, callers must be able to switch between
    # ``thinking.type=enabled`` and ``thinking.type=disabled`` without the
    # completion service silently overriding the requested mode.
    extra_body = dict(extra_body) if isinstance(extra_body, dict) else {}
    thinking_disabled = _thinking_is_disabled(extra_body)
    max_attempts = (
        THINKING_CONTRACT_MAX_ATTEMPTS if retry_thinking_contract_violations else 1
    )
    response = None
    reasoning_content = None
    content = ""
    call_id = ""
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    usage_seen = False
    cache_reported = True

    for attempt in range(1, max_attempts + 1):
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        write_runtime_event(
            "model_calls",
            "model_call_started",
            task_id=task_id,
            turn=turn,
            call_id=call_id,
            model=model,
            attempt=attempt,
            max_attempts=max_attempts,
            base_url=config.LLM_BASE_URL,
            request={
                "messages": litellm_messages,
                "tools": litellm_tools,
                "extra_body": extra_body,
            },
        )

        try:
            response = await _create_openai_compatible_completion(
                model=model,
                messages=litellm_messages,
                tools=litellm_tools,
                extra_body=extra_body,
                task_id=task_id,
                turn=turn,
                call_id=call_id,
                prompt_cache_key=prompt_cache_key,
            )
        except Exception as error:
            logger.error(f"LiteLLM completion failed: {error}")
            write_runtime_event(
                "model_calls",
                "model_call_failed",
                task_id=task_id,
                turn=turn,
                call_id=call_id,
                model=model,
                attempt=attempt,
                duration_seconds=round(time.monotonic() - started, 3),
                error_type=type(error).__name__,
                error=str(error),
            )
            if is_fatal_account_error(error):
                raise FatalAccountError(
                    "model credential is invalid or out of funds",
                    source_kind="model",
                    source_name=model,
                    credential_envs=("LLM_API_KEY",),
                ) from error
            raise

        usage = None
        if not isinstance(response, dict):
            usage = jsonable(getattr(response, "usage", None))
        elif isinstance(response.get("usage"), dict):
            usage = response.get("usage")
        normalized_usage = _normalized_usage(usage)
        if normalized_usage is not None:
            usage_seen = True
            for key in total_usage:
                total_usage[key] += int(normalized_usage[key])
            cache_reported = cache_reported and bool(normalized_usage["cache_reported"])
        write_runtime_event(
            "model_calls",
            "model_call_completed",
            task_id=task_id,
            turn=turn,
            call_id=call_id,
            model=model,
            attempt=attempt,
            duration_seconds=round(time.monotonic() - started, 3),
            usage=usage,
            response=jsonable(response),
        )

        reasoning_content, content = _reasoning_and_content(response)
        _write_token_usage(
            response,
            task_id=task_id,
            turn=turn,
            call_id=call_id,
            model=model,
            messages=messages,
            answer=content,
        )
        leaked_reasoning = isinstance(reasoning_content, str) and bool(
            reasoning_content.strip()
        )
        leaked_think_block = _contains_nonempty_think_block(content)
        if not (thinking_disabled and (leaked_reasoning or leaked_think_block)):
            break

        write_runtime_event(
            "model_calls",
            "thinking_contract_violation",
            task_id=task_id,
            turn=turn,
            call_id=call_id,
            model=model,
            attempt=attempt,
            max_attempts=max_attempts,
            leaked_reasoning_content=leaked_reasoning,
            leaked_think_block=leaked_think_block,
            retry_enabled=retry_thinking_contract_violations,
            will_retry=retry_thinking_contract_violations and attempt < max_attempts,
        )
        if not retry_thinking_contract_violations:
            break
        if attempt == max_attempts:
            raise ThinkingContractViolation(
                "provider returned non-empty thinking while thinking.type=disabled "
                f"for {max_attempts} consecutive attempts"
            )

    try:
        assert response is not None

        # Convert response back to our format
        # Handle tool_calls conversion from OpenAI format to our format
        tool_calls = None
        dropped_tool_calls = 0
        repaired_tool_calls = 0
        if isinstance(response, dict):
            if response["choices"][0]["message"].get("tool_calls"):
                tool_calls = []
                for tool_call in response["choices"][0]["message"]["tool_calls"]:
                    tool_calls.append(
                        {
                            "id": tool_call["id"],
                            "type": tool_call["type"],
                            "function": {
                                "name": tool_call["function"]["name"],
                                "arguments": tool_call["function"]["arguments"],
                            },
                        }
                    )
        else:
            if response.choices[0].message.tool_calls:
                tool_calls = []
                for tool_call in response.choices[0].message.tool_calls:
                    tool_calls.append(
                        {
                            "id": tool_call.id,
                            "type": tool_call.type,
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                    )

        if tool_calls:
            (
                tool_calls,
                dropped_tool_calls,
                repaired_tool_calls,
            ) = _sanitize_tool_calls(tool_calls)
            if dropped_tool_calls or repaired_tool_calls:
                write_runtime_event(
                    "model_calls",
                    "malformed_tool_calls_dropped",
                    task_id=task_id,
                    turn=turn,
                    call_id=call_id,
                    model=model,
                    dropped=dropped_tool_calls,
                    repaired=repaired_tool_calls,
                    kept=len(tool_calls or []),
                )

        original_message = (
            response["choices"][0]["message"]
            if isinstance(response, dict)
            else response.choices[0].message
        )
        assistant_message = AssistantMessage(
            role="assistant",
            content=content,
            reasoning_content=(
                reasoning_content if isinstance(reasoning_content, str) else None
            ),
            tool_calls=tool_calls,
            original_message=original_message,
        )

        return LLMResponse(
            message=assistant_message,
            dropped_tool_calls=dropped_tool_calls,
            repaired_tool_calls=repaired_tool_calls,
            usage=(
                {**total_usage, "cache_reported": cache_reported}
                if usage_seen else None
            ),
        )

    except Exception as error:
        logger.error(f"LiteLLM response parsing failed: {error}")
        write_runtime_event(
            "model_calls",
            "model_response_parsing_failed",
            task_id=task_id,
            turn=turn,
            call_id=call_id,
            model=model,
            error_type=type(error).__name__,
            error=str(error),
        )
        raise


def _transform_tool_calls(tools: List[Dict[str, Any]]) -> List[ToolCallSchema]:
    """Transform tool definitions to ToolCallSchema format."""
    transformed_tools = []
    for tool in tools:
        input_schema = tool.get("input_schema", {})
        if isinstance(input_schema, dict):
            if "required" not in input_schema or input_schema["required"] is None:
                input_schema = {**input_schema, "required": []}
            elif not isinstance(input_schema.get("required"), list):
                input_schema = {**input_schema, "required": []}

        transformed_tool = ToolCallSchema(
            type="function",
            function={
                "name": tool["name"],
                "description": tool["description"],
                "parameters": input_schema,
                "strict": False,
            },
        )
        transformed_tools.append(transformed_tool)

    return transformed_tools
