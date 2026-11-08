# StockPing — Back-in-Stock Notification Engine

StockPing is a high-reliability, production-grade back-in-stock notification engine designed to integrate into ecommerce stores. It enables customers to subscribe to out-of-stock items via an embedded iframe, ingests inventory restock webhooks from warehouse systems, authoritatively verifies stock availability, and dispatches mobile-friendly notification emails via a Resend-compatible API using a transactional outbox pattern.

---

## Architecture & Request Flow

```mermaid
flowchart TD
    subgraph ClientLayer["Clients & Third-Party Stores"]
        StoreIframe["Store Iframe / Customer"] -->|GET /embed\nPOST /api/requests| WebApp["FastAPI Engine"]
        Warehouse["Warehouse / ERP"] -->|POST /webhooks/stock\nX-Webhook-Secret| WebApp
        Admin["Store Administrator"] -->|GET /admin\n(HTTP Basic Auth)| WebApp
    end

    subgraph AppEngine["Single Docker Container / Process"]
        WebApp --> DB[("SQLite (WAL Mode)\nsubscription_requests\nwebhook_events\nemail_outbox")]
        Worker["In-Process Background Outbox Worker\n(FastAPI lifespan asyncio task)"]
        Worker -->|Poll & Claim Leases| DB
    end

    subgraph UpstreamServices["Upstream External APIs"]
        StockAPI["Stock API Service\nGET {STOCK_API_URL}/{sku}"]
        ResendAPI["Resend Email API\nPOST {RESEND_BASE_URL}/emails"]
        WebApp -->|Verify Stock| StockAPI
        Worker -->|Retry Stock Checks| StockAPI
        Worker -->|Dispatch HTML Notifications| ResendAPI
    end
```

### Request Flow
1. **Subscription (`GET /embed` & `POST /api/requests`):**
   - The customer loads the subscription form (rendered by a lightweight React/TypeScript bundle served by FastAPI).
   - Upon submission, the engine validates email syntax, normalizes case and whitespace, and verifies that the SKU exists in the product catalogue via `GET {STOCK_API_URL}`.
   - Atomically creates a pending subscription. If an open subscription (`pending` or `processing`) already exists, it returns a friendly acknowledgment without creating duplicate records.
2. **Webhook Ingestion (`POST /webhooks/stock`):**
   - Authenticates `X-Webhook-Secret` via constant-time HMAC comparison.
   - Idempotently stores the event in SQLite (`webhook_events`). If the `event_id` was previously recorded, it immediately returns `200 OK` (`duplicate_acknowledged`).
   - Authoritatively verifies inventory via `GET {STOCK_API_URL}/{sku}` before scheduling notifications. Webhook `quantity` is **never** trusted as authoritative stock.
   - If in stock, conditionally transitions eligible subscriptions from `pending` to `processing` and schedules jobs in `email_outbox`.
   - If out of stock, marks the event `out_of_stock` and preserves subscriptions for future restocks.
   - If the stock API is down, preserves the event as `pending` for worker retry.
3. **Outbox Worker Dispatch:**
   - Runs periodically inside the FastAPI process.
   - Atomically claims pending jobs with row-level leases (`lease_timeout_at`).
   - Dispatches HTML notifications via `POST {RESEND_BASE_URL}/emails`.
   - On confirmed 200/201 response, marks the job `sent` and transitions the subscription to `notified` with a UTC timestamp.
   - On HTTP 429 or 5xx, schedules retry with exponential backoff (respecting `Retry-After`).
   - On permanent failure (4xx unverified sender/auth), marks the job and subscription `failed`.

---

## Configuration Contract

The application operates strictly using the client-provided environment contract:

| Variable | Description | Default (Dev Fallback) |
| :--- | :--- | :--- |
| `STOCK_API_URL` | Base URL for upstream stock inventory service | `http://localhost:8001/stock` |
| `STOCK_API_KEY` | Bearer token for authenticating with stock service | `dev_stock_key` |
| `RESEND_BASE_URL` | Base URL for the Resend-compatible email API | `https://api.resend.com` |
| `RESEND_API_KEY` | API credential for email dispatching | `dev_resend_key` |
| `WEBHOOK_SECRET` | Shared secret for incoming webhook authenticity | `dev_webhook_secret` |
| `ADMIN_PASSWORD` | Password protecting administrative interface | `dev_admin_password` |
| `PORT` | Listening port for Uvicorn HTTP server | `8000` |
| `FROM_EMAIL` | Optional sender email address for Resend | `StockPing <onboarding@resend.dev>` |
| `DATABASE_URL` | Optional SQLite database URL | `sqlite+aiosqlite:///./stockping.db` |

---

## Local Development Instructions

### Prerequisites
- Python 3.12+ (Python 3.14 tested)
- Node.js 20+ (for building frontend)

### 1. Setup Python Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Build Frontend
```bash
cd frontend
npm install
npm run build
cd ..
```
The compiled assets will be written directly into `app/static/`, allowing FastAPI to serve them without requiring Node.js at runtime.

### 3. Run Development Server
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 4. Run Test Suite
```bash
pytest -v
```

---

## Docker Build & Deployment

StockPing packages as a standalone multi-stage Docker container. Exactly one Uvicorn worker is configured because the background outbox worker runs in-process.

### 1. Build Docker Image
```bash
docker build -t stockping:latest .
```

### 2. Run Container with Volume Persistence
```bash
docker run -d \
  --name stockping \
  -p 8000:8000 \
  -e PORT=8000 \
  -e STOCK_API_URL="https://stock.example.com" \
  -e STOCK_API_KEY="your-stock-key" \
  -e RESEND_BASE_URL="https://api.resend.com" \
  -e RESEND_API_KEY="your-resend-key" \
  -e WEBHOOK_SECRET="your-webhook-secret" \
  -e ADMIN_PASSWORD="your-admin-password" \
  -v stockping_data:/data \
  stockping:latest
```

### SQLite Persistence & Volume Usage
Inside the container, `DATABASE_URL` defaults to `sqlite+aiosqlite:////data/stockping.db`.
- **Why a Volume Mount is Mandatory:** Docker container filesystems are ephemeral. If a container is replaced, updated, or re-created, any SQLite file stored inside the container layer is destroyed.
- **Volume Mount:** Mount a named volume (e.g. `-v stockping_data:/data`) or host directory (`-v /path/on/host:/data`) to preserve subscriptions, webhook audit events, and outbox delivery states across container updates.

---

## Reliability & Delivery Guarantees

### Webhook Authentication & Deduplication
- **HMAC Verification:** `POST /webhooks/stock` validates the `X-Webhook-Secret` header using constant-time comparison (`hmac.compare_digest`). Invalid headers immediately return HTTP 401.
- **Database-Level Deduplication:** `webhook_events.event_id` is a primary key. When a webhook arrives, an atomic `INSERT ... ON CONFLICT DO NOTHING` is performed. Duplicate event deliveries (serial or concurrent) immediately return HTTP 200 with `{"status": "duplicate_acknowledged"}` without creating duplicate notifications.

### Two-Layer Concurrency Defense
1. **Atomic Conditional State Claim:** When an event verifies inventory, it conditionally updates the subscription:
   ```sql
   UPDATE subscription_requests
   SET status = 'processing'
   WHERE id = :subscription_id AND status = 'pending';
   ```
   Only the single transaction with `rowcount == 1` is permitted to create an outbox job. Concurrent or subsequent restock events receive `rowcount == 0` and skip the subscription.
2. **Database Engine Unique Constraint:**
   ```sql
   CREATE UNIQUE INDEX uq_active_outbox_per_sub 
   ON email_outbox(subscription_id) 
   WHERE status IN ('pending', 'processing');
   ```
   Ensures the database engine physically rejects multiple active notification jobs for the same customer subscription.

### Delivery Semantics & The Uncertain-Send Window
- **Technical Reality:** A local SQLite transaction cannot be atomically committed with an external third-party HTTP call (Resend).
- **Minimizing the Uncertainty Window:**
  1. The worker claims a job lease atomically in SQLite (`status = 'processing'`).
  2. The write transaction is committed **before** calling the Resend HTTP API (no database write lock is held during external HTTP requests).
  3. Resend dispatches the email.
  4. Immediately upon receiving HTTP 200/201 from Resend, the worker updates `email_outbox.status = 'sent'` and `subscription_requests.status = 'notified'`.
- If a network drop occurs *after* Resend accepts the message but *before* the client receives the HTTP response, the worker lease will expire and the notification will be retried (at-least-once delivery semantics). We do not claim mathematically impossible exactly-once delivery over external HTTP networks.

### Crash & Restart Recovery
If the application crashes or restarts while a notification is in-flight:
- The job's `lease_timeout_at` expires.
- Upon boot, `recover_expired_leases()` resets the job to `pending`.
- The background worker resumes and dispatches the notification without losing work.

---

## Admin Dashboard

Access the administrative interface at:
```
http://localhost:8000/admin
```
- **Authentication:** Protected with HTTP Basic Auth. Enter any username and the value of `ADMIN_PASSWORD`.
- **Security:** Rejects framing attacks via `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none';`. All user inputs (emails, SKUs, diagnostic messages) are escaped to prevent XSS.
- **Audit View:** Displays aggregated metrics (Total, Pending, Processing, Notified, Failed), customer email, SKU, status badges, subscription timestamp, notification timestamp, retry attempt counts, and error diagnostics.
- **JSON Endpoint:** Programmatic access available at `GET /admin/api/subscriptions`.

---

## Automated Test Suite

The project includes 44 automated tests across 5 test modules:

```bash
$ pytest -v
============================== 44 passed in 2.72s ==============================
```

- [`tests/test_subscriptions.py`](file:///Users/sawankumar/Desktop/privateContribution/back_in_stocks/tests/test_subscriptions.py): Email/SKU normalization, duplicate open subscription handling, 404 on unknown SKU, 502 on stock API drop, concurrent submissions, iframe headers.
- [`tests/test_stock_client.py`](file:///Users/sawankumar/Desktop/privateContribution/back_in_stocks/tests/test_stock_client.py): Catalogue parsing (lists and wrapped dictionaries), individual SKU queries, connection and HTTP error handling.
- [`tests/test_webhooks_and_delivery.py`](file:///Users/sawankumar/Desktop/privateContribution/back_in_stocks/tests/test_webhooks_and_delivery.py): Webhook authentication, deduplication, stock verification, out-of-stock handling, email dispatching, HTTP 429 rate limit backoff, HTTP 500 retry, permanent failure handling, restart recovery.
- [`tests/test_concurrency_audit.py`](file:///Users/sawankumar/Desktop/privateContribution/back_in_stocks/tests/test_concurrency_audit.py): Atomic subscription claiming across distinct events, outbox failure rollback, retryable failure claim preservation, permanent failure policy, re-subscription after notification.
- [`tests/test_admin.py`](file:///Users/sawankumar/Desktop/privateContribution/back_in_stocks/tests/test_admin.py): Basic authentication, invalid password rejection, anti-framing security headers, HTML escaping, empty state rendering, and admin JSON API.
- [`tests/test_e2e_smoke.py`](file:///Users/sawankumar/Desktop/privateContribution/back_in_stocks/tests/test_e2e_smoke.py): Unified end-to-end user and admin lifecycle verification.

---

## AI Coding Workflow & Implementation Timeline

- **AI Tools Used:** Google Deepmind Advanced Agentic Coding (`agy` CLI with Gemini 3.8 Flash).
- **Implementation Timeline (Total: ~43 minutes across 4 phases):**
  - **Phase 1 (Inspection & Planning):** ~5 minutes (Read-only environment discovery and architecture proposal).
  - **Phase 2 (Foundation & Subscription Flow):** ~17 minutes (SQLAlchemy models, SQLite WAL, StockClient, React embed UI, 12 tests).
  - **Phase 3 (Webhook Processing & Outbox Engine):** ~10 minutes (HMAC validation, stock verifier, Resend client, outbox worker, 17 tests).
  - **Phase 3.1 (Concurrency Audit & Atomic Claiming):** ~5 minutes (Fixed duplicate event race condition, partial unique index, 7 tests).
  - **Phase 4 (Admin Dashboard & Packaging):** ~6 minutes (Basic Auth admin interface, Dockerfile, volume persistence, e2e smoke test, README).
