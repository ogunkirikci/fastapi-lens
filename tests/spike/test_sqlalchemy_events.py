import asyncio

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from .probes import SqlCapture


def test_sync_engine_capture_is_explicit_and_reference_counted() -> None:
    engine = create_engine("sqlite://")
    capture = SqlCapture()
    capture.register(engine)
    capture.register(engine)

    events, token = capture.start()
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("select 1")) == 1
    finally:
        capture.stop(token)

    assert len(events) == 1
    assert events[0].statement == "select 1"
    assert events[0].duration_ns >= 0
    assert not events[0].failed

    capture.unregister(engine)
    events_after_one_remove, token = capture.start()
    try:
        with engine.connect() as connection:
            connection.execute(text("select 2"))
    finally:
        capture.stop(token)
    assert len(events_after_one_remove) == 1

    capture.unregister(engine)
    events_after_final_remove, token = capture.start()
    try:
        with engine.connect() as connection:
            connection.execute(text("select 3"))
    finally:
        capture.stop(token)
    assert events_after_final_remove == []
    engine.dispose()


def test_async_engine_capture_uses_sync_engine_target() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        capture = SqlCapture()
        capture.register(engine)
        events, token = capture.start()
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("select 1")) == 1
        finally:
            capture.stop(token)
            capture.unregister(engine)
            await engine.dispose()

        assert len(events) == 1
        assert events[0].statement == "select 1"
        assert not events[0].failed

    asyncio.run(run())


def test_failed_query_is_captured_without_changing_exception() -> None:
    engine = create_engine("sqlite://")
    capture = SqlCapture()
    capture.register(engine)
    events, token = capture.start()
    try:
        try:
            with engine.connect() as connection:
                connection.execute(text("select * from missing_table"))
        except Exception as error:
            captured_type = type(error)
        else:
            raise AssertionError("The invalid query must fail")
    finally:
        capture.stop(token)
        capture.unregister(engine)
        engine.dispose()

    assert captured_type.__name__ == "OperationalError"
    assert len(events) == 1
    assert events[0].failed
    assert "missing_table" in events[0].statement
