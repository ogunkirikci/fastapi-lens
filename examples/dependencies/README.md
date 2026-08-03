# Dependency profiling example

This application demonstrates nested dependencies, cache reuse, and generator
dependency setup and cleanup.

Install the project and run the example:

```bash
uv sync --extra dev
uv run --with uvicorn uvicorn examples.dependencies.app:app
```

Open `http://127.0.0.1:8000/profile`, then inspect the trace at
`http://127.0.0.1:8000/__lens__/`.

The dependency graph shows:

- `current_user` depending on `request_id` and `database_session`
- `request_id` reused from FastAPI's dependency cache
- separate setup and cleanup segments for `database_session`

The example uses static placeholder values and performs no external I/O.
