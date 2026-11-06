"""Empty reasoning is different from an absent field during tool continuation."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from litellm.types.utils import ModelResponse

from mcp_completion.llm import _reasoning_and_content, create_completion
from mcp_completion.schema import ToolCallOutputMessage, UserMessage


@pytest.mark.parametrize('as_dict', [True, False])
@pytest.mark.parametrize('canonical,alias,expected', [
    ('', None, ''), ('', 'alias thought', 'alias thought'),
    ('canonical thought', 'alias thought', 'canonical thought'),
    (None, '', ''), (None, None, None), (None, 'alias thought', 'alias thought'),
])
def test_reasoning_presence_survives_normalization(as_dict, canonical, alias, expected):
    message = dict(role='assistant', content=None)
    if canonical is not None:
        message['reasoning_content'] = canonical
    if alias is not None:
        message['reasoning'] = alias
    response = {'choices': [{'message': message}]}
    if not as_dict:
        response = ModelResponse(**response)
    assert _reasoning_and_content(response) == (expected, '')


def test_empty_thinking_tool_turn_is_sent_back_on_next_request():
    first = {'choices': [{'message': dict(role='assistant', content=None, reasoning_content='',
        tool_calls=[dict(id='call-one', type='function', function=dict(name='lookup', arguments='{}'))])}]}
    second = {'choices': [{'message': dict(role='assistant', content='done', reasoning_content='verified')} ]}
    provider = AsyncMock(side_effect=[first, second])

    async def scenario():
        messages = [UserMessage(role='user', content='Look it up.')]
        result = await create_completion(model='offline-model', messages=messages, tools=[],
                                         extra_body={'thinking': {'type': 'enabled'}})
        assert result.message.reasoning_content == ''
        messages += [result.message, ToolCallOutputMessage(
            role='tool', tool_call_id='call-one', content=[{'type': 'text', 'text': 'found'}])]
        await create_completion(model='offline-model', messages=messages, tools=[],
                                extra_body={'thinking': {'type': 'enabled'}})

    with (patch('mcp_completion.llm.litellm.acompletion', provider),
          patch('mcp_completion.llm.choose_key', return_value='offline-key'),
          patch('mcp_completion.llm.reserve_delay', return_value=0),
          patch('mcp_completion.llm.write_runtime_event'),
          patch('mcp_completion.llm._write_token_usage'),
          patch.dict('os.environ', {'LLM_STREAMING_ENABLED': 'false'})):
        asyncio.run(scenario())
    sent = provider.await_args_list[1].kwargs['messages'][1]
    assert 'reasoning_content' in sent and sent['reasoning_content'] == ''
    assert 'original_message' not in sent
