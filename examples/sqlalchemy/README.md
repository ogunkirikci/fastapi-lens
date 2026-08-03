# SQLAlchemy profiling example

This application explicitly registers a synchronous SQLAlchemy engine and
captures normalized statements without bind values.

Install SQLAlchemy support and run the example:

```bash
uv sync --extra dev
uv run --with uvicorn uvicorn examples.sqlalchemy.app:app
```

Open `http://127.0.0.1:8000/items/1`, then inspect the SQL table at
`http://127.0.0.1:8000/__lens__/`.

The captured query includes its operation, normalized statement, stable
fingerprint, SQLite dialect, duration, status, and row count when the driver
provides one. The `item_id` bind value is not stored.

The example creates `fastapi_lens_example.db` in the working directory. Stop the
application cleanly so the Lens and engine registrations are released.
