# fastapi-lens

`fastapi-lens` is a FastAPI-aware request execution profiler. It records where
request time is spent across lifecycle phases, endpoint handlers, dependencies,
response serialization, custom segments, and explicitly registered SQLAlchemy
engines.

> See exactly where every millisecond of a FastAPI request goes.

The project is currently a pre-alpha. Its in-memory store and dashboard are
designed for local development, tests, and carefully controlled diagnostic
sessions.

## Features

- Pure ASGI request lifecycle timing
- Async and sync endpoint timing
- FastAPI dependency setup, cache, scope, and cleanup visibility
- Response serialization timing
- Explicit SQLAlchemy sync and async engine instrumentation
- Custom sync and async trace segments
- Versioned, bounded JSON output
- Built-in performance diagnostics
- Process-local dashboard with request, route, waterfall, dependency, SQL, and
  diagnostic views
- Redaction, size limits, authorization hooks, CSRF protection, CSP, and
  non-cacheable dashboard responses

## Requirements

- Python 3.11, 3.12, or 3.13
- FastAPI 0.121.0 through 0.141.x
- Pydantic v2
- SQLAlchemy 2.x when using the optional SQL integration

## Installation

```bash
pip install fastapi-lens
```

Install SQLAlchemy support:

```bash
pip install "fastapi-lens[sqlalchemy]"
```

## Quick start

```python
from fastapi import FastAPI
from fastapi_lens import Lens, LensConfig

app = FastAPI()


@app.get("/items/{item_id}")
async def read_item(item_id: int) -> dict[str, int]:
    return {"item_id": item_id}


lens = Lens(
    app,
    config=LensConfig(
        dashboard_enabled=True,
        environment="development",
    ),
)
```

Run the application, make a request, and open `/__lens__/`. The dashboard is
disabled by default and enabling it always requires an explicit environment.

When the application owns a cleanup lifecycle, release process-global adapter
registrations during shutdown:

```python
lens.close()
```

`close()` is terminal for that Lens instance. `disable()` and `enable()` are
the reversible runtime controls for ordinary operation.

## SQLAlchemy

SQL capture never discovers engines automatically. Enable the feature and
register every intended engine explicitly:

```python
from sqlalchemy import create_engine

engine = create_engine("sqlite:///application.db")
lens = Lens(app, config=LensConfig(capture_sql=True))
lens.instrument_sqlalchemy(engine)
```

Bind values are not stored. Statements are normalized, literal values are
masked, and display text is bounded before storage.

## Custom segments

```python
from fastapi_lens import current_trace, trace_segment


@trace_segment("price_order")
async def price_order() -> None:
    async with current_trace.segment("load_exchange_rate"):
        ...
```

The API is a no-op outside an active traced request.

## Documentation

- [Configuration reference](docs/configuration.md)
- [Security guide](docs/security.md)
- [Supported compatibility range](docs/compatibility.md)
- [Dependency example](examples/dependencies/README.md)
- [SQLAlchemy example](examples/sqlalchemy/README.md)
- [Benchmark methodology and commands](benchmarks/README.md)
- [Reference benchmark results](benchmarks/RESULTS.md)
- [Release process](docs/releasing.md)
- [Technical specification](docs/FASTAPI_REQUEST_EXECUTION_PROFILER.md)

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv build
uv run twine check dist/*
```

Repository content, public text, diagnostics, tests, examples, and commit
messages use English only.

## License

`fastapi-lens` is released under the MIT License.
