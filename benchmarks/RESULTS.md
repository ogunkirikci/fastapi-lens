# Benchmark results

These results are an in-process regression reference, not a production capacity claim. Re-run the benchmark on deployment-class hardware before making sizing decisions.

## Environment

- Captured: `2026-08-03T06:33:42+00:00`
- Platform: `macOS-26.5.2-arm64-arm-64bit`
- Machine: `arm64`
- CPU count: `15`
- Python: `3.11.15`
- FastAPI: `0.141.1`
- Starlette: `1.3.1`
- SQLAlchemy: `2.0.51`
- fastapi-lens: `0.1.0a0`

## Methodology

Each scenario warms up with 100 requests. Equivalent plain and instrumented FastAPI applications run sequentially through the in-process ASGI transport. Latency uses `perf_counter` and nearest-rank percentiles.

Allocation peaks are collected in a separate pass with `tracemalloc` over 200 requests, so allocation tracking does not distort the latency samples.

## Results

| Scenario | Concurrency | Baseline req/s | Instrumented req/s | Baseline p50 | Instrumented p50 | Absolute p50 overhead | p50 overhead | Instrumented p95 | Instrumented p99 | Peak allocation | Bytes/trace | Incomplete | Dropped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| minimum-capture | 1 | 11690.5 | 8507.1 | 0.083 ms | 0.115 ms | +0.031 ms | +37.6% | 0.127 ms | 0.147 ms | 184.0 KiB | 0.6 KiB | 0 | 0 |
| dependency-capture | 1 | 11047.3 | 5991.5 | 0.088 ms | 0.163 ms | +0.075 ms | +84.2% | 0.183 ms | 0.204 ms | 462.9 KiB | 1.6 KiB | 0 | 0 |
| sql-capture | 1 | 3677.7 | 2834.9 | 0.269 ms | 0.347 ms | +0.079 ms | +29.3% | 0.384 ms | 0.402 ms | 519.4 KiB | 1.4 KiB | 0 | 0 |
| dashboard-idle | 1 | 11781.4 | 7502.6 | 0.083 ms | 0.130 ms | +0.047 ms | +56.9% | 0.145 ms | 0.163 ms | 308.7 KiB | 0.9 KiB | 0 | 0 |
| concurrent-100 | 100 | 10675.7 | 5440.1 | 0.089 ms | 0.176 ms | +0.087 ms | +97.3% | 0.199 ms | 0.216 ms | 650.1 KiB | 1.9 KiB | 0 | 0 |
| sync-endpoint | 1 | 4227.1 | 3341.0 | 0.234 ms | 0.296 ms | +0.063 ms | +26.8% | 0.326 ms | 0.344 ms | 397.7 KiB | 1.3 KiB | 0 | 0 |
| async-endpoint | 1 | 11862.3 | 6828.6 | 0.083 ms | 0.143 ms | +0.060 ms | +73.1% | 0.160 ms | 0.178 ms | 395.0 KiB | 1.3 KiB | 0 | 0 |

## Interpretation

Negative overhead can occur in short microbenchmarks because of scheduler, allocator, and CPU-frequency noise. Compare repeated runs on an otherwise idle machine and focus on sustained regressions.

The memory store is process-local. Bytes per trace is the mean versioned JSON size of traces captured during the allocation pass; it is not the complete Python object footprint.
