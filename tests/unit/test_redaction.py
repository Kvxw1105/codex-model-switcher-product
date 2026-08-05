import json
from typing import Any

from codex_model_switcher.credentials import (
    build_safe_log_record,
    redact_exception,
    redact_sensitive,
    redact_sse_fragment,
    summarize_subprocess_env,
)


def assert_secret_absent(value: Any, secret: str) -> None:
    if secret in repr(value):
        raise AssertionError("sensitive fixture leaked")


def test_recursive_redactor_covers_headers_query_and_nested_json() -> None:
    secret = "fixture-redaction-secret-value"
    payload = {
        "headers": {
            "Authorization": f"Bearer {secret}",
            "X-Trace-Id": "trace-123",
        },
        "query": {"access_token": secret, "page": "1"},
        "json": {
            "nested": {"api_key": secret, "message": "safe"},
            "account_email": "account@example.invalid",
            "redirect_uri": "https://api.example.invalid/callback",
        },
    }

    redacted = redact_sensitive(payload)

    assert redacted["headers"]["X-Trace-Id"] == "trace-123"
    assert redacted["query"]["page"] == "1"
    assert_secret_absent(redacted, secret)
    assert "account@example.invalid" not in repr(redacted)
    assert "api.example.invalid" not in repr(redacted)


def test_exception_redaction_hides_secret_url_email_and_nested_attributes() -> None:
    secret = "fixture-exception-secret-value"

    class LeakyError(RuntimeError):
        def __init__(self) -> None:
            super().__init__(f"request failed with {secret}")
            self.request_url = "https://api.example.invalid/v1"
            self.account_email = "account@example.invalid"
            self.details = {"Authorization": f"Bearer {secret}"}

    redacted = redact_exception(LeakyError())

    assert_secret_absent(redacted, secret)
    assert "api.example.invalid" not in repr(redacted)
    assert "account@example.invalid" not in repr(redacted)


def test_sse_redaction_hides_authorization_in_event_and_data_json() -> None:
    secret = "fixture-sse-secret-value"
    fragment = (
        "event: response.delta\n"
        f'data: {json.dumps({"Authorization": f"Bearer {secret}", "text": "safe"})}\n\n'
    )

    redacted = redact_sse_fragment(fragment)

    assert "event: response.delta" in redacted
    assert '"text":"safe"' in redacted
    assert_secret_absent(redacted, secret)


def test_sse_redaction_hides_unstructured_data() -> None:
    secret = "fixture-unstructured-sse-secret-value"
    fragment = f"data: {secret}\n\n"

    redacted = redact_sse_fragment(fragment)

    assert_secret_absent(redacted, secret)


def test_subprocess_environment_summary_does_not_include_environment_values() -> None:
    secret = "fixture-subprocess-secret-value"
    environment = {
        "PATH": r"C:\Windows\System32",
        "CMS_PROVIDER_TOKEN": secret,
        "PROMPT": "do not summarize prompt",
    }

    summary = summarize_subprocess_env(environment)

    assert "PATH" in summary["keys"]
    assert "CMS_PROVIDER_TOKEN" in summary["sensitive_keys"]
    assert_secret_absent(summary, secret)


def test_safe_log_record_allows_only_route_telemetry() -> None:
    secret = "fixture-log-secret-value"
    record = build_safe_log_record(
        route_id="cms-deepseek",
        status_code=200,
        elapsed_ms=42,
        byte_count=128,
        trace_id="trace-123",
        prompt="private prompt",
        file_content="private file",
        authorization=f"Bearer {secret}",
        base_url="https://api.example.invalid/v1",
        account_email="account@example.invalid",
    )

    assert record == {
        "route_id": "cms-deepseek",
        "status_code": 200,
        "elapsed_ms": 42,
        "byte_count": 128,
        "trace_id": "trace-123",
    }
    assert_secret_absent(record, secret)
