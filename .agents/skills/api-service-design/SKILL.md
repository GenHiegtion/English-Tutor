---
name: api-service-design
description: REST API/service design standard for Python projects — endpoint conventions, response schema shape, and consistent HTTP error handling. Use when building or extending a service/API (e.g. FastAPI, Flask) inside a Python project.
---

# API / Service Design

Applies only to projects that expose a service/API (e.g. via FastAPI, Flask). Skip this skill for pure scripts/libraries with no HTTP layer.

## Endpoint conventions

- Use plural nouns for resource collections: `/users`, `/orders`, not `/getUser` or `/user`.
- Nest sub-resources under their parent: `/orders/{order_id}/items`, not a flat `/order_items?order_id=...` unless there's a specific reason (e.g. the sub-resource is also queried independently at scale).
- Use HTTP methods per their semantics: `GET` (read, no side effects), `POST` (create), `PUT`/`PATCH` (update — `PUT` replaces, `PATCH` partially updates), `DELETE` (remove). Don't use `GET` with side effects or `POST` for pure reads.
- Version the API from the start if it's meant to be consumed by external clients: `/v1/orders`, not an unversioned path that breaks callers on future changes.
- Keep path parameters for resource identifiers (`/orders/{order_id}`), and query parameters for filtering/sorting/pagination (`/orders?status=paid&limit=20`).

## Response schema

- Every endpoint returns a consistent envelope shape across the whole service — pick one and stick to it, e.g.:
  ```json
  { "data": { ... }, "error": null }
  ```
  or a plain resource body with errors handled separately (see below). Don't mix styles between endpoints in the same service.
- Define response schemas as explicit models (e.g. Pydantic models in FastAPI) rather than returning raw dicts — this documents the contract and catches shape mistakes at the boundary.
- Use consistent field naming (`snake_case` for JSON keys unless the project has an established `camelCase` convention for its API — pick one, don't mix).
- Paginated list endpoints return a consistent shape, e.g. `{ "items": [...], "total": N, "page": N, "page_size": N }`, not a bare array (a bare array can't carry pagination metadata later without breaking clients).

## Error handling

- Use correct HTTP status codes: `400` (bad request/validation error), `401` (not authenticated), `403` (authenticated but not allowed), `404` (resource not found), `409` (conflict, e.g. duplicate), `422` (semantically invalid input), `500` (unexpected server error). Don't return `200` with an error payload inside it.
- Every error response follows one consistent shape across the service, e.g.:
  ```json
  { "error": { "code": "resource_not_found", "message": "Order 123 not found" } }
  ```
- Never leak internal details in error responses sent to clients (stack traces, internal file paths, raw exception messages from a DB driver). Log the full detail server-side (see the error-handling-logging rule) and return a safe, generic message to the client.
- Validate input at the boundary (request schema validation) rather than deep inside business logic — invalid input should fail fast with a `400`/`422` before reaching `core/` logic.
- Centralize error-to-HTTP-status mapping (e.g. one exception handler / middleware) instead of manually building error responses in every endpoint function.
