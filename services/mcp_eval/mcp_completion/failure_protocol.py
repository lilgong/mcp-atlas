"""Stable, non-secret failure receipts for rollout clients."""

from __future__ import annotations

import re
from typing import Any


_TRANSIENT_RE = re.compile(
    r"rate limit|too many requests|timed? out|timeout|temporar|service unavailable|"
    r"connection (?:reset|refused)|http (?:408|409|429|5(?:02|03|04))\b|"
    r"status(?:_code)?[=: ]+(?:408|409|429|502|503|504)\b",
    re.I,
)
_CONTEXT_LIMIT_RE = re.compile(
    r"context (?:length|window)|maximum context|too many tokens|token limit|"
    r"request too large|max(?:imum)?[_ ]tokens",
    re.I,
)


def failure_receipt(
    *,
    failure_class: str,
    reason_code: str,
    retryable: bool,
    source_kind: str,
    source_name: str | None = None,
    detail_code: str | None = None,
) -> dict[str, Any]:
    """Build the versioned public failure shape without raw exception text."""
    receipt: dict[str, Any] = {
        "protocol_version": 1,
        "failure_class": failure_class,
        "reason_code": reason_code,
        "retryable": retryable,
        "source_kind": source_kind,
    }
    if source_name:
        receipt["source_name"] = source_name
    if detail_code:
        receipt["detail_code"] = detail_code
    return receipt


def classify_model_exception(error: BaseException, *, model: str) -> dict[str, Any]:
    """Conservatively classify a terminal model-call exception.

    Unknown failures are terminal infrastructure failures. Retrying an opaque
    error is more likely to duplicate spend than to recover, so only explicit
    transient signals are retryable.
    """
    text = str(error)
    if _CONTEXT_LIMIT_RE.search(text):
        return failure_receipt(
            failure_class="infrastructure",
            reason_code="model_context_length_exceeded",
            retryable=False,
            source_kind="model",
            source_name=model,
            detail_code=type(error).__name__,
        )
    if _TRANSIENT_RE.search(text):
        return failure_receipt(
            failure_class="transient",
            reason_code="model_transient_error",
            retryable=True,
            source_kind="model",
            source_name=model,
            detail_code=type(error).__name__,
        )
    return failure_receipt(
        failure_class="infrastructure",
        reason_code="model_terminal_error",
        retryable=False,
        source_kind="model",
        source_name=model,
        detail_code=type(error).__name__,
    )
