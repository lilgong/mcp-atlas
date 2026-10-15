from mcp_completion.failure_protocol import classify_model_exception, failure_receipt


def test_failure_receipt_has_stable_non_secret_shape():
    assert failure_receipt(
        failure_class="quality",
        reason_code="max_turns_reached",
        retryable=False,
        source_kind="agent",
    ) == {
        "protocol_version": 1,
        "failure_class": "quality",
        "reason_code": "max_turns_reached",
        "retryable": False,
        "source_kind": "agent",
    }


def test_context_limit_is_terminal_infrastructure():
    receipt = classify_model_exception(
        RuntimeError("maximum context length exceeded for this request"),
        model="teacher-model",
    )
    assert receipt["failure_class"] == "infrastructure"
    assert receipt["reason_code"] == "model_context_length_exceeded"
    assert receipt["retryable"] is False
    assert "maximum context" not in str(receipt)


def test_explicit_gateway_timeout_is_transient():
    receipt = classify_model_exception(
        RuntimeError("HTTP 504 upstream temporarily unavailable"),
        model="teacher-model",
    )
    assert receipt["failure_class"] == "transient"
    assert receipt["reason_code"] == "model_transient_error"
    assert receipt["retryable"] is True


def test_unknown_model_error_is_not_automatically_retried():
    receipt = classify_model_exception(RuntimeError("opaque provider failure"), model="m")
    assert receipt["failure_class"] == "infrastructure"
    assert receipt["retryable"] is False
