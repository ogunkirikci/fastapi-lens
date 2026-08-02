import pytest

from fastapi_lens.utils.timing import (
    IntervalErrorReason,
    InvalidIntervalError,
    interval_union_duration_ns,
    merge_intervals_ns,
    self_duration_ns,
    unattributed_duration_ns,
)


def test_merge_intervals_sorts_and_combines_overlaps_and_adjacency() -> None:
    intervals = [(20, 30), (0, 10), (5, 25), (40, 40), (50, 60)]

    assert merge_intervals_ns(intervals) == ((0, 30), (40, 40), (50, 60))


def test_interval_union_does_not_double_count_parallel_work() -> None:
    intervals = [(0, 10), (5, 15), (20, 25)]

    assert interval_union_duration_ns(intervals) == 20


def test_empty_interval_union_has_zero_duration() -> None:
    assert interval_union_duration_ns([]) == 0


def test_self_duration_subtracts_the_child_interval_union() -> None:
    children = [(10, 40), (30, 60), (80, 90)]

    assert self_duration_ns((0, 100), children) == 40


def test_self_duration_without_children_equals_parent_duration() -> None:
    assert self_duration_ns((10, 30), []) == 20


def test_unattributed_duration_subtracts_top_level_interval_union() -> None:
    measured = [(0, 20), (10, 30), (50, 60)]

    assert unattributed_duration_ns((0, 100), measured) == 60


def test_negative_interval_raises_a_typed_validation_error() -> None:
    with pytest.raises(InvalidIntervalError) as captured:
        interval_union_duration_ns([(20, 10)])

    assert captured.value.interval == (20, 10)
    assert captured.value.reason is IntervalErrorReason.NEGATIVE_DURATION
    assert captured.value.bounds is None
    assert str(captured.value) == ("Invalid interval (20, 10): negative_duration.")


def test_invalid_parent_interval_is_rejected_without_clamping() -> None:
    with pytest.raises(InvalidIntervalError) as captured:
        self_duration_ns((100, 0), [])

    assert captured.value.reason is IntervalErrorReason.NEGATIVE_DURATION


@pytest.mark.parametrize("interval", [(-1, 10), (10, 101)])
def test_out_of_bounds_interval_raises_a_typed_validation_error(
    interval: tuple[int, int],
) -> None:
    with pytest.raises(InvalidIntervalError) as captured:
        interval_union_duration_ns([interval], bounds=(0, 100))

    assert captured.value.interval == interval
    assert captured.value.reason is IntervalErrorReason.OUT_OF_BOUNDS
    assert captured.value.bounds == (0, 100)
    assert str(captured.value) == (
        f"Invalid interval {interval}: out_of_bounds for bounds (0, 100)."
    )
