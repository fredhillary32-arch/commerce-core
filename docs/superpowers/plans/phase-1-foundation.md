# Phase 1: Foundation — Implementation Plan

## Objective

Build the foundation layer for the commerce platform backend: project
scaffolding, configuration, database/session infrastructure, centralized
error handling, structured logging, and two operational endpoints
(`/health`, `/ready`). No business domain (auth, catalogue, cart, checkout,
orders, payments, etc.) is implemented in this phase.

## Constraints

- PostgreSQL only, no SQLite substitution, including in tests.
- Must use a new `commerce_platform` database (and `commerce_platform_test`
  for automated tests) — must never read, write, or migrate the pre-existing
  `ecommerce` database.
- `uv` for dependency/project management, `pyproject.toml` as the source of
  truth (no `requirements.txt`).
- SQLAlchemy 2.x sync engine/session, dependency-injected per request.
- TDD: failing test written and confirmed to fail before implementation,
  for every feature below.

## Tasks

1. **Project scaffolding**
   - `uv`-managed project pinned to Python 3.12 (`.python-version`).
   - Domain-oriented `app/` package: `core/`, `db/`, `api/v1/`.
   - `.gitignore`, `.env.example` pointing at `commerce_platform`.

2. **Configuration** (`app/core/config.py`)
   - `Settings(BaseSettings)` covering `APP_NAME`, `APP_ENV`, `DEBUG`,
     `DATABASE_URL`, `TEST_DATABASE_URL`, `LOG_LEVEL`, `API_V1_PREFIX`.
   - Validation that actively rejects a database URL whose database name is
     `ecommerce`, so misconfiguration fails fast instead of silently
     touching the wrong database.
   - Tests: env vars loaded correctly; URLs target `commerce_platform`;
     `ecommerce` is rejected for both `DATABASE_URL` and `TEST_DATABASE_URL`.

3. **Database/session infrastructure** (`app/db/base.py`, `app/db/session.py`)
   - `Base` declarative class with a naming convention for constraints/
     indexes, ready for future models.
   - `engine` (module-level, connection pool — not a shared `Session`) and
     `SessionLocal` factory.
   - `get_db()` FastAPI dependency yielding one session per request, closed
     in a `finally` block.
   - Test: obtain a session against the real test database and execute
     `SELECT 1`.

4. **Centralized error handling** (`app/core/exceptions.py`)
   - `AppException` base class plus `ValidationError`, `AuthenticationError`,
     `AuthorizationError`, `NotFoundError`, `ConflictError`,
     `BusinessRuleError`, `InfrastructureError` subclasses, each with a
     `status_code` and stable `code`.
   - `register_exception_handlers(app)` wires an `AppException` handler
     (structured `{"error": {"code", "message"}}` response) and a fallback
     handler for any unhandled exception (masks internal details, still
     returns the same structured shape).
   - Tests: raising `NotFoundError` from a route produces the expected 404
     body; an unhandled exception produces a 500 without leaking internals.

5. **Structured logging** (`app/core/logging.py`)
   - `JSONFormatter` emitting timestamp/level/logger/message (+ optional
     `request_id` for future correlation-ID support) as JSON, with a
     redaction pass for sensitive keys.
   - `configure_logging()` sets the root logger level from `Settings` and
     installs the formatter on a single stdout handler.
   - Tests: log level follows `LOG_LEVEL`; formatter output is valid,
     structured JSON with the expected fields.

6. **FastAPI application** (`app/main.py`, `app/api/v1/health.py`)
   - `create_app()` factory: configures logging, registers exception
     handlers, mounts the health router under `API_V1_PREFIX`.
   - `GET /health`: liveness only, no database dependency.
   - `GET /ready`: executes `SELECT 1` via `get_db()`; raises
     `InfrastructureError` (→ structured 503) if PostgreSQL is unreachable.
   - Tests: `/health` returns `200 {"status": "ok"}`; `/ready` returns
     `200` with a real database connection and a structured `503` against
     an unreachable one (a real broken connection, not a mock).

7. **Alembic**
   - `alembic/env.py` resolves its `sqlalchemy.url` from `Settings`
     (so the `ecommerce`-rejection validation applies to migrations too)
     and targets `Base.metadata` for future autogenerate support.
   - Baseline migration (`0001_baseline`) that creates no application
     tables — it only establishes Alembic's version table.
   - Test: `upgrade head` → `downgrade base` → `upgrade head` cycle succeeds
     against the real `commerce_platform_test` database (never
     `DATABASE_URL`/`ecommerce`).

8. **Documentation**
   - `README.md`: stack, architecture, database safety rule, local setup,
     migrations, running the app, running tests.
   - This plan document.

## Explicitly out of scope for Phase 1

Authentication/JWT, RBAC, product catalogue, cart, inventory, checkout,
orders, coupons, tax, shipping, payments, refunds, returns, notifications,
outbox processing, and any business tables. These are later phases, started
only after Phase 1 is verified end-to-end.
