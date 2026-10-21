import pytest

from mcp_completion.llm import _normalized_usage, _accumulate_usage
from score_one import usage_accounting


@pytest.mark.parametrize('fields,cache,unknown', [
    ({}, 0, 100),
    ({'prompt_tokens_details': {'cached_tokens': None}}, 0, 100),
    ({'prompt_tokens_details': {'cached_tokens': 0}}, 0, 0),
    ({'prompt_tokens_details': {'cached_tokens': 60}}, 60, 0),
    ({'prompt_cache_hit_tokens': 60}, 60, 0),
    ({'cache_read_input_tokens': 60}, 60, 0),
])
def test_null_unknown_and_provider_cache_aliases(fields, cache, unknown):
    normalized = _normalized_usage({'prompt_tokens': 100, 'completion_tokens': 2, **fields})
    assert normalized['cached_tokens'] == cache
    assert normalized['cache_unknown_input_tokens'] == unknown
    assert normalized['cache_reported'] == (unknown == 0)


def test_partial_cache_survives_retry_turn_and_session_aggregation():
    total = dict(input_tokens=0, output_tokens=0, cached_tokens=0, cache_unknown_input_tokens=0)
    _accumulate_usage(total, _normalized_usage({'prompt_tokens': 1000,
        'prompt_tokens_details': {'cached_tokens': 800}}))
    _accumulate_usage(total, _normalized_usage({'prompt_tokens': 200}))
    outer = dict.fromkeys(total, 0)
    _accumulate_usage(outer, {**total, 'cache_reported': False})
    assert outer == dict(input_tokens=1200, output_tokens=0, cached_tokens=800,
                         cache_unknown_input_tokens=200)


def test_coverage_judge_prices_known_cache_and_unknown_separately():
    result = usage_accounting([
        {'usage': {'prompt_tokens': 1000, 'completion_tokens': 10,
                   'prompt_tokens_details': {'cached_tokens': 800}}},
        {'prompt_tokens': 200, 'completion_tokens': 5},
    ], model='gpt-5.4', input_price=5, cached_price=.5, output_price=30)
    assert result[:4] == (1200, 15, 800, 200)
    assert result[4] == pytest.approx((400 * 5 + 800 * .5 + 15 * 30) / 1e6)


def test_coverage_tier_applies_to_each_request_not_aggregate():
    result = usage_accounting([{'prompt_tokens': 200000, 'completion_tokens': 100}] * 2,
        model='gpt-5.4', input_price=5, cached_price=.5, output_price=30)
    assert result[-1] == pytest.approx((400000*5 + 200*30)/1e6)


def test_stream_usage_only_tail_keeps_cache():
    import asyncio
    from litellm import ModelResponse
    from mcp_completion.streaming import collect_litellm_response

    async def stream():
        yield ModelResponse(stream=True, choices=[{
            'index': 0, 'delta': {'content': 'done'}, 'finish_reason': 'stop'}])
        yield ModelResponse(stream=True, choices=[], usage={
            'prompt_tokens': 1000, 'completion_tokens': 2,
            'prompt_tokens_details': {'cached_tokens': 800}})

    response = asyncio.run(collect_litellm_response(stream(), messages=[]))
    assert _normalized_usage(response.usage)['cached_tokens'] == 800
