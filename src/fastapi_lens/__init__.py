"""Public package interface for fastapi-lens."""

from typing import Any, Final

from .config import LensConfig
from .lens import Lens
from .segments import current_trace, trace_segment

__all__ = [
    "Lens",
    "LensConfig",
    "__version__",
    "current_trace",
    "instrument_sqlalchemy",
    "trace_segment",
    "uninstrument_sqlalchemy",
]

__version__: Final = "0.1.0a0"


def instrument_sqlalchemy(engine: Any, *, owner: object) -> None:
    """Capture queries for an explicitly registered SQLAlchemy engine."""
    from .instrumentation.sqlalchemy import sqlalchemy_instrumentation

    sqlalchemy_instrumentation.register(engine, owner)


def uninstrument_sqlalchemy(engine: Any, *, owner: object) -> None:
    """Release one owner's SQLAlchemy engine registration."""
    from .instrumentation.sqlalchemy import sqlalchemy_instrumentation

    sqlalchemy_instrumentation.unregister(engine, owner)
