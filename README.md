# Cab Booking Platform

A local MVP for rider, driver, and administrator workflows. The application is
not production-ready; deployment is outside the project scope.

## Architecture

- **API:** Python 3.12+, FastAPI, Pydantic Settings, SQLAlchemy 2, Alembic.
- **Database:** PostgreSQL 17 with PostGIS for driver locations and spatial
  matching.
- **Web client:** Next.js App Router, React, and TypeScript.
- **Authentication:** short-lived JWT bearer access tokens and rotating,
  server-tracked refresh sessions in an HTTP-only cookie.
- **Realtime:** authenticated WebSockets for ride and offer updates. The local
  MVP uses in-process ticket storage and fanout; multiple API workers do not
  share WebSocket state.
- **Payments:** local mock provider only. It does not process money or collect
  payment credentials.

## Prerequisites and database

- Python 3.12+
- Node.js 20.9+
- PostgreSQL 17 with PostGIS installed

For local development, use a dedicated non-superuser database role and local
database. The example project configuration uses port `5433`. A PostgreSQL
administrator can provision a local database and PostGIS extension:

```sql
CREATE ROLE cab_booking_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
\password cab_booking_app
CREATE DATABASE cab_booking OWNER cab_booking_app;
\connect cab_booking
CREATE EXTENSION postgis;
GRANT USAGE, CREATE ON SCHEMA public TO cab_booking_app;
```

Never point local tests or migrations at a production database. Integration
tests require a loopback PostgreSQL host and create/drop uniquely named
temporary schemas; they do not reset the main database.

## Configuration

Copy `.env.example` to `.env` and replace the sample signing-key placeholder
with a private generated value. Do not commit `.env` or share its contents.
Generate a signing key in PowerShell:

```powershell
[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
```

| Variable | Purpose | Default / constraints |
| --- | --- | --- |
| `DATABASE_URL` | SQLAlchemy PostgreSQL connection | Local PostgreSQL on `127.0.0.1:5433`, database `cab_booking`; provide a URL-encoded password |
| `JWT_SECRET_KEY` | Signs access tokens | Required, at least 32 characters; replace the placeholder |
| `ACCESS_TOKEN_MINUTES` | Access-token lifetime | 15; 1–60 |
| `REFRESH_TOKEN_DAYS` | Refresh-session lifetime | 14; 1–90 |
| `REFRESH_COOKIE_SECURE` | Require HTTPS for refresh cookie | `false` for local HTTP |
| `CORS_ORIGINS` | Allowed browser origins | `http://localhost:3000`; comma-separated |
| `DRIVER_LOCATION_FRESHNESS_SECONDS` | Driver location eligibility age | 30; 1–300 |
| `DRIVER_SEARCH_RADIUS_METERS` | Nearby-driver search radius | 10,000; 100–100,000 |
| `RIDE_OFFER_TIMEOUT_SECONDS` | Per-driver offer timeout | 20; 1–300 |
| `RIDE_MAX_OFFER_ATTEMPTS` | Maximum sequential driver offers | 5; 1–5 |
| `RIDE_REQUEST_LIFETIME_SECONDS` | Overall ride-request lifetime | 300; 30–3,600 |
| `FARE_BASE_PAISE` | Fare base amount | 5,000 paise |
| `FARE_PER_KM_PAISE` | Straight-line per-kilometre rate | 1,500 paise |
| `ENVIRONMENT` | Runtime environment selector | `development`; supported: `development`, `test`, `production` |

The frontend reads `NEXT_PUBLIC_API_BASE_URL` from `web/.env.local`; the
example is `http://localhost:8000`.

## Install and run locally

From the project root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e 'backend[dev]'
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env: set a private JWT_SECRET_KEY and the local database connection.
.\.venv\Scripts\alembic.exe -c backend\alembic.ini upgrade head
.\.venv\Scripts\uvicorn.exe app.main:app --app-dir backend --reload
```

The API is available at `http://localhost:8000`; OpenAPI documentation is at
`http://localhost:8000/docs`.

In a second PowerShell terminal:

```powershell
if (-not (Test-Path web\.env.local)) { Copy-Item web\.env.local.example web\.env.local }
Set-Location web
npm.cmd install
npm.cmd run dev
```

Open `http://localhost:3000`. Create an administrator interactively (password
input is not echoed):

```powershell
Set-Location ..
.\.venv\Scripts\cab-booking.exe create-admin
```

Public registration supports rider and driver roles only. Use the CLI to
create an admin account.

## Database migrations

Migration history:

1. `0001_initial` — users, rider/driver profiles, and refresh sessions.
2. `0002_booking_matching` — driver location, ride matching, ride status
   history, and admin audit records.
3. `0003_simulated_payments` — unique per-ride payment records and retry state.

Apply migrations without resetting data:

```powershell
.\.venv\Scripts\alembic.exe -c backend\alembic.ini upgrade head
.\.venv\Scripts\alembic.exe -c backend\alembic.ini current
.\.venv\Scripts\alembic.exe -c backend\alembic.ini check
```

## Authentication and API

Except registration, login, refresh, logout, and health, HTTP API operations
require `Authorization: Bearer <access_token>`. Refresh tokens are rotated in
the HTTP-only `cab_refresh` cookie. Logout revokes its server-side session;
existing access tokens remain usable until they expire.

| Endpoint | Access |
| --- | --- |
| `POST /api/v1/auth/register` | Public; rider or driver |
| `POST /api/v1/auth/login` | Public |
| `POST /api/v1/auth/refresh` | Refresh cookie |
| `POST /api/v1/auth/logout` | Refresh cookie |
| `GET /api/v1/auth/me` | Authenticated |
| `GET /health` | Public database health check |
| `POST /api/v1/rides/estimate`, `POST /api/v1/rides` | Rider |
| `GET /api/v1/rides`, `GET /api/v1/rides/{ride_id}` | Rider/driver sees own rides; admin sees all rides |
| `GET /api/v1/rides/{ride_id}/history` | Ride participants or admin |
| `POST /api/v1/rides/{ride_id}/cancel` | Owning rider or assigned driver, subject to ride state |
| `POST /api/v1/rides/{ride_id}/admin-cancel` | Admin; in-progress cancellation requires driver-release confirmation |
| `POST /api/v1/drivers/me/location`, `POST /api/v1/drivers/me/availability` | Driver |
| `GET /api/v1/drivers/me/availability`, `GET /api/v1/drivers/me/offers` | Driver |
| `GET /api/v1/drivers/me/summary` | Driver |
| `POST /api/v1/rides/{ride_id}/offers/accept`, `POST /api/v1/rides/{ride_id}/offers/reject` | Offered driver |
| `POST /api/v1/rides/{ride_id}/arriving`, `/arrived`, `/start`, `/complete` | Assigned driver |
| `GET /api/v1/payments`, `GET /api/v1/payments/{payment_id}` | Authenticated payment owner; admin may inspect one payment |
| `POST /api/v1/payments/{payment_id}/process` | Owning rider |
| `/api/v1/admin/dashboard`, `/users`, `/drivers`, `/rides`, `/payments` | Admin |
| `POST /api/v1/admin/users/{user_id}/activation` | Admin; reason required |
| `POST /api/v1/admin/drivers/{driver_id}/verification` | Admin; reason required |
| `POST /api/v1/ws/tickets` | Authenticated; creates one-use 30-second WebSocket ticket |
| `WS /api/v1/ws/rides/{ride_id}?ticket=...` | Ride participant or admin |
| `WS /api/v1/ws/drivers/offers?ticket=...` | Driver |

WebSocket clients first request a ticket using bearer authentication, then use
the one-time ticket to connect. Tickets are stored in process memory, expire
after 30 seconds, and cannot be reused. Avoid logging query strings. The
in-memory ticket store and WebSocket fanout mean reconnecting sockets must
reach the same API process; this is a local MVP limitation.

## Ride lifecycle and matching

Valid forward transitions are `REQUESTED → ACCEPTED → DRIVER_ARRIVING →
DRIVER_ARRIVED → IN_PROGRESS → COMPLETED`. Riders or assigned drivers may
cancel before `IN_PROGRESS`; admins may cancel at permitted states, with
explicit confirmation required to release a driver during an in-progress
admin cancellation. Terminal rides cannot be changed.

Matching uses PostGIS distance and considers verified, active, online,
available drivers with a location no older than the configured freshness
threshold and within the configured radius. One candidate is offered at a
time; a rejection or timeout moves to the next eligible, not-yet-offered
candidate. Matching stops after the attempt cap or request lifetime. `NO_DRIVER_FOUND`
means the candidate/attempt search was exhausted; `EXPIRED` means the overall
request lifetime elapsed before acceptance. A driver's assignment and
reservation are protected by PostgreSQL transaction/constraint checks.

Fare is an approximate straight-line distance calculation, using the
configurable base plus per-kilometre rate, rounded half-up to integer paise and
stored in INR with the fare inputs.

## Payments, dashboards, and admin

Completing a ride creates exactly one pending payment from its stored fare
snapshot in the same database transaction as completion. The states are
`PENDING`, `PROCESSING`, `SUCCEEDED`, and `FAILED`, with at most three attempts.
The rider's process request contains no amount; the server validates the
completed ride, owner, fare, and currency. Unique ride and reference
constraints prevent duplicate records; retries reuse the same reference as
the provider idempotency key.

The configured local mock always simulates success. Failure and retry paths
are tested with controlled provider outcomes. This is not a real payment
provider, does not move money, and stores no card data. Driver gross completed
fares are not payouts or net earnings.

The rider dashboard supports ride requests, status/history, cancellation,
payment status, mock checkout/retry, and payment history. The driver
dashboard supports location, availability, offers, ride progress, completed
rides, and gross completed-fare summary. The admin dashboard exposes bounded
read-only operational lists and summaries, audited account activation,
driver verification, and ride cancellation. Deactivation revokes refresh
sessions and is refused while a user has an unfinished ride or a driver has a
pending offer.

Driver offers show the remaining time calculated by the API and disable
responses when that countdown reaches zero; the API remains authoritative and
rejects offers that have expired. Rider ride cards show the assigned driver's
name and available vehicle make/model.

Administrative actions are recorded in `admin_audit_logs`; ride transitions
are separately recorded in `ride_status_history`.

## Tests and local verification

From the project root:

```powershell
.\.venv\Scripts\python.exe -m pytest -c backend\pyproject.toml backend\tests -q
.\.venv\Scripts\ruff.exe check backend
.\.venv\Scripts\ruff.exe format --check backend
Set-Location web
npm.cmd run lint
npm.cmd run build
```

The default tests use isolated SQLite databases for non-spatial behavior.
Enable PostgreSQL 17/PostGIS integration tests only for the intended local
database:

```powershell
$env:RUN_POSTGRES_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m pytest -c backend\pyproject.toml backend\tests -q
Remove-Item Env:RUN_POSTGRES_INTEGRATION
```

The fixture refuses non-PostgreSQL, non-loopback, and production settings. It
creates and drops only isolated test schemas and requires permission to create
schemas in the configured local database. Verify Alembic state with the
migration commands above.

## Known limitations

- Payments are simulated; no actual payment processing or payout support.
- Realtime fanout and WebSocket tickets are process-local, not shared across
  workers.
- Driver location is manually entered in the MVP dashboard; there is no
  continuous GPS tracking.
- Fare uses straight-line distance rather than road routing.
- The app is a local MVP and has not been deployed or certified for production.
