"""Dashboard access, CSRF, and output security primitives."""

import hmac
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from html import escape
from typing import Any, Final, TypeAlias

from fastapi.params import Depends as DependsParameter
from starlette.responses import Response

from fastapi_lens.config import LensConfig

AuthorizationDependency: TypeAlias = Callable[..., Any] | DependsParameter

DEFAULT_CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


class DashboardSecurityError(ValueError):
    """Raised when dashboard security configuration is unsafe."""


class CsrfValidationError(PermissionError):
    """Raised when a cookie-authenticated mutation lacks a valid CSRF token."""


def validate_dashboard_configuration(
    config: LensConfig,
    *,
    authorization_dependencies: Sequence[AuthorizationDependency] = (),
) -> None:
    """Fail closed for unsafe dashboard deployment combinations."""
    if not config.dashboard_enabled:
        return
    if config.environment is None:
        raise DashboardSecurityError(
            "Dashboard enablement requires an explicit environment."
        )
    if config.environment in {"staging", "production"}:
        if not config.allow_in_production:
            raise DashboardSecurityError(
                "Staging and production dashboards require allow_in_production=True."
            )
        if not authorization_dependencies:
            raise DashboardSecurityError(
                "Staging and production dashboards require authorization."
            )
    if any(
        not callable(dependency)
        and not callable(getattr(dependency, "dependency", None))
        for dependency in authorization_dependencies
    ):
        raise DashboardSecurityError(
            "Dashboard authorization dependencies must be callable."
        )


class CsrfPolicy:
    """Double-submit CSRF policy for cookie-authenticated mutations."""

    def __init__(
        self,
        *,
        cookie_name: str = "fastapi_lens_csrf",
        header_name: str = "x-fastapi-lens-csrf",
    ) -> None:
        self.cookie_name = cookie_name
        self.header_name = header_name.lower()

    @staticmethod
    def issue_token() -> str:
        """Return a cryptographically random CSRF token."""
        return secrets.token_urlsafe(32)

    def validate(
        self,
        *,
        method: str,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
        cookie_authenticated: bool,
    ) -> None:
        """Validate mutations while leaving non-cookie authentication unchanged."""
        if method.upper() in SAFE_METHODS or not cookie_authenticated:
            return
        cookie_token = cookies.get(self.cookie_name)
        header_token = next(
            (
                value
                for name, value in headers.items()
                if name.lower() == self.header_name
            ),
            None,
        )
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
        ):
            raise CsrfValidationError("CSRF token validation failed.")


def apply_dashboard_security_headers(response: Response) -> None:
    """Apply restrictive caching and browser execution policy headers."""
    for name, value in dashboard_security_headers().items():
        response.headers[name] = value


def dashboard_security_headers() -> dict[str, str]:
    """Return security headers shared by dashboard response surfaces."""
    return {
        "Content-Security-Policy": DEFAULT_CONTENT_SECURITY_POLICY,
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def escape_untrusted_html(value: object) -> str:
    """Escape untrusted values for ordinary HTML text contexts."""
    return escape(str(value), quote=True)


def json_for_html_data(value: Any) -> str:
    """Serialize JSON for a non-executable HTML data block."""
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
