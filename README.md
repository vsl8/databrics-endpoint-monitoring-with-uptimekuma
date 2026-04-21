# Databricks Uptime Kuma Monitor

A lightweight Python container that monitors one or more **Azure Databricks** endpoints (serving endpoints or plain API URLs) using an **Azure Service Principal** for authentication, and pushes heartbeat results to [Uptime Kuma](https://github.com/louislam/uptime-kuma) Push monitors.

## How It Works

1. On startup the app authenticates against **Azure AD** using client credentials (Service Principal).
2. Every `CHECK_INTERVAL_SEC` seconds it queries each Databricks endpoint's **status API** (a zero-cost GET — no inference is triggered).
3. For **serving endpoints** (`/serving-endpoints/<name>/invocations`), the invocation URL is automatically converted to the status API URL and the `state.ready` field is inspected:
   - `READY` → **UP**
   - Anything else (`NOT_READY`, `UPDATING`, …) → **DOWN**
4. For **plain API URLs**, an HTTP 200 response is treated as **UP**.
5. The result (up/down + message) is pushed to the matching **Uptime Kuma Push URL**.
6. Tokens are cached and proactively refreshed ~5 minutes before expiry (configurable).

---

## Prerequisites

- **Docker** and **Docker Compose** installed
- An **Azure Service Principal** with access to the target Databricks workspace(s)
- **Uptime Kuma** instance with one or more **Push**-type monitors configured

---

## Environment Variables

Create a `.env` file in the project root:

| Variable | Required | Default | Description |
|---|---|---|---|
| `AZURE_TENANT_ID` | ✅ | — | Azure AD tenant ID |
| `AZURE_CLIENT_ID` | ✅ | — | Service Principal application (client) ID |
| `AZURE_CLIENT_SECRET` | ✅ | — | Service Principal client secret |
| `DATABRICKS_ENDPOINTS` | ✅ | — | Comma-separated `LABEL::URL` pairs (see below) |
| `UPTIME_KUMA_PUSH_URLS` | ✅ | — | Comma-separated Uptime Kuma Push URLs (same order as endpoints) |
| `CHECK_INTERVAL_SEC` | ❌ | `60` | Seconds between check cycles |
| `REQUEST_TIMEOUT_SEC` | ❌ | `15` | HTTP timeout per request (seconds) |
| `TOKEN_REFRESH_BUFFER_SEC` | ❌ | `300` | Refresh token this many seconds before expiry |
| `DATABRICKS_RESOURCE_ID` | ❌ | `2ff814a6-3304-4ab8-85cb-cd0e6f879c1d` | Azure AD resource ID for Databricks |

### `.env` example

```dotenv
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=your-client-secret

# Endpoint format:  LABEL::URL  (comma-separated for multiple)
DATABRICKS_ENDPOINTS=ClusterAPI::https://adb-111.azuredatabricks.net/api/2.0/clusters/list,ModelServing::https://adb-111.azuredatabricks.net/serving-endpoints/my-model/invocations

# One Push URL per endpoint, same order
UPTIME_KUMA_PUSH_URLS=http://uptime-kuma:3001/api/push/token1,http://uptime-kuma:3001/api/push/token2

CHECK_INTERVAL_SEC=60
REQUEST_TIMEOUT_SEC=15
```

---

## Setting Up Uptime Kuma Push Monitors

1. Open Uptime Kuma → **Add New Monitor**
2. Set **Monitor Type** → **Push**
3. Set **Heartbeat Interval** to match `CHECK_INTERVAL_SEC` (e.g. `60`)
4. Copy the generated Push URL (e.g. `http://localhost:3001/api/push/abcXYZ123`)
5. Paste it into `UPTIME_KUMA_PUSH_URLS` in your `.env`
6. Repeat for each endpoint

---

## Usage with Docker

### 1. Build the image

```bash
docker build -t databricks-monitor:latest .
```

### 2. Run with Docker Compose

```bash
docker compose up -d
```

This starts the `databricks-monitor` container in the background. It reads all configuration from the `.env` file, runs continuously, and restarts automatically unless explicitly stopped.

### 3. View logs

```bash
docker compose logs -f databricks-monitor
```

### 4. Stop the monitor

```bash
docker compose down
```

### 5. Rebuild after code changes

```bash
docker build -t databricks-monitor:latest .
docker compose up -d
```

---

## Running Without Docker

```bash
pip install -r requirements.txt
# Export all required env vars or source your .env
python monitor.py
```

---

## Project Structure

```
.
├── docker-compose.yml   # Compose service definition (uses pre-built image)
├── Dockerfile           # Multi-stage build: Python 3.12-slim, non-root user
├── monitor.py           # Main monitoring loop + Azure AD token management
├── requirements.txt     # Python dependencies (requests)
├── .env                 # Your environment variables (not committed)
└── README.md            # This file
```

---

## Architecture

```
┌──────────────────────┐
│   monitor.py (loop)  │
│                      │
│  1. Get/refresh token│──► Azure AD  (client_credentials)
│                      │
│  2. GET status API   │──► Databricks Workspace(s)
│                      │
│  3. Push heartbeat   │──► Uptime Kuma  (/api/push/...)
└──────────────────────┘
```

---

## Notes

- The monitor uses the **status API** (`GET /api/2.0/serving-endpoints/<name>`), not the invocation endpoint — no inference cost is incurred.
- Tokens are cached in memory and refreshed proactively (`TOKEN_REFRESH_BUFFER_SEC` before expiry), so there is no gap in coverage.
- The container runs as a **non-root** user (`monitor`) for security.
- The number of entries in `DATABRICKS_ENDPOINTS` and `UPTIME_KUMA_PUSH_URLS` **must match**.

> The order of `DATABRICKS_ENDPOINTS` and `UPTIME_KUMA_PUSH_URLS` must match.

---

### 4. Build and run

```bash
# Start everything (Uptime Kuma + monitor)
docker compose up -d --build

# View logs
docker compose logs -f databricks-monitor

# Restart only the monitor after .env changes
docker compose up -d --build databricks-monitor
```

---

### 5. If Uptime Kuma is already running elsewhere

Remove the `uptime-kuma` service from `docker-compose.yml` and update
`UPTIME_KUMA_PUSH_URLS` to use the external host/IP instead of `uptime-kuma`.

```yaml
# docker-compose.yml — only keep this:
services:
  databricks-monitor:
    build: .
    restart: unless-stopped
    env_file: .env
```

---

## Azure AD Permissions

The Service Principal needs the **Databricks Contributor** role (or at minimum read access)
on the Databricks workspace. Assign it in:

> Azure Portal → Databricks Workspace → Access Control (IAM) → Add Role Assignment
