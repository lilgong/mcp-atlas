import importlib.util
import json
from pathlib import Path


def test_summary_separates_models_judges_probes_and_preserves_raw_logs(tmp_path):
    script = Path(__file__).resolve().parents[3] / 'scripts/summarize_api_usage.py'
    spec = importlib.util.spec_from_file_location('summarize_api_usage', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    month = tmp_path / '2026-10'
    month.mkdir()
    records = [
        {'model': 'test-model', 'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13},
        {'model': 'test-model', 'prompt_tokens': 2},
        {'model': 'test-model', 'probe': True, 'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        {'model': 'test-model', 'prompt_tokens': -1},
    ]
    raw = month / 'token_usage_test.jsonl'
    content = '\n'.join(json.dumps(r) for r in records) + '\nnot-json\n'
    raw.write_text(content)
    (month / 'eval_token_usage_test.jsonl').write_text(json.dumps({'model': 'judge', 'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}) + '\n')
    other = tmp_path / '2026-09'
    other.mkdir()
    module.summarize(tmp_path, '2026-10')
    summary = (month / 'summary.md').read_text()
    assert '| 生成模型 | test-model | 2 | 12 | 3 | 13 |' in summary
    assert '| 测试探针 | test-model | 1 | 1 | 1 | 2 |' in summary
    assert '| 评分模型 | judge | 1 | 4 | 2 | 6 |' in summary
    assert '无效记录：2 条' in summary
    assert '缺失 token 字段的记录：1 条' in summary
    assert raw.read_text() == content
    assert not (other / 'summary.md').exists()
