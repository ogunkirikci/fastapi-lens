"""Explicit SQLAlchemy engine instrumentation example."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, text

from fastapi_latensight import Latensight, LatensightConfig

engine: Engine = create_engine(
    "sqlite:///fastapi_latensight_example.db",
    connect_args={"check_same_thread": False},
)
profiler: Latensight


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS items "
                "(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT OR IGNORE INTO items (id, name) VALUES (1, 'example item')")
        )
    yield
    profiler.close()
    engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.get("/items/{item_id}")
def read_item(item_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT id, name FROM items WHERE id = :item_id"),
                {"item_id": item_id},
            )
            .mappings()
            .one_or_none()
        )
    return {"item": dict(row) if row is not None else None}


profiler = Latensight(
    app,
    config=LatensightConfig(
        dashboard_enabled=True,
        environment="development",
        capture_sql=True,
    ),
)
profiler.instrument_sqlalchemy(engine)
