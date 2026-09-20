"""One-task command protocol for the existing MCP-Atlas coverage evaluator.

This is transport only: claim prompts, outcomes and aggregation remain in
``mcp_evals_scores.CoverageEvaluator``. It never judges trajectory behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--input-price", type=float, default=0.0)
    parser.add_argument("--cached-input-price", type=float, default=0.0)
    parser.add_argument("--output-price", type=float, default=0.0)
    args = parser.parse_args()
    request = json.load(sys.stdin)
    inputs = request.get("inputs", {})
    task = inputs.get("task_spec", {}).get("payload", {})
    candidate = inputs.get("candidate", {}).get("payload", {})
    if request.get("protocol_version") != 1 or not isinstance(task, dict) or not isinstance(candidate, dict):
        print(json.dumps({"status": "failure", "failure_class": "infrastructure", "reason_code": "coverage_input_invalid"}))
        return
    claims = [item for item in task.get("claims", []) if isinstance(item, dict) and item.get("role") == "final"]
    answer = candidate.get("final_answer")
    if not claims or not isinstance(answer, str):
        print(json.dumps({"status": "failure", "failure_class": "infrastructure", "reason_code": "coverage_input_invalid"}))
        return
    # The pipeline uses one shared model credential. The scorer itself still
    # reads its native EVAL_* names; the mapping is local to this subprocess.
    os.environ.setdefault("EVAL_LLM_API_KEY", os.environ.get("LLM_API_KEY", ""))
    os.environ.setdefault("EVAL_LLM_BASE_URL", os.environ.get("LLM_BASE_URL", ""))
    if not os.environ.get("EVAL_LLM_API_KEY") or not os.environ.get("EVAL_LLM_BASE_URL"):
        print(json.dumps({"status": "failure", "failure_class": "account_fatal", "reason_code": "evaluator_credentials_missing"}))
        return
    with tempfile.TemporaryDirectory(prefix="atlas-score-one-") as token_dir:
        os.environ["EVAL_TOKEN_LOG_DIR"] = token_dir
        from mcp_evals_scores import (  # noqa: PLC0415
            AsyncLiteLLMClient, CoverageEvaluator, EvaluatorConfig,
            SCORING_POLICY_VERSION, TOKEN_LOG_PATH,
        )
        config = EvaluatorConfig(evaluator_model=args.model, semaphore_limit=min(8, len(claims)), verbose=False)
        evaluator = CoverageEvaluator(AsyncLiteLLMClient(config), config)
        try:
            scored = asyncio.run(evaluator.evaluate(
                [str(item.get("text") or "") for item in claims], answer,
                task_id=str(request.get("context", {}).get("case_id") or "unknown"),
            ))
        except Exception as exc:
            # The original scorer's account guard must remain terminal; other
            # failures are infrastructure, never a fabricated zero score.
            from mcp_completion.account_guard import is_fatal_account_error
            fatal = is_fatal_account_error(exc)
            print(json.dumps({
                "status": "failure", "failure_class": "account_fatal" if fatal else "infrastructure",
                "reason_code": "coverage_account_fatal" if fatal else "coverage_evaluator_failed",
                "detail": type(exc).__name__,
            }))
            return
        usage_rows = []
        log_path = Path(TOKEN_LOG_PATH)
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                try:
                    usage_rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        input_tokens = sum(int(row.get("prompt_tokens") or 0) for row in usage_rows)
        output_tokens = sum(int(row.get("completion_tokens") or 0) for row in usage_rows)
        cost = (input_tokens * args.input_price + output_tokens * args.output_price) / 1_000_000
        scored["per_claim"] = [
            {**item, "claim_id": str(claim.get("id") or "")}
            for claim, item in zip(claims, scored["per_claim"], strict=True)
        ]
        scored["scoring_policy_version"] = SCORING_POLICY_VERSION
        print(json.dumps({
            "status": "success", "payload": scored,
            "metrics": {
                "input_tokens": input_tokens, "cached_tokens": 0,
                "output_tokens": output_tokens, "cost_usd": cost,
                "prompt_cache": {"status": "unknown", "cached_tokens": None},
                "dimensions": {"scorer": "mcp_evals_scores.CoverageEvaluator",
                               "policy_version": SCORING_POLICY_VERSION},
            },
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
