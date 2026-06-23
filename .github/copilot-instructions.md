# Databricks Endpoint Monitoring with Uptime Kuma

A Python container that monitors Azure Databricks endpoints using Service Principal authentication and pushes heartbeat results to Uptime Kuma Push monitors.

## Build, Test, and Run Commands

### Docker (Primary Method)

```bash
# Build the image
docker build -t databricks-monitor:latest .

# Start the monitor
docker compose up -d

# View logs (recommended for monitoring)
docker compose logs -f databricks-monitor

# Stop the monitor
docker compose down

# Rebuild after code changes
docker build -t databricks-monitor:latest .
docker compose up -d
```

### Local Development (Without Docker)

```bash
# Install dependencies
pip install -r requirements.txt

# Run the monitor (requires .env file with all required environment variables)
python monitor.py
```

**Note:** No test suite or linter is configured. The application is validated by running it and checking logs.

## Architecture Overview

### Core Flow

1. **Token Management**: `TokenManager` class handles Azure AD authentication using client credentials (Service Principal) and proactively refreshes tokens ~5 minutes before expiry (configurable via `TOKEN_REFRESH_BUFFER_SEC`)
2. **Endpoint Checking**: Main loop queries each Databricks endpoint every `CHECK_INTERVAL_SEC` seconds
3. **Status API**: For serving endpoints, invocation URLs are automatically converted to status API URLs (`/api/2.0/serving-endpoints/<name>`) to avoid inference costs
4. **Heartbeat Push**: Results (up/down + message) are pushed to corresponding Uptime Kuma Push URLs

### Key Components

- **`TokenManager`**: Single shared instance that caches the Azure AD bearer token and handles transparent refresh using `time.monotonic()` for reliable timing
- **`parse_endpoints()`**: Parses comma-separated `DATABRICKS_ENDPOINTS` and `UPTIME_KUMA_PUSH_URLS`, validates counts match
- **`build_status_url()`**: Converts serving-endpoint invocation URLs to status API URLs using regex matching
- **`check_endpoint()`**: Performs GET request to status API, inspects `state.ready` field for serving endpoints
- **`push_to_uptime_kuma()`**: Sends heartbeat to Uptime Kuma via Push URL with status and message

### Endpoint Types

1. **Serving Endpoints**: URLs like `https://adb-xxx.azuredatabricks.net/serving-endpoints/test-agent/invocations`
   - Automatically converted to status API: `https://adb-xxx.azuredatabricks.net/api/2.0/serving-endpoints/test-agent`
   - Checks `state.ready` field: `READY` → UP, anything else → DOWN
   
2. **Plain API URLs**: Any other Databricks API endpoint
   - HTTP 200 → UP
   - Any other status → DOWN

## Key Conventions

### Environment Configuration

All configuration is via environment variables (loaded from `.env` file):

**Required:**
- `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`: Service Principal credentials
- `DATABRICKS_ENDPOINTS`: Comma-separated `LABEL::URL` pairs (e.g., `ModelServing::https://adb-111.azuredatabricks.net/serving-endpoints/my-model/invocations`)
- `UPTIME_KUMA_PUSH_URLS`: Comma-separated Push URLs (must match endpoint count and order)

**Optional with defaults:**
- `CHECK_INTERVAL_SEC=60`: Check frequency
- `REQUEST_TIMEOUT_SEC=15`: HTTP timeout per request
- `TOKEN_REFRESH_BUFFER_SEC=300`: Refresh token this many seconds before expiry
- `DATABRICKS_RESOURCE_ID=2ff814a6-3304-4ab8-85cb-cd0e6f879c1d`: Azure AD resource ID (global constant for Databricks)

**Critical:** The order of `DATABRICKS_ENDPOINTS` and `UPTIME_KUMA_PUSH_URLS` must match — each endpoint is paired with its corresponding Push URL by index position.

### Code Structure Patterns

- **Single-file application**: All logic lives in `monitor.py` (no modules/packages)
- **Global configuration**: Environment variables read at module level into constants (uppercase names)
- **Type hints**: Uses modern Python 3.10+ syntax (`str | None` instead of `Optional[str]`)
- **Logging**: Uses Python's `logging` module at INFO level with timestamp, level, and message
- **Regex compilation**: Status URL matching regex compiled once at module level (`_SERVING_INVOCATION_RE`)
- **Error handling**: Graceful degradation — if token fetch fails, all endpoints are marked DOWN with error message

### Docker Configuration

- **Base image**: `python:3.12-slim` for minimal footprint
- **Non-root user**: Container runs as `monitor` user for security
- **Restart policy**: `unless-stopped` — survives reboots unless explicitly stopped
- **No volume mounts**: Stateless container, all config via env vars
- **Unbuffered Python output**: Uses `python -u` to ensure logs appear immediately

### Azure AD Token Management

- Tokens are fetched using **client_credentials** grant with scope `{DATABRICKS_RESOURCE}/.default`
- Azure AD tokens live for 3600s (1 hour)
- Default refresh buffer is 300s (5 minutes) — token is refreshed at the 55-minute mark
- Uses `time.monotonic()` instead of `time.time()` to avoid issues with system clock changes
- Token is shared across all endpoints and check cycles (single instance pattern)

### Uptime Kuma Integration

- Uses **Push**-type monitors (not Pull)
- Heartbeat format: `GET {push_url}?status={up|down}&msg={url_encoded_message}&ping=`
- Message is truncated to 100 characters and URL-encoded
- Push failures are logged but don't stop the check loop

## Azure CLI Usage

When working with Azure resources in this project:

- **Use Azure CLI** (`az` commands) for all Azure operations
- **Service Principal permissions**: Requires **Databricks Contributor** role on the workspace (minimum: read access)
- **Assign role**: Azure Portal → Databricks Workspace → Access Control (IAM) → Add Role Assignment

Example commands:

```bash
# List Databricks workspaces
az databricks workspace list --output table

# Show workspace details
az databricks workspace show --name <workspace-name> --resource-group <rg-name>

# Assign role to Service Principal
az role assignment create \
  --assignee <service-principal-client-id> \
  --role "Contributor" \
  --scope "/subscriptions/<sub-id>/resourceGroups/<rg-name>/providers/Microsoft.Databricks/workspaces/<workspace-name>"
```
