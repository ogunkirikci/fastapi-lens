"""Public package interface for fastapi-lens."""

from typing import Final

from .segments import current_trace, trace_segment

__all__ = ["__version__", "current_trace", "trace_segment"]

__version__: Final = "0.1.0a0"
