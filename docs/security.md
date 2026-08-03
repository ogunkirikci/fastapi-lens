# Security guide

The dashboard exposes request timing and diagnostic data. Treat it as an
administrative interface, not a public application feature.

## Secure defaults

- The dashboard is disabled by default.
- Enabling it requires an explicit environment.
- Staging and production require an explicit override and authorization.
- Traces are redacted and bounded before reaching storage.
- SQL bind values are never stored.
- Dashboard responses use a restrictive Content Security Policy.
- Dashboard HTML, JSON, assets, errors, and mutations use
  `Cache-Control: no-store`.
- Cookie-authenticated mutations require double-submit CSRF validation.

The memory store is process-local and disappears when the process stops. It
does not aggregate data across workers.

## Production authorization

Authorization is a FastAPI dependency applied to every dashboard HTML, JSON,
asset, and mutation route:

```python
from typing import Annotated

from fastapi import Header, HTTPException, status
from fastapi_latensight import Latensight, LatensightConfig


def require_operator(
    x_operator_token: Annotated[str | None, Header()] = None,
) -> None:
    if x_operator_token != "replace-with-a-real-verifier":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorized.",
        )


profiler = Latensight(
    app,
    config=LatensightConfig(
        dashboard_enabled=True,
        environment="production",
        allow_in_production=True,
    ),
    dashboard_dependencies=(require_operator,),
)
```

Use an established identity provider or application authorization policy.
Loopback or source-IP checks may be additional controls, but they are not an
authorization boundary.

## Cookie authentication and CSRF

Set `cookie_authenticated_dashboard=True` when a dashboard authorization
dependency relies on cookies. Mutating requests must then submit the same token
in the `fastapi_latensight_csrf` cookie and `x-fastapi-latensight-csrf` header.

```python
profiler = Latensight(
    app,
    config=config,
    dashboard_dependencies=(require_operator,),
    cookie_authenticated_dashboard=True,
)
```

Issue tokens with `CsrfPolicy.issue_token()` and set the cookie with `Secure`,
`HttpOnly` when client-side access is not required by your integration, and an
appropriate `SameSite` policy. The bundled dashboard reads the CSRF cookie to
submit the mutation header, so its default integration requires JavaScript
access to that specific token cookie.

## Stored data

Default sensitive fields include authorization, cookie, API key, password,
secret, token, and private-key variants. Redaction recursively applies to
segment attributes. Trace paths, custom segment names, SQL text, diagnostics,
and error messages remain untrusted even after redaction and are size-bounded.

Redaction is defense in depth. Do not place secrets in URL paths, SQL comments,
custom segment names, exception messages, or arbitrary diagnostic text.

## SQL safety

SQL capture is off by default and applies only to explicitly registered
engines. Statements are normalized and common literal forms are replaced before
storage. Parameters are not captured. Normalization cannot prove that every
vendor-specific literal or comment form is secret-free, so avoid embedding
sensitive values directly in SQL source text.

## Output safety

The dashboard template uses autoescaping. Trace values are fetched as JSON and
inserted with DOM text APIs rather than HTML parsing. The UI does not interpolate
trace JSON into executable script content and does not require inline scripts
or inline styles.

Authorization failure responses contain no trace data. Security and no-store
headers are applied by ASGI middleware so they also cover validation and
authorization errors.

## Deployment checklist

- Keep the dashboard disabled unless a diagnostic session requires it.
- Require strong authentication and application-level authorization.
- Restrict network reachability separately.
- Use HTTPS.
- Enable cookie CSRF mode when authentication uses cookies.
- Keep trace and page limits bounded.
- Confirm application error messages do not contain secrets.
- Register only intended SQLAlchemy engines.
- Remember that each worker exposes a different process-local buffer.
- Call `profiler.close()` during application shutdown to release global adapters.
