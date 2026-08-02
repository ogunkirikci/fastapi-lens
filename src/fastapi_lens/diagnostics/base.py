"""Diagnostic rule protocol."""

from typing import Protocol

from fastapi_lens.models import Diagnostic, RequestTraceSnapshot


class DiagnosticRule(Protocol):
    """Evaluate one finalized lifecycle snapshot."""

    code: str

    def evaluate(self, trace: RequestTraceSnapshot) -> list[Diagnostic]:
        """Return findings produced for one trace."""
        ...
