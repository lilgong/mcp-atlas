import os
import time
import json
import asyncio

from .runtime_log import write_runtime_event

import requests

os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""
os.environ["NO_PROXY"] = "*"

PANGU_TIMEOUT = int(os.getenv("PANGU_TIMEOUT", "1800"))
PANGU_MAX_RETRIES = int(os.getenv("PANGU_MAX_RETRIES", "5"))
PANGU_RETRY_DELAY = int(os.getenv("PANGU_RETRY_DELAY", "3"))


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
        # "stream": False,
        "temperature": 1.0,
        # "top_k": -1,
        "top_p": 0.8,
        "seed": 1234,
    }

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
            response = requests.post(api_url, headers=headers, json=payload, timeout=PANGU_TIMEOUT)
            if response.status_code == 200:
                result = response.json()
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
            last_exception = Exception(f"Pangu Response status code is not 200 (got {response.status_code})")
        except requests.exceptions.Timeout:
            last_exception = Exception(f"Pangu request timed out after {PANGU_TIMEOUT}s")
        except requests.exceptions.RequestException as e:
            last_exception = e

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
    # uvicorn 的 asyncio 事件循环、卡掉整个 server。放到线程里跑才能让 32 路并发真正并行。
    return await asyncio.to_thread(
        generate_pangu,
        model,
        messages,
        tools,
        task_id=task_id,
        turn=turn,
        call_id=call_id,
    )


if __name__ == "__main__":
    model_ = "pangu/92B-B005-stage2-9250-agent"  # 盘古模型开头是pangu/
    messages_ = [{"role": "user", "content": "你好"}]
    tools_ = []
    response_ = generate_pangu(model_, messages_, tools_)
    print(response_)
