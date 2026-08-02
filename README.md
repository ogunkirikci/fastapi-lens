# fastapi-lens

`fastapi-lens` is a FastAPI-aware request execution profiler designed to show
where a request spends its time across handlers, dependencies, serialization,
SQLAlchemy queries, and lifecycle phases.

> See exactly where every millisecond of a FastAPI request goes.

## Status

The repository bootstrap and instrumentation-feasibility spike are complete.
The next implementation stage is the trace domain model. The package currently
exposes only its version; profiler behavior is not implemented yet.

The complete product specification is available in
[`docs/FASTAPI_REQUEST_EXECUTION_PROFILER.md`](docs/FASTAPI_REQUEST_EXECUTION_PROFILER.md).

## Requirements

- Python 3.11, 3.12, or 3.13
- [`uv`](https://docs.astral.sh/uv/) for the documented development workflow

The FastAPI dependency range is provisional until the instrumentation
feasibility spike is complete.

## Development

Create the environment and install the project with development dependencies:

```bash
uv sync --extra dev
```

Run the complete local quality suite:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

Build the package:

```bash
uv build
```

## Language policy

English is the only language used in source code, documentation, tests, user
interface text, logs, errors, and project communication.

## License

`fastapi-lens` is released under the MIT License.
