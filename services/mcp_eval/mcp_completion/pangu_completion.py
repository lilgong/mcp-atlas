import os
import time
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .runtime_log import write_runtime_event

import requests

from .account_guard import FatalAccountError, is_fatal_account_error
from .streaming import llm_streaming_enabled

os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""
os.environ["NO_PROXY"] = "*"

PANGU_TIMEOUT = int(os.getenv("PANGU_TIMEOUT", "1800"))
PANGU_MAX_RETRIES = int(os.getenv("PANGU_MAX_RETRIES", "5"))
PANGU_RETRY_DELAY = int(os.getenv("PANGU_RETRY_DELAY", "3"))
MCP_COMPLETION_CONCURRENCY = int(os.getenv("MCP_COMPLETION_CONCURRENCY", "30"))

# Pangu uses a synchronous requests client, so give it a dedicated executor.
# Matching the task concurrency avoids Python's smaller implicit thread-pool
# limit and prevents long model requests from starving unrelated to_thread work.
_PANGU_EXECUTOR = ThreadPoolExecutor(
    max_workers=MCP_COMPLETION_CONCURRENCY,
    thread_name_prefix="pangu-request",
)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise RuntimeError(f"Missing environment variable: {name}")


def get_pangu_api_url() -> str:
    return os.getenv("PANGU_API_URL") or require_env("LLM_BASE_URL")


def get_pangu_api_key() -> str:
    return os.getenv("PANGU_API_KEY") or require_env("LLM_API_KEY")


def get_pangu_log_path() -> str:
    log_path = os.getenv("PANGU_LOG_PATH")
    if not log_path:
        log_dir = os.getenv("PANGU_LOG_DIR", "completion_results")
        log_path = os.path.join(log_dir, f"pangu_response_{time.strftime('%Y%m%d')}.jsonl")
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    return log_path


def _append_text(current: str, value) -> str:
    return current + (value if isinstance(value, str) else "")


def _collect_pangu_stream(response):
    """Reconstruct one OpenAI-compatible assistant message from SSE deltas."""
    content = ""
    reasoning = ""
    tool_calls = {}
    finish_reason = None
    usage = None
    response_meta = {}
    saw_chunk = False
    saw_done = False

    for raw_line in response.iter_lines(decode_unicode=True):
        line = (raw_line or "").strip()
        if not line or line.startswith(":") or line.startswith("event:"):
            continue
        payload = line[5:].strip() if line.startswith("data:") else line
        if payload == "[DONE]":
            saw_done = True
            break

        chunk = json.loads(payload)
        if chunk.get("error"):
            raise RuntimeError(
                "Pangu stream error: "
                + json.dumps(chunk["error"], ensure_ascii=False)[:500]
            )

        choices = chunk.get("choices") or []
        # Some compatible gateways ignore stream=true and return a complete
        # chat-completion JSON body. Preserve it without reconstructing it.
        if choices and isinstance(choices[0].get("message"), dict):
            return chunk

        saw_chunk = True
        for key in ("id", "created", "model", "system_fingerprint"):
            if chunk.get(key) is not None:
                response_meta[key] = chunk[key]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        if not choices:
            continue

        choice = choices[0]
        delta = choice.get("delta") or {}
        content = _append_text(content, delta.get("content"))
        reasoning = _append_text(
            reasoning,
            delta.get("reasoning_content"),
        )
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]

        for call_delta in delta.get("tool_calls") or []:
            index = call_delta.get("index")
            if not isinstance(index, int):
                raise RuntimeError("Pangu tool-call stream delta lacks an index")
            call = tool_calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            call["id"] = _append_text(call["id"], call_delta.get("id"))
            if isinstance(call_delta.get("type"), str):
                call["type"] = call_delta["type"]
            function = call_delta.get("function") or {}
            call["function"]["name"] = _append_text(
                call["function"]["name"], function.get("name")
            )
            call["function"]["arguments"] = _append_text(
                call["function"]["arguments"], function.get("arguments")
            )

    if not saw_chunk:
        raise RuntimeError("Pangu stream returned no chunks")
    if finish_reason is None and not saw_done:
        raise RuntimeError("Pangu stream ended before a finish marker")

    message = {
        "role": "assistant",
        "content": content or None,
    }
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    result = {
        **response_meta,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        result["usage"] = usage
    return result


def generate_pangu(
    model,
    messages,
    tools,
    *,
    task_id: str = "unknown",
    turn: int = 0,
    call_id: str = "",
):
    assert model.startswith("pangu/"), "盘古模型命名错误"
    model = model.replace("pangu/", "")

    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {get_pangu_api_key()}"}
    api_url = get_pangu_api_url()


    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        # "chat_template_kwargs": {"think": True, "context_thinking": "Interleave"},
        "temperature": 1.0,
        # "top_k": -1,
        "top_p": 0.8,
        "seed": 1234,
    }
    use_streaming = llm_streaming_enabled()
    if use_streaming:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

    last_exception = None
    for attempt in range(1, PANGU_MAX_RETRIES + 1):
        attempt_started = time.monotonic()
        write_runtime_event(
            "model_calls",
            "model_provider_attempt_started",
            task_id=task_id,
            turn=turn,
            call_id=call_id,
            provider="pangu",
            attempt=attempt,
            model=model,
        )
        try:
            request_kwargs = {
                "headers": headers,
                "json": payload,
                "timeout": PANGU_TIMEOUT,
            }
            if use_streaming:
                request_kwargs["stream"] = True
            response = requests.post(api_url, **request_kwargs)
            if response.status_code == 200:
                result = (
                    _collect_pangu_stream(response)
                    if use_streaming
                    and "application/json"
                    not in response.headers.get("content-type", "").lower()
                    else response.json()
                )
                with open(get_pangu_log_path(), 'a+', encoding='utf-8') as out_file:
                    out_file.write(json.dumps({"messages": messages, "response": result}, ensure_ascii=False) + '\n')
                write_runtime_event(
                    "model_calls",
                    "model_provider_attempt_completed",
                    task_id=task_id,
                    turn=turn,
                    call_id=call_id,
                    provider="pangu",
                    attempt=attempt,
                    model=model,
                    duration_seconds=round(time.monotonic() - attempt_started, 3),
                    status_code=200,
                )
                return result
            error_body = response.text.strip()[:500]
            last_exception = Exception(
                f"Pangu HTTP {response.status_code}: {error_body or '<empty body>'}"
            )
            if is_fatal_account_error(last_exception):
                credential_env = (
                    "PANGU_API_KEY" if os.getenv("PANGU_API_KEY") else "LLM_API_KEY"
                )
                raise FatalAccountError(
                    "model credential is invalid or out of funds",
                    source_kind="model",
                    source_name=f"pangu/{model}",
                    credential_envs=(credential_env,),
                ) from last_exception
        except requests.exceptions.Timeout:
            last_exception = Exception(f"Pangu request timed out after {PANGU_TIMEOUT}s")
        except requests.exceptions.RequestException as e:
            last_exception = e
        except FatalAccountError:
            raise
        except Exception as e:
            # Preserve the original non-stream failure boundary exactly: a
            # malformed successful JSON response escaped this provider retry
            # loop and was handled by the outer completion layer. Stream
            # decoding adds new partial-body failure modes that do belong to
            # this provider retry loop, but only when streaming is enabled.
            if not use_streaming:
                raise
            last_exception = e
            if is_fatal_account_error(e):
                credential_env = (
                    "PANGU_API_KEY"
                    if os.getenv("PANGU_API_KEY")
                    else "LLM_API_KEY"
                )
                raise FatalAccountError(
                    "model credential is invalid or out of funds",
                    source_kind="model",
                    source_name=f"pangu/{model}",
                    credential_envs=(credential_env,),
                ) from e

        write_runtime_event(
            "model_calls",
            "model_provider_attempt_failed",
            task_id=task_id,
            turn=turn,
            call_id=call_id,
            provider="pangu",
            attempt=attempt,
            model=model,
            duration_seconds=round(time.monotonic() - attempt_started, 3),
            error=str(last_exception),
        )

        if attempt < PANGU_MAX_RETRIES:
            time.sleep(PANGU_RETRY_DELAY)

    raise Exception(f"Pangu request failed after {PANGU_MAX_RETRIES} attempts: {last_exception}")


async def generate_pangu_async(
    model,
    messages,
    tools,
    *,
    task_id: str = "unknown",
    turn: int = 0,
    call_id: str = "",
):
    # generate_pangu 内部用同步 requests + time.sleep + 文件写入，直接 await 会冻住
    # uvicorn 的 asyncio 事件循环、卡掉整个 server。使用专用线程池，线程数与
    # MCP_COMPLETION_CONCURRENCY 一致，也不会占满 asyncio 的默认线程池。
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _PANGU_EXECUTOR,
        partial(
            generate_pangu,
            model,
            messages,
            tools,
            task_id=task_id,
            turn=turn,
            call_id=call_id,
        ),
    )


if __name__ == "__main__":
    model_ = "pangu/92B-B005-stage2-9250-agent"  # 盘古模型开头是pangu/
    messages_ = [{"role": "user", "content": "你好"}]
    tools_ = []
    response_ = generate_pangu(model_, messages_, tools_)
    print(response_)
