import asyncio
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import fastapi
import pytest
from fastapi.dependencies import utils as dependency_utils
from fastapi.dependencies.models import Dependant

from fastapi_latensight.collector import TraceCollector
from fastapi_latensight.instrumentation.dependencies import (
    DependencyInstrumentation,
    _callable_name,
    _DependencyTraceState,
    _status_for_error,
)
from fastapi_latensight.models import (
    DependencyCacheStatus,
    RequestTrace,
    SegmentStatus,
)


def make_collector() -> TraceCollector:
    return TraceCollector(
        RequestTrace(
            schema_version="1.0",
            id="trace-1",
            method="GET",
            path="/items",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_received_ns=1_000_000,
        )
    )


def test_helper_fallbacks_cover_missing_callables_and_cancellation() -> None:
    assert _callable_name(None) == "<missing dependency>"
    assert _status_for_error(asyncio.CancelledError()) is SegmentStatus.CANCELLED


def test_unsupported_fastapi_version_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrumentation = DependencyInstrumentation()
    monkeypatch.setattr(fastapi, "__version__", "0.142.0")

    with pytest.raises(
        RuntimeError,
        match=r"FastAPI 0\.142\.0 has no dependency adapter\.",
    ):
        instrumentation.install(object())


def test_unsupported_solver_signature_fails_fast() -> None:
    instrumentation = DependencyInstrumentation()

    async def unsupported_solver(*, request: object) -> None:
        del request

    instrumentation._original_utils = unsupported_solver

    with pytest.raises(
        RuntimeError,
        match=r"FastAPI solve_dependencies has an unsupported signature\.",
    ):
        instrumentation.install(object())


def test_unreadable_solver_source_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrumentation = DependencyInstrumentation()

    def unreadable_source(_target: object) -> str:
        raise OSError("source unavailable")

    monkeypatch.setattr(inspect, "getsource", unreadable_source)

    with pytest.raises(
        RuntimeError,
        match=r"FastAPI solve_dependencies source cannot be verified\.",
    ):
        instrumentation.install(object())


def test_solver_behavior_guard_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrumentation = DependencyInstrumentation()
    monkeypatch.setattr(
        inspect, "getsource", lambda _target: "async def solver(): pass"
    )

    with pytest.raises(
        RuntimeError,
        match=r"FastAPI solve_dependencies behavior guard did not match\.",
    ):
        instrumentation.install(object())


def test_legacy_dependant_capability_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_generator = cast(
        Dependant,
        SimpleNamespace(call=lambda: None, is_async_gen_callable=True),
    )
    sync_generator = cast(
        Dependant,
        SimpleNamespace(call=lambda: None, is_gen_callable=True),
    )
    coroutine = cast(
        Dependant,
        SimpleNamespace(call=lambda: None, is_coroutine_callable=True),
    )

    monkeypatch.delattr(dependency_utils, "_is_async_gen_callable")
    monkeypatch.delattr(dependency_utils, "_is_gen_callable")
    monkeypatch.delattr(dependency_utils, "_is_coroutine_callable")

    assert DependencyInstrumentation._is_async_generator(async_generator) is True
    assert DependencyInstrumentation._is_sync_generator(sync_generator) is True
    assert DependencyInstrumentation._is_coroutine(coroutine) is True


def test_legacy_cache_key_and_oauth_scope_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def call() -> None:
        return None

    cache_key = (call, (), "request")
    dependant = cast(
        Dependant,
        SimpleNamespace(
            cache_key=cache_key,
            oauth_scopes=["items:read"],
        ),
    )
    monkeypatch.delattr(dependency_utils, "_get_cache_key")
    monkeypatch.delattr(dependency_utils, "_get_oauth_scopes")

    assert (
        DependencyInstrumentation._cache_key(
            dependant,
            uses_scopes_cache=None,
        )
        == cache_key
    )
    assert DependencyInstrumentation._oauth_scopes(dependant) == ["items:read"]

    dependant.oauth_scopes = None  # type: ignore[attr-defined]
    dependant.security_scopes = ["legacy:read"]  # type: ignore[attr-defined]
    assert DependencyInstrumentation._oauth_scopes(dependant) == ["legacy:read"]


def test_legacy_override_builder_uses_security_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrumentation = DependencyInstrumentation()

    def original_call() -> None:
        return None

    def override_call() -> None:
        return None

    dependant = cast(
        Dependant,
        SimpleNamespace(
            path="/items",
            name="value",
            scope="request",
            oauth_scopes=None,
            security_scopes=["legacy:read"],
        ),
    )
    observed: dict[str, Any] = {}

    def legacy_get_dependant(
        *,
        path: str,
        call: Any,
        name: str | None,
        security_scopes: list[str],
        scope: str | None,
    ) -> Dependant:
        observed.update(
            path=path,
            call=call,
            name=name,
            security_scopes=security_scopes,
            scope=scope,
        )
        return cast(Dependant, SimpleNamespace(call=original_call))

    monkeypatch.delattr(dependency_utils, "_get_oauth_scopes")
    monkeypatch.setattr(dependency_utils, "get_dependant", legacy_get_dependant)

    result = instrumentation._override_dependant(
        dependant,
        call=override_call,
    )

    assert result.call is original_call
    assert observed == {
        "path": "/items",
        "call": override_call,
        "name": "value",
        "security_scopes": ["legacy:read"],
        "scope": "request",
    }


def test_cache_hit_without_local_provenance_is_labeled_external() -> None:
    state = _DependencyTraceState(collector=make_collector())
    source = DependencyInstrumentation._cached_from(
        DependencyCacheStatus.HIT,
        cache_key=(lambda: None, (), ""),
        state=state,
    )

    assert source is not None
    assert source.startswith("external:")
