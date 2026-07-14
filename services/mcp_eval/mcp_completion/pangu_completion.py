import os
import time
import json
import copy
import uuid
import asyncio

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


def generate_tool_call_id():
    return f"chatcmpl-tool-{str(uuid.uuid4().hex)[:16]}"


def pangu_response_refiner(response):
    """针对盘古模型思考过程调工具导致对话终止的情况，做response的修正。"""
    response_result = copy.deepcopy(response)
    message = response["choices"][0]["message"]
    reasoning_content = message.get("reasoning") or message.get("reasoning_content")
    if not reasoning_content:
        return response_result
    # 如果content和tool_calls均为空，则做response的修改，否则直接返回response
    if not message.get("content") and not message.get("tool_calls"):
        if "<|tool_call_start|>" in reasoning_content and reasoning_content.count("<|tool_call_start|>") == 1 and \
                "<|tool_call_end|>" in reasoning_content and reasoning_content.count("<|tool_call_end|>") == 1:
            # 思考过程调用工具的场景
            content = reasoning_content.split("<|tool_call_start|>")[0] + reasoning_content.split("<|tool_call_end|>")[
                -1]
            tool_call = reasoning_content.split("<|tool_call_start|>")[-1].split("<|tool_call_end|>")[0]
            tool_call = json.loads(tool_call)
            tool_call_result = []
            for tc in tool_call:
                tool_call_result.append({
                    "id": generate_tool_call_id(),
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"] if isinstance(tc["arguments"], str) else json.dumps(
                            tc["arguments"]),
                    }
                })
            response_result["choices"][0]["message"]["content"] = content
            response_result["choices"][0]["message"]["tool_calls"] = tool_call_result
            response_result["choices"][0]["message"]["reasoning"] = ""
            response_result["choices"][0]["message"]["reasoning_content"] = ""
        else:
            # 思考过程没调用工具，但是content为空的场景
            response_result["choices"][0]["message"]["content"] = reasoning_content
            response_result["choices"][0]["message"]["reasoning"] = ""
            response_result["choices"][0]["message"]["reasoning_content"] = ""
    return response_result


def generate_pangu(model, messages, tools):
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
        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=PANGU_TIMEOUT)
            if response.status_code == 200:
                result = response.json()
                # 修正"思考过程里调用工具导致提前终止"的回复；异常则回退到原始响应
                try:
                    result = pangu_response_refiner(result)
                except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                    pass
                with open(get_pangu_log_path(), 'a+', encoding='utf-8') as out_file:
                    out_file.write(json.dumps({"messages": messages, "response": result}, ensure_ascii=False) + '\n')
                return result
            last_exception = Exception(f"Pangu Response status code is not 200 (got {response.status_code})")
        except requests.exceptions.Timeout:
            last_exception = Exception(f"Pangu request timed out after {PANGU_TIMEOUT}s")
        except requests.exceptions.RequestException as e:
            last_exception = e

        if attempt < PANGU_MAX_RETRIES:
            time.sleep(PANGU_RETRY_DELAY)

    raise Exception(f"Pangu request failed after {PANGU_MAX_RETRIES} attempts: {last_exception}")


async def generate_pangu_async(model, messages, tools):
    # generate_pangu 内部用同步 requests + time.sleep + 文件写入，直接 await 会冻住
    # uvicorn 的 asyncio 事件循环、卡掉整个 server。放到线程里跑才能让 32 路并发真正并行。
    return await asyncio.to_thread(generate_pangu, model, messages, tools)


if __name__ == "__main__":
    model_ = "pangu/92B-B005-stage2-9250-agent"  # 盘古模型开头是pangu/
    messages_ = [{"role": "user", "content": "你好"}]
    tools_ = []
    response_ = generate_pangu(model_, messages_, tools_)
    print(response_)
