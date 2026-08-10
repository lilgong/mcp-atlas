from mcp_completion.response_validation import (
    is_completely_empty_agent_response,
)


def assistant(**fields):
    return {
        "type": "message",
        "data": {
            "role": "assistant",
            "content": None,
            "reasoning_content": None,
            "tool_calls": None,
            **fields,
        },
    }


def test_empty_list_is_completely_empty():
    assert is_completely_empty_agent_response([])


def test_observed_null_assistant_shape_is_completely_empty():
    payload = [assistant(original_message={
        "role": "assistant",
        "content": None,
        "tool_calls": None,
        "function_call": None,
    })]
    assert is_completely_empty_agent_response(payload)


def test_whitespace_only_assistant_is_completely_empty():
    assert is_completely_empty_agent_response([
        assistant(content=" \n", reasoning_content="\t"),
    ])


def test_any_text_or_reasoning_prevents_retry():
    assert not is_completely_empty_agent_response([assistant(content="answer")])
    assert not is_completely_empty_agent_response([
        assistant(reasoning_content="thinking"),
    ])


def test_any_tool_call_prevents_retry():
    assert not is_completely_empty_agent_response([
        assistant(tool_calls=[{"id": "call-1"}]),
    ])


def test_original_message_payload_prevents_retry():
    assert not is_completely_empty_agent_response([
        assistant(original_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1"}],
        }),
    ])


def test_tool_or_error_event_prevents_retry():
    tool_message = {
        "type": "message",
        "data": {"role": "tool", "content": "permission denied"},
    }
    error_event = {"type": "error", "data": {"message": "failed"}}
    assert not is_completely_empty_agent_response([
        assistant(),
        tool_message,
    ])
    assert not is_completely_empty_agent_response([error_event])


def test_unknown_or_missing_payload_is_not_assumed_empty():
    assert not is_completely_empty_agent_response(None)
    assert not is_completely_empty_agent_response({})
    assert not is_completely_empty_agent_response([{"unexpected": True}])

