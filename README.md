# StockPing

High-reliability, production-grade back-in-stock notification engine built with FastAPI, SQLAlchemy 2.x, SQLite, and React + TypeScript.

## Architecture & Design Decisions

- **FastAPI Backend:** Clean separation into configuration, database layer, models, schemas, HTTP services, and route handlers.
- **SQLite Concurrency & Integrity:**
  - Configured with Write-Ahead Logging (`PRAGMA journal_mode=WAL;`), busy timeout (`PRAGMA busy_timeout=5000;`), and foreign key enforcement (`PRAGMA foreign_keys=ON;`).
  - Strict partial unique index (`uq_open_subscription`) prevents duplicate open requests (`pending` or `processing`) atomically at the database engine level, while allowing customers to resubscribe once a prior request has been marked `notified`.
- **Upstream Stock Integration:**
  - `StockClient` queries `GET {STOCK_API_URL}` with `Authorization: Bearer {STOCK_API_KEY}` to verify SKU presence in the product catalogue prior to accepting subscriptions.
  - Returns HTTP 404 for invalid/unknown SKUs and HTTP 502 for upstream failures without exposing API credentials or internal stack traces.
- **Embed UI (`GET /embed`):**
  - Built with React 18, TypeScript, and custom CSS without bulky UI libraries.
  - Pre-compiled via Vite into `app/static` and served directly by FastAPI (zero Node.js dependency at runtime).
  - Configured specifically with `Content-Security-Policy: frame-ancestors *;` and no restrictive `X-Frame-Options` on `/embed`, allowing third-party ecommerce stores to frame the subscription widget. All other application endpoints retain strict framing defenses (`X-Frame-Options: SAMEORIGIN`).

## Configuration Contract

The service operates on the six standard client environment variables:
- `STOCK_API_URL`
- `STOCK_API_KEY`
- `RESEND_API_KEY`
- `RESEND_BASE_URL`
- `WEBHOOK_SECRET`
- `ADMIN_PASSWORD`
- `PORT` (listening port, defaults to `8000`)

## Running the Application

### Setup Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Start Server
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Run Automated Tests
```bash
pytest -v
```
