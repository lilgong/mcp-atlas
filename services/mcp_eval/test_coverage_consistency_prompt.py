"""Prompt-only regression: do not import provider config or make model calls."""
import ast
import re
from pathlib import Path


def test_response_contradictions_and_exact_dates_are_not_approximate_coverage():
    path = Path(__file__).with_name('mcp_evals_scores.py')
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and any(isinstance(m, ast.FunctionDef) and m.name == '_get_single_claim_evaluation_prompt' for m in n.body))
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_get_single_claim_evaluation_prompt')
    namespace = {'re': re}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
    prompt = namespace[method.name](None, 'Correct timestamp', 'Wrong UTC date')
    assert 'correct Unix timestamp next to an incorrect explicitly' in prompt
    assert 'do not apply the 5% tolerance' in prompt
    assert 'clearly superseded intermediate guess' in prompt
    converted = namespace[method.name](None, 'timestamp 1787762784.732289',
                                       '2026-08-25 UTC (timestamp `1787762784.732289`)')
    assert '1787762784.732289 -> 2026-08-26T16:46:24' in converted
    version = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'SCORING_POLICY_VERSION' for t in n.targets))
    assert version == 'coverage-judge-v3-consistency'
