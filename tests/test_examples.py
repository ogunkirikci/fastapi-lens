from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool


def test_dependency_example_runs_and_exposes_dashboard() -> None:
    from examples.dependencies.app import app, lens

    with TestClient(app) as client:
        response = client.get("/profile")
        dashboard = client.get("/__lens__/")

    assert response.status_code == 200
    assert response.json()["cached_request_id"] == "example-request"
    assert dashboard.status_code == 200
    assert lens.enabled is False


def test_sqlalchemy_example_runs_without_persisting_local_data() -> None:
    from examples.sqlalchemy import app as example

    original_engine = example.engine
    example.lens.uninstrument_sqlalchemy(original_engine)
    original_engine.dispose()
    example.engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    example.lens.instrument_sqlalchemy(example.engine)

    with TestClient(example.app) as client:
        response = client.get("/items/1")
        dashboard_api = client.get("/__lens__/api/traces")

    assert response.status_code == 200
    assert response.json() == {
        "item": {
            "id": 1,
            "name": "example item",
        }
    }
    assert dashboard_api.status_code == 200
    assert dashboard_api.json()["items"][0]["sql_query_count"] == 1
