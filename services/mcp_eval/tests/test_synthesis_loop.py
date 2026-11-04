import json
import unittest
from unittest.mock import patch

from litellm.types.utils import Message as LiteLLMMessage

from mcp_completion import synthesis_loop
from mcp_completion.llm import LLMResponse
from mcp_completion.schema import (
    AssistantMessage,
    CallToolResponse,
    SynthesisControl,
    TextContent,
    ToolCall,
    UserMessage,
)


def _assistant(calls, text=""):
    tool_calls = [
        ToolCall(id=f"c{index}", type="function", function={
            "name": name, "arguments": json.dumps(arguments),
        })
        for index, (name, arguments) in enumerate(calls)
    ]
    raw = LiteLLMMessage(
        role="assistant", content=text,
        tool_calls=[call.model_dump() for call in tool_calls] or None,
    )
    return LLMResponse(message=AssistantMessage(
        role="assistant", content=text, original_message=raw,
        tool_calls=tool_calls or None,
    ))


class FakeTool:
    def __init__(self, name, schema=None, server=None):
        self.row = {"name": name, "description": name,
                    "input_schema": schema or {"type": "object"}, "server": server}

    def model_dump(self):
        return dict(self.row)


class FakeClient:
    def __init__(self, tools, results=None, errors=None):
        self.tools = tools
        self.results = results or {}
        self.errors = errors or {}
        self.calls = []

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.errors:
            raise self.errors[name]
        return CallToolResponse(content=[TextContent(type="text", text=self.results.get(name, "ok"))])


async def _run(client, responses, max_tool_calls=20, max_turns=30, control=None):
    seen = []

    async def fake_completion(**kwargs):
        seen.append(list(kwargs["messages"]))
        return responses.pop(0)

    with patch.object(synthesis_loop, "create_completion", fake_completion), \
         patch.object(synthesis_loop, "_transform_tool_calls", lambda tools: []):
        outputs = [item async for item in synthesis_loop.run_synthesis_eval(
            client, "model", [UserMessage(role="user", content="mission")],
            max_turns, max_tool_calls, control or SynthesisControl(),
        )]
    return outputs, seen


class SynthesisLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_status_precedes_every_turn_and_results_are_bounded_for_the_model(self):
        client = FakeClient([FakeTool("wiki_search")], results={"wiki_search": "x" * 9000})
        outputs, seen = await _run(client, [
            _assistant([("wiki_search", {"q": "a"})]),
            _assistant([], text="summary"),
        ], control=SynthesisControl(perResultMaxChars=6000))
        self.assertEqual(seen[0][-1].content.split(":")[0], "BUDGET STATUS")
        self.assertIn("20 tool-call slots", seen[0][-1].content)
        self.assertIn("19 tool-call slots", seen[1][-1].content)
        visible = seen[1][-2]
        self.assertEqual(len(visible.content[0].text), 6000)
        stored = [item.data for item in outputs if item.data.get("role") == "tool"][0]
        self.assertEqual(len(stored["content"]), 9000)
        self.assertFalse(any(item.type == "error" for item in outputs))

    async def test_calls_beyond_the_budget_are_dropped_and_the_run_stops(self):
        client = FakeClient([FakeTool("a_x")])
        outputs, _ = await _run(client, [
            _assistant([("a_x", {"n": 1}), ("a_x", {"n": 2}), ("a_x", {"n": 3})]),
        ], max_tool_calls=2)
        self.assertEqual(len(client.calls), 2)
        assistant = [item.data for item in outputs if item.data.get("role") == "assistant"][0]
        self.assertEqual(len(assistant["tool_calls"]), 2)
        self.assertEqual(len(assistant["original_message"]["tool_calls"]), 2)
        self.assertEqual(outputs[-1].data["reason_code"], "max_tool_calls_reached")

    async def test_repeated_tool_warnings_escalate_once_per_tier(self):
        client = FakeClient([FakeTool("a_x")])
        responses = [_assistant([("a_x", {"n": index})]) for index in range(5)]
        responses.append(_assistant([], text="done"))
        outputs, _ = await _run(client, responses)
        notices = [item.data["content"] for item in outputs if item.data.get("role") == "user"]
        self.assertEqual(len(notices), 1)
        self.assertIn("called 4 times", notices[0])

    async def test_timed_out_fragile_server_is_quarantined(self):
        client = FakeClient(
            [FakeTool("osm-mcp-server_geocode", server="osm-mcp-server")],
            errors={"osm-mcp-server_geocode": TimeoutError("gateway 504 timed out")},
        )
        outputs, _ = await _run(client, [
            _assistant([("osm-mcp-server_geocode", {"q": "a"})]),
            _assistant([("osm-mcp-server_geocode", {"q": "b"})]),
            _assistant([], text="done"),
        ], control=SynthesisControl(noRetryTimeoutServers=["osm-mcp-server"]))
        self.assertEqual(len(client.calls), 1)
        texts = [item.data["content"] for item in outputs if item.data.get("role") == "tool"]
        self.assertIn("already timed out", texts[1])

    async def test_json_string_arguments_are_decoded_against_the_schema(self):
        schema = {"type": "object", "properties": {"filter": {"type": "object"}}}
        client = FakeClient([FakeTool("db_find", schema)])
        await _run(client, [
            _assistant([("db_find", {"filter": "{\"a\": 1}"})]),
            _assistant([], text="done"),
        ])
        self.assertEqual(client.calls[0][1], {"filter": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
