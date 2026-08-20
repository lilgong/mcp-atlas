# mcp_eval_scores.py
#
# Description:
# This script processes, evaluates, and analyzes model performance based on ground truth data.
# Uses LiteLLM for all providers (Gemini, OpenAI, Claude, etc.) - unified interface.
#
# Example Usage from command line:
#
# uv run mcp_evals_scores.py \
#   --input-file="completion_results/sample_4o_results.csv" \
#   --model-label="gpt4o" \
#   --evaluator-model="gemini/gemini-2.5-pro" \  # optional
#   --num-tasks=10  # optional

import pandas as pd
import numpy as np
import asyncio
import os
import json
import ast
import hashlib
import logging
import argparse
from typing import List, Dict, Any, Optional, Tuple, Set
from dataclasses import dataclass
from enum import Enum
# from datetime import datetime
import datetime
from collections import defaultdict, Counter
from abc import ABC, abstractmethod

# Third-party libraries
import litellm
from tenacity import (
    retry,
    retry_if_not_exception_type,
    wait_random_exponential,
    stop_after_attempt,
)
from tqdm.asyncio import tqdm as async_tqdm
from tqdm import tqdm
import matplotlib.pyplot as plt
import nest_asyncio
from dotenv import load_dotenv
from mcp_completion.account_guard import (
    FatalAccountError,
    describe_fatal_account_error,
    is_fatal_account_error,
)
from mcp_completion.streaming import (
    collect_litellm_response,
    llm_streaming_enabled,
)

# Load environment variables from .env file
load_dotenv()

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()

# Configure LiteLLM - suppress verbose logging
litellm.set_verbose = False
litellm.ssl_verify = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""
os.environ["NO_PROXY"] = "*"

# Per-read idle timeout for a streamed claim judgement. litellm's default is
# 6000s, which turns one upstream stall into an hour of held concurrency. A
# healthy long response can now exceed this wall time as long as chunks keep
# arriving; 120 seconds with no next chunk still counts as an upstream stall.
EVAL_REQUEST_TIMEOUT = float(os.getenv("EVAL_REQUEST_TIMEOUT", "120"))

# =========================================================================
# 1. CONFIGURATION AND SETUP
# =========================================================================


@dataclass
class EvaluatorConfig:
    """Configuration for the evaluator and analyzer."""

    evaluator_model: str
    semaphore_limit: int
    request_delay: float = 0.2
    verbose: bool = True
    save_partial_on_error: bool = True
    strict_evaluation: bool = True
    num_tasks: Optional[int] = None


def setup_logging(verbose: bool = True):
    """Set up the logging configuration."""
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)


def get_litellm_config():
    """Get LiteLLM configuration from environment variables."""
    api_key = os.getenv("EVAL_LLM_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        raise ValueError(
            "LiteLLM API key not found. Set EVAL_LLM_API_KEY or LLM_API_KEY env var."
        )

    api_base = os.getenv("EVAL_LLM_BASE_URL", "")
    return api_key, api_base

def month_log_root(base_root_path: str) -> str:
    leaf = os.path.basename(os.path.normpath(base_root_path))
    try:
        datetime.datetime.strptime(leaf, "%Y-%m")
        return base_root_path
    except ValueError:
        return os.path.join(base_root_path, datetime.date.today().strftime("%Y-%m"))


def build_token_log_path(api_key: str, env_name: str = "EVAL_TOKEN_LOG_DIR") -> str:
    base_root_path = os.getenv(env_name) or os.getenv("TOKEN_LOG_DIR", "token_usage_log")
    root_path = month_log_root(base_root_path)
    key_suffix = api_key[-8:] if api_key else "no-key"
    log_file_name = f"eval_token_usage_{key_suffix}_{str(datetime.date.today()).replace('-', '')}.jsonl"
    os.makedirs(root_path, exist_ok=True)
    return os.path.join(root_path, log_file_name)


TOKEN_LOG_PATH = build_token_log_path(get_litellm_config()[0])

# =========================================================================
# 2. CORE EVALUATION FRAMEWORK (SCORING) - GEMINI VERSION
# =========================================================================

import json
import ast
import re
from typing import List, Union


RESULT_COLUMNS = (
    "coverage_score",
    "fully_covered_claims",
    "partially_covered_claims",
    "total_claims",
    "coverage_details_json",
    "evaluation_confidence",
)

# Bump this whenever the judge prompt, output contract, or score aggregation
# semantics change.  Resume is safe only when both the judged input and this
# policy identity are unchanged.
SCORING_POLICY_VERSION = "coverage-judge-v2"
SCORING_FINGERPRINT_COLUMN = "scoring_fingerprint"
SCORING_MODEL_COLUMN = "scoring_evaluator_model"
SCORING_POLICY_COLUMN = "scoring_policy_version"


def response_for_scoring(row: Any) -> Any:
    """Return the completion text the judge is shown.

    Resume decides whether an old score still applies by comparing what that
    score was computed from, so the fingerprint and the evaluator have to read
    the same column. Selecting it in one place keeps them from drifting apart.
    """
    for column in ("script_model_response", "response"):
        if column in row and pd.notna(row[column]):
            return row.get(column, "")
    return ""


def terminal_failure_counts(df: pd.DataFrame) -> Counter:
    """Count framework terminal-result rows by their explicit failure kind."""
    counts: Counter = Counter()
    for _, row in df.iterrows():
        response = response_for_scoring(row)
        if not isinstance(response, str) or not response.startswith("ERROR ["):
            continue
        closing = response.find("]", len("ERROR ["))
        kind = response[len("ERROR [") : closing] if closing != -1 else "unknown"
        counts[kind or "unknown"] += 1
    return counts


def scored_input_fingerprint(row: Any, evaluator_model: str) -> str:
    """Identify what a coverage score was computed from.

    Only the claims and the response reach the judge, so a row whose completion
    was re-run produces a different fingerprint and is scored again, while an
    untouched row can keep its score.
    """
    payload = {
        "policy_version": SCORING_POLICY_VERSION,
        "evaluator_model": str(evaluator_model),
        "claims": str(row.get("GTFA_CLAIMS", "")),
        "response": str(response_for_scoring(row)),
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_reusable_scores(
    scored_path: str,
    df_input: pd.DataFrame,
    logger: logging.Logger,
    evaluator_model: str,
) -> Dict[Any, Dict[str, Any]]:
    """Map input row index to the previous result columns worth reusing.

    Rows left unscored last time are judge/infrastructure failures rather than
    results, so they are retried instead of being carried over.
    """
    try:
        previous = pd.read_csv(scored_path)
    except Exception as error:
        logger.warning(f"Could not read '{scored_path}' for resume: {error}")
        return {}

    absent = [
        c for c in (
            "TASK", *RESULT_COLUMNS, SCORING_FINGERPRINT_COLUMN,
            SCORING_MODEL_COLUMN, SCORING_POLICY_COLUMN,
        )
        if c not in previous.columns
    ]
    if absent:
        logger.warning(
            f"Ignoring '{scored_path}' for resume; missing columns: {absent}"
        )
        return {}

    by_task: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for _, previous_row in previous.iterrows():
        if pd.isna(previous_row.get("coverage_score")):
            continue
        by_task[str(previous_row.get("TASK"))] = (
            str(previous_row[SCORING_FINGERPRINT_COLUMN]),
            {column: previous_row[column] for column in RESULT_COLUMNS},
        )

    reusable: Dict[Any, Dict[str, Any]] = {}
    restaled = 0
    for index, row in df_input.iterrows():
        entry = by_task.get(str(row.get("TASK")))
        if entry is None:
            continue
        fingerprint, result = entry
        if fingerprint != scored_input_fingerprint(row, evaluator_model):
            restaled += 1
            continue
        reusable[index] = result

    if restaled:
        logger.info(
            f"{restaled} tasks changed since the last scoring run; rescoring them"
        )
    return reusable


def extract_claims(claim_blob: Union[str, List, None]) -> List[str]:
    """
    Extracts and cleans individual claims from various input formats.

    Args:
        claim_blob: Can be:
            - A list of strings (direct claims)
            - A list of dicts with 'claim' key (e.g., [{"claim": "text", "essential": "yes"}])
            - A JSON string representing a list
            - A multi-line text with various separators
            - None or empty input

    Returns:
        A list of cleaned claim strings
    """

    # Handle None or empty inputs
    if claim_blob is None:
        return []

    # If it's already a list, process based on content type
    if isinstance(claim_blob, list):
        cleaned_claims = []
        for item in claim_blob:
            # Handle object format: {"claim": "text", "essential": "yes"}
            if isinstance(item, dict) and "claim" in item:
                claim_text = item["claim"]
                cleaned = clean_claim_text(str(claim_text))
                if cleaned and len(cleaned) > 3:
                    cleaned_claims.append(cleaned)
            # Handle string format
            else:
                cleaned = clean_claim_text(str(item))
                if cleaned and len(cleaned) > 3:
                    cleaned_claims.append(cleaned)
        return cleaned_claims

    # Convert to string if not already
    if not isinstance(claim_blob, str):
        claim_blob = str(claim_blob)

    # Remove any leading/trailing whitespace
    claim_blob = claim_blob.strip()

    # Return empty list for empty strings
    if not claim_blob:
        return []

    # Try to parse as JSON/Python list first
    # This handles cases like: '["claim1", "claim2"]' or '[{"claim": "text", "essential": "yes"}]'
    if claim_blob.startswith("[") and claim_blob.endswith("]"):
        try:
            parsed_list = json.loads(claim_blob)
            if isinstance(parsed_list, list):
                # Clean and filter the parsed claims
                cleaned_claims = []
                for item in parsed_list:
                    # Handle object format: {"claim": "text", "essential": "yes"}
                    if isinstance(item, dict) and "claim" in item:
                        claim_text = item["claim"]
                        cleaned = clean_claim_text(str(claim_text))
                        if cleaned and len(cleaned) > 3:
                            cleaned_claims.append(cleaned)
                    # Handle string format
                    else:
                        cleaned = clean_claim_text(str(item))
                        if cleaned and len(cleaned) > 3:
                            cleaned_claims.append(cleaned)
                return cleaned_claims
        except (json.JSONDecodeError, ValueError) as json_error:
            # If JSON parsing fails, try ast.literal_eval as fallback
            # ast.literal_eval is more forgiving with Python string literals
            try:
                parsed_list = ast.literal_eval(claim_blob)
                if isinstance(parsed_list, list):
                    cleaned_claims = []
                    for item in parsed_list:
                        # Handle object format: {"claim": "text", "essential": "yes"}
                        if isinstance(item, dict) and "claim" in item:
                            claim_text = item["claim"]
                            cleaned = clean_claim_text(str(claim_text))
                            if cleaned and len(cleaned) > 3:
                                cleaned_claims.append(cleaned)
                        # Handle string format
                        else:
                            cleaned = clean_claim_text(str(item))
                            if cleaned and len(cleaned) > 3:
                                cleaned_claims.append(cleaned)
                    return cleaned_claims
            except (ValueError, SyntaxError):
                # If both parsing methods fail, log the issue and continue to text-splitting logic
                # This might happen with malformed JSON like '["text "inner" text"]'
                # where CSV double-quotes were converted incorrectly
                import logging

                logging.debug(
                    f"Failed to parse claim_blob as JSON or Python literal: {claim_blob[:100]}"
                )
                pass

    # Try to detect numbered list pattern (1. 2. 3. etc.) using regex
    # This handles patterns like "1. claim\n2. claim\n3. claim"
    numbered_pattern = r"(?:^|\n)(\d+)\.\s+"
    if re.search(numbered_pattern, claim_blob):
        # Split by numbered pattern
        parts = re.split(numbered_pattern, claim_blob)

        # parts will be like: ['', '1', 'claim text', '2', 'claim text', ...]
        # We need to pair up the numbers with their text
        claims = []
        i = 1
        while i < len(parts):
            # Skip the number itself, take the text
            if i + 1 < len(parts):
                claim_text = parts[i + 1].strip()
                if claim_text and len(claim_text) > 3:
                    # Clean up the claim text
                    claim_text = claim_text.rstrip("\n").strip()
                    claims.append(claim_text)
                i += 2
            else:
                break

        if claims:
            return claims

    # Fallback to original text-splitting logic for backward compatibility
    # This handles plain text with various separators
    separators = ["\n•", "\n-", "\n*", ";", "||"]
    for sep in separators:
        if sep in claim_blob:
            parts = claim_blob.split(sep)
            claims = []
            for p in parts:
                cleaned = clean_claim_text(p)
                if cleaned and len(cleaned) > 3:
                    claims.append(cleaned)
            if claims:
                return claims

    # Try splitting by newlines as last resort
    lines = claim_blob.strip().split("\n")
    claims = []
    for line in lines:
        cleaned = clean_claim_text(line)
        if cleaned and len(cleaned) > 3:
            claims.append(cleaned)
    return claims


def clean_claim_text(text: str) -> str:
    """
    Cleans individual claim text by removing unwanted characters and formatting.

    Args:
        text: Raw claim text

    Returns:
        Cleaned claim text
    """
    # Strip whitespace
    text = text.strip()

    # Remove common bullet point markers and numbering from the start
    text = re.sub(r"^[-*•·◦‣⁃]\s*", "", text)  # Bullet points
    text = re.sub(r"^\d+[.)]\s*", "", text)  # Numbered lists

    # Replace Unicode quotes with standard quotes
    text = text.replace("\u201c", '"')  # Left double quote
    text = re.sub(r'[\u201d"]', '"', text)  # Right double quote
    text = text.replace("\u2018", "'")  # Left single quote
    text = text.replace("\u2019", "'")  # Right single quote

    # Remove other problematic Unicode characters
    text = text.replace("\u2013", "-")  # En dash
    text = text.replace("\u2014", "-")  # Em dash
    text = text.replace("\u2026", "...")  # Ellipsis

    # Clean up any trailing punctuation issues (like ." or .")
    text = re.sub(r'[.\s]*["\']+$', "", text)  # Remove trailing quotes with dots
    text = re.sub(r'["\']+\.*$', "", text)  # Remove trailing quotes and dots

    # Final strip of whitespace and basic punctuation
    text = text.strip(" \t\n\r")

    return text


# Define Gemini schemas - Modified for single claim evaluation
def get_single_claim_evaluation_schema():
    """Define the response schema for single claim evaluation"""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claim_text": {"type": "string"},
            "coverage_outcome": {
                "type": "string",
                "enum": ["fulfilled", "partially_fulfilled", "not_fulfilled"],
            },
            "justification": {"type": "string"},
            "confidence_level": {"type": "number"},
        },
        "required": [
            "claim_text",
            "coverage_outcome",
            "justification",
            "confidence_level",
        ],
    }


# =========================================================================
# 3. LITELLM CLIENT (Unified Interface for All Providers)
# =========================================================================


class AsyncLLMClient(ABC):
    """Abstract base class for LLM clients"""

    @abstractmethod
    async def generate_structured_content(
        self,
        prompt: str,
        response_schema: Dict,
        temperature: float = 0.0,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Generate structured content with retry logic."""
        pass

    @abstractmethod
    def get_stats(self) -> Dict[str, int]:
        """Get request statistics"""
        pass


class AsyncLiteLLMClient(AsyncLLMClient):
    """Manages async LiteLLM requests with rate limiting - supports all providers via LiteLLM"""

    def __init__(self, config: EvaluatorConfig):
        self.config = config
        self.semaphore = asyncio.Semaphore(config.semaphore_limit)
        self.logger = logging.getLogger(__name__)
        self.request_count = 0
        self.error_count = 0

        # Initialize LiteLLM configuration from environment
        api_key, api_base = get_litellm_config()
        litellm.api_key = api_key
        if api_base:
            litellm.api_base = api_base

    @retry(
        retry=retry_if_not_exception_type(FatalAccountError),
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    async def generate_structured_content(
        self,
        prompt: str,
        response_schema: Dict,
        temperature: float = 0.0,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Generate structured content using LiteLLM with retry logic."""
        context = context or {}
        task_id = context.get("task_id", "unknown")
        claim_index = context.get("claim_index", "unknown")
        claim_count = context.get("claim_count", "unknown")
        try:
            # The semaphore covers opening and fully consuming the HTTP stream,
            # but not the rate-limit sleep or local JSON parsing.
            async with self.semaphore:
                self.request_count += 1

                # Use the standard OpenAI-compatible strict structured-output
                # envelope. Putting ``response_schema`` next to
                # ``type=json_object`` is a Gemini-style extension and leaves
                # the schema unenforced on OpenAI-compatible gateways.
                use_streaming = llm_streaming_enabled()
                provider_response = await litellm.acompletion(
                    model=self.config.evaluator_model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "single_claim_evaluation",
                            "strict": True,
                            "schema": response_schema,
                        },
                    },
                    temperature=(
                        1
                        if self.config.evaluator_model.split("/", 1)[-1].startswith("gpt-5")
                        else temperature
                    ),  # gpt-5 models only support temperature=1,实测 yibu 的可以temp=0但是不起效，参数基本无用，且实验后 temp 误差在 1 分以内可接受
                    api_key=litellm.api_key,
                    api_base=(
                        litellm.api_base
                        if hasattr(litellm, "api_base") and litellm.api_base
                        else None
                    ),
                    # Judging one claim is a small, fast call. Without this it
                    # inherits litellm's 6000s default, so an upstream stall
                    # blocks a slot for over an hour.
                    timeout=EVAL_REQUEST_TIMEOUT,
                    **(
                        {
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        }
                        if use_streaming
                        else {}
                    ),
                    # The OpenAI SDK retries twice on its own, which multiplies
                    # with the tenacity retry above (6 x 3 = 18 requests). Keep
                    # retries in one place so the total is predictable.
                    max_retries=0,
                )
                # Keep the semaphore until the final chunk. Releasing it after
                # only opening the HTTP stream would silently bypass the judge
                # concurrency limit while the network bodies were consumed.
                response = (
                    await collect_litellm_response(
                        provider_response,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    if use_streaming
                    else provider_response
                )

            # Rate limiting delay
            await asyncio.sleep(self.config.request_delay)

            # Parse JSON response. Keep diagnostics metadata-only: no prompt, claim, response, or key.
            choice = response.choices[0] if response.choices else None
            message = getattr(choice, "message", None) if choice else None
            content = getattr(message, "content", None) if message else None
            finish_reason = getattr(choice, "finish_reason", None) if choice else None
            content_len = len(content) if isinstance(content, str) else 0
            try:
                parsed_content = json.loads(content or "")
            except json.JSONDecodeError as e:
                self.logger.error(
                    "LiteLLM JSON parse failed: "
                    f"task_id={task_id} claim={claim_index}/{claim_count} "
                    f"model={self.config.evaluator_model} content_len={content_len} "
                    f"finish_reason={finish_reason} error={e}"
                )
                raise

            # 统计token使用
            token_usage = {
                "model": self.config.evaluator_model,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "prompt": prompt,
                "answer": content,
            }
            os.makedirs(os.path.dirname(TOKEN_LOG_PATH) or ".", exist_ok=True)
            with open(TOKEN_LOG_PATH, 'a+', encoding="utf-8") as log_out:
                log_out.write(json.dumps(token_usage, ensure_ascii=False) + "\n")

            return parsed_content

        except json.JSONDecodeError:
            self.error_count += 1
            raise
        except Exception as e:
            self.error_count += 1
            self.logger.error(
                "LiteLLM request failed: "
                f"task_id={task_id} claim={claim_index}/{claim_count} "
                f"model={self.config.evaluator_model} "
                f"error_type={type(e).__name__} error={e}"
            )
            if is_fatal_account_error(e):
                credential_env = (
                    "EVAL_LLM_API_KEY"
                    if os.getenv("EVAL_LLM_API_KEY")
                    else "LLM_API_KEY"
                )
                raise FatalAccountError(
                    "evaluator credential is invalid or out of funds",
                    source_kind="evaluator_model",
                    source_name=self.config.evaluator_model,
                    credential_envs=(credential_env,),
                ) from e
            raise

    def get_stats(self) -> Dict[str, int]:
        """Get request statistics"""
        return {
            "total_requests": self.request_count,
            "errors": self.error_count,
            "success_rate": (self.request_count - self.error_count)
            / max(self.request_count, 1),
        }


# =========================================================================
# 4. COVERAGE EVALUATOR
# =========================================================================


class CoverageEvaluator:
    """Evaluates claim coverage with continuous scoring (0-1) - one claim at a time."""

    def __init__(self, client: AsyncLLMClient, config: EvaluatorConfig):
        self.client = client
        self.config = config
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def _coerce_confidence(value: Any, default: float = 0.5) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _get_single_claim_evaluation_prompt(self, claim: str, response: str) -> str:
        """Generate prompt for evaluating a single claim"""
        output_format = """{
"claim_text": "str type",
"confidence_level": "float type",
"coverage_outcome": "should be one of ['fulfilled', 'partially_fulfilled', 'not_fulfilled']",
"justification": "str type"
}"""

        return f"""You are evaluating how well a model's response addresses a specific expert-defined claim.
SCORING CRITERIA:
- fulfilled: Claim is completely and accurately addressed. The response covers all key details.
- partially_fulfilled: Claim is partially addressed. The response covers some but not all key details.
- not_fulfilled: Claim is not addressed. The response does not include any key details.
NUMERICAL COMPARISON GUIDELINES:
- For numerical values, use reasonable approximation thresholds:
  * Exact match NOT required for decimals
  * Values within 5% of the claimed number are considered matching
  * For percentages, ±1 percentage points is acceptable
  * Round to appropriate significant figures based on context
- Consider the precision appropriate to the domain:
  * Scientific measurements may need higher precision
  * General statistics/estimates can have looser matching
  * Financial figures should match to reasonable business precision (e.g., millions/billions don't need exact cents)
- If a number is expressed differently but mathematically equivalent (e.g., "0.5" vs "50%" vs "half"), consider it a match
CLAIM TO EVALUATE:
{claim}
MODEL RESPONSE TO ANALYZE:
{response}
INSTRUCTIONS:
1. Determine if the core requirement of the claim is met in the response
2. Check if all key components from the claim appear substantively in the response
   - For numerical values, apply the flexible matching guidelines above
   - Focus on whether the same magnitude and meaning are conveyed
3. Assign the appropriate coverage_outcome
4. Provide specific justification referencing what was/wasn't covered
   - When numbers differ slightly, note if they're within acceptable range
5. Provide a confidence level (0.0-1.0) for your assessment
Be rigorous but fair in your assessment. Focus on whether the response conveys the same information as the claim, not on exact numerical precision unless precision is critical to the claim's meaning.
FINAL OUTPUT FORMAT
- Output only one JSON
- Must be parseable by json.loads()
- Must not contain any explanations/comments/extra text
- Final output must follow this format:
{output_format}

IMPORTANT: DO NOT wrap JSON in ```json or ``` markdown blocks.
ONLY output RAW JSON string directly. No any other text. No formatting. No code blocks.
"""

    async def evaluate_single_claim(
        self,
        claim: str,
        response: str,
        task_id: str = "unknown",
        claim_index: int = 0,
        claim_count: int = 0,
    ) -> Dict[str, Any]:
        """Evaluate a single claim against the response"""
        prompt = self._get_single_claim_evaluation_prompt(claim, response)

        try:
            result = await self.client.generate_structured_content(
                prompt=prompt,
                response_schema=get_single_claim_evaluation_schema(),
                temperature=0.0,
                context={
                    "task_id": task_id,
                    "claim_index": claim_index,
                    "claim_count": claim_count,
                },
            )
            return result
        except FatalAccountError:
            raise
        except Exception as e:
            self.logger.warning(
                f"Single claim evaluation failed: task_id={task_id} "
                f"claim={claim_index}/{claim_count} error={e}"
            )
            # An evaluator/infrastructure failure is not evidence that the
            # model failed the claim. A task score is only comparable when all
            # of its claims were judged, so let the row-level guard mark the
            # entire row unscored (coverage_score=None). Downstream statistics
            # exclude such rows instead of silently converting the failure
            # into a zero or averaging only a subset of claims.
            raise

    async def evaluate(
        self, claims: List[str], response: str, task_id: str = "unknown"
    ) -> Dict[str, Any]:
        """Evaluate all claims by making individual API calls for each claim"""
        if not claims:
            return {
                "per_claim": [],
                "coverage_score": None,
                "explanation": "No claims provided",
                "confidence": 1.0,
            }

        # The completion runner emits this prefix only after a task has
        # exhausted its retries. It is a real terminal model/task outcome, not
        # text that needs an LLM judge. Score it deterministically as zero and
        # avoid paying for claim-evaluator calls that cannot change the result.
        if isinstance(response, str) and response.startswith("ERROR ["):
            reason = response[:1000]
            return {
                "per_claim": [
                    {
                        "claim": claim,
                        "score": 0.0,
                        "covered": False,
                        "reason": reason,
                    }
                    for claim in claims
                ],
                "coverage_score": 0.0,
                "total_claims": len(claims),
                "fully_covered_claims": 0,
                "partially_covered_claims": 0,
                "explanation": "Completion ended with a terminal task failure",
                "confidence": 1.0,
            }

        # Define coverage outcome to score mapping
        coverage_to_score = {
            "fulfilled": 1.0,
            "partially_fulfilled": 0.5,
            "not_fulfilled": 0.0,
        }

        # Evaluate each claim individually
        claim_count = len(claims)
        tasks = [
            self.evaluate_single_claim(claim, response, task_id, i, claim_count)
            for i, claim in enumerate(claims, start=1)
        ]
        claim_results = await asyncio.gather(*tasks)

        # Aggregate results
        per_claim = []
        total_score = 0
        fulfilled_count = 0
        partially_fulfilled_count = 0
        total_confidence = 0

        for result in claim_results:
            coverage_outcome = result.get("coverage_outcome", "not_fulfilled")
            score = coverage_to_score.get(coverage_outcome, 0.0)
            total_score += score
            total_confidence += self._coerce_confidence(
                result.get("confidence_level", 0.5)
            )

            if score >= 1.0:
                fulfilled_count += 1
                covered = True
            elif score >= 0.5:
                partially_fulfilled_count += 1
                covered = "partial"
            else:
                covered = False

            per_claim.append(
                {
                    "claim": result.get("claim_text", ""),
                    "score": score,
                    "covered": covered,
                    "reason": result.get("justification", ""),
                }
            )

        coverage_score = round(total_score / len(claims), 3) if claims else 0.0
        avg_confidence = total_confidence / len(claims) if claims else 0.5

        return {
            "per_claim": per_claim,
            "coverage_score": coverage_score,
            "total_claims": len(claims),
            "fully_covered_claims": fulfilled_count,
            "partially_covered_claims": partially_fulfilled_count,
            "explanation": "Evaluation complete",
            "confidence": avg_confidence,
        }

async def evaluate_dataframe_async(
    df: pd.DataFrame, evaluator: CoverageEvaluator
) -> pd.DataFrame:
    """Asynchronously evaluates all rows in a dataframe."""
    logger = logging.getLogger(__name__)

    async def safe_evaluate(row_idx, row):
        try:
            claims = extract_claims(row.get("GTFA_CLAIMS", ""))
            response = response_for_scoring(row)
            task_id = str(row.get("TASK", row_idx))
            result = await evaluator.evaluate(claims, response, task_id=task_id)
            return row_idx, result
        except FatalAccountError:
            raise
        except Exception as e:
            logger.error(f"Error processing row {row_idx}: {e}")
            return row_idx, {"coverage_score": None, "explanation": f"Failed: {e}"}

    tasks = [
        asyncio.create_task(safe_evaluate(idx, row))
        for idx, row in df.iterrows()
    ]
    results_list = []
    try:
        for future in tqdm(
            asyncio.as_completed(tasks), total=len(tasks), desc="Scoring Rows"
        ):
            results_list.append(await future)
    except FatalAccountError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    results_dict = {idx: res for idx, res in results_list}

    out_df = df.copy()
    result_cols = {
        "coverage_score": [],
        "fully_covered_claims": [],
        "partially_covered_claims": [],
        "total_claims": [],
        "coverage_details_json": [],
        "evaluation_confidence": [],
    }

    for idx in df.index:
        result = results_dict.get(idx, {})
        result_cols["coverage_score"].append(result.get("coverage_score"))
        result_cols["fully_covered_claims"].append(
            result.get("fully_covered_claims", 0)
        )
        result_cols["partially_covered_claims"].append(
            result.get("partially_covered_claims", 0)
        )
        result_cols["total_claims"].append(result.get("total_claims", 0))
        result_cols["coverage_details_json"].append(json.dumps(result))
        result_cols["evaluation_confidence"].append(result.get("confidence", 0.0))

    for col, data in result_cols.items():
        out_df[col] = data

    return out_df


# =========================================================================
# 3. STATISTICAL ANALYSIS AND PLOTTING
# =========================================================================


def generate_statistics_and_plots(
    scored_csv_path: str,
    model_label: str,
    output_dir: str,
    pass_threshold: float = 0.75,
):
    """Generates a summary stats CSV and a histogram plot of coverage scores."""
    logger = logging.getLogger(__name__)
    logger.info(f"Step 4: Generating statistics and plots for '{scored_csv_path}'...")

    try:
        df = pd.read_csv(scored_csv_path)
        if "coverage_score" not in df.columns:
            raise KeyError("'coverage_score' column missing.")

        # --- Generate and save statistics ---
        stats_df = (
            df["coverage_score"]
            .describe()
            .to_frame(name="value")
            .reset_index()
            .rename(columns={"index": "stat"})
        )

        # Rename "mean" to "mean coverage score"
        stats_df.loc[stats_df["stat"] == "mean", "stat"] = "mean coverage score"

        # Calculate pass rate (% of tasks where coverage_score >= pass_threshold)
        valid_scores = df["coverage_score"].dropna()
        pass_count = (valid_scores >= pass_threshold).sum()
        total_count = len(valid_scores)
        pass_rate = pass_count / total_count if total_count > 0 else 0.0

        # Insert pass rate row right after "mean coverage score"
        mean_idx = stats_df[stats_df["stat"] == "mean coverage score"].index[0]
        pass_rate_row = pd.DataFrame({"stat": ["pass rate"], "value": [pass_rate]})
        stats_df = pd.concat(
            [
                stats_df.iloc[: mean_idx + 1],
                pass_rate_row,
                stats_df.iloc[mean_idx + 1 :],
            ]
        ).reset_index(drop=True)

        stats_path = os.path.join(output_dir, f"coverage_stats_{model_label}.csv")
        stats_df.to_csv(stats_path, index=False)
        logger.info(f"Saved summary statistics to '{stats_path}'")
        print("\nCoverage Score Summary:")
        print(stats_df)

        # --- Generate and save histogram plot ---
        scores = df["coverage_score"].dropna().to_numpy()

        # Only create plot if we have data
        if len(scores) > 0:
            fig, ax = plt.subplots(figsize=(8, 5))
            ax.hist(scores, bins=min(50, len(scores)), edgecolor="black", alpha=0.7)
            ax.set_title(f"Coverage Score Distribution ({model_label})")
            ax.set_xlabel("Coverage Score")
            ax.set_ylabel("Frequency")
            ax.axvline(
                scores.mean(),
                color="red",
                linestyle="--",
                label=f"Mean: {scores.mean():.3f}",
            )
            ax.legend()
            plt.tight_layout()

            plot_path = os.path.join(
                output_dir, f"coverage_histogram_{model_label}.png"
            )
            plt.savefig(plot_path)
            logger.info(f"Saved histogram plot to '{plot_path}'")
            plt.close(fig)
        else:
            logger.warning("No valid scores to plot")

    except FileNotFoundError:
        logger.error(f"Scored file not found at '{scored_csv_path}'")
        raise
    except Exception as e:
        logger.error(f"Failed to generate statistics and plots: {e}")
        raise


# =========================================================================
# 4. MAIN EXECUTION
# =========================================================================


async def main(args):
    """Main function to run the entire pipeline."""
    setup_logging()
    logger = logging.getLogger(__name__)

    # Log if running on limited tasks
    if args.num_tasks:
        logger.info(f"🔬 Running evaluation on first {args.num_tasks} tasks only")

    # Create output directory if it doesn't exist
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    # Define file path for scored results
    scored_path = os.path.join(output_dir, f"scored_{args.model_label}.csv")

    # ===== 打印实际生效的配置（密钥脱敏）=====
    _eval_key, _eval_base = get_litellm_config()
    if not _eval_key:
        _key_disp = "MISSING"
    elif len(_eval_key) <= 8:
        _key_disp = "set"
    else:
        _key_disp = f"set ({_eval_key[:4]}...{_eval_key[-4:]})"  # 首4…尾4，中间脱敏
    logger.info("===== Resolved config (实际生效) =====")
    logger.info(f"  evaluator_model = {args.evaluator_model}")
    logger.info(f"  input_file      = {args.input_file}")
    logger.info(f"  model_label     = {args.model_label}")
    logger.info(f"  output_dir      = {output_dir}")
    logger.info(f"  scored_output   = {scored_path}")
    logger.info(f"  concurrency     = {args.concurrency}")
    logger.info(f"  num_tasks       = {args.num_tasks if args.num_tasks else '全部'}")
    logger.info(f"  pass_threshold  = {args.pass_threshold}")
    logger.info(f"  eval_base_url   = {_eval_base or '(litellm 默认/官方)'}")
    logger.info(f"  eval_api_key    = {_key_disp}")
    logger.info(f"  llm_streaming   = {llm_streaming_enabled()}")
    logger.info("======================================")

    try:
        # --- Create Evaluator Configuration ---
        logger.info(f"Using evaluator model: {args.evaluator_model}")
        config = EvaluatorConfig(
            evaluator_model=args.evaluator_model,
            semaphore_limit=args.concurrency,
            strict_evaluation=True,
            num_tasks=args.num_tasks,
        )

        # --- Pipeline Execution ---
        # 1. Load input file (already contains both ground truth and completion data)
        logger.info(f"Loading input file: {args.input_file}")
        df_input = pd.read_csv(args.input_file)

        # Apply task limit if specified
        if args.num_tasks is not None and args.num_tasks > 0:
            original_size = len(df_input)
            df_input = df_input.head(args.num_tasks)
            logger.info(
                f"Limited dataset from {original_size} to {len(df_input)} tasks"
            )

        # Verify required columns exist
        required_cols = ["TASK", "PROMPT", "TRAJECTORY", "GTFA_CLAIMS"]
        missing_cols = [col for col in required_cols if col not in df_input.columns]
        if missing_cols:
            logger.error(
                f"Missing required columns in {args.input_file}: {missing_cols}"
            )
            raise KeyError(f"Missing required columns: {missing_cols}")

        logger.info(f"Successfully loaded {len(df_input)} tasks")

        # 2. Run scoring evaluation
        client = AsyncLiteLLMClient(config)
        evaluator = CoverageEvaluator(client, config)

        # Judging a claim costs tokens, so a rerun after one more completion
        # lands should only pay for that completion. Scores whose claims and
        # response are byte-identical to the previous run are carried over.
        reusable: Dict[Any, Dict[str, Any]] = {}
        if args.resume and os.path.exists(scored_path):
            reusable = load_reusable_scores(
                scored_path, df_input, logger, args.evaluator_model,
            )

        pending = df_input.drop(index=list(reusable))
        if reusable:
            logger.info(
                f"Resume: reusing {len(reusable)} scores from '{scored_path}', "
                f"scoring {len(pending)} tasks"
            )

        scored_parts = []
        if reusable:
            carried = df_input.loc[list(reusable)].copy()
            for column in RESULT_COLUMNS:
                carried[column] = [reusable[i][column] for i in carried.index]
            scored_parts.append(carried)
        if len(pending):
            scored_parts.append(await evaluate_dataframe_async(pending, evaluator))
        else:
            logger.info("Every task already holds a current score; nothing to rescore")

        # Reindexing on the input restores its row order, so a resumed run and a
        # full run produce the same file.
        df_scored = pd.concat(scored_parts).reindex(df_input.index)
        df_scored[SCORING_FINGERPRINT_COLUMN] = [
            scored_input_fingerprint(row, args.evaluator_model)
            for _, row in df_scored.iterrows()
        ]
        df_scored[SCORING_MODEL_COLUMN] = args.evaluator_model
        df_scored[SCORING_POLICY_COLUMN] = SCORING_POLICY_VERSION
        df_scored.to_csv(scored_path, index=False)

        logger.info(f"✅ Saved scored file to '{scored_path}'")
        terminal_counts = terminal_failure_counts(df_scored)
        if terminal_counts:
            details = ", ".join(
                f"{kind}={count}" for kind, count in sorted(terminal_counts.items())
            )
            logger.warning(
                "%d/%d terminal completion failures counted as zero: %s",
                sum(terminal_counts.values()),
                len(df_scored),
                details,
            )
        valid_scores = df_scored["coverage_score"].dropna()
        unscored_count = len(df_scored) - len(valid_scores)
        if unscored_count:
            logger.warning(
                f"{unscored_count}/{len(df_scored)} rows unscored; excluded "
                "from statistics. Inspect coverage_details_json for each reason."
            )
        if len(valid_scores) > 0:
            # A resumed run judges only what changed, so name the scope: the
            # average covers the whole dataset, not just this run's rescores.
            logger.info(
                f"Evaluation complete. Average coverage over all "
                f"{len(valid_scores)}/{len(df_scored)} scored tasks: "
                f"{valid_scores.mean():.3f} "
                f"(rescored {len(pending)} this run, reused {len(reusable)})"
            )
        else:
            logger.warning("Evaluation produced no valid coverage scores.")

        # 3. Generate statistics and plots
        generate_statistics_and_plots(
            scored_path, args.model_label, output_dir, args.pass_threshold
        )

        logger.info(f"\n🚀 Pipeline finished successfully!")
        logger.info(f"Results available in: {output_dir}")

        if args.num_tasks:
            logger.info(f"📊 Note: Results are based on {args.num_tasks} tasks only")

    except FatalAccountError as e:
        logger.critical(
            "Stopping scoring: %s",
            describe_fatal_account_error(e),
        )
        raise
    except (FileNotFoundError, KeyError) as e:
        logger.error(f"Pipeline stopped due to an error: {e}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
        raise


def get_parser(input_path=None, model_label=None):
    default_input_file = input_path or os.getenv("EVAL_INPUT_FILE", "")
    default_model_label = model_label or os.getenv("EVAL_MODEL_LABEL", "")

    parser = argparse.ArgumentParser(
        description="Run model evaluation pipeline with coverage scoring."
    )

    parser.add_argument(
        "--input-file",
        type=str,
        default=default_input_file,
        required=not bool(default_input_file),
        help="Path to the completion results CSV file containing both ground truth and model outputs.",
    )
    parser.add_argument(
        "--model-label",
        type=str,
        default=default_model_label,
        required=not bool(default_model_label),
        help="Short identifier for the model being evaluated (e.g., 'gpt51'). Used in output filenames.",
    )
    parser.add_argument(
        "--evaluator-model",
        type=str,
        default=os.getenv("EVAL_LLM_MODEL", "gemini/gemini-2.5-pro"),
        help="Model name in LiteLLM format. Default: EVAL_LLM_MODEL env var or 'gemini/gemini-2.5-pro'",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="evaluation_results",
        help="Directory to save all output files.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Number of concurrent requests to the LLM API.",
    )
    parser.add_argument(
        "--num-tasks",
        type=int,
        default=None,
        help="Limit evaluation to first N tasks (useful for testing). If not specified, processes all tasks.",
    )
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help=(
            "Rescore every task, even ones whose claims and response are "
            "unchanged since the existing scored_<label>.csv was written."
        ),
    )
    parser.set_defaults(resume=True)
    parser.add_argument(
        "--pass-threshold",
        type=float,
        default=0.75,
        help="Coverage score threshold for pass rate calculation (default: 0.75)",
    )

    args = parser.parse_args()
    return args

if __name__ == "__main__":
    args = get_parser()

    # Run the main async function
    asyncio.run(main(args))
