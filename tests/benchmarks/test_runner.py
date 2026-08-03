import asyncio

import pytest

from tests.benchmarks.runner import (
    SCENARIOS,
    environment_metadata,
    percentile,
    render_markdown,
    results_payload,
    run_scenario,
)


def test_percentile_uses_nearest_rank_and_validates_input() -> None:
    values = [40.0, 10.0, 30.0, 20.0]

    assert percentile(values, 50) == 20.0
    assert percentile(values, 95) == 40.0
    assert percentile(values, 99) == 40.0

    with pytest.raises(ValueError, match="at least one"):
        percentile([], 50)
    with pytest.raises(ValueError, match="between 1 and 100"):
        percentile(values, 0)


def test_minimum_capture_benchmark_reports_overhead_and_trace_metrics() -> None:
    result = asyncio.run(
        run_scenario(
            SCENARIOS["minimum-capture"],
            request_count=4,
            warmup_count=1,
            memory_request_count=2,
        )
    )

    assert result.baseline.stored_trace_count == 0
    assert result.instrumented.stored_trace_count == 2
    assert result.instrumented.incomplete_trace_count == 0
    assert result.instrumented.dropped_trace_count == 0
    assert result.instrumented.approximate_bytes_per_trace is not None
    assert result.baseline.p50_ms > 0
    assert result.instrumented.p99_ms > 0


def test_results_render_environment_methodology_and_absolute_overhead() -> None:
    result = asyncio.run(
        run_scenario(
            SCENARIOS["minimum-capture"],
            request_count=2,
            warmup_count=1,
            memory_request_count=1,
        )
    )
    payload = results_payload(
        [result],
        metadata=environment_metadata(),
        warmup_count=1,
        memory_request_count=1,
    )

    markdown = render_markdown(payload)

    assert payload["schema_version"] == "1.0"
    assert payload["methodology"]["percentile"] == "nearest-rank"
    assert payload["environment"]["python"]
    assert "Absolute p50 overhead" in markdown
    assert "minimum-capture" in markdown
    assert "Bytes/trace" in markdown
