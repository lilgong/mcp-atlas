"""Keep known public benchmark answers out of model-visible tool traffic.

This is a deterministic source policy, not a network sandbox. Arbitrary code
and external execution services still require independent egress controls.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import unquote

from .schema import CallToolResponse, TextContent

POLICY_VERSION = "atlas-answer-access-v2"
DENIED = "Access denied: benchmark answer materials are not available during evaluation."
FILTERED = "[Benchmark answer sources were filtered from this tool result.]"
# Repository paths cover GitHub API/raw URLs, mirrors, git clones and search queries.
_REPOSITORY = re.compile(
    r"(?<![\w.-])[\w.-]+/(?:mcp[-_]atlas(?:-harbor)?|mcp-atlas-easy)(?![\w.-])",
    re.IGNORECASE,
)
# Observed republications; keep these exact instead of matching every
# repository with an "atlas" suffix or every ScaleAI dataset.
_REPUBLICATION = re.compile(
    r"(?<![\w.-])(?:iflow-mcp/scaleapi-mcp-atlas|scaleai/(?:dummy_mcp|lhaw))(?![\w.-])",
    re.IGNORECASE,
)
_CACHED_DATASET = re.compile(r"(?<![\w.-])mcp[-_]atlas\.(?:parquet|csv)(?![\w.-])", re.IGNORECASE)
_SEARCH_ITEMS = re.compile(r"(?m)(?=^Title:\s|^\d+\.\s+(?:\*\*|\[))")


def _normalize(text: str) -> str:
    text = html.unescape(text).replace('\\/', '/')
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def protected_source(value: Any) -> bool:
    if isinstance(value, dict):
        owner = value.get('owner') or value.get('organization')
        repo = value.get('repo') or value.get('repository')
        if isinstance(owner, str) and isinstance(repo, str) and protected_source(f'{owner}/{repo}'):
            return True
        return any(protected_source(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(protected_source(v) for v in value)
    if not isinstance(value, str):
        return False
    text = _normalize(value)
    folded = text.casefold()
    if "atlas" not in folded and "dummy_mcp" not in folded and "lhaw" not in folded:
        return False
    return bool(_REPOSITORY.search(text) or _REPUBLICATION.search(text))


def request_is_protected(tool_name: str, arguments: Any) -> bool:
    if protected_source(arguments):
        return True
    # Locally cached copies may no longer contain an explicit origin URL.
    if any(word in tool_name.lower() for word in ('execute', 'run_code', 'run-code', 'run_command', 'read_file', 'read_text')):
        text = json.dumps(arguments, ensure_ascii=False) if not isinstance(arguments, str) else arguments
        return bool(_CACHED_DATASET.search(_normalize(text)))
    return False


def _filter(value: Any) -> tuple[Any, bool, bool]:
    """Return (safe value, changed, contains retained payload)."""
    if isinstance(value, list):
        kept, changed = [], False
        for item in value:
            # A search hit must remain whole: never retain its answer snippet
            # after removing only the URL/repository that identifies it.
            if protected_source(item):
                changed = True
                continue
            kept.append(item)
        return kept, changed, bool(kept) if changed else True
    if isinstance(value, dict):
        identity = {key: item for key, item in value.items() if key in {
            'url', 'link', 'uri', 'href', 'path', 'repository', 'repo', 'owner',
            'organization', 'full_name', 'source',
        }}
        if protected_source(identity):
            return None, True, False
        safe, changed, retained = {}, False, False
        payload_removed = False
        for key, item in value.items():
            filtered, removed, useful = _filter(item)
            changed |= removed
            if removed:
                payload_removed = True
                retained |= useful
            if useful:
                safe[key] = filtered
                if key not in {'total', 'count', 'total_results', 'total_count', 'status', 'success'}:
                    retained |= bool(filtered)
        return safe, changed, retained if payload_removed else True
    if not isinstance(value, str) or not protected_source(value):
        return value, False, True
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (list, dict)):
        safe, changed, useful = _filter(parsed)
        return json.dumps(safe, ensure_ascii=False) if useful else None, changed, useful
    parts = _SEARCH_ITEMS.split(value)
    if len(parts) > 1:
        header = parts[0]
        hits = [part for part in parts[1:] if not protected_source(part)]
        if hits and not protected_source(header):
            # Search totals describe the original response; avoid retaining a
            # count that now refers to removed entries.
            header = re.sub(r'Found \d+ (code results|repositories)',
                            lambda m: f'Found {len(hits)} {m[1]}', header)
            return header + ''.join(hits), True, True
    return None, True, False


def filter_response(response: CallToolResponse) -> tuple[CallToolResponse, bool]:
    content, changed, useful = [], False, False
    for block in response.content:
        if isinstance(block, TextContent):
            safe, removed, retained = _filter(block.text)
            changed |= removed
            if retained:
                content.append(block.model_copy(update={'text': safe}))
                useful |= bool(safe.strip())
        else:
            data = block.model_dump()
            if protected_source(data):
                changed = True
            else:
                content.append(block)
                useful = True
    if not changed:
        return response, False
    if not useful:
        return CallToolResponse(content=[TextContent(type='text', text=DENIED)], is_error=True), True
    content.append(TextContent(type='text', text=FILTERED))
    return response.model_copy(update={'content': content}), True
