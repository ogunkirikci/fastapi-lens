"""Route filtering with conservative raw-path prefiltering."""

import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class _CompiledPattern:
    expression: re.Pattern[str]
    contains_route_parameter: bool

    def matches(self, value: str) -> bool:
        return self.expression.fullmatch(value) is not None


def _compile_pattern(pattern: str) -> _CompiledPattern:
    expression = re.escape(pattern).replace(r"\*", ".+")
    return _CompiledPattern(
        expression=re.compile(expression),
        contains_route_parameter="{" in pattern and "}" in pattern,
    )


class RouteFilter:
    """Apply exclude-first include rules to paths and route templates."""

    def __init__(
        self,
        *,
        include: Sequence[str] = ("*",),
        exclude: Sequence[str] = (),
    ) -> None:
        self._include = tuple(_compile_pattern(pattern) for pattern in include)
        self._exclude = tuple(_compile_pattern(pattern) for pattern in exclude)

    def allows_raw_path(self, path: str) -> bool:
        """Return a conservative decision before routing has completed."""
        if any(pattern.matches(path) for pattern in self._exclude):
            return False
        return any(
            pattern.contains_route_parameter or pattern.matches(path)
            for pattern in self._include
        )

    def allows_route(self, route: str | None, raw_path: str) -> bool:
        """Return the final exclude-first decision after routing."""
        target = route if route is not None else raw_path
        if any(pattern.matches(target) for pattern in self._exclude):
            return False
        return any(pattern.matches(target) for pattern in self._include)
