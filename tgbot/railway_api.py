"""
Lightweight Railway GraphQL API helper.
Used to fetch real-time balance and trial info for stored accounts.
Also provides TCP proxy helpers for MongoDB public URL extraction.
"""
import logging
import time
import requests

log = logging.getLogger(__name__)

RAILWAY_GQL = "https://backboard.railway.app/graphql/v2"


# ── TCP Proxy helpers (for MongoDB public URL) ────────────────────────────────

TCP_PROXIES_QUERY = """
query TcpProxies($serviceId: String!, $environmentId: String!) {
  tcpProxies(serviceId: $serviceId, environmentId: $environmentId) {
    id
    domain
    proxyPort
    applicationPort
  }
}
"""

TCP_PROXY_CREATE = """
mutation TcpProxyCreate($input: TCPProxyCreateInput!) {
  tcpProxyCreate(input: $input) {
    id
    domain
    proxyPort
    applicationPort
  }
}
"""


def get_or_create_tcp_proxy(
    railway_token: str,
    service_id: str,
    environment_id: str,
    app_port: int = 27017,
) -> dict | None:
    """
    Query existing TCP proxies for a service. If none exist, create one.
    Returns dict with 'domain' and 'proxyPort', or None on failure.
    Used to build MongoDB public URL: mongodb://user:pass@domain:proxyPort/db
    """
    if not railway_token or not service_id or not environment_id:
        log.warning("[TCP Proxy] Missing token/serviceId/environmentId — skipping")
        return None

    headers = {
        "Authorization": f"Bearer {railway_token}",
        "Content-Type":  "application/json",
    }

    # ── Step 1: Check existing proxies ──────────────────────────────────────
    try:
        resp = requests.post(
            RAILWAY_GQL,
            json={
                "query": TCP_PROXIES_QUERY,
                "variables": {"serviceId": service_id, "environmentId": environment_id},
            },
            headers=headers,
            timeout=12,
        )
        if resp.status_code == 200:
            data = resp.json().get("data") or {}
            proxies = data.get("tcpProxies") or []
            if proxies:
                proxy = proxies[0]
                log.info(
                    "[TCP Proxy] Found existing proxy: %s:%s",
                    proxy.get("domain"), proxy.get("proxyPort"),
                )
                return {"domain": proxy["domain"], "proxyPort": proxy["proxyPort"]}
        else:
            log.warning("[TCP Proxy] tcpProxies query HTTP %s", resp.status_code)
    except Exception as e:
        log.warning("[TCP Proxy] tcpProxies query error: %s", e)

    # ── Step 2: Create TCP proxy for port 27017 (MongoDB) ────────────────────
    try:
        log.info("[TCP Proxy] No existing proxy — creating for port %d", app_port)
        resp2 = requests.post(
            RAILWAY_GQL,
            json={
                "query": TCP_PROXY_CREATE,
                # Railway changed the mutation to use an input wrapper object
                "variables": {
                    "input": {
                        "environmentId":  environment_id,
                        "serviceId":      service_id,
                        "applicationPort": app_port,
                    }
                },
            },
            headers=headers,
            timeout=15,
        )
        if resp2.status_code == 200:
            result = (resp2.json().get("data") or {}).get("tcpProxyCreate") or {}
            if result.get("domain"):
                log.info(
                    "[TCP Proxy] Created proxy: %s:%s",
                    result.get("domain"), result.get("proxyPort"),
                )
                return {"domain": result["domain"], "proxyPort": result["proxyPort"]}
            errors = resp2.json().get("errors")
            if errors:
                log.warning("[TCP Proxy] tcpProxyCreate mutation errors: %s", errors)
                # If Railway blocks TCP proxy creation for this token/account, stop retrying.
                msg = (errors[0].get("message") or "").lower() if isinstance(errors, list) and errors else ""
                if "not authorized" in msg:
                    return {"error": "not_authorized"}
        else:
            log.warning("[TCP Proxy] tcpProxyCreate HTTP %s — %s", resp2.status_code, resp2.text[:300])
    except Exception as e:
        log.warning("[TCP Proxy] tcpProxyCreate error: %s", e)

    # ── Step 3: Retry querying proxies — Railway takes ~5-10s to provision ────
    # If creation was blocked, we won't reach here (we return error above).
    # After creation the domain may not be returned immediately (Railway async).
    # Re-poll the list — Railway can take 30–120s to provision TCP proxies on trial accounts.
    # Poll up to 24 times with a 5-second gap (~2 minutes).
    for attempt in range(1, 25):
        time.sleep(5)
        log.info("[TCP Proxy] Polling for proxy after creation (attempt %d/24) …", attempt)
        try:
            r = requests.post(
                RAILWAY_GQL,
                json={
                    "query": TCP_PROXIES_QUERY,
                    "variables": {"serviceId": service_id, "environmentId": environment_id},
                },
                headers=headers,
                timeout=12,
            )
            if r.status_code == 200:
                proxies = (r.json().get("data") or {}).get("tcpProxies") or []
                if proxies:
                    proxy = proxies[0]
                    log.info(
                        "[TCP Proxy] Proxy available after %ds: %s:%s",
                        attempt * 5, proxy.get("domain"), proxy.get("proxyPort"),
                    )
                    return {"domain": proxy["domain"], "proxyPort": proxy["proxyPort"]}
        except Exception as e:
            log.warning("[TCP Proxy] Poll attempt %d error: %s", attempt, e)

    log.warning("[TCP Proxy] Proxy not available after retries — falling back to private URL")
    return None

BALANCE_QUERY = """
query {
  me {
    email
    registrationStatus
    customer {
      creditBalance
      remainingUsageCreditCents
      trialCreditBalance
      state
    }
  }
}
"""


def fetch_railway_balance(railway_token: str) -> dict:
    """
    Call Railway GraphQL with the stored account token.
    Returns a dict with keys:
      credit_balance          – total remaining credit in cents
      remaining_usage_credit  – remaining usage credit in cents
      trial_credit            – trial credit balance in cents
      state                   – customer state string (e.g. "TRIAL", "ACTIVE")
      registration_status     – "ONBOARDED" / etc.
      error                   – set if the call failed
    Returns {} on total failure (no token, network error).
    """
    if not railway_token or len(railway_token) < 20:
        return {}

    headers = {
        "Authorization": f"Bearer {railway_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            RAILWAY_GQL,
            json={"query": BALANCE_QUERY},
            headers=headers,
            timeout=10,
        )
        log.info("[RailwayAPI] balance query status=%s", resp.status_code)

        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}

        body = resp.json()
        errors = body.get("errors")
        if errors:
            msg = errors[0].get("message", "unknown")
            log.warning("[RailwayAPI] GraphQL error: %s", msg)
            return {"error": msg}

        me = (body.get("data") or {}).get("me") or {}
        customer = me.get("customer") or {}

        return {
            "credit_balance":         customer.get("creditBalance", 0),
            "remaining_usage_credit": customer.get("remainingUsageCreditCents", 0),
            "trial_credit":           customer.get("trialCreditBalance", 0),
            "state":                  customer.get("state", ""),
            "registration_status":    me.get("registrationStatus", ""),
        }

    except Exception as e:
        log.warning("[RailwayAPI] Request failed: %s", e)
        return {"error": str(e)}


def format_balance_lines(info: dict, created_at_str: str = "") -> tuple[str, str]:
    """
    Given the result of fetch_railway_balance(), return (balance_line, trial_line)
    suitable for the detail card.
    Falls back to date-based estimate if API data is unavailable.
    """
    from datetime import datetime, timezone

    def _days_since(s):
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            return 0

    # ── API data available ────────────────────────────────────────────────────
    if info and "error" not in info and ("credit_balance" in info or "state" in info):
        state = (info.get("state") or "").upper()
        reg   = (info.get("registration_status") or "").upper()

        # Credit balance in cents → dollars
        credit_cents = info.get("credit_balance") or 0
        trial_cents  = info.get("trial_credit") or 0
        usage_cents  = info.get("remaining_usage_credit") or 0

        # Prefer remaining_usage_credit if non-zero, else credit_balance
        balance_cents = usage_cents if usage_cents else credit_cents
        balance_usd   = balance_cents / 100.0

        if state in ("TRIAL", "FREE_TRIAL", ""):
            days = _days_since(created_at_str)
            remaining_days = max(0, 30 - days)
            if remaining_days > 0:
                trial_line = f"🟢 <b>Trial:</b> {remaining_days} days remaining (Railway)"
            else:
                trial_line = "🔴 <b>Trial:</b> Expired (30-day limit reached)"
        elif state == "ACTIVE":
            trial_line = "✅ <b>Plan:</b> Active subscription"
        elif state in ("CANCELLED", "CANCELED"):
            trial_line = "🔴 <b>Plan:</b> Cancelled"
        else:
            trial_line = f"ℹ️ <b>Status:</b> {state or 'Unknown'}"

        if balance_usd > 0:
            bal_line = f"💰 <b>Credit:</b> ${balance_usd:.2f} remaining"
        elif trial_cents > 0:
            bal_line = f"💰 <b>Credit:</b> ${trial_cents/100:.2f} trial credit"
        else:
            bal_line = "💰 <b>Credit:</b> $0.00 (depleted or expired)"

        return bal_line, trial_line

    # ── Fallback: date-based estimate ─────────────────────────────────────────
    days = _days_since(created_at_str)
    remaining_days = max(0, 30 - days)
    if remaining_days == 0:
        trial_line = f"🔴 <b>Trial:</b> Likely expired ({days}d old) — check Railway"
        bal_line   = "💰 <b>Credit:</b> Estimated $0.00 (>30 days)"
    else:
        trial_line = f"🟡 <b>Trial:</b> ~{remaining_days} days left (estimated)"
        bal_line   = "💰 <b>Credit:</b> ~$5.00 initial — usage deducted"

    error = info.get("error", "") if info else "no token"
    if error:
        bal_line += f"\n<i>⚠️ Live balance unavailable: {error[:60]}</i>"

    return bal_line, trial_line
