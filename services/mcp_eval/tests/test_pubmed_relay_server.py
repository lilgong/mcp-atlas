from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "pubmed_relay_server",
    ROOT / "scripts" / "pubmed_relay_server.py",
)
assert SPEC and SPEC.loader
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)


def test_ipwo_proxy_auth_failure_latches(monkeypatch):
    calls = []

    def fake_fetch(url, allow_redirects, *, session_id=None):
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

    def fake_fetch(url, allow_redirects, *, session_id=None):
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
        lambda _url, _allow_redirects, *, session_id=None: (
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

    def fake_fetch(_url, _allow_redirects, *, session_id=None):
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


def test_ipwo_username_adds_sticky_suffix_to_rotating_account(monkeypatch):
    monkeypatch.setattr(
        relay,
        "IPWO_PROXY_USERNAME",
        "account_custom_zone_US",
    )
    monkeypatch.setattr(relay, "IPWO_PROXY_COUNTRY", "US")

    assert relay._ipwo_username_for_request("10001234") == (
        "account_custom_zone_US_sid_10001234_time_10"
    )


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

    def fake_fetch(_url, _allow_redirects, *, session_id=None):
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

    def fake_fetch(url, _allow_redirects, *, session_id=None):
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


def test_arxiv_controller_caps_concurrent_upstream_requests(monkeypatch):
    release = threading.Event()
    two_started = threading.Event()
    third_started = threading.Event()
    lock = threading.Lock()
    started = 0

    def fake_fetch(url, _allow_redirects, *, session_id=None):
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
    controller = relay.Controller(
        0, max_in_flight=2, adaptive_rate_limit=True,
    )
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


def test_arxiv_controller_reduces_on_429_then_recovers():
    controller = relay.Controller(
        0, max_in_flight=2, adaptive_rate_limit=True,
    )

    controller._wait_for_slot()
    controller._finish_attempt(status=429)
    assert controller.current_limit() == 1
    for expected in (1, 1, 2):
        controller._wait_for_slot()
        controller._finish_attempt(status=200)
        assert controller.current_limit() == expected
    assert controller.current_limit() == 2


def test_wikipedia_controller_pool_uses_independent_sticky_lanes(monkeypatch):
    url = "https://en.wikipedia.org/w/api.php"
    session_ids = []

    def fake_fetch(_url, _allow_redirects, *, session_id=None):
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

    def fake_fetch(_url, _allow_redirects, *, session_id=None):
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
        ("https://arxiv.org/e-print/2501.00001", "arxiv"),
        ("https://arxiv.org/src/2501.00001", "arxiv"),
        ("https://www.arxiv.org/src/2501.00001", "arxiv"),
        ("https://nominatim.openstreetmap.org/search", "osm-nominatim"),
        ("https://overpass-api.de/api/interpreter", "osm-overpass"),
        ("https://router.project-osrm.org/route/v1/car/1,2;3,4", "osm-routing"),
    ],
)
def test_relay_allows_only_named_arxiv_and_osm_paths(url, group):
    assert relay._validate_url(url) == group


@pytest.mark.parametrize(
    "url",
    [
        "https://export.arxiv.org/unknown",
        "https://nominatim.openstreetmap.org/status.php",
        "https://overpass-api.de/",
        "https://example.com/api/query",
    ],
)
def test_relay_rejects_unapproved_arxiv_and_osm_paths(url):
    with pytest.raises(ValueError, match="allowed MCP egress"):
        relay._validate_url(url)


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
