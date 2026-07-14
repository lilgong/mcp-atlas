import copy
import datetime
import json
import os
from json import JSONDecodeError
import time
import tiktoken
import pandas as pd
# from log.new_write_log import write_model_log
import argparse
import httpx
from openai import OpenAI
from tqdm import tqdm
from typing import List
import concurrent.futures
from dotenv import load_dotenv, find_dotenv

from new_ import semantic_similarity

# 从仓库根目录的 .env 读取配置（与其他脚本一致），源码里不写死任何 key / 路径
load_dotenv(find_dotenv())

DEFAULT_VERIFY_MODEL = "gpt-5.4"

# 运行期句柄：在 main() 里按解析出的配置初始化后，供工作线程使用
_CLIENT = None
_MODEL = None
_TOKEN_LOG_PATH = None

system_prompt = """你是一名数据质检专家，现在需要你根据规则质检数据，结果输出为json结构"""

judge_prompt = """
    # 数据讲解
    1.标准答案为list[dict],模型调用工具输出为dict。标准答案list中的dict结构与模型调用工具输出一致
    2.dict中包括工具名称，参数，结果
    3.有些工具输出结果为：做超长处理，返回内容非空。是我做了单独的处理（内容大于300个字符）。不是错误
    
    # 质检目的
    判断实际工具调用中是否存在因环境存在的错误
    
    # 错误类型
    1.api 错误：超出额度，api key错误，网络超时等
    2.调用失败。例如:Failed to call tool
    3.返回内容为空，尤其是搜索类工具。包括：（1）返回结构体中内容为空，（2）空字符，（3）语义表示没内容：no result之类的。
        注意：此项判断要参考标准答案，若是标准答案返回同样为空则，判断正确。
    4.其他因为环境导致的工具调用失败
    
    # 规避错误
    1.参数引发错误。例如：Error: Expecting value: line 1 column 1 (char 0)

    # 标准工具调用情况
    [trajectory_function]
    
    # 实际工具调用情况
    [raw_conversation_history]


    # 输出结果
    请严格按照以下结果输出，不要使用md中json字符包裹
    {
        "func_name":str<函数名称>,
        "error":str<明确模型错误类型，无错误为空>,
        "error_content":str<工具的错误输出>,
        "reason":str<判断原因>
    }
    """

def get_verify_config():
    """质检(verified)裁判模型配置，全部来自 .env；专属变量缺省时回退到评分裁判 / 通用 LLM。"""
    api_key = (
        os.getenv("VERIFY_LLM_API_KEY")
        or os.getenv("EVAL_LLM_API_KEY")
        or os.getenv("LLM_API_KEY")
    )
    if not api_key:
        raise ValueError(
            "质检裁判 API key 未找到。请在 .env 设置 VERIFY_LLM_API_KEY"
            "（缺省可回退 EVAL_LLM_API_KEY / LLM_API_KEY）。"
        )
    api_base = (
        os.getenv("VERIFY_LLM_BASE_URL")
        or os.getenv("EVAL_LLM_BASE_URL")
        or os.getenv("LLM_BASE_URL")
        or ""
    )
    model = os.getenv("VERIFY_LLM_MODEL", DEFAULT_VERIFY_MODEL)
    return api_key, api_base, model


def month_log_root(base_root_path: str) -> str:
    leaf = os.path.basename(os.path.normpath(base_root_path))
    try:
        datetime.datetime.strptime(leaf, "%Y-%m")
        return base_root_path
    except ValueError:
        return os.path.join(base_root_path, datetime.date.today().strftime("%Y-%m"))


def build_token_log_path(api_key: str) -> str:
    """token 用量日志路径，目录来自 .env，不再写死机器路径与工号。"""
    base_root_path = (
        os.getenv("VERIFY_TOKEN_LOG_DIR")
        or os.getenv("EVAL_TOKEN_LOG_DIR")
        or os.getenv("TOKEN_LOG_DIR", "token_usage_log")
    )
    root_path = month_log_root(base_root_path)
    key_suffix = api_key[-8:] if api_key and len(api_key) >= 8 else "no-key"
    log_file_name = f"verify_token_usage_{key_suffix}_{str(datetime.date.today()).replace('-', '')}.jsonl"
    os.makedirs(root_path, exist_ok=True)
    return os.path.join(root_path, log_file_name)


def init_runtime():
    """按 .env 解析出的配置初始化全局 OpenAI 客户端 / 模型 / token 日志路径。"""
    global _CLIENT, _MODEL, _TOKEN_LOG_PATH
    api_key, api_base, model = get_verify_config()
    httpx_client = httpx.Client(verify=False)
    _CLIENT = OpenAI(base_url=api_base or None, api_key=api_key, http_client=httpx_client)
    _MODEL = model
    _TOKEN_LOG_PATH = build_token_log_path(api_key)
    return api_key, api_base, model

# with open(r"D:\PythonLearing\mcp-atlas\revise_mark\error_classification_prompt.md","r",encoding="utf-8") as f:
#     model_prompt = f.read()

def get_data(input_dir):
    # with open(input_dir, "r", encoding="utf-8") as f:
    #     for line in f:
    #         yield json.loads(line)
    df = pd.read_csv(input_dir, encoding='utf-8')
    dict_list = df.to_dict(orient='records')

    return dict_list

def get_tokens_num(text):
    # 1. 选择对应模型的编码器
    encoder = tiktoken.get_encoding("cl100k_base")
    # 2. 将文本编码为Token ID列表
    if not isinstance(text, str):
        text = str(text)
    tokens = encoder.encode(text)

    # 3. 计算Token数量
    token_count = len(tokens)

    return token_count

def get_completion(messages: List[dict]):
    response = _CLIENT.chat.completions.create(model=_MODEL, messages=messages)

    token_usage = {
        "model": _MODEL,
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }
    with open(_TOKEN_LOG_PATH, 'a+', encoding="utf-8") as log_out:
        log_out.write(json.dumps(token_usage, ensure_ascii=False) + "\n")
    return response, response.choices[0].message.content

def statical_func_error(raw_function,traj_function) -> dict:
    user_prompt = (judge_prompt.replace("[raw_conversation_history]", json.dumps(raw_function, ensure_ascii=False))
                   .replace("[trajectory_function]", json.dumps(traj_function, ensure_ascii=False)))

    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]

    response, answer = get_completion(messages)
    try:
        true = True
        false = False
        null = None
        answer_ = eval(answer)
        return answer_
    except JSONDecodeError as e:
        return answer
    except Exception as e:
        return answer

# def statical_model_error(data) -> dict:
#     messages = [{"role": "system", "content": model_prompt},
#                 {"role": "user", "content": json.dumps(data)}]

#     if get_tokens_num(json.dumps(messages)) > 922000:
#         return {
#             "error":"",
#             "reason":"数据超长"
#         }
#     response, answer = get_completion(messages)
#     try:
#         true = True
#         false = False
#         null = None
#         answer_ = eval(answer)
#         return answer_
#     except JSONDecodeError as e:
#         return answer
#     except Exception as e:
#         return answer


def get_function_call(TRAJECTORY):
    func_call_list = []
    trajectory_list = json.loads(TRAJECTORY)
    for index, trajectory in enumerate(trajectory_list):
        if trajectory["role"] == "assistant" and trajectory["tool_calls"]:
            for in1, item in enumerate(trajectory["tool_calls"]):
                func_call_list.append({
                    "name": item["function"]["name"],
                    "arguments": item["function"]["arguments"],
                    "result": trajectory_list[index + in1 + 1]["content"],
                })

    return func_call_list

from typing import List, Optional

def find_index_of_max_above_threshold(lst: List[float], threshold: float = 0.4) -> Optional[int]:
    """
    返回列表中大于 threshold 的最大值所对应的索引。

    参数:
        lst: 元素为 [0, 1] 之间浮点数的列表。
        threshold: 阈值，默认为 0.4。

    返回:
        符合条件的元素索引（int）；如果没有满足条件的元素，返回 None。
    """
    max_val = float('-inf')
    max_idx = None

    for idx, val in enumerate(lst):
        if val > threshold and val > max_val:
            max_val = val
            max_idx = idx

    return max_idx

def handle_function_result(function):
    if not isinstance(function["result"], str):
        function["result"] = json.dumps(function["result"], ensure_ascii=False)
    if len(function["result"]) > 300:
        function["result"] = "做超长处理，返回内容非空"

    return function

def get_raw_traj_function(trajectory, raw_conversation_function,threshold=0.4):
    # 语义去重
    reversed_function = list(reversed(raw_conversation_function))
    new_raw_function = [reversed_function[0]]
    new_raw_function_name = [item["name"] for item in new_raw_function]
    for item in reversed_function:
        # 函数名称不在，直接加
        if item["name"] not in new_raw_function_name:
            new_raw_function.append(item)
            new_raw_function_name.append(item["name"])
        else: #在，则判断，相似则跳过，不相似添加
            same_name_function = [function for function in new_raw_function if function["name"] == item["name"]]
            no_exist_function = True  # 默认不相似
            for function in same_name_function:
                item_arguments = json.dumps(item["arguments"], ensure_ascii=False)
                raw_arguments = json.dumps(function["arguments"],ensure_ascii=False)
                # 判断参数是否相似 阈值：0.4
                result = semantic_similarity(item_arguments, raw_arguments,threshold)
                if result["is_related"]: #True:相似  False:不想似
                    no_exist_function = False # 一个匹配成功
            if no_exist_function:
                new_raw_function.append(item)

    # 判断去重后函数是否在trajectory中
    exist_function = []
    for function in new_raw_function:
        same_name_trajectory_function = [item for item in trajectory if item["name"] == function["name"]]
        related_function = []
        for item in same_name_trajectory_function:
            item_arguments = json.dumps(item["arguments"], ensure_ascii=False)
            function_arguments = json.dumps(function["arguments"], ensure_ascii=False)
            # 判断参数是否相似 阈值：0.4
            result = semantic_similarity(item_arguments, function_arguments,threshold)
            related_function.append(result["similarity"])

        max_index = find_index_of_max_above_threshold(related_function,threshold)
        if max_index is not None:
            max_same_function = same_name_trajectory_function[max_index]
            exist_function.append({
                "trajectory_function": handle_function_result(max_same_function),
                "raw_function": handle_function_result(function),
            })

    return exist_function

def judge_function(data):
    model_check_data = copy.deepcopy(data)
    model_check_data["check_result"] = []
    model_check_data["error"] = "其他错误"
    model_check_data["TRAJECTORY"] = json.loads(data["TRAJECTORY"])
    if isinstance(data["raw_conversation_history"], float):
        model_check_data["raw_conversation_history"] = data["raw_conversation_history"]
    else:
        model_check_data["raw_conversation_history"] = json.loads(data["raw_conversation_history"])

    data_is_error = False
    # 1.获取TRAJ 与 raw 里工具调用对应关系
    trajectory = get_function_call(data["TRAJECTORY"])
    if  isinstance(data["raw_conversation_history"], float):
        print("没有真实对话")
        model_check_data["check_result"].append("格式错误:结果为空")
        model_check_data["error"]="格式错误"
        return data_is_error,model_check_data

    raw_conversation_function = get_function_call(data["raw_conversation_history"])
    raw_conversation_history = json.loads(data["raw_conversation_history"])
    if not raw_conversation_function: # True ：过滤
        if "<|tool_call_start|>" in raw_conversation_history[0]["content"] and "<|tool_call_end|>" in raw_conversation_history[0]["content"]:
            print("格式错误:工具调用解析错误")
            model_check_data["error"] = "格式错误"  # todo区分
            model_check_data["check_result"].append("格式错误:工具调用解析错误")
            return data_is_error, model_check_data
        elif "<function_calls>" in raw_conversation_history[0]["content"] and "</function_calls>" in raw_conversation_history[0]["content"]:
            print("格式错误:使用错误的工具调用special token")
            model_check_data["error"] = "格式错误" #todo区分
            model_check_data["check_result"].append("格式错误:使用错误的工具调用special token")
            return data_is_error,model_check_data
        else:
            print("assistant未调用任何工具")
            model_check_data["check_result"].append("模型错误:assistant未调用任何工具")
            model_check_data["error"] = "模型错误"
            return data_is_error,model_check_data
    threshold = 0.4
    # 2.去重并获取存在于trajectory中的工具调用
    exist_function = get_raw_traj_function(trajectory, raw_conversation_function,threshold)
    if not exist_function:
        print("没有调用TRAJECTOR中工具")
        model_check_data["error"] = "模型错误"
        model_check_data["check_result"].append("模型错误:没有调用正确工具")
        return data_is_error,model_check_data

    # 3.判断
    for item in exist_function:
        raw_function = item["raw_function"]
        traj_function = item["trajectory_function"]
        error_detail = statical_func_error(raw_function,traj_function)
        model_check_data["check_result"].append(error_detail)
        if isinstance(error_detail, str):
            continue
        if error_detail["error"] not in ["","无错误"]:
            print(json.dumps(raw_function, ensure_ascii=False) + "\n" +
                  json.dumps(traj_function,ensure_ascii=False) + "\n" +
                  json.dumps(error_detail, ensure_ascii=False) + "\n"+
                  "="*50)
            data_is_error = True
            model_check_data["error"] = "环境错误"
    if model_check_data["error"] == "环境错误":
        return data_is_error,model_check_data
    # 4.增加一个非环境错误的reason分析
    # model_result = statical_model_error(data)
    # model_check_data["error"] = model_result["error"]
    # model_check_data["check_result"].append(model_result["reason"])

    return data_is_error,model_check_data


def main():
    parser = argparse.ArgumentParser(
        description="对评分结果(scored_*.csv)做环境/模型错误归因，并给出剔除环境错误后的修正分数。"
    )
    parser.add_argument(
        "--input-file",
        default=os.getenv("VERIFY_INPUT_FILE"),
        help="评分结果 csv（mcp_evals_scores.py 产出的 scored_*.csv）。缺省取 .env 的 VERIFY_INPUT_FILE。",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出 jsonl 路径。缺省在输入同目录、同名加 _result.jsonl。",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("VERIFY_CONCURRENCY", "20")),
        help="并发线程数。缺省 .env 的 VERIFY_CONCURRENCY 或 20。",
    )
    parser.add_argument(
        "--pass-threshold",
        type=float,
        default=float(os.getenv("VERIFY_PASS_THRESHOLD", "0.75")),
        help="判定失败题的 coverage_score 阈值，低于此值才复核。默认 0.75。",
    )
    args = parser.parse_args()

    if not args.input_file:
        parser.error("必须提供 --input-file（或在 .env 设置 VERIFY_INPUT_FILE）")

    input_dir = args.input_file
    out_path = args.output or input_dir.replace(".csv", "_result.jsonl")
    max_workers = args.concurrency
    pass_threshold = args.pass_threshold

    # 初始化裁判模型客户端（从 .env 读取，全程不打印明文 key）
    api_key, api_base, model = init_runtime()
    if not api_key:
        key_disp = "MISSING"
    elif len(api_key) <= 8:
        key_disp = "set"
    else:
        key_disp = f"set ({api_key[:4]}...{api_key[-4:]})"  # 首4…尾4，中间脱敏
    print("===== Resolved config (实际生效) =====")
    print(f"  input_file      = {input_dir}")
    print(f"  output          = {out_path}")
    print(f"  verify_model    = {model}")
    print(f"  concurrency     = {max_workers}")
    print(f"  pass_threshold  = {pass_threshold}")
    print(f"  token_log_path  = {_TOKEN_LOG_PATH}")
    print(f"  verify_base_url = {api_base or '(SDK 默认/官方)'}")
    print(f"  verify_api_key  = {key_disp}")
    print("======================================")

    datas = get_data(input_dir)
    judge_score = 0  # 非环境错误的失败数（真模型失败）
    judge_data = []  # 失败数据
    error_type = 0   # 格式错误数据量

    for data in datas:
        if data["coverage_score"] < pass_threshold:
            judge_data.append(data)

    all_score = len(datas) - len(judge_data)  # 通过数
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor,\
            open(out_path, "w",encoding="utf-8") as out_file:
        futures = {executor.submit(judge_function, data): data for data in judge_data}
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="Judging"):
            judge_error,model_check_data = future.result() # True:有环境错误，不计入
            if model_check_data["error"] == "格式错误":
                error_type += 1
                continue
            if not judge_error:
                judge_score += 1
            out_file.write(json.dumps(model_check_data, ensure_ascii=False) + "\n")

    total = len(datas)

    def _pct(numer, denom):
        return f"{numer / denom * 100:.2f}" if denom else "N/A (分母为0)"

    print("原始情况")
    print("="*20)
    print(f"总数据量：{total}")
    print(f"总分数：{all_score}")
    print(f"分数：{_pct(all_score, total)}")
    print(f"失败数据量：",len(judge_data))
    print("="*20)
    print(f"工具(环境)错误数据量：{len(judge_data) - judge_score - error_type}")
    print(f"格式错误数量：",error_type)
    print("="*20)
    print("修正后，不过滤格式错误")
    print(f"失败数据量：",judge_score + error_type)
    print(f"总数据量：",all_score + judge_score + error_type)
    print(f"分数{_pct(all_score, all_score + judge_score + error_type)}")
    print("="*20)
    print("修正后，过滤格式错误")
    print(f"失败数据量：",judge_score)
    print(f"总数据量：",all_score + judge_score)
    print(f"分数{_pct(all_score, all_score + judge_score)}")
    print("="*20)


if __name__ == "__main__":
    main()