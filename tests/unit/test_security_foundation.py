import math
from datetime import UTC, datetime

import pytest
from fastapi import Depends
from starlette.responses import Response

from fastapi_lens import LensConfig
from fastapi_lens.config import enabled_from_environment
from fastapi_lens.exporters.json import trace_snapshot_to_json
from fastapi_lens.models import (
    DependencyCacheStatus,
    Diagnostic,
    LogicalDependencyNode,
    RequestTrace,
    SegmentStatus,
    SegmentType,
    TraceError,
    TraceSegment,
)
from fastapi_lens.redaction import (
    REDACTED_VALUE,
    TraceSanitizer,
    TraceSanitizerConfig,
    truncate_text,
)
from fastapi_lens.security import (
    DEFAULT_CONTENT_SECURITY_POLICY,
    CsrfPolicy,
    CsrfValidationError,
    DashboardSecurityError,
    apply_dashboard_security_headers,
    escape_untrusted_html,
    json_for_html_data,
    validate_dashboard_configuration,
)


def make_trace(*, segment_count: int = 1) -> RequestTrace:
    segments = [
        TraceSegment(
            id=f"segment-{index}",
            trace_id="trace-1",
            type=SegmentType.CUSTOM,
            name=f"segment-name-{index}",
            start_ns=index,
            end_ns=index + 1,
            status=SegmentStatus.OK,
            attributes={
                "Authorization": "Bearer private-value",
                "metadata": {
                    "password": "private-password",
                    "safe": "visible",
                },
            },
            error=TraceError(
                type="ValueError",
                message="segment-error-message",
                stack="segment-stack",
            ),
        )
        for index in range(segment_count)
    ]
    return RequestTrace(
        schema_version="1.0",
        id="trace-1",
        method="GET",
        path="/items/private-path",
        route="/items/{item_id}",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_received_ns=0,
        response_started_ns=1,
        response_body_completed_ns=2,
        application_completed_ns=3,
        status_code=500,
        segments=segments,
        diagnostics=[
            Diagnostic(
                code="diagnostic-code",
                severity="warning",
                message="diagnostic-message",
            )
        ],
        error=TraceError(
            type="RuntimeError",
            message="request-error-message",
            stack="request-stack",
        ),
        complete=True,
    )


def test_truncate_text_preserves_the_requested_limit() -> None:
    assert truncate_text("short", max_length=10) == "short"
    assert truncate_text("long-value", max_length=5) == "long…"
    assert truncate_text("long-value", max_length=1) == "…"


def test_trace_sanitizer_redacts_sensitive_fields_and_limits_untrusted_text() -> None:
    trace = make_trace(segment_count=3)
    trace.logical_dependencies.extend(
        [
            LogicalDependencyNode(
                id=f"dependency-{index}",
                trace_id="trace-1",
                name=f"dependency-name-{index}",
                cache_status=DependencyCacheStatus.MISS,
            )
            for index in range(2)
        ]
    )
    trace.segments[0].attributes.update(
        {
            "x-api-key": "private-key",
            "list": list(range(10)),
            "non_finite": math.inf,
            "long_value": "x" * 40,
        }
    )
    sanitizer = TraceSanitizer(
        TraceSanitizerConfig(
            max_segments_per_trace=2,
            max_dependencies_per_trace=1,
            max_diagnostics_per_trace=5,
            max_attributes_per_segment=10,
            max_collection_items=3,
            max_nesting_depth=3,
            max_attribute_length=12,
            max_error_length=10,
            max_trace_bytes=10_000,
        )
    )

    sanitizer.sanitize(trace)
    snapshot = trace.snapshot()

    assert len(snapshot.segments) == 2
    assert len(snapshot.logical_dependencies) == 1
    attributes = dict(snapshot.segments[0].attributes)
    assert attributes["Authorizati…"] == REDACTED_VALUE
    assert attributes["x-api-key"] == REDACTED_VALUE
    metadata = dict(attributes["metadata"])  # type: ignore[arg-type]
    assert metadata["password"] == REDACTED_VALUE
    assert metadata["safe"] == "visible"
    assert attributes["list"] == (0, 1, 2)
    assert attributes["non_finite"] is None
    assert attributes["long_value"] == "xxxxxxxxxxx…"
    assert snapshot.error is not None
    assert snapshot.error.message == "request-e…"
    assert snapshot.segments[0].error is not None
    assert snapshot.segments[0].error.stack == "segment-s…"
    assert snapshot.diagnostics[0].code == "trace_truncated"


def test_nested_values_stop_at_the_configured_depth() -> None:
    trace = make_trace()
    trace.segments[0].attributes = {
        "nested": {"level_1": {"level_2": {"level_3": "value"}}}
    }
    sanitizer = TraceSanitizer(
        TraceSanitizerConfig(
            max_nesting_depth=2,
            max_trace_bytes=10_000,
        )
    )

    sanitizer.sanitize(trace)

    nested = dict(trace.snapshot().segments[0].attributes)["nested"]
    assert "MAX_DEPTH" in str(nested)
    assert trace.diagnostics[0].code == "trace_truncated"


def test_byte_limit_removes_data_before_export() -> None:
    trace = make_trace(segment_count=20)
    for segment in trace.segments:
        segment.attributes["payload"] = "x" * 500
    sanitizer = TraceSanitizer(
        TraceSanitizerConfig(
            max_attribute_length=500,
            max_trace_bytes=2_000,
        )
    )

    sanitizer.sanitize(trace)
    payload = trace_snapshot_to_json(trace.snapshot(), max_bytes=2_000)

    assert len(payload) <= 2_000
    assert trace.diagnostics[0].code == "trace_truncated"
    assert len(trace.segments) < 20


def test_truncation_diagnostic_is_canonical_and_respects_diagnostic_limit() -> None:
    trace = make_trace(segment_count=2)
    trace.diagnostics = [
        Diagnostic(
            code="trace_truncated",
            severity="info",
            message="untrusted replacement",
        )
    ]
    sanitizer = TraceSanitizer(
        TraceSanitizerConfig(
            max_segments_per_trace=1,
            max_diagnostics_per_trace=1,
            max_trace_bytes=10_000,
        )
    )

    sanitizer.sanitize(trace)

    assert trace.diagnostics == [
        Diagnostic(
            code="trace_truncated",
            severity="warning",
            message="Trace data was truncated by pre-storage safety limits.",
        )
    ]


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("true", True),
        (" YES ", True),
        ("1", True),
        ("off", False),
        ("No", False),
        ("0", False),
    ],
)
def test_enabled_environment_values_are_strictly_parsed(
    raw_value: str,
    expected: bool,
) -> None:
    assert enabled_from_environment({"FASTAPI_LENS_ENABLED": raw_value}) is expected


def test_missing_and_invalid_enabled_environment_values() -> None:
    assert enabled_from_environment({}) is None
    with pytest.raises(
        ValueError,
        match=r"FASTAPI_LENS_ENABLED must be a boolean value",
    ):
        enabled_from_environment({"FASTAPI_LENS_ENABLED": "sometimes"})


def test_lens_config_is_secure_by_default_and_validates_limits() -> None:
    config = LensConfig()

    assert config.dashboard_enabled is False
    assert config.environment is None
    assert config.allow_in_production is False

    with pytest.raises(ValueError, match=r"dashboard_path must start"):
        LensConfig(dashboard_path="lens")
    with pytest.raises(ValueError, match=r"must not replace"):
        LensConfig(dashboard_path="/")
    with pytest.raises(ValueError, match=r"max_trace_bytes must be greater"):
        LensConfig(max_trace_bytes=0)


def require_admin() -> None:
    return None


def test_dashboard_policy_requires_explicit_environment() -> None:
    validate_dashboard_configuration(LensConfig())

    with pytest.raises(
        DashboardSecurityError,
        match=r"requires an explicit environment",
    ):
        validate_dashboard_configuration(LensConfig(dashboard_enabled=True))

    validate_dashboard_configuration(
        LensConfig(dashboard_enabled=True, environment="development")
    )
    validate_dashboard_configuration(
        LensConfig(dashboard_enabled=True, environment="test")
    )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_dashboard_requires_override_and_authorization(
    environment: str,
) -> None:
    with pytest.raises(
        DashboardSecurityError,
        match=r"allow_in_production=True",
    ):
        validate_dashboard_configuration(
            LensConfig(
                dashboard_enabled=True,
                environment=environment,  # type: ignore[arg-type]
            )
        )

    config = LensConfig(
        dashboard_enabled=True,
        environment=environment,  # type: ignore[arg-type]
        allow_in_production=True,
    )
    with pytest.raises(
        DashboardSecurityError,
        match=r"require authorization",
    ):
        validate_dashboard_configuration(config)

    validate_dashboard_configuration(
        config,
        authorization_dependencies=(require_admin,),
    )
    validate_dashboard_configuration(
        config,
        authorization_dependencies=(Depends(require_admin),),
    )
    with pytest.raises(
        DashboardSecurityError,
        match=r"must be callable",
    ):
        validate_dashboard_configuration(
            config,
            authorization_dependencies=(object(),),  # type: ignore[arg-type]
        )


def test_csrf_policy_only_requires_matching_tokens_for_cookie_mutations() -> None:
    policy = CsrfPolicy()
    token = policy.issue_token()

    assert len(token) >= 32
    policy.validate(
        method="GET",
        headers={},
        cookies={},
        cookie_authenticated=True,
    )
    policy.validate(
        method="POST",
        headers={},
        cookies={},
        cookie_authenticated=False,
    )
    policy.validate(
        method="DELETE",
        headers={"X-FastAPI-Lens-CSRF": token},
        cookies={"fastapi_lens_csrf": token},
        cookie_authenticated=True,
    )

    with pytest.raises(
        CsrfValidationError,
        match=r"CSRF token validation failed\.",
    ):
        policy.validate(
            method="POST",
            headers={"x-fastapi-lens-csrf": "wrong"},
            cookies={"fastapi_lens_csrf": token},
            cookie_authenticated=True,
        )


def test_dashboard_security_headers_are_restrictive_and_non_cacheable() -> None:
    response = Response()

    apply_dashboard_security_headers(response)

    assert response.headers["content-security-policy"] == (
        DEFAULT_CONTENT_SECURITY_POLICY
    )
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_safe_rendering_helpers_escape_html_and_script_terminators() -> None:
    assert escape_untrusted_html("<script>'\"&") == ("&lt;script&gt;&#x27;&quot;&amp;")

    payload = json_for_html_data(
        {
            "html": "</script><script>alert('x')</script>",
            "separator": "\u2028",
        }
    )

    assert "</script>" not in payload
    assert "<" not in payload
    assert ">" not in payload
    assert "&" not in payload
    assert "\u2028" not in payload
    assert "\\u003c/script\\u003e" in payload


def test_invalid_sanitizer_limits_fail_fast() -> None:
    with pytest.raises(
        ValueError,
        match=r"Trace sanitizer limits must be greater than zero\.",
    ):
        TraceSanitizerConfig(max_collection_items=0)
