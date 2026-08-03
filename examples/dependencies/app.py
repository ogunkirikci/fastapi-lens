"""Dependency setup, caching, and cleanup profiling example."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI

from fastapi_latensight import Latensight, LatensightConfig

profiler: Latensight


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    profiler.close()


app = FastAPI(lifespan=lifespan)


async def request_id() -> str:
    return "example-request"


async def database_session() -> AsyncIterator[str]:
    session = "example-session"
    try:
        yield session
    finally:
        pass


async def current_user(
    selected_request_id: Annotated[str, Depends(request_id)],
    session: Annotated[str, Depends(database_session)],
) -> dict[str, str]:
    return {
        "request_id": selected_request_id,
        "session": session,
        "user": "example-user",
    }


@app.get("/profile")
async def profile(
    user: Annotated[dict[str, str], Depends(current_user)],
    cached_request_id: Annotated[str, Depends(request_id)],
) -> dict[str, object]:
    return {
        "cached_request_id": cached_request_id,
        "user": user,
    }


profiler = Latensight(
    app,
    config=LatensightConfig(
        dashboard_enabled=True,
        environment="development",
        capture_dependencies=True,
    ),
)
