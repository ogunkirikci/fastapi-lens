from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine

from fastapi_lens import instrument_sqlalchemy, uninstrument_sqlalchemy
from fastapi_lens.instrumentation.sqlalchemy import (
    SqlAlchemyInstrumentation,
    fingerprint_sql,
    normalize_sql,
    sql_operation,
    sqlalchemy_instrumentation,
    truncate_sql,
)


def test_sql_normalization_masks_literals_comments_and_whitespace() -> None:
    normalized = normalize_sql(
        """
        SELECT *
        FROM users -- private comment
        WHERE email = 'person@example.com'
          AND score >= -12.5e2
          AND active = TRUE
          AND deleted_at IS NULL
        """
    )

    assert normalized == (
        "SELECT * FROM users WHERE email = ? AND score >= ? "
        "AND active = ? AND deleted_at IS ?"
    )


def test_fingerprint_uses_full_normalized_statement() -> None:
    first = normalize_sql("select * from items where id = 1 and name = 'first'")
    second = normalize_sql("select * from items where id = 2 and name = 'second'")

    assert first == second
    assert fingerprint_sql(first) == fingerprint_sql(second)
    assert len(fingerprint_sql(first)) == 64


@pytest.mark.parametrize(
    ("statement", "operation"),
    [
        ("select * from items", "SELECT"),
        ("  (insert into items values (?))", "INSERT"),
        ("", "UNKNOWN"),
    ],
)
def test_sql_operation_uses_the_first_keyword(
    statement: str,
    operation: str,
) -> None:
    assert sql_operation(statement) == operation


def test_display_sql_truncation_preserves_the_limit() -> None:
    assert truncate_sql("select", max_length=10) == "select"
    assert truncate_sql("select 12345", max_length=8) == "select …"
    assert len(truncate_sql("select 12345", max_length=8)) == 8


def test_invalid_configuration_and_engine_types_fail_fast() -> None:
    with pytest.raises(
        ValueError,
        match=r"max_sql_length must be greater than zero\.",
    ):
        SqlAlchemyInstrumentation(max_sql_length=0)

    with pytest.raises(
        TypeError,
        match=r"Expected a SQLAlchemy Engine or AsyncEngine\.",
    ):
        SqlAlchemyInstrumentation().register(cast(Any, object()), object())


def test_public_registration_api_is_lazy_and_owner_aware() -> None:
    engine = create_engine("sqlite://")
    first_owner = object()
    second_owner = object()

    instrument_sqlalchemy(engine, owner=first_owner)
    instrument_sqlalchemy(engine, owner=first_owner)
    instrument_sqlalchemy(engine, owner=second_owner)
    try:
        assert sqlalchemy_instrumentation.is_registered(engine) is True
        uninstrument_sqlalchemy(engine, owner=first_owner)
        assert sqlalchemy_instrumentation.is_registered(engine) is True
        uninstrument_sqlalchemy(engine, owner=second_owner)
        assert sqlalchemy_instrumentation.is_registered(engine) is False
    finally:
        sqlalchemy_instrumentation.restore_all()
        engine.dispose()


def test_corrupted_connection_stack_is_replaced_without_failure() -> None:
    instrumentation = SqlAlchemyInstrumentation()
    info: dict[str, Any] = {instrumentation._stack_key: "invalid"}

    stack = instrumentation._stack(info)

    assert stack == []
    assert info[instrumentation._stack_key] is stack
    assert instrumentation._pop_execution(info) is None


def test_pre_ping_and_missing_connection_errors_are_ignored() -> None:
    instrumentation = SqlAlchemyInstrumentation()

    instrumentation._handle_error(SimpleNamespace(is_pre_ping=True))
    instrumentation._handle_error(SimpleNamespace(is_pre_ping=False, connection=None))
    instrumentation._handle_error(
        SimpleNamespace(
            is_pre_ping=False,
            connection=SimpleNamespace(info={}),
            execution_context=None,
        )
    )
    instrumentation._handle_error(
        SimpleNamespace(
            is_pre_ping=False,
            connection=SimpleNamespace(info={}),
            execution_context=object(),
        )
    )


def test_execution_stack_requires_matching_context_and_cleans_up() -> None:
    instrumentation = SqlAlchemyInstrumentation()
    expected_context = object()
    execution: Any = SimpleNamespace(context_id=id(expected_context))
    info: dict[str, Any] = {
        instrumentation._stack_key: cast(Any, [execution]),
    }

    assert instrumentation._pop_execution(info, context=object()) is None
    assert (
        instrumentation._pop_execution(
            info,
            context=expected_context,
        )
        is execution
    )
    assert instrumentation._stack_key not in info


def test_event_callback_failures_do_not_escape() -> None:
    instrumentation = SqlAlchemyInstrumentation()
    invalid_connection = SimpleNamespace(info=None)

    instrumentation._after_cursor_execute(
        cast(Any, invalid_connection),
        SimpleNamespace(rowcount=-1),
        "select 1",
        (),
        object(),
        False,
    )
    instrumentation._handle_error(
        SimpleNamespace(
            is_pre_ping=False,
            connection=invalid_connection,
            execution_context=object(),
        )
    )


def test_sync_engine_target_is_returned_unchanged() -> None:
    engine = create_engine("sqlite://")
    try:
        assert SqlAlchemyInstrumentation._target(engine) is engine
    finally:
        engine.dispose()
