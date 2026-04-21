import os
import re
import time
import logging
import requests
from urllib.parse import quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Auth config ────────────────────────────────────────────────────────────────
AZURE_TENANT_ID     = os.environ["AZURE_TENANT_ID"]
AZURE_CLIENT_ID     = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]

# ── Endpoint + Uptime Kuma config ──────────────────────────────────────────────
# Comma-separated pairs: LABEL::URL
# Supports both serving-endpoint invocation URLs and plain API URLs
# e.g. "TestAgent::https://adb-xxx.azuredatabricks.net/serving-endpoints/test-agent/invocations"
ENDPOINTS_RAW         = os.environ["DATABRICKS_ENDPOINTS"]       # required
UPTIME_KUMA_PUSH_URLS = os.environ["UPTIME_KUMA_PUSH_URLS"]      # comma-separated, same order

CHECK_INTERVAL_SEC  = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))
REQUEST_TIMEOUT_SEC = int(os.environ.get("REQUEST_TIMEOUT_SEC", "15"))

# Refresh the token this many seconds BEFORE it actually expires.
# Azure AD tokens live for 3600s (1 hour). We refresh at 3300s (55 min)
# so there is always a valid token in hand even if the refresh call is slow.
TOKEN_REFRESH_BUFFER_SEC = int(os.environ.get("TOKEN_REFRESH_BUFFER_SEC", "300"))

# Databricks resource ID (fixed for Azure AD — global Azure)
DATABRICKS_RESOURCE = os.environ.get(
    "DATABRICKS_RESOURCE_ID",
    "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
)

# Regex to detect a serving-endpoint invocation URL
_SERVING_INVOCATION_RE = re.compile(
    r"(https://[^/]+)/serving-endpoints/([^/]+)/invocations", re.IGNORECASE
)


class TokenManager:
    """
    Caches an Azure AD bearer token and transparently refreshes it
    TOKEN_REFRESH_BUFFER_SEC seconds before it expires.

    Timeline example (default settings):
      t=0     → fetch token, expires_at = t+3600
      t=3300  → buffer window reached, proactively refresh
      t=3600  → old token would have expired — already using new one
    """

    def __init__(self):
        self._token: str | None = None
        self._expires_at: float = 0.0       # epoch seconds via monotonic clock

    def get_token(self) -> str:
        now       = time.monotonic()
        remaining = self._expires_at - now

        if self._token is None:
            log.info("Token cache empty — fetching initial token.")
            self._fetch()
        elif remaining <= TOKEN_REFRESH_BUFFER_SEC:
            log.info(
                "Token expires in %ds (buffer=%ds) — proactively refreshing.",
                int(remaining), TOKEN_REFRESH_BUFFER_SEC
            )
            self._fetch()
        else:
            log.debug("Using cached token (valid for another %ds).", int(remaining))

        return self._token

    def _fetch(self):
        url = (
            f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
            f"/oauth2/v2.0/token"
        )
        payload = {
            "grant_type":    "client_credentials",
            "client_id":     AZURE_CLIENT_ID,
            "client_secret": AZURE_CLIENT_SECRET,
            "scope":         f"{DATABRICKS_RESOURCE}/.default",
        }
        resp = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()

        self._token      = data["access_token"]
        expires_in       = int(data.get("expires_in", 3600))  # Azure always returns this
        self._expires_at = time.monotonic() + expires_in

        log.info(
            "Token fetched — expires in %ds (~%dm), will refresh after %ds (~%dm).",
            expires_in,      expires_in // 60,
            expires_in - TOKEN_REFRESH_BUFFER_SEC,
            (expires_in - TOKEN_REFRESH_BUFFER_SEC) // 60,
        )


# Single shared instance — one token reused across all endpoints & cycles
token_manager = TokenManager()


def parse_endpoints():
    """Parse DATABRICKS_ENDPOINTS and UPTIME_KUMA_PUSH_URLS into a list of dicts."""
    raw_endpoints = [e.strip() for e in ENDPOINTS_RAW.split(",") if e.strip()]
    raw_push_urls = [u.strip() for u in UPTIME_KUMA_PUSH_URLS.split(",") if u.strip()]

    if len(raw_endpoints) != len(raw_push_urls):
        raise ValueError(
            f"DATABRICKS_ENDPOINTS has {len(raw_endpoints)} entries but "
            f"UPTIME_KUMA_PUSH_URLS has {len(raw_push_urls)}. They must match."
        )

    monitors = []
    for ep, push_url in zip(raw_endpoints, raw_push_urls):
        if "::" in ep:
            label, url = ep.split("::", 1)
        else:
            label, url = ep, ep

        url = url.strip()
        status_url, endpoint_name = build_status_url(url)

        monitors.append({
            "label":         label.strip(),
            "original_url":  url,
            "status_url":    status_url,
            "endpoint_name": endpoint_name,
            "push_url":      push_url,
        })

    return monitors


def build_status_url(url: str) -> tuple[str, str | None]:
    """
    Convert a serving-endpoint invocation URL to its status API URL.

    Input : https://adb-xxx.azuredatabricks.net/serving-endpoints/test-agent/invocations
    Output: https://adb-xxx.azuredatabricks.net/api/2.0/serving-endpoints/test-agent

    For non-invocation URLs the original URL is returned unchanged.
    """
    match = _SERVING_INVOCATION_RE.match(url)
    if match:
        host, name = match.group(1), match.group(2)
        status_url = f"{host}/api/2.0/serving-endpoints/{name}"
        log.debug("Resolved status URL: %s → %s", url, status_url)
        return status_url, name
    return url, None



def check_endpoint(monitor: dict, token: str) -> tuple[bool, str]:
    """
    Check a Databricks endpoint via the status API (GET, no inference cost).

    For serving endpoints the status API returns JSON like:
      { "name": "test-agent", "state": { "ready": "READY" }, ... }

    UP   → HTTP 200 AND state.ready == "READY"
    DOWN → HTTP 200 but state.ready != "READY"  (e.g. "NOT_READY", "UPDATING")
    DOWN → any non-200 HTTP status
    """
    headers = {"Authorization": f"Bearer {token}"}
    url = monitor["status_url"]

    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SEC)

        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"

        # For serving-endpoint status URLs, inspect the ready state
        if monitor["endpoint_name"]:
            body = resp.json()
            ready_state = (
                body.get("state", {}).get("ready", "UNKNOWN")
            )
            if ready_state == "READY":
                return True, f"READY (HTTP 200)"
            else:
                return False, f"State={ready_state} (HTTP 200)"

        # Plain API URL — 200 is sufficient
        return True, "HTTP 200 OK"

    except requests.exceptions.Timeout:
        return False, "Timeout"
    except requests.exceptions.ConnectionError as e:
        return False, f"ConnectionError: {str(e)[:80]}"
    except Exception as e:
        return False, f"Error: {str(e)[:80]}"


def push_to_uptime_kuma(push_url: str, is_up: bool, message: str):
    """Send heartbeat to Uptime Kuma push endpoint."""
    status   = "up" if is_up else "down"
    safe_msg = quote(message[:100])
    full_url = f"{push_url}?status={status}&msg={safe_msg}&ping="
    try:
        resp = requests.get(full_url, timeout=10)
        resp.raise_for_status()
        log.info("  ↳ Pushed to Uptime Kuma: %s", status.upper())
    except Exception as e:
        log.error("  ↳ Failed to push to Uptime Kuma: %s", e)


def run_checks(monitors: list):
    """Run one round of checks for all monitors."""
    log.info("── Starting check cycle (%d endpoint(s)) ──", len(monitors))
    try:
        token = token_manager.get_token()
    except Exception as e:
        log.error("Failed to obtain token: %s — marking all endpoints DOWN", e)
        for m in monitors:
            push_to_uptime_kuma(m["push_url"], False, f"Token error: {str(e)[:80]}")
        return

    for m in monitors:
        log.info("Checking [%s]", m["label"])
        log.info("  Status URL : %s", m["status_url"])
        is_up, msg = check_endpoint(m, token)
        status_str = "UP ✓" if is_up else "DOWN ✗"
        log.info("  Result     : %s | %s", status_str, msg)
        push_to_uptime_kuma(m["push_url"], is_up, msg)


def main():
    monitors = parse_endpoints()
    log.info("Databricks Monitor started.")
    log.info("Monitoring %d endpoint(s) every %ds:", len(monitors), CHECK_INTERVAL_SEC)
    for m in monitors:
        log.info(
            "  • [%s]  invocation → %s  |  status check → %s",
            m["label"], m["original_url"], m["status_url"]
        )

    while True:
        run_checks(monitors)
        log.info("Sleeping %ds until next cycle…\n", CHECK_INTERVAL_SEC)
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
