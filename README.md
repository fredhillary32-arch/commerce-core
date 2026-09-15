# Commerce Platform API

A commerce backend built as a modular monolith with clean domain boundaries.
This repository currently contains **Phase 1: Foundation** only — application
configuration, database/session infrastructure, centralized error handling
and logging, and two operational endpoints. No business domains (auth,
catalogue, cart, checkout, orders, payments, etc.) exist yet; those arrive in
later phases.

## Technology stack

- Python 3.12
- FastAPI
- PostgreSQL (the only supported database — no SQLite)
- SQLAlchemy 2.x (sync engine/session)
- Alembic for migrations
- Pydantic v2 / Pydantic Settings for configuration
- psycopg 3 as the PostgreSQL driver
- pytest + httpx/httpx2 (via FastAPI's `TestClient`) for tests
- `uv` as the package/project manager

## Architecture

```
Router -> Service -> Repository -> SQLAlchemy -> PostgreSQL
```

Business logic belongs in services, database access in repositories, and
routers stay thin. Pydantic schemas (request/response) are kept separate
from SQLAlchemy models. Phase 1 has no business domain yet, so only the
shared foundation pieces exist:

```
app/
├── main.py                 # FastAPI app factory
├── core/
│   ├── config.py            # Pydantic Settings
│   ├── exceptions.py        # AppException hierarchy + handlers
│   └── logging.py           # Structured JSON logging setup
├── db/
│   ├── base.py               # Declarative Base + naming convention
│   └── session.py            # Engine, session factory, get_db dependency
└── api/v1/
    └── health.py             # GET /health, GET /ready
```

Future domains will each follow the same shape:

```
module/
├── router.py
├── schemas.py
├── models.py
├── repository.py
├── service.py
└── dependencies.py
```

## Database safety

There is a **pre-existing** PostgreSQL database called `ecommerce` from an
earlier, unrelated project on this machine. This application must never
read, write, migrate, or drop that database. It uses a dedicated, newly
created database instead:

- App / dev database: `commerce_platform`
- Test database: `commerce_platform_test`

`app/core/config.py` actively rejects any `DATABASE_URL` / `TEST_DATABASE_URL`
whose database name is `ecommerce` — `Settings` raises a `ValueError` at
startup if this happens, so a misconfigured `.env` fails fast instead of
touching the wrong database.

## Local setup

1. **Install `uv`** if you don't have it (`pip install uv`), then let it
   provision the pinned interpreter and dependencies:

   ```
   uv sync
   ```

   The project is pinned to Python 3.12 via `.python-version`; `uv` will
   download it automatically if it isn't already installed.

2. **Provide a PostgreSQL server.** Either use an existing local install, or
   start one with Docker:

   ```
   docker compose up -d
   ```

3. **Create the databases** (skip if `docker compose up` already created
   `commerce_platform` for you via `POSTGRES_DB`):

   ```
   psql -U postgres -h localhost -c "CREATE DATABASE commerce_platform;"
   psql -U postgres -h localhost -c "CREATE DATABASE commerce_platform_test;"
   ```

4. **Configure environment variables.** Copy `.env.example` to `.env` and
   fill in real local values (host, port, username, password). Never commit
   `.env`.

   ```
   APP_NAME=commerce-platform-api
   APP_ENV=development
   DEBUG=true
   DATABASE_URL=postgresql+psycopg://postgres:<password>@localhost:5432/commerce_platform
   TEST_DATABASE_URL=postgresql+psycopg://postgres:<password>@localhost:5432/commerce_platform_test
   LOG_LEVEL=INFO
   API_V1_PREFIX=/api/v1
   ```

## Running migrations

Alembic reads `DATABASE_URL` from `Settings` (via `alembic/env.py`), so it
always targets whatever `.env`/environment currently points at — verify that
value before running anything destructive.

```
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic current
```

The Phase 1 baseline migration (`0001_baseline`) creates no application
tables; it only establishes Alembic's version tracking. Future phases add
their own migrations on top of it.

## Starting the application

```
uv run uvicorn app.main:app --reload
```

- `GET /api/v1/health` — liveness only; never touches PostgreSQL.
- `GET /api/v1/ready` — readiness; executes `SELECT 1` against PostgreSQL
  and returns HTTP 503 with a structured error body if that fails.

## Running tests

Tests run against the real `commerce_platform_test` PostgreSQL database
(no SQLite substitution, no mocked database calls). Make sure `.env` is
configured and both databases from step 3 above exist, then:

```
uv run pytest
```

## Error responses

All application-raised errors share one JSON shape:

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "Resource not found"
  }
}
```

Routers/services raise a subclass of `AppException`
(`app/core/exceptions.py`) rather than `HTTPException` directly; a single
FastAPI exception handler converts it to this response shape.

## What's intentionally not here yet

Authentication, RBAC, product catalogue, cart, inventory, checkout, orders,
payments, refunds, shipping, returns, coupons, notifications, audit logging,
background workers, and reconciliation are all future phases. Phase 1 only
provides the foundation those phases will build on.
