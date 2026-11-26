import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from mcp_completion.answer_material_policy import DENIED, FILTERED, filter_response, request_is_protected
from mcp_completion.mcp_client.isolated_client import IsolatedMCPClient
from mcp_completion.schema import CallToolResponse, ResourceContent, TextContent
from mcp_completion.tool_policy import route_for_tool


def response(text):
    return CallToolResponse(content=[TextContent(type='text', text=text)])


@pytest.mark.parametrize(('tool', 'args'), [
    ('github_get_file_contents', {'owner': 'Osmosis-AI', 'repo': 'mcp-atlas-harbor', 'path': 'tasks/example/solution/answer.txt'}),
    ('github_search_code', {'q': 'repo:scaleapi/mcp-atlas Crime_records.csv'}),
    ('fetch_fetch', {'url': 'https://raw.githubusercontent.com/Osmosis-AI/mcp-atlas-harbor/main/tasks/example/tests/claims.json'}),
    ('fetch_fetch', {'url': 'https://github.com/a-fork/mcp-atlas/blob/main/README.md'}),
    ('desktop-commander_read_file', {'path': 'https://datasets-server.huggingface.co/search?dataset=ScaleAI%252FMCP-Atlas&query=crime', 'isUrl': True}),
    ('oxylabs_universal_scraper', {'url': 'https://huggingface.co/datasets/ScaleAI/dummy_mcp'}),
    ('exa_web_search_exa', {'query': 'https://github.laiyagushi.com/Osmosis-AI/mcp-atlas-harbor'}),
    ('e2b-server_run_code', {'code': "urllib.request.urlopen('https://huggingface.co/datasets/ScaleAI/MCP-Atlas/resolve/main/MCP-Atlas.parquet')"}),
    ('mcp-code-executor_execute_code', {'code': "pd.read_parquet('/tmp/mcp_atlas.parquet')"}),
    ('mcp-code-executor_execute_code', {'code': "open('/tmp/mcp-atlas-harbor/tasks/example/solution/answer.txt').read()"}),
])
def test_known_answer_access_is_denied_before_execution(tool, args):
    assert request_is_protected(tool, args)


@pytest.mark.parametrize(('tool', 'args'), [
    ('github_get_file_contents', {'owner': 'lgrammel', 'repo': 'storyteller', 'path': 'README.md'}),
    ('github_search_repositories', {'query': 'assaultcube'}),
    ('exa_web_search_exa', {'query': 'MCP Atlas benchmark paper'}),
    ('fetch_fetch', {'url': 'https://huggingface.co/datasets/other/normal-data'}),
    ('fetch_fetch', {'url': 'https://github.com/other/mcp-atlassian'}),
    ('filesystem_read_text_file', {'path': '/data/.atlas-fixture.json'}),
    ('git_git_show', {'repo_path': '/data/repos/storyteller', 'revision': 'main'}),
    ('notion_API-post-search', {'query': 'mcp-atlas-notion-data'}),
    ('e2b-server_run_code', {'code': 'print(2 + 2)'}),
    ('mcp-code-executor_execute_code', {'code': "pd.read_csv('/data/Crime_records.csv')"}),
])
def test_normal_tools_and_task_data_remain_available(tool, args):
    assert not request_is_protected(tool, args)


def test_github_search_filters_the_whole_protected_hit():
    text = ('### Found 2 code results:\n\n'
            '1. **Osmosis-AI/mcp-atlas-harbor** / `tasks/example/solution/answer.txt`\n```\nSECRET\n```\n\n'
            '2. **lgrammel/storyteller** / `README.md`\n```\nnormal project\n```\n')
    out, changed = filter_response(response(text))
    result = '\n'.join(c.text for c in out.content)
    assert changed and not out.is_error
    assert 'SECRET' not in result and 'Osmosis-AI' not in result
    assert 'normal project' in result and 'Found 1 code results' in result and FILTERED in result


def test_exa_text_retains_clean_result():
    out, _ = filter_response(response(
        'Title: answers\nURL: https://github.com/Osmosis-AI/mcp-atlas-harbor\nText: SECRET\n\n'
        'Title: normal weather\nURL: https://weather.example/forecast\nText: sunny\n'))
    text = '\n'.join(c.text for c in out.content)
    assert 'SECRET' not in text and 'sunny' in text and not out.is_error


@pytest.mark.parametrize('container', [lambda items: {'results': items, 'total': 2},
                                         lambda items: {'web': {'results': items}, 'status': 'ok'},
                                         lambda items: items])
def test_structured_search_filters_protected_items(container):
    payload = container([{'url': 'https://huggingface.co/datasets/ScaleAI/MCP-Atlas', 'text': 'SECRET'},
                         {'url': 'https://example.org', 'text': 'clean'}])
    out, changed = filter_response(response(json.dumps(payload)))
    assert changed and not out.is_error
    safe = json.dumps(out.model_dump())
    assert 'SECRET' not in safe and 'clean' in safe
    json.loads(out.content[0].text)


def test_protected_document_or_resource_is_an_explicit_error():
    for original in [response('Contents of https://github.com/scaleapi/mcp-atlas: SECRET'),
                     CallToolResponse(content=[ResourceContent(type='resource', resource={
                         'uri': 'https://github.com/scaleapi/mcp-atlas', 'text': 'SECRET'})])]:
        out, changed = filter_response(original)
        assert changed and out.is_error and out.content[0].text == DENIED
        assert 'SECRET' not in str(out) and 'SECRET' in str(original)


def test_clean_response_is_not_rewritten():
    original = response('The result is 42; GTFA_CLAIMS is just a field name.')
    out, changed = filter_response(original)
    assert out is original and not changed


@pytest.mark.parametrize('tool', ['fetch_fetch', 'e2b-server_run_code', 'mcp-code-executor_execute_code'])
def test_unified_client_does_not_dispatch_denied_requests(tool):
    client = IsolatedMCPClient(task_id='policy-test', shared_url='http://localhost', enabled_tools=[tool])
    client.allowed_tools = {tool}
    upstream = AsyncMock()
    client._clients[route_for_tool(tool)] = upstream
    with patch('mcp_completion.mcp_client.isolated_client.write_runtime_event') as event:
        out = asyncio.run(client.call_tool(tool, {'url': 'https://github.com/scaleapi/mcp-atlas'}))
    upstream.call_tool.assert_not_awaited()
    assert out.is_error and out.content[0].text == DENIED
    assert event.call_args.kwargs['stage'] == 'request'


def test_unified_client_filters_before_returning_to_agent():
    tool = 'fetch_fetch'
    client = IsolatedMCPClient(task_id='policy-test', shared_url='http://localhost', enabled_tools=[tool])
    client.allowed_tools = {tool}
    upstream = AsyncMock()
    upstream.call_tool.return_value = response('Redirected to https://github.com/scaleapi/mcp-atlas SECRET')
    client._clients[route_for_tool(tool)] = upstream
    with patch('mcp_completion.mcp_client.isolated_client.write_runtime_event'):
        out = asyncio.run(client.call_tool(tool, {'url': 'https://example.org/redirect'}))
    upstream.call_tool.assert_awaited_once()
    assert out.is_error and 'SECRET' not in str(out)


def test_large_result_filter_does_not_block_other_coroutines():
    import threading
    started, proceed = threading.Event(), threading.Event()
    original = response('Normal project documentation.\n' * 5000)

    def slow_filter(value):
        started.set()
        assert proceed.wait(1), 'filter blocked the event loop'
        return value, False

    async def run():
        tool = 'fetch_fetch'
        client = IsolatedMCPClient(task_id='policy-test', shared_url='http://localhost', enabled_tools=[tool])
        client.allowed_tools = {tool}
        upstream = AsyncMock()
        upstream.call_tool.return_value = original
        client._clients[route_for_tool(tool)] = upstream

        async def another_request():
            while not started.is_set():
                await asyncio.sleep(.001)
            proceed.set()

        with patch('mcp_completion.mcp_client.isolated_client.filter_response', side_effect=slow_filter), \
             patch('mcp_completion.mcp_client.isolated_client.write_runtime_event'):
            out, _ = await asyncio.gather(client.call_tool(tool, {'url': 'https://example.org'}), another_request())
        assert out is original

    asyncio.run(run())


@pytest.mark.parametrize(('tool', 'args'), [
    ('github_get_file_contents', {'owner': 'iflow-mcp', 'repo': 'scaleapi-mcp-atlas', 'path': 'services/mcp_eval/sample_tasks.csv'}),
    ('mcp-code-executor_execute_code', {'code': "urllib.request.urlopen('https://raw.githubusercontent.com/iflow-mcp/scaleapi-mcp-atlas/main/services/mcp_eval/sample_tasks.csv')"}),
    ('e2b-server_run_code', {'code': "requests.get('https://huggingface.co/datasets/ScaleAI/lhaw/resolve/main/test.json')"}),
    ('fetch_fetch', {'url': 'https://datasets-server.huggingface.co/rows?dataset=ScaleAI%252Flhaw&split=test'}),
])
def test_observed_republications_are_denied(tool, args):
    assert request_is_protected(tool, args)


@pytest.mark.parametrize('query', [
    'https://github.com/iflow-mcp/scaleapi-mcp-atlassian',
    'https://huggingface.co/datasets/ScaleAI/lhaw-tools',
    'https://huggingface.co/datasets/other/lhaw',
    'https://github.com/company/scaleapi-mcp-atlas-client',
    'MCP-Atlas benchmark paper',
    'https://arxiv.org/html/2602.00933v2',
    'https://labs.scale.com/papers/mcpatlas',
    '/data/Crime_records.csv',
])
def test_neighboring_names_papers_and_business_data_are_not_blocked(query):
    assert not request_is_protected('fetch_fetch', {'url': query})
    original = response(query)
    out, changed = filter_response(original)
    assert out is original and not changed


@pytest.mark.parametrize('source', [
    'https://github.com/iflow-mcp/scaleapi-mcp-atlas',
    'https://huggingface.co/datasets/ScaleAI/lhaw',
])
def test_republication_filter_preserves_unrelated_search_hits(source):
    original = response('Title: benchmark source\nURL: ' + source + '\nText: ANSWER_DATA\n\n'
                        'Title: ordinary business result\nURL: https://example.org/crime\nText: ACTUAL_TASK_DATA\n')
    out, changed = filter_response(original)
    assert changed and not out.is_error
    text = '\n'.join(b.text for b in out.content)
    assert 'ANSWER_DATA' not in text and 'ACTUAL_TASK_DATA' in text
    assert FILTERED in text
