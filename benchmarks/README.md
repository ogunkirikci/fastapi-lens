# Performance benchmarks

The benchmark runner compares equivalent plain and instrumented FastAPI
applications through an in-process ASGI transport. It records throughput,
nearest-rank latency percentiles, absolute and percentage p50 overhead,
allocation peaks, approximate serialized bytes per trace, and incomplete or
dropped trace counts.

Install development dependencies before running benchmarks:

```bash
uv sync --extra dev
```

Run the reproducible default suite:

```bash
uv run python -m tests.benchmarks.runner \
  --requests 1000 \
  --warmup 100 \
  --memory-requests 200 \
  --output-json benchmarks/results.json \
  --output-markdown benchmarks/RESULTS.md
```

Run a shorter smoke measurement:

```bash
uv run python -m tests.benchmarks.runner \
  --scenario minimum-capture \
  --scenario async-endpoint \
  --requests 100 \
  --warmup 10 \
  --memory-requests 25
```

Available scenarios:

- `minimum-capture`
- `dependency-capture`
- `sql-capture`
- `dashboard-idle`
- `concurrent-100`
- `sync-endpoint`
- `async-endpoint`

The runner captures environment and dependency versions in each report.
Results are intended for regression comparisons on the same idle machine, not
as production capacity claims. For release comparisons, run at least five
times, retain every raw JSON report, and compare medians across runs.
