"""LLM completion functionality using LiteLLM."""

import json
import logging
from typing import Any, Dict, List, Optional
import os
import datetime

import httpx
import litellm
from pydantic import BaseModel

from .schema import Message, ToolCallSchema, AssistantMessage
from .config import config
from .pangu_completion import generate_pangu_async

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
    log_file_name = f"token_usage_{key_suffix}_{str(datetime.date.today()).replace('-', '')}.jsonl"
    os.makedirs(root_path, exist_ok=True)
    return os.path.join(root_path, log_file_name)


TOKEN_LOG_PATH = build_token_log_path(config.LLM_API_KEY)


class LLMResponse(BaseModel):
    """Response from LLM completion."""

    message: AssistantMessage
    original_content: Optional[str] = None


def configure_litellm():
    litellm.api_base = config.LLM_BASE_URL  # could also be just openai url
    litellm.api_key = config.LLM_API_KEY


# Configure LiteLLM once at module level
configure_litellm()


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
) -> LLMResponse:
    """Create a completion using LiteLLM."""

    # Convert our schema to LiteLLM form at
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
        litellm_messages = [msg.model_dump() for msg in messages]
        litellm_tools = [tool.model_dump() for tool in tools]

    # These specific models route through an internal proxy that expects the
    # "openai/" prefix in the model name. LiteLLM strips one "openai/" prefix
    # when a custom api_base is set, so we double-prepend it here so the proxy
    # receives the correct name (e.g. "openai/macaroni-alpha").
    _PROXY_PREFIX_MODELS = ("openai/macaroni-alpha", "openai/galapagos-alpha")
    if config.LLM_BASE_URL and model in _PROXY_PREFIX_MODELS:
        proxy_model = "openai/" + model
    else:
        proxy_model = model

    try:
        # 慢思考模式：复制一份再改，避免就地修改调用方传入的 dict
        extra_body = dict(extra_body) if isinstance(extra_body, dict) else {}
        extra_body["thinking"] = {**extra_body.get("thinking", {}), "type": "enabled"}

        if "pangu" in proxy_model:
            response = await generate_pangu_async(
                model=proxy_model,
                messages=litellm_messages,
                tools=litellm_tools
            )
        else:
            response = await litellm.acompletion(
                model=proxy_model,
                messages=litellm_messages,
                tools=litellm_tools,
                api_key=config.LLM_API_KEY,
                api_base=config.LLM_BASE_URL,
                timeout=config.DEFAULT_TIMEOUT,
                **({"extra_body": extra_body} if extra_body else {}),
            )

        # Convert response back to our format
        # Handle tool_calls conversion from OpenAI format to our format
        tool_calls = None
        if isinstance(response, dict):  # 盘古接口返回的是dict格式
            # 对于盘古思考过程调用工具提前终止的行为做预处理

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
        else:  # 开源接口返回的是ModelResponse格式
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

        # 获取助手轮次思考过程
        if isinstance(response, dict):  # 盘古接口返回的是dict格式
            reasoning_content = response["choices"][0]["message"].get("reasoning_content", None)
            if isinstance(reasoning_content, str):
                content = "<think>" + reasoning_content + "</think>" + str(response["choices"][0]["message"]["content"])
            else:
                content = response["choices"][0]["message"]["content"]
        else:  # 开源接口返回的是ModelResponse格式
            reasoning_content = getattr(response.choices[0].message, "reasoning_content", None)
            if isinstance(reasoning_content, str):
                content = "<think>" + reasoning_content + "</think>" + str(response.choices[0].message.content)
            else:
                content = response.choices[0].message.content

        # 记录token使用量
        if isinstance(response, dict):  # 盘古接口返回的是dict格式
            # todo 这里记录每一轮的盘古的回复，用于debug，分析是否存在格式错误
            pass
        else:  # 开源接口返回的是ModelResponse格式
            token_usage = {
                "model": proxy_model,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "prompt": [item.role + str(item.content) for item in messages],
                "answer": content,
            }
            os.makedirs(os.path.dirname(TOKEN_LOG_PATH) or ".", exist_ok=True)
            with open(TOKEN_LOG_PATH, 'a+', encoding="utf-8") as log_out:
                log_out.write(json.dumps(token_usage, ensure_ascii=False) + "\n")

        if isinstance(response, dict):  # 盘古接口返回的是dict格式
            assistant_message = AssistantMessage(
                role="assistant",
                content=content,
                tool_calls=tool_calls,
                original_message=response["choices"][0]["message"],
            )
        else:  # 开源接口返回的是ModelResponse格式
            if "deepseek" not in proxy_model:
                assistant_message = AssistantMessage(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                    original_message=response.choices[0].message,
                )
            else:
                assistant_message = AssistantMessage(
                    role="assistant",
                    content=str(response.choices[0].message.content),
                    reasoning_content=reasoning_content,
                    tool_calls=tool_calls,
                    original_message=response.choices[0].message,
                )

        return LLMResponse(message=assistant_message)

    except Exception as error:
        logger.error(f"LiteLLM completion failed: {error}")
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
