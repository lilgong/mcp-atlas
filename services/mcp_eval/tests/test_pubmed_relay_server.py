from __future__ import annotations

import importlib.util
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


@pytest.mark.parametrize(
    ("status", "marker"),
    [
        (401, "SCRAPERAPI_INVALID_KEY"),
        (407, "SCRAPERAPI_INVALID_KEY"),
        (403, "SCRAPERAPI_CREDITS_EXHAUSTED"),
    ],
)
def test_scraperapi_account_failure_latches_without_more_requests(
    monkeypatch, status, marker,
):
    calls = []

    def fake_fetch(url, allow_redirects):
        calls.append((url, allow_redirects))
        return status, {}, b"provider rejection", url

    monkeypatch.setattr(relay, "_fetch_once", fake_fetch)
    controller = relay.Controller()

    with pytest.raises(relay.ScraperAPIAccountError, match=marker):
        controller.fetch(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            True,
        )
    with pytest.raises(relay.ScraperAPIAccountError, match=marker):
        controller.fetch(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            True,
        )

    assert len(calls) == 1
    assert marker in str(controller.current_account_error())
