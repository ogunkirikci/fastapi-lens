# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes yet.

## [0.1.0a0] - 2026-08-03

### Added

- Public `Lens` orchestration with runtime enable and disable controls, async
  trace-store access, and explicit SQLAlchemy registration.
- Pure ASGI request lifecycle tracing with complete, incomplete, streaming,
  disconnect, cancellation, and post-response behavior.
- Async and sync handler instrumentation.
- Logical FastAPI dependency graphs with cache, scope, setup, and cleanup
  metadata.
- FastAPI response serialization instrumentation with compatibility guards.
- Explicit, reference-counted SQLAlchemy sync and async engine instrumentation.
- Sync and async custom segment context managers and decorators.
- Immutable, versioned, byte-bounded trace snapshots and process-local memory
  storage.
- Built-in slow request, slow dependency, repeated query, possible N+1,
  expensive serialization, and integrity diagnostics.
- Pre-storage redaction and safety limits.
- Secured dashboard JSON API, request UI, route summaries, waterfall,
  dependency graph, SQL table, diagnostics, and error views.
- Reproducible benchmark suite with environment metadata, absolute and
  percentage overhead, memory observations, and reference results.
- Python 3.11 through 3.13 CI and FastAPI capability probes.
- Configuration, security, compatibility, example, benchmark, and release
  documentation.
- Trusted Publishing workflows for TestPyPI and PyPI.

### Security

- Dashboard disabled by default with explicit environment requirements.
- Staging and production guard requiring authorization and an explicit
  production override.
- Cookie-authenticated mutation CSRF validation.
- Autoescaped templates, DOM-safe rendering, restrictive CSP, and non-cacheable
  dashboard responses.
- SQL bind exclusion and sensitive-field redaction before storage.

[Unreleased]: https://github.com/ogunkirikci/fastapi-lens/compare/v0.1.0a0...HEAD
[0.1.0a0]: https://github.com/ogunkirikci/fastapi-lens/releases/tag/v0.1.0a0
