"""Trace storage interfaces and process-local implementations."""

from .base import TraceStore
from .memory import MemoryTraceStore

__all__ = ["MemoryTraceStore", "TraceStore"]
