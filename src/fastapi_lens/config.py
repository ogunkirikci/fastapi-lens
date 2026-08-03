"""Validated fastapi-lens configuration."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Environment = Literal["development", "test", "staging", "production"]


@dataclass(slots=True, frozen=True)
class LensConfig:
    """Core runtime, storage, and dashboard security configuration."""

    enabled: bool = True
    dashboard_enabled: bool = False
    dashboard_path: str = "/__lens__"
    include_routes: tuple[str, ...] = ("*",)
    exclude_routes: tuple[str, ...] = (
        "/docs",
        "/redoc",
        "/openapi.json",
        "/health",
        "/metrics",
        "/__lens__*",
    )
    slow_request_threshold_ms: float = 250.0
    slow_dependency_threshold_ms: float = 100.0
    capture_dependencies: bool = True
    capture_sql: bool = False
    capture_serialization: bool = True
    max_traces: int = 500
    max_api_page_size: int = 200
    environment: Environment | None = None
    allow_in_production: bool = False
    max_segments_per_trace: int = 1_000
    max_attribute_length: int = 2_000
    max_error_length: int = 8_000
    max_trace_bytes: int = 1_000_000
    max_sql_length: int = 2_000

    def __post_init__(self) -> None:
        if not self.dashboard_path.startswith("/"):
            raise ValueError("dashboard_path must start with '/'.")
        if self.dashboard_path == "/":
            raise ValueError("dashboard_path must not replace the application root.")
        for name, value in (
            ("max_traces", self.max_traces),
            ("max_api_page_size", self.max_api_page_size),
            ("max_segments_per_trace", self.max_segments_per_trace),
            ("max_attribute_length", self.max_attribute_length),
            ("max_error_length", self.max_error_length),
            ("max_trace_bytes", self.max_trace_bytes),
            ("max_sql_length", self.max_sql_length),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero.")
        for name, threshold in (
            ("slow_request_threshold_ms", self.slow_request_threshold_ms),
            ("slow_dependency_threshold_ms", self.slow_dependency_threshold_ms),
        ):
            if threshold < 0:
                raise ValueError(f"{name} must be zero or greater.")


def enabled_from_environment(
    environment: Mapping[str, str],
) -> bool | None:
    """Parse FASTAPI_LENS_ENABLED without silently accepting invalid values."""
    raw_value = environment.get("FASTAPI_LENS_ENABLED")
    if raw_value is None:
        return None
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        "FASTAPI_LENS_ENABLED must be a boolean value "
        "(true/false, yes/no, on/off, or 1/0)."
    )
