"""Exercise Relay classification and the actual OSM endpoint fallback boundary."""
import asyncio
import io
import json
from pathlib import Path
import sys
import types
import unittest
import urllib.error
from http.client import IncompleteRead
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/agent_environment'))
import egress_relay_client as relay

# These tests exercise the request contexts, without starting an upstream MCP.
sys.modules.setdefault('osm_mcp_server', types.SimpleNamespace(server=types.SimpleNamespace()))
import osm_mcp_compat as osm


class RelayClassification(unittest.TestCase):
    def fail(self, error):
        with patch.object(relay, 'RELAY_URL', 'http://relay.test'), patch.object(relay, 'RELAY_TOKEN', 'test'), patch.object(relay.urllib.request, 'urlopen', side_effect=error):
            relay.fetch('POST', 'https://overpass.test/api/interpreter')

    def test_gateway_network_errors_are_retryable(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('http://relay.test', status, '', {}, io.BytesIO(b'{"error":"upstream request failed: RelayUpstreamError"}'))
                with self.assertRaises(relay.RelayNetworkError):
                    self.fail(error)

    def test_transport_and_truncated_response_errors_are_safe(self):
        for error in (urllib.error.URLError('private-proxy-credential'), TimeoutError('private-token'), ConnectionResetError('private-token'), IncompleteRead(b'', 123)):
            with self.subTest(kind=type(error).__name__):
                with self.assertRaises(relay.RelayNetworkError) as caught:
                    self.fail(error)
                self.assertNotIn('private', str(caught.exception))

    def test_auth_account_and_configuration_errors_stay_fatal(self):
        for status in (400, 401, 402, 403, 502):
            payload = {'code': 'relay_account_error', 'error': 'account unavailable'}
            error = urllib.error.HTTPError('http://relay.test', status, '', {}, io.BytesIO(json.dumps(payload).encode()))
            with self.subTest(status=status), self.assertRaises(RuntimeError) as caught:
                self.fail(error)
            self.assertNotIsInstance(caught.exception, relay.RelayNetworkError)


class EndpointFallback(unittest.IsolatedAsyncioTestCase):
    def context(self, outcomes):
        attempts = []
        class Context:
            async def __aenter__(self):
                result = outcomes[len(attempts) - 1]
                if isinstance(result, Exception):
                    raise result
                return types.SimpleNamespace(status=result)
            async def __aexit__(self, *_):
                return False
        def request(_session, target, *_args, **_kwargs):
            attempts.append(target)
            return Context()
        return osm._FallbackRequestContext(None, request, ('first', 'second'), (), {}), attempts

    async def test_relay_failure_tries_next_configured_endpoint(self):
        context, attempts = self.context([relay.RelayNetworkError('network'), 200])
        self.assertEqual((await context.__aenter__()).status, 200)
        self.assertEqual(attempts, ['first', 'second'])
        await context.__aexit__(None, None, None)

    async def test_third_overpass_node_is_tried_after_two_network_failures(self):
        context, attempts = self.context([relay.RelayNetworkError('first'), relay.RelayNetworkError('second'), 200])
        context.urls = ('first', 'second', 'third')
        self.assertEqual((await context.__aenter__()).status, 200)
        self.assertEqual(attempts, ['first', 'second', 'third'])
        await context.__aexit__(None, None, None)

    async def test_new_https_provider_needs_no_wrapper_host_list(self):
        with patch.object(osm, 'relay_enabled', return_value=True):
            context = osm._relay_request('POST', 'https://new-overpass.example/api/interpreter', {})
        self.assertIsInstance(context, osm._RelayRequestContext)

    async def test_private_coffee_never_bypasses_configured_relay(self):
        with patch.object(osm, 'relay_enabled', return_value=True):
            context = osm._relay_request('POST', 'https://overpass.private.coffee/api/interpreter', {})
        self.assertIsInstance(context, osm._RelayRequestContext)

    async def test_all_network_failures_are_bounded_and_return_final_error(self):
        final = relay.RelayNetworkError('second failed')
        context, attempts = self.context([relay.RelayNetworkError('first failed'), final])
        with self.assertRaises(relay.RelayNetworkError) as caught:
            await context.__aenter__()
        self.assertIs(caught.exception, final)
        self.assertEqual(attempts, ['first', 'second'])

    async def test_fatal_errors_do_not_try_next_endpoint(self):
        context, attempts = self.context([RuntimeError('account/configuration error'), 200])
        with self.assertRaises(RuntimeError):
            await context.__aenter__()
        self.assertEqual(attempts, ['first'])


class RelayRequestBudgets(unittest.TestCase):
    def test_wrapper_deadline_reaches_relay_and_preserves_delivery_margin(self):
        captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self):
                return b'{"status_code":200,"headers":{},"body_base64":"","url":"https://overpass.test"}'
        def send(request, timeout):
            captured.append((json.loads(request.data)['timeout_seconds'], timeout))
            return Response()
        with patch.object(relay, 'RELAY_URL', 'http://relay.test'), patch.object(relay, 'RELAY_TOKEN', 'test'), patch.object(relay, 'RELAY_TIMEOUT', 90), patch.object(relay.urllib.request, 'urlopen', side_effect=send):
            relay.fetch('POST', 'https://overpass.test', timeout_seconds=45)
            relay.fetch('POST', 'https://overpass.test', timeout_seconds=180)
            relay.fetch('POST', 'https://overpass.test')
        self.assertEqual(captured, [(40.5, 45), (85, 90), (85, 90)])

    def test_invalid_deadline_never_sends_request(self):
        with patch.object(relay, 'RELAY_URL', 'http://relay.test'), patch.object(relay, 'RELAY_TOKEN', 'test'), patch.object(relay.urllib.request, 'urlopen') as send:
            for value in [0, -1, float('nan'), float('inf'), True]:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    relay.fetch('POST', 'https://overpass.test', timeout_seconds=value)
            send.assert_not_called()


class OsmRelayBudgets(unittest.IsolatedAsyncioTestCase):
    async def test_aiohttp_timeout_is_forwarded_and_fallback_can_continue(self):
        seen = []
        def send(method, url, **kwargs):
            seen.append((url, kwargs['timeout_seconds']))
            if len(seen) == 1:
                raise relay.RelayNetworkError('first endpoint timed out')
            return relay.RelayResponse(200, {}, b'{}', url)
        with patch.object(osm, 'relay_fetch', side_effect=send):
            context = osm._FallbackRequestContext(
                None, lambda _session, url, **kwargs: osm._RelayRequestContext('POST', url, kwargs),
                ('https://first.test', 'https://second.test'), (), {},
            )
            self.assertEqual((await context.__aenter__()).status, 200)
            await context.__aexit__(None, None, None)
        self.assertEqual(seen, [('https://first.test', 45), ('https://second.test', 45)])


if __name__ == '__main__':
    unittest.main()
