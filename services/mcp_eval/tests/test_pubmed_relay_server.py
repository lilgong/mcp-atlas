from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "pubmed_relay_server",
    ROOT / "scripts" / "egress_relay.py",
)
assert SPEC and SPEC.loader
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)


@pytest.fixture(autouse=True)
def public_target_dns(monkeypatch):
    original = relay.socket.getaddrinfo
    def resolve(host, port, *args, **kwargs):
        if str(host) in {"localhost", "127.0.0.1", "::1"}:
            return original(host, port, *args, **kwargs)
        return [(relay.socket.AF_INET, relay.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(relay.socket, "getaddrinfo", resolve)


def test_ipwo_proxy_auth_failure_latches(monkeypatch):
    calls = []

    def fake_fetch(url, allow_redirects, *, session_id=None, budget=None):
        calls.append((url, allow_redirects))
        return 407, {}, b"proxy authentication required", url

    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    controller = relay.Controller()

    with pytest.raises(relay.RelayAccountError, match="IPWO_PROXY_AUTH_FAILED"):
        controller.fetch(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            True,
        )
    with pytest.raises(relay.RelayAccountError, match="IPWO_PROXY_AUTH_FAILED"):
        controller.fetch(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            True,
        )

    assert len(calls) == 1


def test_ipwo_tunnel_auth_failure_latches(monkeypatch):
    calls = []

    def fake_fetch(url, allow_redirects, *, session_id=None, budget=None):
        calls.append((url, allow_redirects))
        raise relay.RelayAccountError(
            "IPWO_PROXY_AUTH_FAILED: IPWO rejected the proxy credential"
        )

    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    controller = relay.Controller()

    with pytest.raises(relay.RelayAccountError, match="IPWO_PROXY_AUTH_FAILED"):
        controller.fetch(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            True,
        )
    with pytest.raises(relay.RelayAccountError, match="IPWO_PROXY_AUTH_FAILED"):
        controller.fetch(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            True,
        )

    assert len(calls) == 1


def test_ipwo_does_not_treat_target_403_as_proxy_account_failure(monkeypatch):
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"

    monkeypatch.setattr(
        relay,
        "_fetch_once",
        lambda _url, _allow_redirects, *, session_id=None, budget=None: (
            403,
            {},
            b"target rejection",
            url,
        ),
    )
    controller = relay.Controller()

    response = controller.fetch(url, True)

    assert response[0] == 403
    assert controller.current_account_error() is None


def test_transient_proxy_failure_is_retried(monkeypatch):
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"
    calls = []

    def fake_fetch(_url, _allow_redirects, *, session_id=None, budget=None):
        calls.append(_url)
        if len(calls) == 1:
            raise relay.RelayUpstreamError("TimeoutError")
        return 200, {}, b"ok", url

    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    monkeypatch.setattr(relay, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(relay.time, "sleep", lambda _seconds: None)
    controller = relay.Controller()

    response = controller.fetch(url, True)

    assert response[0] == 200
    assert response[4] == 2
    assert len(calls) == 2


def test_ipwo_username_uses_requested_country_and_explicit_sid(monkeypatch):
    monkeypatch.setattr(
        relay,
        "IPWO_PROXY_USERNAME",
        "account_custom_zone_GLOBAL_sid_12345678_time_10",
    )
    monkeypatch.setattr(relay, "IPWO_PROXY_COUNTRY", "US")
    username = relay._ipwo_username_for_request("10001234")

    assert username == "account_custom_zone_US_sid_10001234_time_10"


def test_ipwo_username_preserves_rotating_account_without_sticky_suffix(monkeypatch):
    monkeypatch.setattr(
        relay,
        "IPWO_PROXY_USERNAME",
        "account_custom_zone_US",
    )
    monkeypatch.setattr(relay, "IPWO_PROXY_COUNTRY", "US")

    assert relay._ipwo_username_for_request("10001234") == "account_custom_zone_US"
    assert relay._ipwo_username_for_request("10005678") == "account_custom_zone_US"
    assert relay._ipwo_username_for_request() == "account_custom_zone_US"


def test_ipwo_tunnel_407_is_recognized_without_exposing_proxy_url():
    error = OSError("Tunnel connection failed: 407 Proxy Authentication Required")

    assert relay._proxy_tunnel_status(error) == 407


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "IPWO_PROXY_ACCESS_DENIED"),
        (431, "IPWO_PROXY_ACCOUNT_LIMIT"),
    ],
)
def test_ipwo_tunnel_account_failures_are_precise(monkeypatch, status, code):
    class FailingOpener:
        def open(self, _request, timeout):
            raise OSError(f"Tunnel connection failed: {status} rejected")

    monkeypatch.setattr(relay.urllib.request, "build_opener", lambda *_args: FailingOpener())

    with pytest.raises(relay.RelayAccountError, match=code):
        relay._fetch_once("https://example.com", False, session_id="10001234")


def test_controller_reuses_sid_until_failure_then_rotates(monkeypatch):
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"
    session_ids = []

    def fake_fetch(_url, _allow_redirects, *, session_id=None, budget=None):
        session_ids.append(session_id)
        if len(session_ids) == 2:
            raise relay.RelayUpstreamError("TimeoutError")
        return 200, {}, b"ok", url

    generated = iter(["10000001", "10000002"])
    monkeypatch.setattr(relay, "_new_ipwo_session_id", lambda: next(generated))
    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    monkeypatch.setattr(relay, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(relay.time, "sleep", lambda _seconds: None)
    controller = relay.Controller()

    assert controller.fetch(url, True)[0] == 200
    assert controller.fetch(url, True)[0] == 200

    assert session_ids == ["10000001", "10000001", "10000002"]
    assert controller.current_rotation_count() == 1


def test_slow_upstream_request_does_not_block_another_request(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    calls = []

    def fake_fetch(url, _allow_redirects, *, session_id=None, budget=None):
        calls.append(url)
        if url.endswith("first"):
            first_started.set()
            assert release_first.wait(2)
        else:
            second_finished.set()
        return 200, {}, b"ok", url

    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    monkeypatch.setattr(relay, "MIN_INTERVAL", 0.0)
    controller = relay.Controller()
    first = threading.Thread(target=controller.fetch, args=("https://test/first", True))
    second = threading.Thread(target=controller.fetch, args=("https://test/second", True))
    first.start()
    assert first_started.wait(1)
    second.start()
    try:
        assert second_finished.wait(1)
    finally:
        release_first.set()
        first.join(2)
        second.join(2)
    assert calls == ["https://test/first", "https://test/second"]


def test_arxiv_controller_obeys_configured_concurrency(monkeypatch):
    release = threading.Event()
    two_started = threading.Event()
    third_started = threading.Event()
    lock = threading.Lock()
    started = 0

    def fake_fetch(url, _allow_redirects, *, session_id=None, budget=None):
        nonlocal started
        with lock:
            started += 1
            if started == 2:
                two_started.set()
            elif started == 3:
                third_started.set()
        assert release.wait(2)
        return 200, {}, b"ok", url

    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    controller = relay.Controller(0)
    controller.max_concurrency = 2
    threads = [
        threading.Thread(
            target=controller.fetch,
            args=(f"https://test/{index}", True),
        )
        for index in range(3)
    ]
    for thread in threads:
        thread.start()
    try:
        assert two_started.wait(1)
        assert not third_started.wait(0.2)
    finally:
        release.set()
        for thread in threads:
            thread.join(2)
    assert third_started.is_set()


def test_arxiv_controller_has_no_adaptive_concurrency_cap():
    controller = relay.Controller(relay.DEFAULT_LIMITS["arxiv"]["minIntervalMs"] / 1000)
    assert controller.min_interval == 0.0
    assert controller.max_concurrency is None
    assert "concurrency" not in relay.DEFAULT_LIMITS["arxiv"]
    assert not hasattr(controller, "adaptive_rate_limit")


@pytest.mark.parametrize("cancelled", [False, True])
def test_arxiv_queue_removes_expired_or_cancelled_request(cancelled):
    controller = relay.Controller(0)
    controller.max_concurrency = 1
    controller._wait_for_slot(relay.RequestBudget(10))
    budget = relay.RequestBudget(0.02, disconnected=lambda: cancelled)
    error = relay.RelayRequestCancelled if cancelled else relay.RelayRequestExpired
    with pytest.raises(error):
        controller._wait_for_slot(budget)
    assert not controller.pending
    assert controller.active == 1
    assert controller.max_concurrency == 1
    with controller.slots:
        controller.active -= 1
    controller._wait_for_slot(relay.RequestBudget(10))
    assert controller.active == 1


def test_wikipedia_controller_pool_uses_independent_sticky_lanes(monkeypatch):
    url = "https://en.wikipedia.org/w/api.php"
    session_ids = []

    def fake_fetch(_url, _allow_redirects, *, session_id=None, budget=None):
        session_ids.append(session_id)
        return 200, {}, b"ok", url

    generated = iter(["10000001", "10000002"])
    monkeypatch.setattr(relay, "_new_ipwo_session_id", lambda: next(generated))
    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    monkeypatch.setattr(relay, "MIN_INTERVAL", 0.0)
    pool = relay.ControllerPool(2)

    assert pool.fetch(url, True)[0] == 200
    assert pool.fetch(url, True)[0] == 200
    assert pool.fetch(url, True)[0] == 200

    assert session_ids == ["10000001", "10000002", "10000001"]


def test_controller_pool_latches_account_failure_across_lanes(monkeypatch):
    calls = 0

    def fake_fetch(_url, _allow_redirects, *, session_id=None, budget=None):
        nonlocal calls
        calls += 1
        raise relay.RelayAccountError("IPWO_PROXY_AUTH_FAILED: rejected")

    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    pool = relay.ControllerPool(2)
    with pytest.raises(relay.RelayAccountError, match="IPWO_PROXY_AUTH_FAILED"):
        pool.fetch("https://en.wikipedia.org/w/api.php", True)
    with pytest.raises(relay.RelayAccountError, match="IPWO_PROXY_AUTH_FAILED"):
        pool.fetch("https://en.wikipedia.org/w/api.php", True)
    assert calls == 1


def test_json_response_treats_broken_pipe_as_client_disconnect():
    class BrokenWriter:
        def write(self, _body):
            raise BrokenPipeError(32, "broken pipe")

    handler = object.__new__(relay.Handler)
    handler.wfile = BrokenWriter()
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.requestline = "POST /v1/fetch HTTP/1.1"
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    assert handler._json(200, {"status": "ok"}) is False


@pytest.mark.parametrize(
    ("url", "group"),
    [
        ("https://export.arxiv.org/api/query", "arxiv"),
        ("https://export.arxiv.org/pdf/2501.00001", "arxiv"),
        ("https://arxiv.org/abs/2501.00001", "arxiv"),
        ("https://arxiv.org/pdf/2501.00001", "arxiv"),
        ("https://arxiv.org/html/2501.00001", "arxiv"),
        ("https://arxiv.org/format/2412.17451", "arxiv"),
        ("https://www.arxiv.org/format/2412.17451", "arxiv"),
        ("https://arxiv.org/e-print/2501.00001", "arxiv"),
        ("https://arxiv.org/src/2501.00001", "arxiv"),
        ("https://www.arxiv.org/src/2501.00001", "arxiv"),
        ("https://nominatim.openstreetmap.org/search", "osm-nominatim"),
        ("https://overpass-api.de/api/interpreter", "osm-overpass"),
        ("https://router.project-osrm.org/route/v1/car/1,2;3,4", "osm-routing"),
    ],
)
def test_relay_preserves_upstream_category_for_known_hosts(url, group):
    assert relay._validate_url(url) == group


@pytest.mark.parametrize("host,group", list(relay.UPSTREAM_GROUPS.items()))
@pytest.mark.parametrize("path", ["", "/", "/new-api/v2/item", "/format-admin/paper", "/a%2Fb?q=x%2Fy"])
def test_relay_allows_all_paths_on_each_registered_host(host, group, path):
    assert relay._validate_url("https://" + host + path) == group


@pytest.mark.parametrize("url", [
    "http://arxiv.org/format/paper",
    "https://user:password@arxiv.org/new-api",
    "https://arxiv.org/new-api#fragment",
    "https://127.0.0.1/new-api",
])
def test_relay_rejects_non_https_and_unsafe_url_components(url):
    with pytest.raises(ValueError):
        relay._validate_url(url)


def test_relay_checks_redirect_hosts_but_not_paths():
    request = relay.urllib.request.Request("https://arxiv.org/format/paper")
    redirect = relay.SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://export.arxiv.org/new-source/paper",
    )
    assert redirect.full_url == "https://export.arxiv.org/new-source/paper"
    with pytest.raises(ValueError):
        relay.SafeRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://127.0.0.1/new-source/paper",
        )


def test_relay_allows_arxiv_eprint_redirect_to_source_archive():
    request = relay.urllib.request.Request(
        "https://arxiv.org/e-print/1706.03762",
    )
    redirected = relay.SafeRedirect().redirect_request(
        request,
        None,
        301,
        "Moved Permanently",
        {},
        "https://arxiv.org/src/1706.03762",
    )

    assert redirected is not None
    assert redirected.full_url == "https://arxiv.org/src/1706.03762"


def test_controller_forwards_post_body_and_headers(monkeypatch):
    captured = {}

    def fake_fetch(url, allow_redirects, **kwargs):
        captured.update(url=url, allow_redirects=allow_redirects, **kwargs)
        return 200, {}, b"{}", url

    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    controller = relay.Controller(0)
    response = controller.fetch(
        "https://overpass-api.de/api/interpreter",
        True,
        method="POST",
        body=b"data=query",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response[0] == 200
    assert captured["method"] == "POST"
    assert captured["body"] == b"data=query"
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


@pytest.fixture(autouse=True)
def temporary_audit_path(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "LOG_PATH", tmp_path / "usage.jsonl")
    monkeypatch.setattr(relay, "LIMITS_PATH", tmp_path / "limits.json")


def test_live_limits_validate_and_persist(monkeypatch, tmp_path):
    old = relay.current_limits()
    values = {name: dict(value) for name, value in old.items()}
    values["wikipedia"] = {"minIntervalMs": 500, "concurrency": 2}
    try:
        relay.apply_limits(values)
        assert relay.current_limits()["wikipedia"] == values["wikipedia"]
        assert __import__("json").loads(relay.LIMITS_PATH.read_text()) == values
        invalid = dict(values)
        invalid["wikipedia"] = {"minIntervalMs": -1}
        with pytest.raises(ValueError):
            relay.apply_limits(invalid)
        assert relay.current_limits() == values
    finally:
        relay.apply_limits(old, persist=False)


def test_all_overpass_nodes_share_the_same_relay_policy():
    for host in ("overpass-api.de", "maps.mail.ru", "overpass.private.coffee"):
        url = f"https://{host}/api/interpreter"
        group = relay._validate_url(url)
        assert group == "osm-overpass"
        assert relay.controller_for_url(url, group) is relay.CONTROLLERS["osm-overpass"]


def test_ncbi_key_and_pmc_have_separate_http_policies():
    assert relay.controller_for_url("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?api_key=test", "ncbi") is relay.CONTROLLERS["ncbi-key"]
    assert relay.controller_for_url("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", "ncbi") is relay.CONTROLLERS["ncbi"]
    assert relay.controller_for_url("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123", "ncbi") is relay.CONTROLLERS["ncbi-pmc"]
    url = "https://pmc.ncbi.nlm.nih.gov:443/articles/PMC123/pdf/"
    assert relay._validate_url(url) == "ncbi"
    assert relay.controller_for_url(url, "ncbi") is relay.CONTROLLERS["ncbi-pmc"]
    redirected = relay.SafeRedirect().redirect_request(
        relay.urllib.request.Request("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/pdf/"),
        None, 301, "Moved", {}, url,
    )
    assert redirected.full_url == url


def test_each_http_attempt_is_logged_without_query_or_keys(monkeypatch, tmp_path):
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?api_key=do-not-log&term=private-query"
    monkeypatch.setattr(relay, "_fetch_once", lambda target, redirects, **kwargs: (200, {}, b"private body", target))
    relay.Controller(0).fetch(url, False)
    raw = relay.LOG_PATH.read_text()
    records = [__import__("json").loads(line) for line in raw.splitlines()]
    records = [r for r in records if r["event"] in {"http_started", "http_finished"}]
    assert [item["event"] for item in records] == ["http_started", "http_finished"]
    assert records[0]["request_id"] == records[1]["request_id"]
    assert "do-not-log" not in raw and "private-query" not in raw and "private body" not in raw


def wait_until(predicate, timeout=1):
    import time
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end
        time.sleep(0.005)


def test_fifo_admission_and_expired_waiter_do_not_leave_ghost_requests(monkeypatch):
    controller = relay.Controller(0)
    controller.max_concurrency = 1
    started = threading.Event()
    release = threading.Event()
    calls, errors = [], []

    def upstream(url, redirects, **kwargs):
        calls.append(url)
        if url.endswith('/first'):
            started.set()
            assert release.wait(2)
        return 200, {}, b'ok', url

    monkeypatch.setattr(relay, '_fetch_once', upstream)
    def run(name, timeout=2):
        try:
            controller.fetch('https://test/' + name, False, budget=relay.RequestBudget(timeout))
        except Exception as exc:
            errors.append((name, exc))
    first = threading.Thread(target=run, args=('first',)); first.start()
    assert started.wait(1)
    expired = threading.Thread(target=run, args=('expired', 0.05)); expired.start()
    wait_until(lambda: len(controller.pending) == 1)
    second = threading.Thread(target=run, args=('second',)); second.start()
    wait_until(lambda: len(controller.pending) == 2)
    third = threading.Thread(target=run, args=('third',)); third.start()
    expired.join(1)
    assert not expired.is_alive()
    assert len(errors) == 1 and isinstance(errors[0][1], relay.RelayRequestExpired)
    release.set()
    for thread in (first, second, third):
        thread.join(2); assert not thread.is_alive()
    assert calls == ['https://test/first', 'https://test/second', 'https://test/third']
    assert controller.active == 0 and not controller.pending


def test_retry_rejoins_fifo_and_does_not_hold_network_slot(monkeypatch):
    controller = relay.Controller(0)
    controller.max_concurrency = 1
    first_started, release = threading.Event(), threading.Event()
    calls, results = [], []
    cooldown = controller._set_cooldown
    monkeypatch.setattr(controller, '_set_cooldown', lambda seconds: cooldown(0.05))

    def upstream(url, redirects, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            first_started.set(); assert release.wait(2)
            return 429, {}, b'limited', url
        return 200, {}, b'ok', url

    monkeypatch.setattr(relay, '_fetch_once', upstream)
    def run(name):
        results.append(controller.fetch('https://test/' + name, False))
    first = threading.Thread(target=run, args=('retry',)); first.start()
    assert first_started.wait(1)
    other = threading.Thread(target=run, args=('other',)); other.start()
    wait_until(lambda: len(controller.pending) == 1)
    release.set()
    wait_until(lambda: len(controller.pending) == 2)
    assert controller.active == 0
    for thread in (first, other):
        thread.join(2); assert not thread.is_alive()
    assert calls == ['https://test/retry', 'https://test/other', 'https://test/retry']
    assert sorted(result[4] for result in results) == [1, 2]
    assert all(result[0] == 200 for result in results)


def test_cancelled_waiter_is_removed_without_upstream_call(monkeypatch):
    controller = relay.Controller(0)
    controller.max_concurrency = 1
    controller.active = 1
    cancelled = threading.Event()
    errors = []
    monkeypatch.setattr(relay, '_fetch_once', lambda *a, **k: pytest.fail('cancelled request sent'))
    def run():
        try:
            controller.fetch('https://test/', False, budget=relay.RequestBudget(2, cancelled.is_set))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run); thread.start()
    wait_until(lambda: len(controller.pending) == 1)
    cancelled.set(); thread.join(1)
    assert not thread.is_alive() and isinstance(errors[0], relay.RelayRequestCancelled)
    assert not controller.pending and controller.active == 1


def test_cooldown_exceeding_budget_stops_retry(monkeypatch):
    controller = relay.Controller(0)
    calls = []
    def upstream(url, redirects, **kwargs):
        calls.append(url)
        return 429, {'Retry-After': '60'}, b'limited', url
    monkeypatch.setattr(relay, '_fetch_once', upstream)
    with pytest.raises(relay.RelayRequestExpired, match='queue'):
        controller.fetch('https://test/', False, budget=relay.RequestBudget(0.05))
    assert len(calls) == 1 and controller.active == 0 and not controller.pending


def test_response_watchdog_interrupts_blocking_read_on_deadline():
    import socket
    import time
    reader, writer = socket.socketpair()
    start = time.monotonic()
    try:
        with relay._watch_socket(reader, relay.RequestBudget(0.05)):
            assert reader.recv(1) == b''
        assert time.monotonic() - start < 0.5
    finally:
        reader.close(); writer.close()


@pytest.fixture
def http_relay(monkeypatch):
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(relay, 'TOKEN', 'test-relay-token')
    server = ThreadingHTTPServer(('127.0.0.1', 0), relay.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown(); server.server_close(); thread.join(1)


def test_http_queue_timeout_is_classified_and_logged(http_relay, monkeypatch):
    import json
    import urllib.request
    controller = relay.Controller(0)
    controller._set_cooldown(1)
    monkeypatch.setattr(relay, 'controller_for_url', lambda *a: controller)
    monkeypatch.setattr(relay, '_fetch_once', lambda *a, **k: pytest.fail('expired request sent'))
    payload = json.dumps({'url': 'https://export.arxiv.org/api/query', 'timeout_seconds': 0.05}).encode()
    req = urllib.request.Request('http://%s:%s/v1/fetch' % http_relay, data=payload,
                                 headers={'Authorization': 'Bearer test-relay-token'})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=2)
    assert caught.value.code == 504
    assert json.loads(caught.value.read())['code'] == 'queue_timeout'
    records = [json.loads(line) for line in relay.LOG_PATH.read_text().splitlines()]
    summary = next(r for r in records if r['event'] == 'http_completed')
    assert summary['status'] == 504 and summary['attempts'] == 0
    assert summary['queued_ms'] >= 40 and summary['request_id']
    assert not controller.pending and controller.active == 0


def test_http_disconnect_cancels_queued_request_and_next_request_recovers(http_relay, monkeypatch):
    import json
    import socket
    import urllib.request
    controller = relay.Controller(0)
    controller.max_concurrency = 1
    controller.active = 1
    calls = []
    monkeypatch.setattr(relay, 'controller_for_url', lambda *a: controller)
    def upstream(url, redirects, **kwargs):
        calls.append(url); return 200, {}, b'ok', url
    monkeypatch.setattr(relay, '_fetch_once', upstream)
    body = json.dumps({'url': 'https://export.arxiv.org/api/query', 'timeout_seconds': 2}).encode()
    sock = socket.create_connection(http_relay)
    sock.sendall(b'POST /v1/fetch HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-relay-token\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
    wait_until(lambda: len(controller.pending) == 1)
    sock.shutdown(socket.SHUT_RDWR); sock.close()
    wait_until(lambda: len(controller.pending) == 0)
    assert calls == []
    def cancelled_logged():
        return any(json.loads(l).get('category') == 'client_cancelled'
                   for l in relay.LOG_PATH.read_text().splitlines())
    wait_until(cancelled_logged)
    with controller.slots:
        controller.active = 0; controller.slots.notify_all()
    req = urllib.request.Request('http://%s:%s/v1/fetch' % http_relay, data=body,
                                 headers={'Authorization': 'Bearer test-relay-token'})
    with urllib.request.urlopen(req, timeout=2) as response:
        assert json.load(response)['status_code'] == 200
    assert len(calls) == 1


def test_http_retry_returns_success_in_one_client_call(http_relay, monkeypatch):
    import json
    import urllib.request
    controller = relay.Controller(0)
    monkeypatch.setattr(controller, '_set_cooldown', lambda seconds: None)
    monkeypatch.setattr(relay, 'controller_for_url', lambda *a: controller)
    attempts = []
    def upstream(url, redirects, **kwargs):
        attempts.append(url)
        return (429 if len(attempts) == 1 else 200), {}, b'ok', url
    monkeypatch.setattr(relay, '_fetch_once', upstream)
    body = json.dumps({'url': 'https://export.arxiv.org/api/query'}).encode()
    req = urllib.request.Request('http://%s:%s/v1/fetch' % http_relay, data=body,
                                 headers={'Authorization': 'Bearer test-relay-token'})
    with urllib.request.urlopen(req, timeout=2) as response:
        assert json.load(response)['status_code'] == 200
    assert len(attempts) == 2
    records = [json.loads(l) for l in relay.LOG_PATH.read_text().splitlines()]
    parent_ids = {r['parent_request_id'] for r in records if r['event'] == 'http_started'}
    summary = next(r for r in records if r['event'] == 'http_completed')
    assert parent_ids == {summary['request_id']} and summary['attempts'] == 2


def test_many_expired_requests_do_not_block_a_new_request(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    controller = relay.Controller(0)
    controller.max_concurrency = 1
    controller.active = 1
    calls = []
    def upstream(url, redirects, **kwargs):
        calls.append(url); return 200, {}, b'ok', url
    monkeypatch.setattr(relay, '_fetch_once', upstream)
    def expired(_):
        try:
            controller.fetch('https://test/expired', False, budget=relay.RequestBudget(0.05))
        except relay.RelayRequestExpired:
            return 'expired'
    with ThreadPoolExecutor(max_workers=40) as pool:
        assert list(pool.map(expired, range(40))) == ['expired'] * 40
    assert calls == [] and not controller.pending
    controller.active = 0
    assert controller.fetch('https://test/new', False)[0] == 200
    assert calls == ['https://test/new'] and controller.active == 0


def test_twenty_concurrent_calls_succeed_with_pacing_and_single_network_slot(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    controller = relay.Controller(0.005)
    controller.max_concurrency = 1
    starts = []
    active = peak = 0
    lock = threading.Lock()
    def upstream(url, redirects, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1; peak = max(peak, active); starts.append(time.monotonic())
        time.sleep(0.002)
        with lock:
            active -= 1
        return 200, {}, b'ok', url
    monkeypatch.setattr(relay, '_fetch_once', upstream)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda i: controller.fetch(f'https://test/{i}', False,
                           budget=relay.RequestBudget(2)), range(20)))
    assert len(results) == 20 and all(r[0] == 200 and r[4] == 1 for r in results)
    assert peak == 1 and all(b - a >= 0.004 for a, b in zip(starts, starts[1:]))
    assert not controller.pending and controller.active == 0


def test_deadline_interrupts_slow_response_headers():
    import http.client
    import socket
    import time
    reader, writer = socket.socketpair()
    connection = relay.BudgetHTTPSConnection('unused', budget=relay.RequestBudget(0.05))
    connection.sock = reader
    connection._HTTPConnection__state = http.client._CS_REQ_SENT
    writer.sendall(b'HTTP/1.1 200 OK\r\nX-Incomplete: ')
    start = time.monotonic()
    try:
        with pytest.raises(relay.RelayRequestExpired):
            connection.getresponse()
        assert time.monotonic() - start < 0.5
    finally:
        connection.close(); writer.close()


def test_queue_diagnostics_distinguish_capacity_spacing_and_cooldown(monkeypatch):
    records = []
    monkeypatch.setattr(relay, '_append_log', records.append)
    controller = relay.Controller(3)
    controller.max_concurrency = 1
    controller.active = 1
    now = relay.time.monotonic()
    controller.last_started = now
    controller.blocked_until = now + 60
    budget = relay.RequestBudget(0.015)
    with pytest.raises(relay.RelayRequestExpired):
        controller._wait_for_slot(budget)
    waits = [r for r in records if r['event'] == 'queue_wait']
    assert len(waits) == 1
    assert waits[0]['parent_request_id'] == budget.request_id
    assert waits[0]['active'] == 1 and waits[0]['max_concurrency'] == 1
    assert waits[0]['queued_ahead'] == 0 and waits[0]['min_interval_ms'] == 3000
    assert waits[0]['cooldown_wait_ms'] > 59000
    assert waits[0]['interval_wait_ms'] > 2900
    assert controller.active == 1 and not controller.pending


@pytest.mark.parametrize('outcome', ['network_timeout', 'http_504'])
def test_arxiv_timeout_retries_without_extra_cooldown(monkeypatch, outcome):
    monkeypatch.setattr(relay, 'ARXIV_IMMEDIATE_TIMEOUT_RETRY', True)
    controller = relay.Controller(0)
    monkeypatch.setattr(controller, '_set_cooldown', lambda seconds: pytest.fail('Unexpected backoff'))
    sessions = []
    def fetch(url, redirects, *, session_id=None, **kwargs):
        sessions.append(session_id)
        if len(sessions) == 1:
            if outcome == 'network_timeout': raise relay.RelayUpstreamTimeout('TimeoutError')
            return 504, {}, b'timeout', url
        return 200, {}, b'ok', url
    monkeypatch.setattr(relay, '_fetch_once', fetch)
    assert controller.fetch('https://export.arxiv.org/api/query', False)[0] == 200
    assert len(sessions) == 2 and sessions[0] != sessions[1]
    assert controller.active == 0


@pytest.mark.parametrize('case', ['other_host', 'disabled', 'non_timeout', 'http_429', 'retry_after_504'])
def test_timeout_experiment_preserves_other_backoff(monkeypatch, case):
    monkeypatch.setattr(relay, 'IPWO_PROXY_USERNAME', 'test_sid_12345678')
    monkeypatch.setattr(relay, 'ARXIV_IMMEDIATE_TIMEOUT_RETRY', case != 'disabled')
    controller = relay.Controller(0)
    waits = []
    monkeypatch.setattr(controller, '_set_cooldown', waits.append)
    calls = []
    def fetch(url, redirects, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            if case == 'http_429': return 429, {'Retry-After': '12'}, b'limited', url
            if case == 'retry_after_504': return 504, {'Retry-After': '9'}, b'busy', url
            if case == 'non_timeout': raise relay.RelayUpstreamError('ConnectionResetError')
            raise relay.RelayUpstreamTimeout('TimeoutError')
        return 200, {}, b'ok', url
    monkeypatch.setattr(relay, '_fetch_once', fetch)
    url = 'https://en.wikipedia.org/w/api.php' if case == 'other_host' else 'https://export.arxiv.org/api/query'
    assert controller.fetch(url, False)[0] == 200
    assert waits == [12 if case == 'http_429' else 9 if case == 'retry_after_504' else 5]


def test_wrapped_socket_timeout_is_recognized_without_leaking_credentials(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise relay.urllib.error.URLError(TimeoutError('private-proxy-credential'))
    monkeypatch.setattr(relay.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(relay.RelayUpstreamTimeout) as caught:
        relay._fetch_once('https://export.arxiv.org/api/query', False)
    assert str(caught.value) == 'URLError'


def test_four_network_slots_run_while_the_fifth_waits(monkeypatch):
    controller = relay.Controller(0)
    controller.max_concurrency = 4
    release = threading.Event()
    lock = threading.Lock()
    calls = []
    failures = []
    def fetch(url, redirects, **kwargs):
        with lock: calls.append(url)
        assert release.wait(2)
        return 200, {}, b'ok', url
    def run(index):
        try: controller.fetch('https://export.arxiv.org/api/query?i='+str(index), False)
        except Exception as error: failures.append(error)
    monkeypatch.setattr(relay, '_fetch_once', fetch)
    threads = [threading.Thread(target=run, args=(index,)) for index in range(5)]
    try:
        for thread in threads: thread.start()
        wait_until(lambda: len(calls) == 4 and len(controller.pending) == 1)
        assert controller.active == 4
        assert len(calls) == 4
    finally:
        release.set()
        for thread in threads: thread.join(2)
    assert not failures and len(calls) == 5
    assert controller.active == 0 and not controller.pending


@pytest.mark.parametrize("url", [
    "https://example.com/api/query?q=hello",
    "https://new.provider.example:8443/new-api",
    "https://8.8.8.8/",
    "https://[2606:4700:4700::1111]/",
])
def test_any_public_https_destination_is_supported(url):
    assert relay._validate_url(url) == "https"


@pytest.mark.parametrize("url", [
    "https://localhost/", "https://host.docker.internal/", "https://service.local/",
    "https://10.0.0.1/", "https://172.16.0.1/", "https://192.168.0.22/",
    "https://169.254.169.254/", "https://100.64.0.1/", "https://0.0.0.0/",
    "https://[::1]/", "https://[fe80::1]/", "https://[fc00::1]/",
    "https://[::ffff:127.0.0.1]/", "https://224.0.0.1/",
    "https://example.com:0/", "https://@example.com/", "https://example.com/#",
    "https://example.com/\nprivate", "https:///missing-host",
])
def test_private_and_malformed_destinations_are_rejected(url):
    with pytest.raises(relay.RelayTargetError):
        relay._validate_url(url)


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("93.184.216.34", "10.0.0.1")])
def test_dns_must_resolve_only_to_public_addresses(monkeypatch, addresses):
    monkeypatch.setattr(relay.socket, "getaddrinfo", lambda *_a, **_k: [
        (relay.socket.AF_INET, relay.socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ])
    with pytest.raises(relay.RelayTargetError):
        relay._validate_url("https://public-looking.example/api")


def test_cross_domain_public_https_redirect_is_allowed():
    request = relay.urllib.request.Request("https://arxiv.org/source")
    redirected = relay.SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://new-cdn.example:8443/archive/paper",
    )
    assert redirected.full_url == "https://new-cdn.example:8443/archive/paper"


def test_generic_destinations_have_no_extra_rate_or_concurrency_limit():
    first = relay.controller_for_url("https://first.example/a", "https")
    assert first.min_interval == 0
    assert first.max_concurrency is None
    assert relay.controller_for_url("https://first.example/b", "https") is first
    assert relay.controller_for_url("https://second.example/a", "https") is not first


def test_proxy_tunnel_pins_checked_ip_and_restores_tls_hostname(monkeypatch):
    budget = relay.RequestBudget(5)
    assert relay._validate_url("https://new.provider.example/", budget) == "https"
    # A later DNS change cannot change this request's CONNECT destination.
    monkeypatch.setattr(relay.socket, "getaddrinfo", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("DNS rebound")))
    observed = []
    monkeypatch.setattr(relay.http.client.HTTPConnection, "_tunnel", lambda self: observed.append((self._tunnel_host, dict(self._tunnel_headers))))
    connection = relay.BudgetHTTPSConnection("proxy.example", budget=budget)
    connection.set_tunnel("new.provider.example", 443, headers={"Proxy-Authorization": "private-test", "Host": "new.provider.example:443"})
    connection._tunnel()
    assert observed == [("93.184.216.34", {"Proxy-Authorization": "private-test", "Host": "93.184.216.34:443"})]
    assert connection._tunnel_host == "new.provider.example"
    assert connection._tunnel_headers["Host"] == "new.provider.example:443"


def test_dns_lookup_obeys_request_deadline(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(relay.socket, "getaddrinfo", lambda *_a, **_k: release.wait(1))
    try:
        with pytest.raises(relay.RelayRequestExpired):
            relay._validate_url("https://slow-dns.example/", relay.RequestBudget(.03))
    finally:
        release.set()


def test_failure_response_diagnostics_are_metadata_only():
    import hashlib
    import json
    private = "secret-key-with-private-query"
    body = ("<?xml version='1.0'?><eSearchResult><ERROR>Invalid API key: " + private + "</ERROR></eSearchResult>").encode()
    metadata = relay._failure_response_metadata(500, {
        "Content-Type": "text/xml; charset=UTF-8", "Server": "envoy",
        "Via": "1.1 private-proxy-" + private, "X-Request-Id": private,
        "Set-Cookie": private, "Location": "https://example.test?api_key=" + private,
    }, body)
    assert metadata["response_error_reason"] == "invalid_api_key"
    assert metadata["response_source_hint"] == "ncbi_xml"
    assert metadata["response_content_type"] == "text/xml"
    assert metadata["response_server_product"] == "envoy"
    assert metadata["response_via_present"] is True
    assert metadata["response_body_sha256"] == hashlib.sha256(body).hexdigest()
    assert private not in json.dumps(metadata)
    assert "<ERROR>" not in json.dumps(metadata)
    assert relay._failure_response_metadata(200, {}, body) == {}


def test_relay_error_diagnostics_do_not_echo_untrusted_headers_or_body():
    import json
    private = "do-not-persist-key-or-query"
    metadata = relay._failure_response_metadata(500, {
        "Content-Type": private, "Server": private, "Via": private,
    }, ("<html>ERR_CONNECT_FAIL " + private + "</html>").encode())
    assert metadata["response_source_hint"] == "proxy_document"
    assert metadata["response_error_reason"] == "proxy_error"
    assert metadata["response_content_type"] == "other"
    assert metadata["response_server_product"] == "unknown"
    assert private not in json.dumps(metadata)


def test_error_diagnostics_keep_three_attempts_and_the_original_response(monkeypatch):
    records = []
    calls = []
    original = (500, {"Content-Type": "text/xml"}, b"<eSearchResult><ERROR>temporary backend failure</ERROR></eSearchResult>", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")
    def fetch(*args, **kwargs):
        calls.append(args)
        return original
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    monkeypatch.setattr(relay, "_append_log", records.append)
    controller = relay.Controller(0)
    monkeypatch.setattr(controller, "_set_cooldown", lambda _seconds: None)
    result = controller.fetch(original[3], False)
    assert len(calls) == 3
    assert result[:4] == original
    assert result[4] == 3
    finished = [r for r in records if r["event"] == "http_finished"]
    assert len(finished) == 3
    assert all(r["response_source_hint"] == "ncbi_xml" for r in finished)
    assert all(r["response_error_reason"] == "ncbi_application_error" for r in finished)


def test_overpass_failure_cools_only_that_node_and_returns_original_response(monkeypatch):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_sid_12345678")
    calls, records = [], []
    bad = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    good = "https://overpass-api.de/api/interpreter"
    def fetch(url, *args, **kwargs):
        calls.append(url)
        return (504, {"Content-Type": "text/plain"}, b"upstream busy", url) if url == bad else (200, {}, b'{"elements":[]}', url)
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    monkeypatch.setattr(relay, "_append_log", records.append)
    controller = relay.Controller(0)
    failure = controller.fetch(bad, False)
    assert failure[:4] == (504, {"Content-Type": "text/plain"}, b"upstream busy", bad)
    assert failure[4] == 1
    assert controller.fetch(good, False)[0] == 200
    with pytest.raises(relay.RelayEndpointCooling):
        controller.fetch(bad, False)
    assert calls == [bad, good]
    assert controller.active == 0
    assert not controller.pending
    assert controller.blocked_until == 0
    assert any(r["event"] == "endpoint_cooldown" for r in records)


def test_overpass_network_failure_leaves_backup_node_usable(monkeypatch):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_sid_12345678")
    calls = []
    bad = "https://overpass.private.coffee/api/interpreter"
    good = "https://overpass-api.de/api/interpreter"
    def fetch(url, *args, **kwargs):
        calls.append(url)
        if url == bad:
            raise relay.RelayUpstreamTimeout("network timeout")
        return 200, {}, b"ok", url
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    controller = relay.Controller(0)
    with pytest.raises(relay.RelayUpstreamTimeout):
        controller.fetch(bad, False)
    assert controller.fetch(good, False)[0] == 200
    assert calls == [bad, good]
    assert controller.active == 0


def test_overpass_node_cooldown_does_not_drop_shared_spacing_or_capacity(monkeypatch):
    import time
    controller = relay.Controller(0.035)
    controller.max_concurrency = 1
    started, release = threading.Event(), threading.Event()
    first = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    second = "https://overpass-api.de/api/interpreter"
    calls = []
    def fetch(url, *args, **kwargs):
        calls.append((url, time.monotonic()))
        if url == first:
            started.set()
            assert release.wait(2)
        return 200, {}, b"ok", url
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    result, errors = [], []
    def run(url):
        try:result.append(controller.fetch(url, False))
        except Exception as exc:errors.append(exc)
    a = threading.Thread(target=run, args=(first,))
    b = threading.Thread(target=run, args=(second,))
    a.start()
    assert started.wait(2)
    b.start()
    wait_until(lambda: len(controller.pending) == 1)
    assert len(calls) == 1
    release.set()
    a.join(2)
    b.join(2)
    assert not errors and len(result) == 2
    assert calls[1][1] - calls[0][1] >= 0.025
    assert controller.active == 0 and not controller.pending


def test_overpass_429_retains_retry_after_for_only_that_node(monkeypatch):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_sid_12345678")
    calls = []
    first = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    second = "https://overpass-api.de/api/interpreter"
    def fetch(url, *args, **kwargs):
        calls.append(url)
        return (429, {"Retry-After": "120"}, b"limited", url) if url == first else (200, {}, b"ok", url)
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    controller = relay.Controller(0)
    result = controller.fetch(first, False)
    assert result[:3] == (429, {"Retry-After": "120"}, b"limited")
    assert controller.node_blocked_until[("maps.mail.ru", 443)] - relay.time.monotonic() > 119
    assert controller.fetch(second, False)[0] == 200
    assert calls == [first, second]



def test_overpass_upstream_deadline_cools_node_but_queue_cancellation_does_not(monkeypatch):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_sid_12345678")
    bad = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    good = "https://overpass-api.de/api/interpreter"
    def fetch(url, *args, **kwargs):
        if url == bad:
            raise relay.RelayRequestExpired("upstream")
        return 200, {}, b"ok", url
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    controller = relay.Controller(0)
    with pytest.raises(relay.RelayRequestExpired):
        controller.fetch(bad, False)
    assert ("maps.mail.ru", 443) in controller.node_blocked_until
    assert controller.fetch(good, False)[0] == 200
    assert controller.active == 0
    cancelled = relay.RequestBudget(1, disconnected=lambda: True)
    with pytest.raises(relay.RelayRequestCancelled):
        controller.fetch(good, False, budget=cancelled)
    assert ("overpass-api.de", 443) not in controller.node_blocked_until
    assert not controller.pending


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_rotating_overpass_failure_does_not_pause_other_exits(monkeypatch, status):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_rotating")
    calls, records = [], []
    url = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    def fetch(target, *args, **kwargs):
        calls.append(target)
        if len(calls) == 1:
            return status, {"Retry-After": "120"}, b"temporary failure", target
        return 200, {}, b"ok", target
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    monkeypatch.setattr(relay, "_append_log", records.append)
    controller = relay.Controller(0)
    assert controller.fetch(url, False)[0] == status
    assert controller.fetch(url, False)[0] == 200
    assert calls == [url, url]
    assert not controller.node_blocked_until
    assert controller.blocked_until == 0
    assert not any(r["event"] in {"cooldown", "endpoint_cooldown"} for r in records)
    assert controller.active == 0 and not controller.pending


@pytest.mark.parametrize("failure", [
    relay.RelayUpstreamTimeout("network timeout"),
    relay.RelayUpstreamError("connection refused"),
    relay.RelayRequestExpired("upstream"),
])
def test_rotating_overpass_network_error_does_not_poison_next_request(monkeypatch, failure):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_rotating")
    url = "https://overpass-api.de/api/interpreter"
    calls = []
    def fetch(target, *args, **kwargs):
        calls.append(target)
        if len(calls) == 1:
            raise failure
        return 200, {}, b"ok", target
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    controller = relay.Controller(0)
    with pytest.raises(type(failure)):
        controller.fetch(url, False)
    assert controller.fetch(url, False)[0] == 200
    assert not controller.node_blocked_until and controller.blocked_until == 0
    assert len(calls) == 2


def test_rotating_overpass_ignores_existing_node_cooldown(monkeypatch):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_rotating")
    url = "https://overpass-api.de/api/interpreter"
    controller = relay.Controller(0)
    controller.node_blocked_until[("overpass-api.de", 443)] = relay.time.monotonic() + 120
    monkeypatch.setattr(relay, "_fetch_once", lambda target, *args, **kwargs: (200, {}, b"ok", target))
    assert controller.fetch(url, False)[0] == 200


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_rotating_arxiv_retries_three_times_without_shared_cooldown(monkeypatch, status):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_rotating")
    calls, records = [], []
    url = "https://export.arxiv.org/api/query"
    def fetch(target, *args, **kwargs):
        calls.append(target)
        return status, {"Retry-After": "120"}, b"unavailable", target
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    monkeypatch.setattr(relay, "_append_log", records.append)
    controller = relay.Controller(0)
    result = controller.fetch(url, False)
    assert result[:4] == (status, {"Retry-After": "120"}, b"unavailable", url)
    assert result[4] == 3 and len(calls) == 3
    assert controller.blocked_until == 0 and not controller.node_blocked_until
    assert not any(r["event"] == "cooldown" for r in records)
    assert len([r for r in records if r["event"] == "retry_scheduled"]) == 2
    monkeypatch.setattr(relay, "_fetch_once", lambda target, *a, **k: (200, {}, b"ok", target))
    assert controller.fetch(url, False)[0] == 200


@pytest.mark.parametrize("failure", [relay.RelayUpstreamTimeout("timeout"), relay.RelayUpstreamError("reset")])
def test_rotating_arxiv_network_retry_retains_three_attempt_cap(monkeypatch, failure):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_rotating")
    monkeypatch.setattr(relay, "ARXIV_IMMEDIATE_TIMEOUT_RETRY", False)
    calls = []
    def fetch(target, *args, **kwargs):
        calls.append(target)
        raise failure
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    controller = relay.Controller(0)
    with pytest.raises(type(failure)):
        controller.fetch("https://export.arxiv.org/api/query", False)
    assert len(calls) == 3 and controller.blocked_until == 0
    assert controller.active == 0 and not controller.pending


def test_rotating_arxiv_deadline_still_prevents_additional_attempts(monkeypatch):
    monkeypatch.setattr(relay, "IPWO_PROXY_USERNAME", "test_rotating")
    calls = []
    def fetch(target, *args, budget=None, **kwargs):
        calls.append(target)
        budget.deadline = relay.time.monotonic() - 1
        return 429, {}, b"limited", target
    monkeypatch.setattr(relay, "_fetch_once", fetch)
    controller = relay.Controller(0)
    with pytest.raises(relay.RelayRequestExpired):
        controller.fetch("https://export.arxiv.org/api/query", False)
    assert len(calls) == 1 and controller.blocked_until == 0
