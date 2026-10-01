# Commerce Platform API

A commerce backend built as a modular monolith with clean domain boundaries.
This repository currently contains:

- **Phase 1: Foundation** — application configuration, database/session
  infrastructure, centralized error handling and logging, and two
  operational endpoints.
- **Phase 2: Authentication + RBAC** — accounts, Argon2id passwords, access
  tokens, rotating database-backed refresh tokens with reuse detection,
  role-based authorization with per-user overrides, and a security audit
  trail.
- **Phase 3: Product catalogue** - products with a status lifecycle, options
  and option values, variants with SKUs and pricing, categories with a
  hierarchy, product image metadata backed by Supabase Storage, a public
  storefront read API and catalogue administration endpoints.
- **Phase 4: Cart + inventory** — one active cart per customer, stock levels
  in disjoint buckets, expiring reservations taken under a row lock, and an
  immutable stock ledger that reconciles against the quantity columns.
- **Phase 5: Checkout + orders** — a single-transaction checkout that
  reserves stock and writes the order together or not at all, immutable
  order snapshots, saved addresses, and tax and shipping configuration.

Payments, refunds, shipping fulfilment, returns, coupons, notifications and
background workers arrive in later phases.

Each phase has a contract in [`docs/superpowers/plans/`](docs/superpowers/plans/),
which is the detailed reference; this README is the overview.

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
from SQLAlchemy models.

```
app/
├── main.py                  # FastAPI app factory
├── cli.py                   # Operator-only management commands
├── core/
│   ├── config.py             # Pydantic Settings
│   ├── security.py           # Argon2id hashing, JWT, refresh-token secrets
│   ├── permissions.py        # Permission resolution + require_permission
│   ├── storage.py            # Supabase Storage wrapper (product image files)
│   ├── exceptions.py         # AppException hierarchy + handlers
│   └── logging.py            # Structured JSON logging setup
├── db/
│   ├── base.py               # Declarative Base + naming convention
│   ├── session.py            # Engine, session factory, get_db dependency
│   └── models/               # SQLAlchemy models (user, rbac, refresh_token,
│                             #   audit, catalog, cart, inventory, order,
│                             #   address, tax, shipping)
├── modules/
│   ├── auth/                 # register, login, refresh, logout, change-password
│   ├── users/                # GET /users/me + RBAC administration
│   ├── catalog/              # Products, options, variants, categories, images
│   ├── cart/                 # The customer's basket
│   ├── inventory/            # Stock levels, reservations, the stock ledger
│   ├── addresses/            # Saved delivery and billing addresses
│   ├── tax/                  # Tax rates by destination
│   ├── shipping/             # Shipping rules by destination and order value
│   ├── orders/               # Checkout, orders, cancellation, confirmation
│   └── audit/                # Security event recording
└── api/v1/
    └── health.py             # GET /health, GET /ready
```

Each module follows the same shape — `router.py`, `schemas.py`,
`repository.py`, `service.py`, `dependencies.py` — and only creates the
files it actually needs.

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

The one sanctioned form of contact with `ecommerce` is a **read-only schema
fingerprint**, taken before and after every phase so that "we did not touch
it" is evidence rather than an assurance:

```
uv run python scripts/fingerprint_legacy_db.py
```

The script refuses to open any database but `ecommerce`, connects with
`default_transaction_read_only = on`, and reads only `information_schema`
and `pg_catalog`. The serialisation, the current baseline and the procedure
are in
[`docs/superpowers/plans/legacy-db-fingerprint.md`](docs/superpowers/plans/legacy-db-fingerprint.md).

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
   JWT_SECRET_KEY=<generated secret>
   ACCESS_TOKEN_EXPIRE_MINUTES=15
   REFRESH_TOKEN_EXPIRE_DAYS=30
   SUPABASE_URL=https://<project-ref>.supabase.co
   SUPABASE_SERVICE_KEY=<service role key>
   SUPABASE_PRODUCT_IMAGE_BUCKET=product-images
   SUPABASE_SIGNED_URL_EXPIRY_SECONDS=3600
   ```

   `JWT_SECRET_KEY` has **no default** — the application refuses to start
   without it, so a signing key can never be accidentally baked into source
   control. Generate one with:

   ```
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Rotating it invalidates every access token already issued.

   `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` have **no default** either, for
   the same reason. Take both from the Supabase dashboard (Settings → API);
   the *service role* key is a secret and stays server-side.

5. **Create the image bucket.** In the Supabase dashboard, add a Storage
   bucket named to match `SUPABASE_PRODUCT_IMAGE_BUCKET` and leave it
   **private**. Images are delivered through short-lived signed URLs, so a
   public bucket would hand out permanent access the application never
   intended to grant. The tests do not need this — they stub out every
   Supabase call — but running the application does.

## Running migrations

Alembic reads `DATABASE_URL` from `Settings` (via `alembic/env.py`), so it
always targets whatever `.env`/environment currently points at — verify that
value before running anything destructive.

```
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic current
```

Migrations so far:

| Revision | Purpose |
| --- | --- |
| `0001_baseline` | Establishes Alembic version tracking. Creates no tables. |
| `0002_phase_2_auth_and_rbac_schema` | `users`, `roles`, `permissions`, `user_roles`, `role_permissions`, `user_permission_overrides`, `refresh_tokens`, `audit_logs`. |
| `0003_seed_rbac_bootstrap` | Seeds the `admin` role, the `rbac.manage` permission and the grant between them. Idempotent; creates no user. |
| `0004_phase_3_catalogue_schema` | The eight catalogue tables. |
| `0005_seed_catalogue_permissions` | `catalog.read`, `catalog.write`, `images.write`. Granted to no role. |
| `0006_phase_4_cart_and_inventory_schema` | `carts`, `cart_items`, `inventory`, `inventory_reservations`, `inventory_transactions`, `inventory_adjustments`, plus a zero-quantity backfill for existing variants. |
| `0007_seed_inventory_permissions` | `inventory.read`, `inventory.adjust`. Granted to no role. |
| `0008_phase_5_checkout_and_orders_schema` | `addresses`, `tax_rates`, `shipping_rules`, `orders`, `order_items`. |
| `0009_seed_order_permissions` | `orders.read`, `orders.manage`, `tax.manage`, `shipping.manage`. Granted to no role. |

`0003` is a data migration kept separate from the schema migration on
purpose. Its `downgrade` is only meaningful in a fresh environment: once a
real user holds the admin role, deleting that role hits the `RESTRICT`
foreign key on `user_roles` and the downgrade fails. That is the intended
protection, not a defect — Alembic's transactional DDL leaves the database
untouched when it happens.

## Starting the application

```
uv run uvicorn app.main:app --reload
```

- `GET /api/v1/health` — liveness only; never touches PostgreSQL.
- `GET /api/v1/ready` — readiness; executes `SELECT 1` against PostgreSQL
  and returns HTTP 503 with a structured error body if that fails.

## Authentication

| Endpoint | Auth required | Purpose |
| --- | --- | --- |
| `POST /api/v1/auth/register` | no | Create an account. |
| `POST /api/v1/auth/login` | no | Exchange credentials for a token pair. |
| `POST /api/v1/auth/refresh` | no (refresh token) | Rotate the session. |
| `POST /api/v1/auth/logout` | yes | End the session the token belongs to. |
| `POST /api/v1/auth/change-password` | yes | Change password, ending every session. |
| `GET /api/v1/users/me` | yes | The authenticated account. |

**Passwords** are hashed with Argon2id (`argon2-cffi` defaults). Plaintext
passwords and hashes are never returned, never logged and never written to
audit records. Logging in with an address that has no account costs the same
as logging in with the wrong password, and returns the identical response,
so the API cannot be used to enumerate accounts.

**Access tokens** are short-lived JWTs carrying `sub`, `iat`, `exp`, `jti`
and `auth_version` — identity only. Permissions are deliberately *not* in
the token; they are resolved from the database on each authorized request
so a revoked role takes effect on the next request rather than at the next
token expiry. Every authenticated request also re-checks `is_active`, so
disabling an account locks it out immediately.

**Refresh tokens** are stored only as a SHA-256 hash and belong to a
*family*: one family per login. Refreshing rotates the token, marking the
old one `rotated_at` and pointing `replaced_by_id` at its replacement. A
user may hold many families at once (phone, laptop, tablet); logging in on
a new device never disturbs the others. Presenting an already-rotated token
is treated as a compromise signal: the request is refused with
`REFRESH_TOKEN_REUSED`, the whole family is revoked, and the user's other
families are left alone. Rotation takes a `SELECT ... FOR UPDATE` row lock,
so two simultaneous refreshes of one token cannot both succeed.

Changing a password revokes every session across every family, but does
*not* bump `authorization_version` — what the user may do has not changed.

## Authorization (RBAC)

Permissions are `resource.action` codes (`orders.read`, `inventory.adjust`)
and never imply one another. They reach a user through roles, with per-user
exceptions on top. The resolver applies exactly this precedence:

1. user `DENY` override
2. user `ALLOW` override
3. role grant
4. default deny

`users.authorization_version` is bumped whenever a user's roles or overrides
change, which retires access tokens minted under the old state — the holder
gets `STALE_AUTHORIZATION` and must sign in again.

These endpoints all require the `rbac.manage` permission:

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/roles` | Create a role. |
| `GET /api/v1/roles` | List roles. |
| `POST /api/v1/roles/{role_id}/permissions` | Grant a permission to a role. |
| `POST /api/v1/users/{user_id}/roles` | Assign a role to a user. |
| `POST /api/v1/users/{user_id}/permission-overrides` | Set a user `ALLOW`/`DENY` override. |

## Bootstrapping the first administrator

Every RBAC endpoint requires `rbac.manage`, so the first administrator
cannot be created through the API. There is no hidden endpoint, no
hard-coded admin address and no "first user becomes admin" rule; the
appointment is a deliberate, audited, out-of-band act.

1. Run the migrations:

   ```
   uv run alembic upgrade head
   ```

2. Confirm the seeded role and permission exist:

   ```
   psql -U postgres -h localhost -d commerce_platform \
     -c "SELECT name FROM roles WHERE name='admin';" \
     -c "SELECT code FROM permissions WHERE code='rbac.manage';"
   ```

3. Register the intended first user through the normal flow:

   ```
   curl -X POST http://localhost:8000/api/v1/auth/register \
     -H "Content-Type: application/json" \
     -d '{"email":"admin@example.com","password":"<a real passphrase>"}'
   ```

4. Note the `id` from that response — the user's UUID.

5. Run the one-off management command (`app/cli.py`):

   ```
   uv run python -m app.cli assign-admin <user_id>
   ```

   It is a real Python command rather than a raw SQL statement precisely so
   the appointment is recorded: it inserts the `user_roles` row, bumps the
   user's `authorization_version`, and writes an
   `rbac.bootstrap.admin_assigned` entry through the same audit mechanism
   the rest of the application uses. Running it twice changes nothing.

6. Log in again (the version bump retires any token issued before the
   appointment) and the RBAC endpoints are now reachable.

## Management commands

Operator-only actions that have no HTTP endpoint, in `app/cli.py`:

```
uv run python -m app.cli assign-admin <user_id>       # bootstrap the first admin
uv run python -m app.cli disable-account <user_id>    # suspend; revokes all sessions
uv run python -m app.cli enable-account <user_id>     # restore
```

Each writes an audit entry. They act on the database named by
`DATABASE_URL`, so check that value first.

## Security audit trail

`audit_logs` records the security-relevant events — registration, login
success and failure, logout, password change, refresh-token reuse, role and
override changes, account enable/disable, and the bootstrap appointment —
with the actor, the target, the request's IP and user agent, and short
non-sensitive context. Passwords, password hashes and raw tokens are never
written to it.

The catalogue writes to the same table rather than keeping a history of its
own, and only for meaningful state changes: `product.created`,
`product.published`, `product.archived`, `product.discontinued`,
`variant.created`, `variant.discontinued`, `category.created`,
`category.archived`, `category.parent_changed`, `image.uploaded` and
`image.deleted`. Renaming a product or editing its description is not one of
them.

## Running tests

Tests run against the real `commerce_platform_test` PostgreSQL database
(no SQLite substitution, no mocked database calls). The suite migrates that
database to head itself, then resets it between tests back to the state the
seed migrations leave behind. Supabase is never contacted: the catalogue
tests replace `app/core/storage.py`'s four functions with an in-memory
stand-in, so no test needs a bucket, credentials or a network. Make sure `.env` is configured and
both databases from step 3 above exist, then:

```
uv run pytest
```

Some tests are deliberately *falsifiable*: they pair a safety claim with a
companion test that breaks the protection and asserts the failure appears —
the reservation row lock, checkout's single commit boundary, and the
deterministic lock-acquisition order that prevents deadlocks. A concurrency
test that still passes once the protection is removed is proving nothing,
and this project has shipped that mistake before.

## Product catalogue

Eight tables: `products`, `product_options`, `product_option_values`,
`product_variants`, `variant_option_values`, `categories`,
`product_categories` and `product_images`. Inventory is deliberately not
among them - how many of something exist is a separate concern from what the
catalogue says about it.

### Status lifecycles

Both lifecycles live in an explicit transition table in
`app/modules/catalog/service.py`, not in scattered conditionals:

| Entity | From | May become |
| --- | --- | --- |
| Product | `DRAFT` | `ACTIVE`, `ARCHIVED` |
| Product | `ACTIVE` | `ARCHIVED`, `DISCONTINUED` |
| Product | `ARCHIVED` | `ACTIVE`, `DISCONTINUED` |
| Product | `DISCONTINUED` | - (terminal) |
| Variant | `DRAFT` | `ACTIVE` |
| Variant | `ACTIVE` | `DISCONTINUED` |
| Variant | `DISCONTINUED` | - (terminal) |

A product cannot become `ACTIVE` without at least one `ACTIVE` variant, and a
variant cannot become `ACTIVE` without both a price and a currency. Only
`ACTIVE` products appear on the public storefront. Discontinuing a product's
last active variant does **not** unpublish the product - withdrawing it from
sale is a merchant's decision, never a side effect.

### Variant uniqueness

`product_variants.variant_signature` is a deterministic fingerprint of a
variant's option-value assignments, and `UNIQUE (product_id,
variant_signature)` is what makes "no duplicate combination within a product"
a database guarantee rather than an application convention. The column is
**nullable** and stays null until the variant has at least one assignment:
PostgreSQL treats every null as distinct, so any number of half-configured
draft variants can sit on one product without colliding. The signature is
recomputed in the same transaction as every assignment change.

`variant_option_values.option_id` is deliberately redundant with the option
its `option_value_id` already belongs to. That is the catalogue's only
denormalisation, and it earns its place: it turns "at most one value per
option per variant" into a plain unique constraint, while a composite foreign
key onto `product_option_values (option_id, id)` guarantees the two columns
can never disagree.

### Endpoints

Public, unauthenticated, always filtered to `ACTIVE` products and active
categories:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/catalog/products` | Published products, paged via `limit`/`offset`. |
| `GET /api/v1/catalog/products/{product_id}` | One published product with options, active variants, images and categories. |
| `GET /api/v1/catalog/categories` | Active categories with their hierarchy. |
| `GET /api/v1/catalog/categories/{category_id}` | One active category and the published products in it. |

Administration, each requiring the noted permission:

| Endpoint | Permission |
| --- | --- |
| `POST /api/v1/products` | `catalog.write` |
| `GET /api/v1/products/{product_id}` | `catalog.read` |
| `PATCH /api/v1/products/{product_id}` | `catalog.write` |
| `DELETE /api/v1/products/{product_id}` | `catalog.write` |
| `POST /api/v1/products/{product_id}/options` | `catalog.write` |
| `POST /api/v1/products/{product_id}/options/{option_id}/values` | `catalog.write` |
| `POST /api/v1/products/{product_id}/variants` | `catalog.write` |
| `PATCH /api/v1/variants/{variant_id}` | `catalog.write` |
| `POST /api/v1/variants/{variant_id}/option-values` | `catalog.write` |
| `POST /api/v1/categories` | `catalog.write` |
| `PATCH /api/v1/categories/{category_id}` | `catalog.write` |
| `DELETE /api/v1/categories/{category_id}` | `catalog.write` |
| `POST /api/v1/products/{product_id}/categories` | `catalog.write` |
| `POST /api/v1/products/{product_id}/images/upload-url` | `images.write` |
| `POST /api/v1/products/{product_id}/images/confirm` | `images.write` |
| `DELETE /api/v1/products/{product_id}/images/{image_id}` | `images.write` |

Migration `0005` registers `catalog.read`, `catalog.write` and
`images.write` but grants them to **no role** - deciding which staff may
publish products is an administrator's decision, not a migration's.

A product or category may only be deleted while it is untouched: a `DRAFT`
product with no variants, category assignments or images, and a category with
no child categories or assigned products. Anything else is withdrawn
(`ARCHIVED`/`DISCONTINUED`) or deactivated instead, so history survives.

## Cart, inventory and reservations

Six tables: `carts`, `cart_items`, `inventory`, `inventory_reservations`,
`inventory_transactions` and `inventory_adjustments`.

**The cart** is authentication-only — ownership *is* the authorization. No
route accepts a cart id or a user id, so there is nothing to tamper with,
and no `cart.*` permission exists: new users start with zero roles, so any
such code would refuse every new customer access to their own basket. A
partial unique index allows one `ACTIVE` cart per customer while keeping
`CONVERTED` history. Cart items carry **no price**; what something costs is
read from the catalogue at display time.

**Stock lives in disjoint buckets, not nested ones.** `quantity_on_hand` is
good sellable stock; damaged and quarantined units sit *alongside* it rather
than inside it, so a warehouse stock-take matches the sum of all three:

```
available            = quantity_on_hand - quantity_reserved
total physical stock = quantity_on_hand + quantity_damaged + quantity_quarantined
```

Moving a unit between buckets genuinely decrements one column and increments
another, which is what lets the immutable ledger reconcile exactly: for each
bucket, inbound minus outbound equals that bucket's column.

**Reservations** hold stock for a cart for five minutes. Reserving is a
genuine check-then-act, so the inventory row is locked with
`SELECT ... FOR UPDATE` *before* availability is computed and the lock is
held until commit. Nothing schedules expiry — workers are a later phase — so
a past-due hold is swept opportunistically inside that same lock, at the
moment somebody else wants the stock. Holding, releasing and expiring move
nothing physical and so never touch the ledger; only conversion, at payment,
permanently deducts stock.

| Endpoint | Permission |
| --- | --- |
| `GET`/`POST`/`PATCH`/`DELETE` `/api/v1/cart[/items[/{id}]]` | authenticated (own cart) |
| `POST /api/v1/inventory` | `inventory.adjust` |
| `GET /api/v1/inventory/{variant_id}` | `inventory.read` |
| `POST /api/v1/inventory/{variant_id}/adjustments` | `inventory.adjust` |
| `GET /api/v1/inventory/{variant_id}/transactions` | `inventory.read` |
| `GET /api/v1/inventory/{variant_id}/reservations` | `inventory.read` |

There is deliberately no reservation endpoint: reservations are created,
converted and released through the service layer, by checkout.

## Checkout and orders

Five tables: `addresses`, `tax_rates`, `shipping_rules`, `orders` and
`order_items`.

**Checkout is one transaction.** Every reservation, the order, its lines and
the cart's transition to `CONVERTED` commit together or not at all, so a
partially successful checkout is not something to detect and clean up — it
cannot happen. The cart row is locked first, and inventory rows are locked
**sorted by variant id**, so two baskets containing the same two variants in
opposite order cannot deadlock.

**An order is a snapshot.** Product name, SKU, option summary, unit price,
tax rate and both addresses are copied onto it. Renaming a product,
repricing a variant, amending a tax rate or deleting a saved address can
never change what a placed order says. The money arithmetic is enforced by
CHECK constraints rather than trusted to the service, and every amount is a
`Decimal` rounded `ROUND_HALF_UP` per line and then summed, so the printed
lines add up to the printed total.

**Idempotency** is one constraint: `orders.cart_id UNIQUE`. One cart
produces exactly one order, so a double-submitted checkout returns the order
that already exists (200) rather than buying twice.

**Absence is never a silent default.** No tax rate configured for a
destination rejects the sale rather than charging 0%; no shipping rule
rejects it rather than shipping free. "No tax applies here" is expressed as
a deliberate `0.0000` rate row that somebody chose.

An order is `PENDING_PAYMENT` until a later phase confirms it — `status`
*is* the payment boundary and there is no separate payment column.
Cancelling an unpaid order releases its stock and hands the basket back to
the customer as a fresh active cart.

| Endpoint | Permission |
| --- | --- |
| `POST /api/v1/checkout` | authenticated (own cart) |
| `GET /api/v1/orders`, `GET /api/v1/orders/{id}` | authenticated (own orders) |
| `POST /api/v1/orders/{id}/cancel` | authenticated (own order) |
| `GET`/`POST`/`PATCH`/`DELETE` `/api/v1/addresses[/{id}]` | authenticated (own addresses) |
| `GET`/`GET {id}` `/api/v1/admin/orders` | `orders.read` |
| `POST /api/v1/admin/orders/{id}/cancel` | `orders.manage` |
| `GET`/`POST`/`PATCH` `/api/v1/tax-rates` | `tax.manage` |
| `GET`/`POST`/`PATCH` `/api/v1/shipping-rules` | `shipping.manage` |

Migration `0009` registers the four administrative codes and grants them to
**no role**. None implies another: viewing a customer's order is not licence
to cancel it, and tax configuration is finance work while shipping
configuration is operations work. Customer routes carry no permission code
at all, for the same zero-roles reason the cart does not.

## Product images and Supabase Storage

Image files never enter PostgreSQL. They live in a **private** Supabase
Storage bucket; the database stores only `storage_path`. The integration is
one module, `app/core/storage.py`, with four functions:
`create_upload_target`, `object_exists`, `delete_object` and
`get_delivery_url`.

Because the bucket is private there is no permanent public URL. Every
response that shows an image calls `get_delivery_url`, which asks Supabase
for a fresh signed read URL valid for
`SUPABASE_SIGNED_URL_EXPIRY_SECONDS`. **These URLs expire, so nothing stores
or caches one** - neither the database nor a client.

Uploading is a three-step handshake with no placeholder database row:

1. `POST .../images/upload-url` - the **server** generates the path, always
   under `products/<product_id>/images/`, and returns it with a short-lived
   signed upload URL. The client never chooses a path. No row is written.
2. The client uploads the file directly to Supabase using that URL, which
   Supabase binds to that exact path.
3. `POST .../images/confirm` - the service checks the path begins with this
   product's prefix (a mismatch is rejected immediately, without contacting
   storage), then confirms the object really exists, and only then writes the
   `product_images` row.

Because the server invents every path and Supabase binds each signed upload
URL to it, an object can only exist under a product's prefix if someone asked
for a target for *that* product - which is what makes the prefix check
sufficient to stop a cross-product replay.

Deletion checks the row's `product_id` against the URL's, removes the file
from Supabase, and only then deletes the row. If the row delete then fails,
the gallery shows a broken image until someone tidies up; the opposite
ordering would leave an orphaned file that nothing references and nobody
notices, which is worse.

Configuration lives in `.env` alongside everything else. `SUPABASE_URL` and
`SUPABASE_SERVICE_KEY` have **no defaults** - the application refuses to
start without them, exactly like `JWT_SECRET_KEY`. The service-role key is
used only server-side and is never handed to a client.

The bucket must be created as private in the Supabase dashboard. The
application assumes this and will sign URLs for a public bucket just as
happily, without noticing that the signing was pointless.

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
FastAPI exception handler converts it to this response shape. A raiser can
narrow the category's default code when the caller needs to distinguish
cases:

| Code | Status | Meaning |
| --- | --- | --- |
| `VALIDATION_ERROR` | 422 | Request body failed validation. |
| `DUPLICATE_EMAIL` | 409 | That address already has an account. |
| `INVALID_CREDENTIALS` | 401 | Wrong password, or no such account. |
| `ACCOUNT_DISABLED` | 401 | The account is suspended. |
| `AUTHORIZATION_REQUIRED` | 401 | No bearer token was supplied. |
| `INVALID_ACCESS_TOKEN` | 401 | Token missing claims, expired, or badly signed. |
| `STALE_AUTHORIZATION` | 401 | The user's authorization changed; sign in again. |
| `INVALID_REFRESH_TOKEN` | 401 | Unknown, revoked or expired refresh token. |
| `REFRESH_TOKEN_REUSED` | 401 | A rotated token was replayed; the family is revoked. |
| `PERMISSION_DENIED` | 403 | Authenticated, but lacks the permission. |
| `ROLE_NOT_FOUND` / `PERMISSION_NOT_FOUND` | 404 | No such role / permission. |
| `ROLE_ALREADY_EXISTS` / `ROLE_ALREADY_ASSIGNED` / `PERMISSION_ALREADY_GRANTED` | 409 | The relationship already exists. |
| `PRODUCT_NOT_FOUND` / `VARIANT_NOT_FOUND` / `CATEGORY_NOT_FOUND` | 404 | No such catalogue record, or it is not publicly visible. |
| `OPTION_NOT_FOUND` / `OPTION_VALUE_NOT_FOUND` / `IMAGE_NOT_FOUND` | 404 | No such record under the product named in the URL. |
| `IMAGE_UPLOAD_NOT_FOUND` | 404 | Confirm was called with a path belonging to another product, or before the object was uploaded. |
| `DUPLICATE_SLUG` / `DUPLICATE_SKU` | 409 | That slug or SKU is already taken. |
| `DUPLICATE_VARIANT_COMBINATION` | 409 | The product already has a variant with that combination of option values. |
| `OPTION_ALREADY_ASSIGNED` | 409 | The variant already has a value for that option. |
| `CATEGORY_ALREADY_ASSIGNED` | 409 | The product is already in that category. |
| `PRIMARY_IMAGE_ALREADY_SET` / `IMAGE_ALREADY_CONFIRMED` | 409 | One primary image per product; one row per storage path. |
| `PRODUCT_HAS_DEPENDENTS` / `CATEGORY_HAS_DEPENDENTS` | 409 | Deletion is blocked; withdraw or deactivate it instead. |
| `INVALID_STATUS_TRANSITION` | 422 | That move is not in the entity's transition table. |
| `PRODUCT_HAS_NO_ACTIVE_VARIANT` | 422 | A product cannot be published without one. |
| `VARIANT_PRICE_REQUIRED` | 422 | A variant needs a price and currency before activation. |
| `INVALID_CATEGORY_PARENT` | 422 | The proposed parent would close a cycle. |
| `INVALID_SLUG` | 422 | No slug could be derived from the name; supply one. |
| `STORAGE_UNAVAILABLE` | 503 | Supabase Storage could not be reached. |
| `INVALID_CART_ITEM_QUANTITY` | 422 | A cart line quantity must be at least 1 (0 removes it). |
| `VARIANT_NOT_AVAILABLE` | 422 | Not purchasable: the variant or its product is not `ACTIVE`, or it has no price. |
| `CART_NOT_FOUND` / `CART_ITEM_NOT_FOUND` | 404 | No live cart, or no such line in it. |
| `CART_NOT_ACTIVE` | 422 | The cart has already become an order and cannot hold new stock. |
| `CART_EMPTY` | 422 | There is nothing to buy. |
| `MIXED_CURRENCY_CART` | 422 | The basket is priced in more than one currency. |
| `INSUFFICIENT_INVENTORY` | 422 | Not enough available stock; the message names the SKU. |
| `INVENTORY_NOT_FOUND` / `INVENTORY_ALREADY_EXISTS` | 404 / 409 | One inventory row per variant. |
| `INVALID_INVENTORY_ADJUSTMENT` | 422 | The movement is malformed, or would leave stock negative or over-reserved. |
| `INVALID_RESERVATION_QUANTITY` / `INVALID_RESERVATION_STATE` | 422 | A reservation is for at least one unit, and its terminal states are terminal. |
| `ADDRESS_NOT_FOUND` | 404 | No such saved address belonging to the caller. |
| `DEFAULT_ADDRESS_CONFLICT` | 409 | Two simultaneous "make this my default" requests; retry. |
| `ORDER_NOT_FOUND` | 404 | No such order belonging to the caller. |
| `INVALID_ORDER_STATE` | 422 | That move is not in the order transition table. |
| `NO_TAX_RATE` / `NO_SHIPPING_RULE` | 422 | The destination is not configured; the sale is refused rather than guessed. |
| `SHIPPING_RULE_CURRENCY_MISMATCH` | 422 | The rule is priced in another currency; nothing is converted. |
| `TAX_RATE_NOT_FOUND` / `TAX_RATE_ALREADY_EXISTS` | 404 / 409 | One active rate per jurisdiction. |
| `SHIPPING_RULE_NOT_FOUND` / `INVALID_SHIPPING_RULE` | 404 / 422 | A rule's maximum order subtotal must exceed its minimum. |

## Deferred to a later phase

**Email verification and password reset are not implemented.** The
`users.email_verified` column exists because it belongs to the account
model and is the foundation a verification workflow will build on, but
Phase 2 ships no verification tokens, no reset tokens, no email delivery and
no `verify-email` / `request-password-reset` / `reset-password` endpoints.
Nothing in Phase 2 can set `email_verified` to true, so login deliberately
does **not** require it — gating login on that flag would lock out every
account permanently.

**The catalogue carries no inventory, and no price history.** A variant's
`price` is what it costs *now*; when orders arrive they will snapshot the
price actually charged, so later catalogue edits can never rewrite what a
customer paid. Whether something can actually be bought is the composition of
product status, variant status, stock and business rules - Phase 3
deliberately keeps those separate rather than inventing compound statuses,
and they get combined where purchasability is really decided.

Variant-specific images are also out of scope; images belong to products.

**A password change does not end existing access tokens.** It revokes every
refresh token immediately, but deliberately leaves `authorization_version`
alone, because what the user *may do* has not changed. An access token
minted just before the change therefore keeps working until it expires
(15 minutes by default). This is recorded and tested rather than assumed.

Payments, refunds, shipping fulfilment, returns, coupons, promotions, search
infrastructure, notification workflows, background workers and scheduled
reconciliation are all future phases. The `ABANDONED` cart status has no
writer yet, and the stock reservation window is a fixed five minutes with no
background sweep — expiry happens opportunistically when the next customer
wants that stock.
