"""Inspect the FastAPI capabilities required by the first adapter family."""

from __future__ import annotations

import inspect
import json
from typing import Any

import fastapi
import fastapi.routing
from fastapi import Depends
from fastapi.dependencies import utils


def collect_capabilities() -> dict[str, Any]:
    depends_parameters = inspect.signature(Depends).parameters
    solve_parameters = inspect.signature(utils.solve_dependencies).parameters
    serialization_parameters = inspect.signature(
        fastapi.routing.serialize_response
    ).parameters
    request_response_source = inspect.getsource(fastapi.routing.request_response)

    return {
        "fastapi": fastapi.__version__,
        "dependency_scope": "scope" in depends_parameters,
        "handler_hook": hasattr(fastapi.routing, "run_endpoint_function"),
        "serialization_hook": hasattr(fastapi.routing, "serialize_response"),
        "serialization_endpoint_context": ("endpoint_ctx" in serialization_parameters),
        "serialization_dump_json": "dump_json" in serialization_parameters,
        "solver_scope_cache": "_uses_scopes_cache" in solve_parameters,
        "request_exit_stack": "fastapi_inner_astack" in request_response_source,
        "function_exit_stack": "fastapi_function_astack" in request_response_source,
    }


def main() -> None:
    capabilities = collect_capabilities()
    required = (
        "dependency_scope",
        "handler_hook",
        "serialization_hook",
        "request_exit_stack",
        "function_exit_stack",
    )
    missing = [name for name in required if not capabilities[name]]
    print(json.dumps(capabilities, sort_keys=True))
    if missing:
        raise SystemExit(f"Missing required FastAPI capabilities: {missing}")


if __name__ == "__main__":
    main()
