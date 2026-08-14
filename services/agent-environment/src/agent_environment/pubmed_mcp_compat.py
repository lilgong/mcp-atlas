"""Rate-safe PubMed MCP implementation with the upstream public tool schema.

The upstream server performs one ESearch followed by one EFetch per PMID. A
single default call therefore emits eleven unpaced requests. This module keeps
the same four MCP tools while batching metadata retrieval and pacing NCBI
E-utilities traffic.
"""

from __future__ import annotations

import base64
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PMC_BASE = "https://www.ncbi.nlm.nih.gov/pmc/articles"
REQUEST_INTERVAL_SECONDS = 0.4
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULTS = 100
RELAY_URL = (os.getenv("PUBMED_RELAY_URL") or "").strip().rstrip("/")
RELAY_TOKEN = (os.getenv("PUBMED_RELAY_TOKEN") or "").strip()
RELAY_TIMEOUT_SECONDS = float(
    os.getenv("PUBMED_RELAY_TIMEOUT_SECONDS") or "90"
)


class PubMedUpstreamError(RuntimeError):
    """A concise, actionable failure returned by NCBI or PMC."""


class RequestPacer:
    def __init__(self, interval: float = REQUEST_INTERVAL_SECONDS) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._last_started = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self.interval - (time.monotonic() - self._last_started)
            if delay > 0:
                time.sleep(delay)
            self._last_started = time.monotonic()


_PACER = RequestPacer()
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "mcp_atlas_eval/1.0"})


def _request_via_relay(
    url: str,
    *,
    params: dict[str, Any],
    allow_redirects: bool,
) -> requests.Response:
    if not RELAY_TOKEN:
        raise PubMedUpstreamError(
            "PUBMED_RELAY_URL is set but PUBMED_RELAY_TOKEN is empty"
        )
    try:
        relay_response = _SESSION.post(
            f"{RELAY_URL}/v1/fetch",
            headers={"Authorization": f"Bearer {RELAY_TOKEN}"},
            json={
                "url": url,
                "params": params,
                "allow_redirects": allow_redirects,
            },
            timeout=RELAY_TIMEOUT_SECONDS,
        )
        if relay_response.status_code == 402:
            try:
                error = str(relay_response.json().get("error") or "")
            except (requests.JSONDecodeError, AttributeError, TypeError, ValueError):
                error = ""
            if error.startswith((
                "SCRAPERAPI_CREDITS_EXHAUSTED:",
                "SCRAPERAPI_INVALID_KEY:",
            )):
                raise PubMedUpstreamError(error)
            raise PubMedUpstreamError(
                "SCRAPERAPI_ACCOUNT_REJECTED: PubMed relay account is unavailable"
            )
        relay_response.raise_for_status()
        payload = relay_response.json()
        content = base64.b64decode(payload["body_base64"], validate=True)
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        raise PubMedUpstreamError(f"PubMed relay request failed: {exc}") from exc

    response = requests.Response()
    response.status_code = int(payload["status_code"])
    response.headers.update(payload.get("headers") or {})
    response._content = content
    response.url = str(payload.get("url") or url)
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    return response


def _identity() -> dict[str, str]:
    params = {"tool": (os.getenv("NCBI_TOOL") or "mcp_atlas_eval").strip()}
    email = (os.getenv("NCBI_EMAIL") or "").strip()
    api_key = (os.getenv("NCBI_API_KEY") or "").strip()
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key
    return params


def _is_blocked(response: requests.Response) -> bool:
    location = response.headers.get("Location", "").lower()
    sample = response.text[:4096].lower() if response.content else ""
    return (
        "misuse.ncbi.nlm.nih.gov" in location
        or "blocked for possible abuse" in sample
        or ("access denied" in sample and "ncbi" in sample)
    )


def _request(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    eutils: bool = False,
    allow_redirects: bool = False,
) -> requests.Response:
    merged = dict(params or {})
    if eutils:
        merged.update(_identity())
    if RELAY_URL:
        response = _request_via_relay(
            url,
            params=merged,
            allow_redirects=allow_redirects,
        )
        if _is_blocked(response):
            raise PubMedUpstreamError(
                "NCBI blocked the relay egress IP for possible abuse; "
                "stop PubMed traffic and request an unblock"
            )
        if response.is_redirect and not allow_redirects:
            raise PubMedUpstreamError(
                "NCBI returned an unexpected redirect to "
                f"{response.headers.get('Location', '<unknown>')}"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise PubMedUpstreamError(
                f"NCBI returned HTTP {response.status_code} after relay retries"
            )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PubMedUpstreamError(
                f"upstream returned HTTP {response.status_code}"
            ) from exc
        return response

    last_error: Exception | None = None
    for attempt in range(3):
        _PACER.wait()
        try:
            response = _SESSION.get(
                url,
                params=merged,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=allow_redirects,
            )
        except requests.RequestException as exc:
            last_error = exc
        else:
            if _is_blocked(response):
                raise PubMedUpstreamError(
                    "NCBI blocked this public egress IP for possible abuse; "
                    "stop PubMed traffic and request an unblock"
                )
            if response.is_redirect and not allow_redirects:
                raise PubMedUpstreamError(
                    "NCBI returned an unexpected redirect to "
                    f"{response.headers.get('Location', '<unknown>')}"
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = PubMedUpstreamError(
                    f"NCBI returned HTTP {response.status_code}"
                )
            else:
                try:
                    response.raise_for_status()
                except requests.RequestException as exc:
                    raise PubMedUpstreamError(
                        f"upstream returned HTTP {response.status_code}"
                    ) from exc
                return response
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise PubMedUpstreamError(f"upstream request failed: {last_error}")


def _request_xml(endpoint: str, params: dict[str, Any]) -> ET.Element:
    response = _request(
        f"{EUTILS_BASE}/{endpoint}.fcgi",
        params={**params, "retmode": "xml"},
        eutils=True,
    )
    if "html" in response.headers.get("Content-Type", "").lower():
        raise PubMedUpstreamError("NCBI returned HTML instead of E-utilities XML")
    try:
        return ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise PubMedUpstreamError(f"NCBI returned invalid XML: {exc}") from exc


def _text(node: ET.Element | None, default: str = "") -> str:
    if node is None:
        return default
    value = "".join(node.itertext()).strip()
    return re.sub(r"\s+", " ", value) if value else default


def _metadata(article: ET.Element) -> dict[str, str] | None:
    citation = article.find(".//MedlineCitation")
    if citation is None:
        return None
    pmid = _text(citation.find("PMID"))
    article_node = citation.find("Article")
    if not pmid or article_node is None:
        return None
    authors = []
    for author in article_node.findall(".//AuthorList/Author"):
        name = _text(author.find("CollectiveName")) or _text(
            author.find("LastName")
        )
        if name:
            authors.append(name)
    pub_date = article_node.find(".//Journal/JournalIssue/PubDate")
    year = _text(pub_date.find("Year") if pub_date is not None else None)
    if not year and pub_date is not None:
        match = re.search(r"\b(?:19|20)\d{2}\b", _text(pub_date))
        year = match.group(0) if match else ""
    abstract_parts = [
        _text(item) for item in article_node.findall(".//Abstract/AbstractText")
    ]
    return {
        "PMID": pmid,
        "Title": _text(article_node.find("ArticleTitle"), "No title available"),
        "Authors": ", ".join(authors) if authors else "No authors available",
        "Journal": _text(
            article_node.find(".//Journal/Title"), "No journal available"
        ),
        "Publication Date": year or "No publication date available",
        "Abstract": " ".join(filter(None, abstract_parts))
        or "No abstract available",
    }


def _search_ids(term: str, num_results: int) -> list[str]:
    root = _request_xml(
        "esearch",
        {
            "db": "pubmed",
            "term": term,
            "retmax": max(1, min(int(num_results), MAX_RESULTS)),
        },
    )
    return [node.text for node in root.findall("./IdList/Id") if node.text]


def _fetch(pmids: list[str]) -> tuple[ET.Element, list[dict[str, str]]]:
    if not pmids:
        return ET.Element("PubmedArticleSet"), []
    root = _request_xml("efetch", {"db": "pubmed", "id": ",".join(pmids)})
    rows = [
        item
        for article in root.findall(".//PubmedArticle")
        if (item := _metadata(article)) is not None
    ]
    return root, rows


def search_key_words(key_words: str, num_results: int = 10) -> list[dict[str, str]]:
    return _fetch(_search_ids(key_words, num_results))[1]


def search_advanced(
    term: str | None = None,
    title: str | None = None,
    author: str | None = None,
    journal: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    num_results: int = 10,
) -> list[dict[str, str]]:
    parts = []
    if term:
        parts.append(term)
    if title:
        parts.append(f"{title}[Title]")
    if author:
        parts.append(f"{author}[Author]")
    if journal:
        parts.append(f"{journal}[Journal]")
    if start_date and end_date:
        parts.append(f"{start_date}:{end_date}[Date - Publication]")
    return _fetch(_search_ids(" AND ".join(parts), num_results))[1]


def get_metadata(pmid: str | int) -> dict[str, str] | None:
    return next(iter(_fetch([str(pmid)])[1]), None)


def download_pdf(pmid: str | int) -> str:
    pmid_text = str(pmid)
    root, _ = _fetch([pmid_text])
    pmc_node = root.find(".//ArticleId[@IdType='pmc']")
    if pmc_node is None or not pmc_node.text:
        return (
            f"No PMC ID found for PMID: {pmid_text}\n"
            f"You can check the article availability at: "
            f"https://pubmed.ncbi.nlm.nih.gov/{pmid_text}/"
        )
    pmc_id = pmc_node.text
    response = _request(f"{PMC_BASE}/{pmc_id}/pdf/", allow_redirects=True)
    content_type = response.headers.get("Content-Type", "").lower()
    if "pdf" not in content_type and not response.content.startswith(b"%PDF"):
        return (
            f"No downloadable PDF found for PMID: {pmid_text}\n"
            f"You can check the article availability at: {PMC_BASE}/{pmc_id}/"
        )
    output = Path("/data") / f"PMID_{pmid_text}_PMC_{pmc_id}.pdf"
    output.write_bytes(response.content)
    return f"PDF for PMID {pmid_text} has been downloaded as {output}"


mcp = FastMCP("pubmed")


@mcp.tool()
def search_pubmed_key_words(
    key_words: str, num_results: int = 10
) -> list[dict[str, str]]:
    try:
        return search_key_words(key_words, num_results)
    except Exception as exc:
        return [{"error": f"An error occurred while searching: {exc}"}]


@mcp.tool()
def search_pubmed_advanced(
    term: str | None = None,
    title: str | None = None,
    author: str | None = None,
    journal: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    num_results: int = 10,
) -> list[dict[str, str]]:
    try:
        return search_advanced(
            term, title, author, journal, start_date, end_date, num_results
        )
    except Exception as exc:
        return [{"error": f"An error occurred while performing advanced search: {exc}"}]


@mcp.tool()
def get_pubmed_article_metadata(pmid: str | int) -> dict[str, str]:
    try:
        return get_metadata(pmid) or {"error": f"No metadata found for PMID: {pmid}"}
    except Exception as exc:
        return {"error": f"An error occurred while fetching metadata: {exc}"}


@mcp.tool()
def download_pubmed_pdf(pmid: str | int) -> str:
    try:
        return download_pdf(pmid)
    except Exception as exc:
        return f"An error occurred while attempting to download the PDF: {exc}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
