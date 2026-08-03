# Contributing

Thank you for contributing to `fastapi-latensight`.

## Development setup

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv build
uv run twine check dist/*
```

## Project conventions

- Use English for all repository content.
- Add tests for every behavior change.
- Keep public APIs fully typed.
- Run tests, linting, formatting, and type checking before submitting a change.
- Use focused commits with Conventional Commit-style messages.

The profiler specification and task order are documented in
[`docs/FASTAPI_REQUEST_EXECUTION_PROFILER.md`](docs/FASTAPI_REQUEST_EXECUTION_PROFILER.md).

Do not commit generated local databases, credentials, trace exports, or
deployment data. Security-sensitive changes should include tests for failure
paths and data leakage.

Release preparation and Trusted Publishing setup are documented in
[`docs/releasing.md`](docs/releasing.md).
