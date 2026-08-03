# Serialization Instrumentation

`fastapi-latensight` records one combined serialization segment around FastAPI's
`serialize_response` boundary. The segment includes response-model validation
and encoding performed by that hook. It does not claim separate validation,
encoding, or rendering durations.

FastAPI bypasses `serialize_response` when an endpoint returns a `Response`
instance directly. Explicit `JSONResponse`, custom response classes, and
`StreamingResponse` therefore have no serialization segment. This means
serialization is not applicable for that response path; it does not mean that
serialization took zero milliseconds.

For streaming responses, the endpoint handler segment ends when the response
object is created. Stream iteration and body transmission remain visible
through ASGI lifecycle checkpoints and are never labeled as serialization.

The adapter forwards FastAPI's keyword arguments unchanged. Capability checks
cover the optional `endpoint_ctx` and `dump_json` parameters across the
supported FastAPI range.
