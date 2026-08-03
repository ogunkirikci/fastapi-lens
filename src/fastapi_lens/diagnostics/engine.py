"""Diagnostic rule orchestration."""

from collections.abc import Sequence
from dataclasses import dataclass

from fastapi_lens.diagnostics.base import DiagnosticRule
from fastapi_lens.diagnostics.rules import (
    ExpensiveSerializationRule,
    IntegrityRule,
    PossibleNPlusOneRule,
    RepeatedQueryRule,
    SlowDependencyRule,
    SlowRequestRule,
)
from fastapi_lens.models import Diagnostic, RequestTraceSnapshot


@dataclass(slots=True, frozen=True)
class DiagnosticConfig:
    """Thresholds used to construct the built-in diagnostic rule set."""

    slow_request_threshold_ms: float = 250.0
    slow_dependency_threshold_ms: float = 100.0
    slow_dependency_percentage: float = 25.0
    repeated_query_minimum: int = 2
    n_plus_one_minimum: int = 10
    n_plus_one_percentage: float = 10.0
    serialization_threshold_ms: float = 50.0
    serialization_percentage: float = 20.0


class DiagnosticEngine:
    """Evaluate deterministic rules while isolating individual failures."""

    code = "diagnostic_engine"

    def __init__(
        self,
        *,
        config: DiagnosticConfig | None = None,
        rules: Sequence[DiagnosticRule] | None = None,
    ) -> None:
        selected_config = config if config is not None else DiagnosticConfig()
        self.rules = (
            tuple(rules)
            if rules is not None
            else (
                IntegrityRule(),
                SlowRequestRule(selected_config.slow_request_threshold_ms),
                SlowDependencyRule(
                    selected_config.slow_dependency_threshold_ms,
                    selected_config.slow_dependency_percentage,
                ),
                RepeatedQueryRule(selected_config.repeated_query_minimum),
                PossibleNPlusOneRule(
                    selected_config.n_plus_one_minimum,
                    selected_config.n_plus_one_percentage,
                ),
                ExpensiveSerializationRule(
                    selected_config.serialization_threshold_ms,
                    selected_config.serialization_percentage,
                ),
            )
        )

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        """Return all findings without allowing one rule to stop another."""
        findings: list[Diagnostic] = []
        for rule in self.rules:
            try:
                findings.extend(rule.evaluate(trace))
            except Exception:
                findings.append(
                    Diagnostic(
                        code="diagnostic_rule_failure",
                        severity="error",
                        message=f"Diagnostic rule '{rule.code}' failed.",
                    )
                )
        return findings
