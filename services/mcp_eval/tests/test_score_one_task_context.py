"""Offline scorer-adapter regressions; no provider configuration or model calls."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


def adapter():
    path = Path(__file__).resolve().parents[1] / "score_one.py"
    spec = importlib.util.spec_from_file_location("score_one_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_context_keeps_original_rules_and_distinguishes_translation_from_quotation():
    class Base:
        def _get_single_claim_evaluation_prompt(self, claim, response):
            return "ORIGINAL SCORING RULES\n" + claim + "\n" + response

    task = 'Translate the paragraph into Japanese; keep `(type = "...")` unchanged.'
    evaluator = adapter().task_context_evaluator_type(Base, task)()
    text = evaluator._get_single_claim_evaluation_prompt("デコレータ", "デコレーター")
    assert text.startswith("ORIGINAL SCORING RULES\nデコレータ\nデコレーター")
    assert json.dumps(task, ensure_ascii=False) in text
    assert "Equivalent terminology or spelling variants" in text
    assert "negation and modality" in text
    assert "Exact wording is required only" in text
    assert "do not demand a redundant English quotation" in text
    assert "multiple different valid answers" in text
    assert "explicitly asks for all matches" in text


@pytest.mark.parametrize("joint", [False, True])
def test_only_joint_author_uses_context_and_never_posthoc_changes_scores(monkeypatch, capsys, tmp_path, joint):
    module = adapter()
    received = []

    class Evaluator:
        def __init__(self, *_):
            pass

        def _get_single_claim_evaluation_prompt(self, claim, response):
            return "BASE PROMPT\n" + claim + "\n" + response

        async def evaluate(self, claims, answer, **kwargs):
            received.append(self._get_single_claim_evaluation_prompt(claims[0], answer))
            return {"coverage_score": .5, "per_claim": [{"score": .5, "reason": "stub verdict"}]}

    monkeypatch.setitem(sys.modules, "mcp_evals_scores", SimpleNamespace(
        CoverageEvaluator=Evaluator, AsyncLiteLLMClient=lambda c: object(),
        EvaluatorConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        SCORING_POLICY_VERSION="unchanged-base", TOKEN_LOG_PATH=tmp_path / "absent.jsonl"))
    task = {"prompt": "Which date?", "claims": [{"id": "c1", "text": "One date.", "role": "final"}]}
    if joint:
        task["design_verified"] = {"design": {"authoring_mode": "joint-author-v3"}}
    request = {"protocol_version": 1, "inputs": {
        "task_spec": {"payload": task}, "candidate": {"payload": {"final_answer": "Several dates fit."}}}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "argv", ["score_one", "--model", "stub"])
    monkeypatch.setenv("EVAL_LLM_API_KEY", "unused")
    monkeypatch.setenv("EVAL_LLM_BASE_URL", "http://unused.invalid")
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert result["payload"]["coverage_score"] == .5
    assert result["payload"]["per_claim"] == [{"score": .5, "reason": "stub verdict", "claim_id": "c1"}]
    assert ("TASK-CONTEXT INTERPRETATION" in received[0]) is joint
    version = "unchanged-base" + ("-" + module.TASK_CONTEXT_POLICY if joint else "")
    assert result["payload"]["scoring_policy_version"] == version
