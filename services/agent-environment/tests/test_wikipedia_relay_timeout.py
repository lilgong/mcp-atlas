"""Validate the Wikipedia adapter without installing its process-wide hooks."""
import ast
import json
from pathlib import Path
import types

import pytest


@pytest.fixture
def wiki():
    path = Path(__file__).resolve().parents[1] / 'src/agent_environment/wikipedia_preload/sitecustomize.py'
    tree = ast.parse(path.read_text(), filename=str(path))
    assert isinstance(tree.body[-1], ast.Expr)
    assert isinstance(tree.body[-1].value, ast.Call)
    assert tree.body[-1].value.func.id == '_install'
    tree.body.pop()
    module = types.ModuleType('wikipedia_timeout_test')
    exec(compile(tree, str(path), 'exec'), module.__dict__)
    return module


def test_wikipedia_delivery_margin_reaches_relay(wiki, monkeypatch):
    monkeypatch.setenv('MCP_RELAY_URL', 'http://relay.test')
    monkeypatch.setenv('MCP_RELAY_TOKEN', 'test-only')
    monkeypatch.delenv('MCP_RELAY_CLIENT_TIMEOUTS', raising=False)
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"status_code":200,"headers":{},"body_base64":"","url":"https://en.wikipedia.org/w/api.php"}'
    def send(request, timeout):
        seen.append((json.loads(request.data)['timeout_seconds'], timeout))
        return Response()
    monkeypatch.setattr(wiki.urllib.request, 'urlopen', send)
    assert wiki._relay_fetch('https://en.wikipedia.org/w/api.php', {})[0] == 200
    assert seen == [(140, 145)]
    monkeypatch.setenv('MCP_RELAY_CLIENT_TIMEOUTS', '{"wikipedia":160}')
    assert wiki._relay_settings()[2] == 160


@pytest.mark.parametrize('payload', ['[]', '{"wikipedia":true}', '{"wikipedia":0}', '{"wikipedia":NaN}'])
def test_invalid_wikipedia_timeout_is_not_silently_ignored(wiki, monkeypatch, payload):
    monkeypatch.setenv('MCP_RELAY_URL', 'http://relay.test')
    monkeypatch.setenv('MCP_RELAY_TOKEN', 'test-only')
    monkeypatch.setenv('MCP_RELAY_CLIENT_TIMEOUTS', payload)
    with pytest.raises(RuntimeError):
        wiki._relay_settings()
