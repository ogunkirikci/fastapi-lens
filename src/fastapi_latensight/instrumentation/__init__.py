"""FastAPI-aware instrumentation adapters."""

from .dependencies import dependency_instrumentation
from .handler import handler_instrumentation
from .serialization import serialization_instrumentation

__all__ = [
    "dependency_instrumentation",
    "handler_instrumentation",
    "serialization_instrumentation",
]
