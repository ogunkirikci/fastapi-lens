"""Compare plain and instrumented FastAPI applications under fixed workloads."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import math
import os
import platform
import sys
import tracemalloc
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Final, Literal

import fastapi
import httpx2
import jinja2
import starlette
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, text

import fastapi_lens
from fastapi_lens.config import LensConfig
from fastapi_lens.dashboard import create_dashboard_app
from fastapi_lens.exporters.json import trace_snapshot_to_json
from fastapi_lens.instrumentation.dependencies import dependency_instrumentation
from fastapi_lens.instrumentation.handler import handler_instrumentation
from fastapi_lens.instrumentation.serialization import (
    serialization_instrumentation,
)
from fastapi_lens.instrumentation.sqlalchemy import sqlalchemy_instrumentation
from fastapi_lens.middleware import LensMiddleware
from fastapi_lens.storage.memory import MemoryTraceStore

NANOSECONDS_PER_SECOND: Final = 1_000_000_000
DEFAULT_SCENARIOS: Final = (
    "minimum-capture",
    "dependency-capture",
    "sql-capture",
    "dashboard-idle",
    "concurrent-100",
    "sync-endpoint",
    "async-endpoint",
)
EndpointKind = Literal["async", "sync", "sql"]


class BenchmarkPayload(BaseModel):
    """Small response model that exercises FastAPI serialization."""

    value: int
    source: str


async def benchmark_dependency() -> int:
    """Return a stable dependency value without simulated I/O."""
    return 41


@dataclass(slots=True, frozen=True)
class Scenario:
    name: str
    description: str
    endpoint_kind: EndpointKind = "async"
    dependency: bool = False
    capture_handler: bool = False
    capture_dependency: bool = False
    capture_serialization: bool = False
    capture_sql: bool = False
    dashboard_enabled: bool = False
    concurrency: int = 1


SCENARIOS: Final = {
    "minimum-capture": Scenario(
        name="minimum-capture",
        description="Lifecycle middleware with optional phase capture disabled.",
    ),
    "dependency-capture": Scenario(
        name="dependency-capture",
        description="Handler and FastAPI dependency capture enabled.",
        dependency=True,
        capture_handler=True,
        capture_dependency=True,
    ),
    "sql-capture": Scenario(
        name="sql-capture",
        description="Handler and SQLAlchemy query capture enabled.",
        endpoint_kind="sql",
        capture_handler=True,
        capture_sql=True,
    ),
    "dashboard-idle": Scenario(
        name="dashboard-idle",
        description="Dashboard mounted and idle while handler capture is enabled.",
        capture_handler=True,
        dashboard_enabled=True,
    ),
    "concurrent-100": Scenario(
        name="concurrent-100",
        description="One hundred concurrent dependency-backed async requests.",
        dependency=True,
        capture_handler=True,
        capture_dependency=True,
        capture_serialization=True,
        concurrency=100,
    ),
    "sync-endpoint": Scenario(
        name="sync-endpoint",
        description="Synchronous endpoint with handler and serialization capture.",
        endpoint_kind="sync",
        capture_handler=True,
        capture_serialization=True,
    ),
    "async-endpoint": Scenario(
        name="async-endpoint",
        description="Asynchronous endpoint with handler and serialization capture.",
        capture_handler=True,
        capture_serialization=True,
    ),
}


@dataclass(slots=True)
class RunningApplication:
    app: Any
    store: MemoryTraceStore | None
    cleanup_callbacks: list[Callable[[], None]]

    def close(self) -> None:
        """Restore process-global adapters and release database resources."""
        for callback in reversed(self.cleanup_callbacks):
            callback()


@dataclass(slots=True, frozen=True)
class WorkloadResult:
    requests_per_second: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    peak_allocated_bytes: int
    stored_trace_count: int
    incomplete_trace_count: int
    dropped_trace_count: int
    approximate_bytes_per_trace: float | None


@dataclass(slots=True, frozen=True)
class ScenarioResult:
    name: str
    description: str
    request_count: int
    concurrency: int
    baseline: WorkloadResult
    instrumented: WorkloadResult
    absolute_p50_overhead_ms: float
    percentage_p50_overhead: float
    throughput_change_percent: float


def percentile(values: Sequence[float], selected_percentile: int) -> float:
    """Return a nearest-rank percentile from a non-empty sample."""
    if not values:
        raise ValueError("percentile requires at least one value.")
    if not 1 <= selected_percentile <= 100:
        raise ValueError("selected_percentile must be between 1 and 100.")
    ordered = sorted(values)
    rank = max(1, math.ceil(selected_percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def _endpoint_app(
    scenario: Scenario,
    *,
    engine: Engine | None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    dependencies = [Depends(benchmark_dependency)] if scenario.dependency else []

    if scenario.endpoint_kind == "sync":

        @app.get(
            "/benchmark",
            response_model=BenchmarkPayload,
            dependencies=dependencies,
        )
        def sync_endpoint() -> BenchmarkPayload:
            return BenchmarkPayload(value=42, source="sync")

    elif scenario.endpoint_kind == "sql":
        if engine is None:
            raise RuntimeError("The SQL benchmark requires an engine.")

        @app.get(
            "/benchmark",
            response_model=BenchmarkPayload,
            dependencies=dependencies,
        )
        def sql_endpoint() -> BenchmarkPayload:
            with engine.connect() as connection:
                value = connection.execute(text("SELECT 42")).scalar_one()
            return BenchmarkPayload(value=value, source="sql")

    else:

        @app.get(
            "/benchmark",
            response_model=BenchmarkPayload,
            dependencies=dependencies,
        )
        async def async_endpoint() -> BenchmarkPayload:
            return BenchmarkPayload(value=42, source="async")

    return app


def build_application(
    scenario: Scenario,
    *,
    instrumented: bool,
    store_capacity: int,
) -> RunningApplication:
    """Build one isolated benchmark application and its adapter cleanup stack."""
    cleanup_callbacks: list[Callable[[], None]] = []
    engine = (
        create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        if scenario.endpoint_kind == "sql"
        else None
    )
    if engine is not None:
        cleanup_callbacks.append(engine.dispose)

    app = _endpoint_app(scenario, engine=engine)
    if not instrumented:
        return RunningApplication(
            app=app,
            store=None,
            cleanup_callbacks=cleanup_callbacks,
        )

    owner = object()
    if scenario.capture_handler:
        handler_instrumentation.install(owner)
        cleanup_callbacks.append(lambda: handler_instrumentation.remove(owner))
    if scenario.capture_dependency:
        dependency_instrumentation.install(owner)
        cleanup_callbacks.append(lambda: dependency_instrumentation.remove(owner))
    if scenario.capture_serialization:
        serialization_instrumentation.install(owner)
        cleanup_callbacks.append(lambda: serialization_instrumentation.remove(owner))
    if scenario.capture_sql:
        if engine is None:
            raise RuntimeError("SQL capture requires a benchmark engine.")
        sqlalchemy_instrumentation.register(engine, owner)
        cleanup_callbacks.append(
            lambda: sqlalchemy_instrumentation.unregister(engine, owner)
        )

    store = MemoryTraceStore(
        max_traces=store_capacity,
        max_page_size=store_capacity,
    )
    if scenario.dashboard_enabled:
        app.mount(
            "/__lens__",
            create_dashboard_app(
                store,
                config=LensConfig(
                    dashboard_enabled=True,
                    environment="development",
                ),
                max_page_size=store_capacity,
            ),
        )
    profiled_app = LensMiddleware(
        app,
        store=store,
        exclude_routes=("/__lens__*",),
    )
    return RunningApplication(
        app=profiled_app,
        store=store,
        cleanup_callbacks=cleanup_callbacks,
    )


async def _request_once(client: httpx2.AsyncClient) -> float:
    started = perf_counter()
    response = await client.get("/benchmark")
    elapsed_ms = (perf_counter() - started) * 1_000
    if response.status_code != 200:
        raise RuntimeError(
            f"Benchmark request failed with status {response.status_code}."
        )
    return elapsed_ms


async def _execute_requests(
    app: Any,
    *,
    request_count: int,
    concurrency: int,
    retain_latencies: bool,
) -> tuple[float, list[float]]:
    transport = httpx2.ASGITransport(app=app)
    latencies: list[float] = []
    started = perf_counter()
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://benchmark.local",
    ) as client:
        if concurrency == 1:
            for _ in range(request_count):
                latency = await _request_once(client)
                if retain_latencies:
                    latencies.append(latency)
        else:
            for offset in range(0, request_count, concurrency):
                batch_size = min(concurrency, request_count - offset)
                batch = await asyncio.gather(
                    *(_request_once(client) for _ in range(batch_size))
                )
                if retain_latencies:
                    latencies.extend(batch)
    return perf_counter() - started, latencies


async def _store_metrics(
    store: MemoryTraceStore | None,
    *,
    expected_trace_count: int,
) -> tuple[int, int, int, float | None]:
    if store is None:
        return 0, 0, 0, None
    traces = await store.list(limit=expected_trace_count)
    trace_count = len(traces)
    incomplete_count = sum(not trace.complete for trace in traces)
    dropped_count = max(0, expected_trace_count - trace_count)
    bytes_per_trace = (
        sum(len(trace_snapshot_to_json(trace)) for trace in traces) / trace_count
        if traces
        else None
    )
    return trace_count, incomplete_count, dropped_count, bytes_per_trace


async def measure_application(
    running: RunningApplication,
    *,
    request_count: int,
    warmup_count: int,
    concurrency: int,
    memory_request_count: int,
) -> WorkloadResult:
    """Measure latency first, then allocations without contaminating latency."""
    await _execute_requests(
        running.app,
        request_count=warmup_count,
        concurrency=min(concurrency, warmup_count),
        retain_latencies=False,
    )
    if running.store is not None:
        await running.store.clear()

    elapsed_seconds, latencies = await _execute_requests(
        running.app,
        request_count=request_count,
        concurrency=concurrency,
        retain_latencies=True,
    )

    if running.store is not None:
        await running.store.clear()
    gc.collect()
    tracemalloc.start()
    try:
        await _execute_requests(
            running.app,
            request_count=memory_request_count,
            concurrency=min(concurrency, memory_request_count),
            retain_latencies=False,
        )
        _, peak_allocated_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    (
        trace_count,
        incomplete_count,
        dropped_count,
        bytes_per_trace,
    ) = await _store_metrics(
        running.store,
        expected_trace_count=memory_request_count,
    )
    return WorkloadResult(
        requests_per_second=request_count / elapsed_seconds,
        p50_ms=percentile(latencies, 50),
        p95_ms=percentile(latencies, 95),
        p99_ms=percentile(latencies, 99),
        peak_allocated_bytes=peak_allocated_bytes,
        stored_trace_count=trace_count,
        incomplete_trace_count=incomplete_count,
        dropped_trace_count=dropped_count,
        approximate_bytes_per_trace=bytes_per_trace,
    )


async def run_scenario(
    scenario: Scenario,
    *,
    request_count: int,
    warmup_count: int,
    memory_request_count: int,
) -> ScenarioResult:
    """Run equivalent baseline and instrumented workloads for one scenario."""
    capacity = max(request_count, warmup_count, memory_request_count, 200)
    baseline_app = build_application(
        scenario,
        instrumented=False,
        store_capacity=capacity,
    )
    try:
        baseline = await measure_application(
            baseline_app,
            request_count=request_count,
            warmup_count=warmup_count,
            concurrency=scenario.concurrency,
            memory_request_count=memory_request_count,
        )
    finally:
        baseline_app.close()

    instrumented_app = build_application(
        scenario,
        instrumented=True,
        store_capacity=capacity,
    )
    try:
        instrumented = await measure_application(
            instrumented_app,
            request_count=request_count,
            warmup_count=warmup_count,
            concurrency=scenario.concurrency,
            memory_request_count=memory_request_count,
        )
    finally:
        instrumented_app.close()

    absolute_overhead = instrumented.p50_ms - baseline.p50_ms
    percentage_overhead = (
        absolute_overhead / baseline.p50_ms * 100 if baseline.p50_ms else 0.0
    )
    throughput_change = (
        (instrumented.requests_per_second - baseline.requests_per_second)
        / baseline.requests_per_second
        * 100
        if baseline.requests_per_second
        else 0.0
    )
    return ScenarioResult(
        name=scenario.name,
        description=scenario.description,
        request_count=request_count,
        concurrency=scenario.concurrency,
        baseline=baseline,
        instrumented=instrumented,
        absolute_p50_overhead_ms=absolute_overhead,
        percentage_p50_overhead=percentage_overhead,
        throughput_change_percent=throughput_change,
    )


def environment_metadata() -> dict[str, str | int | None]:
    """Return reproducibility metadata without host-specific secrets."""
    return {
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "fastapi": fastapi.__version__,
        "starlette": starlette.__version__,
        "sqlalchemy": __import__("sqlalchemy").__version__,
        "jinja2": jinja2.__version__,
        "fastapi_lens": fastapi_lens.__version__,
    }


def results_payload(
    results: Sequence[ScenarioResult],
    *,
    metadata: dict[str, str | int | None],
    warmup_count: int,
    memory_request_count: int,
) -> dict[str, Any]:
    """Build the stable machine-readable benchmark document."""
    return {
        "schema_version": "1.0",
        "environment": metadata,
        "methodology": {
            "transport": "in-process ASGI",
            "clock": "time.perf_counter",
            "percentile": "nearest-rank",
            "warmup_requests": warmup_count,
            "memory_sample_requests": memory_request_count,
            "allocation_measurement": "tracemalloc in a separate workload pass",
        },
        "scenarios": [asdict(result) for result in results],
    }


def _format_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1024:.1f} KiB"


def render_markdown(payload: dict[str, Any]) -> str:
    """Render a human-readable report from the machine-readable payload."""
    metadata = payload["environment"]
    methodology = payload["methodology"]
    lines = [
        "# Benchmark results",
        "",
        (
            "These results are an in-process regression reference, not a production "
            "capacity claim. Re-run the benchmark on deployment-class hardware "
            "before making sizing decisions."
        ),
        "",
        "## Environment",
        "",
        f"- Captured: `{metadata['captured_at']}`",
        f"- Platform: `{metadata['platform']}`",
        f"- Machine: `{metadata['machine']}`",
        f"- CPU count: `{metadata['cpu_count']}`",
        f"- Python: `{metadata['python']}`",
        f"- FastAPI: `{metadata['fastapi']}`",
        f"- Starlette: `{metadata['starlette']}`",
        f"- SQLAlchemy: `{metadata['sqlalchemy']}`",
        f"- fastapi-lens: `{metadata['fastapi_lens']}`",
        "",
        "## Methodology",
        "",
        (
            f"Each scenario warms up with {methodology['warmup_requests']} requests. "
            "Equivalent plain and instrumented FastAPI applications run sequentially "
            "through the in-process ASGI transport. Latency uses `perf_counter` and "
            "nearest-rank percentiles."
        ),
        "",
        (
            "Allocation peaks are collected in a separate pass with `tracemalloc` "
            f"over {methodology['memory_sample_requests']} requests, so allocation "
            "tracking does not distort the latency samples."
        ),
        "",
        "## Results",
        "",
        (
            "| Scenario | Concurrency | Baseline req/s | Instrumented req/s | "
            "Baseline p50 | Instrumented p50 | Absolute p50 overhead | "
            "p50 overhead | Instrumented p95 | Instrumented p99 | "
            "Peak allocation | Bytes/trace | Incomplete | Dropped |"
        ),
        ("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"),
    ]
    for result in payload["scenarios"]:
        baseline = result["baseline"]
        instrumented = result["instrumented"]
        lines.append(
            "| {name} | {concurrency} | {baseline_rps:.1f} | "
            "{instrumented_rps:.1f} | {baseline_p50:.3f} ms | "
            "{instrumented_p50:.3f} ms | {absolute:+.3f} ms | "
            "{percentage:+.1f}% | {p95:.3f} ms | {p99:.3f} ms | "
            "{peak} | {per_trace} | {incomplete} | {dropped} |".format(
                name=result["name"],
                concurrency=result["concurrency"],
                baseline_rps=baseline["requests_per_second"],
                instrumented_rps=instrumented["requests_per_second"],
                baseline_p50=baseline["p50_ms"],
                instrumented_p50=instrumented["p50_ms"],
                absolute=result["absolute_p50_overhead_ms"],
                percentage=result["percentage_p50_overhead"],
                p95=instrumented["p95_ms"],
                p99=instrumented["p99_ms"],
                peak=_format_bytes(instrumented["peak_allocated_bytes"]),
                per_trace=_format_bytes(instrumented["approximate_bytes_per_trace"]),
                incomplete=instrumented["incomplete_trace_count"],
                dropped=instrumented["dropped_trace_count"],
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "Negative overhead can occur in short microbenchmarks because of "
                "scheduler, allocator, and CPU-frequency noise. Compare repeated runs "
                "on an otherwise idle machine and focus on sustained regressions."
            ),
            "",
            (
                "The memory store is process-local. Bytes per trace is the mean "
                "versioned JSON size of traces captured during the allocation pass; "
                "it is not the complete Python object footprint."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--requests",
        type=int,
        default=1_000,
        help="Measured requests per baseline or instrumented workload.",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=100,
        help="Warmup requests before each measured workload.",
    )
    parser.add_argument(
        "--memory-requests",
        type=int,
        default=200,
        help="Requests in the separate allocation-measurement workload.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=tuple(SCENARIOS),
        help="Run one scenario; repeat the option to select multiple scenarios.",
    )
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.requests < 100:
        raise ValueError("--requests must be at least 100.")
    if args.warmup <= 0:
        raise ValueError("--warmup must be greater than zero.")
    if args.memory_requests <= 0:
        raise ValueError("--memory-requests must be greater than zero.")
    selected_names = args.scenario or list(DEFAULT_SCENARIOS)
    if "concurrent-100" in selected_names and args.requests < 100:
        raise ValueError("The concurrent-100 scenario requires at least 100 requests.")

    results = []
    for name in selected_names:
        result = await run_scenario(
            SCENARIOS[name],
            request_count=args.requests,
            warmup_count=args.warmup,
            memory_request_count=args.memory_requests,
        )
        results.append(result)
        print(
            f"{name}: {result.instrumented.requests_per_second:.1f} req/s, "
            f"p50 {result.instrumented.p50_ms:.3f} ms, "
            f"overhead {result.absolute_p50_overhead_ms:+.3f} ms"
        )

    payload = results_payload(
        results,
        metadata=environment_metadata(),
        warmup_count=args.warmup,
        memory_request_count=args.memory_requests,
    )
    markdown = render_markdown(payload)
    return payload, markdown


def main(argv: Sequence[str] | None = None) -> int:
    """Run selected scenarios and optionally write JSON and Markdown reports."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        payload, markdown = asyncio.run(_run(args))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.output_markdown is not None:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown, encoding="utf-8")
    if args.output_json is None and args.output_markdown is None:
        print()
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
