"""Exercise production routing code without requiring the Python 3.13 MCP package."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'src/agent_environment/osm_mcp_compat.py'


def load_route():
    # Only the external MCP server import needs Python 3.13. Compile the actual
    # production function/constants unchanged; fake HTTP responses below test
    # backend selection and failure handling, not a duplicate implementation.
    tree = ast.parse(SOURCE.read_text())
    names = {'ROUTE_URL_PREFIXES', 'ROUTE_ATTEMPT_TIMEOUT_SECONDS'}
    body = [node for node in tree.body if
            (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets))
            or (isinstance(node, ast.AsyncFunctionDef) and node.name == 'resilient_get_route')]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *body], type_ignores=[])
    namespace = {'aiohttp': aiohttp}
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), namespace)
    return namespace['resilient_get_route']


class Response:
    def __init__(self, status=200, data=None, error=None):
        self.status, self.data, self.error = status, data, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.data


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def route_data(*modes):
    return {'code': 'Ok', 'routes': [{'distance': 710, 'duration': 517,
            'legs': [{'steps': [{'mode': mode} for mode in modes]}]}]}


@pytest.mark.parametrize('requested,actual', [('car', 'driving'), ('bike', 'cycling'), ('foot', 'walking')])
def test_correct_backend_preserves_original_response(requested, actual):
    data = route_data(actual)
    session = Session(Response(data=data))
    result = asyncio.run(load_route()(SimpleNamespace(session=session), 1, 2, 3, 4, requested))
    assert result is data
    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert url == f'https://routing.openstreetmap.de/routed-{requested}/route/v1/driving/2,1;4,3'
    assert options['params']['steps'] == 'true'
    assert options['timeout'].total == 20


@pytest.mark.parametrize('mode', ['car', 'bike', 'foot'])
@pytest.mark.parametrize('response', [Response(status=429), Response(status=502),
                                      Response(error=asyncio.TimeoutError()),
                                      Response(error=aiohttp.ClientConnectionError()),
                                      Response(data={'code': 'NoRoute', 'routes': []})])
def test_failure_never_falls_back_to_another_profile(mode, response):
    session = Session(response)
    with pytest.raises((RuntimeError, asyncio.TimeoutError, aiohttp.ClientError)):
        asyncio.run(load_route()(SimpleNamespace(session=session), 1, 2, 3, 4, mode))
    assert len(session.calls) == 1
    assert f'/routed-{mode}/' in session.calls[0][0]


@pytest.mark.parametrize('requested,actual', [('foot','driving'), ('foot','cycling'), ('bike','driving'), ('car','walking'), ('foot',None)])
def test_http_success_cannot_hide_wrong_route_profile(requested, actual):
    session = Session(Response(data=route_data(actual)))
    with pytest.raises(RuntimeError, match='profile mismatch'):
        asyncio.run(load_route()(SimpleNamespace(session=session), 1, 2, 3, 4, requested))
    assert len(session.calls) == 1


@pytest.mark.parametrize('mode,modes', [('foot', ['walking','ferry']), ('bike',['cycling','pushing bike','walking'])])
def test_valid_mixed_route_segments_are_preserved(mode,modes):
    data = route_data(*modes)
    session = Session(Response(data=data))
    assert asyncio.run(load_route()(SimpleNamespace(session=session),1,2,3,4,mode)) is data


def test_unknown_input_mode_is_not_silently_mapped_to_car():
    session = Session(Response())
    with pytest.raises(ValueError):
        asyncio.run(load_route()(SimpleNamespace(session=session),1,2,3,4,'invalid'))
    assert not session.calls
