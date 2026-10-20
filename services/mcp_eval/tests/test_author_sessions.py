import asyncio
import copy
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from mcp_completion import agent_eval, author_sessions, synthesis_loop
from mcp_completion.author_sessions import AuthorSessions
from mcp_completion.schema import RunAgentAPIRequestBody, UserMessage
from test_synthesis_loop import FakeClient, FakeTool, _assistant


def body(action="start", **changes):
    fields = dict(model="test-model", messages=[{"role": "user", "content": "draft a task"}],
                  enabledTools=["read"], taskId="case", authorSessionId="a" * 64,
                  authorSessionAction=action, maxTurns=5, maxToolCalls=4,
                  synthesisControl={"authoringMode": "joint-author-v3"}, includeTelemetry=True)
    fields.update(changes)
    return RunAgentAPIRequestBody(**fields)


class Client(FakeClient):
    def __init__(self):
        super().__init__([FakeTool("read")], results={"read": "persisted document"})
        self.enters = self.closes = 0

    async def __aenter__(self):
        self.enters += 1
        return self

    async def __aexit__(self, *_):
        self.closes += 1


class AuthorSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_tools_labels_and_per_request_usage_survive_review(self):
        registry = AuthorSessions()
        client = Client()
        seen = []
        answers = [_assistant([("read", {})]), _assistant([], "first draft"),
                   _assistant([("read", {})]), _assistant([], "corrected draft")]
        answers[0].message.tool_calls[0].id = "old-call"
        answers[2].message.tool_calls[0].id = "new-call"

        async def model(**kwargs):
            seen.append(copy.deepcopy(kwargs["messages"]))
            return answers.pop(0)

        with patch.object(author_sessions, "AUTHOR_SESSIONS", registry), \
             patch.object(agent_eval, "IsolatedMCPClient", return_value=client), \
             patch.object(synthesis_loop, "create_completion", side_effect=model):
            first = [x async for x in agent_eval.handle_run_mcp_eval(body())]
            self.assertEqual(client.closes, 0)  # Waiting for an external judge.
            entry = registry.entries["a" * 64]
            self.assertFalse(entry.busy)
            second = [x async for x in agent_eval.handle_run_mcp_eval(body("resume", messages=[
                {"role": "user", "content": "Reviewer: inspect the missing paragraph"}]))]
            self.assertEqual(client.enters, 1)
            self.assertEqual(client.closes, 0)
            self.assertEqual(entry.state["evidence_count"], 2)
            self.assertEqual(sum(x.type == "telemetry" for x in second), 1)
            first_messages = [x.data for x in first if x.type == "message"]
            self.assertEqual(first_messages,
                             [x.data for x in second if x.type == "message"][:len(first_messages)])
            final_context = "\n".join(str(m.model_dump()) for m in seen[-1])
            self.assertIn("first draft", final_context)
            self.assertIn("Reviewer: inspect", final_context)
            self.assertIn("Evidence E1; source_event_id=old-call", final_context)
            self.assertIn("Evidence E2; source_event_id=new-call", final_context)
            with self.assertRaises(HTTPException) as exc:
                await registry.begin(body("resume"), lambda: Client())
            self.assertEqual(exc.exception.status_code, 409)
            await registry.close("a" * 64)
            self.assertEqual(client.closes, 1)

    async def test_missing_busy_or_changed_session_is_never_recreated(self):
        registry = AuthorSessions()
        with self.assertRaises(HTTPException) as exc:
            await registry.begin(body("resume"), lambda: Client())
        self.assertEqual(exc.exception.status_code, 410)
        client = Client()
        entry = await registry.begin(body(), lambda: client)
        with self.assertRaises(HTTPException):
            await registry.close("a" * 64)
        registry.idle(entry)
        with self.assertRaises(HTTPException):
            await registry.begin(body("resume", enabledTools=["different"]), lambda: Client())
        entry.touched -= 1801
        await registry.reap()
        self.assertEqual(client.closes, 1)
        self.assertFalse(registry.entries)

    async def test_cancelled_model_cleans_environment(self):
        registry = AuthorSessions()
        client = Client()
        with patch.object(author_sessions, "AUTHOR_SESSIONS", registry), \
             patch.object(agent_eval, "IsolatedMCPClient", return_value=client), \
             patch.object(synthesis_loop, "create_completion", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                _ = [x async for x in agent_eval.handle_run_mcp_eval(body())]
        self.assertFalse(registry.entries)
        self.assertEqual(client.closes, 1)

    def test_evaluation_cannot_accidentally_open_author_session(self):
        with self.assertRaises(ValidationError):
            body(synthesisControl=None)
