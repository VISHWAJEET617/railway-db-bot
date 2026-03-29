"""
Multiple disposable email providers.
Providers are shuffled randomly so each DB creation uses a different one.
"""
import logging
import random
import re
import requests
import time
import uuid

log = logging.getLogger(__name__)

MAILTM_API = "https://api.mail.tm"

# ── mail.tm ───────────────────────────────────────────────────────────────────

def _create_mailtm() -> dict | None:
    try:
        resp = requests.get(f"{MAILTM_API}/domains", timeout=10)
        if resp.status_code != 200:
            return None
        domains = [d["domain"] for d in resp.json().get("hydra:member", [])]
        if not domains:
            return None

        chosen = None
        for domain in domains:
            try:
                kb = requests.get(
                    f"https://open.kickbox.com/v1/disposable/{domain}", timeout=5
                ).json()
                if not kb.get("disposable"):
                    chosen = domain
                    break
            except:
                chosen = domain
                break
        if not chosen:
            chosen = domains[0]

        # uuid4 hex is 32 chars of cryptographically random hex — guaranteed unique
        username = uuid.uuid4().hex[:16]
        email    = f"{username}@{chosen}"
        password = uuid.uuid4().hex

        cr = requests.post(f"{MAILTM_API}/accounts",
                           json={"address": email, "password": password}, timeout=10)
        if cr.status_code not in (200, 201):
            return None

        tr = requests.post(f"{MAILTM_API}/token",
                           json={"address": email, "password": password}, timeout=10)
        if tr.status_code != 200:
            return None

        return {
            "email":    email,
            "provider": "mail.tm",
            "_token":   tr.json().get("token"),
        }
    except Exception as e:
        log.warning("[mail.tm] create failed: %s", e)
        return None


def _poll_mailtm(account: dict, timeout: int) -> str | None:
    token    = account["_token"]
    headers  = {"Authorization": f"Bearer {token}"}
    deadline = time.time() + timeout
    seen     = set()
    first    = True
    while time.time() < deadline:
        if first:
            first = False   # no sleep on the very first check
        else:
            time.sleep(2)   # 2s between subsequent checks (was 5s)
        try:
            r = requests.get(f"{MAILTM_API}/messages", headers=headers, timeout=10)
            if r.status_code == 200:
                for msg in r.json().get("hydra:member", []):
                    mid = msg.get("id", "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    full = requests.get(f"{MAILTM_API}/messages/{mid}",
                                        headers=headers, timeout=10)
                    if full.status_code == 200:
                        body = (full.json().get("text", "") or
                                full.json().get("html", "") or
                                str(full.json()))
                        otp = _extract_otp(body)
                        if otp:
                            return otp
        except:
            pass
    return None


# ── tempmail.plus ─────────────────────────────────────────────────────────────
# Public mailbox — no registration needed, just pick any name@tempmail.plus
# MX: mx.imena.ua (real delivery), REST API accessible without auth

TEMPMAIL_API = "https://tempmail.plus/api/mails"

def _create_tempmail() -> dict | None:
    try:
        username = uuid.uuid4().hex[:14]
        email    = f"{username}@tempmail.plus"
        # Verify API is reachable
        r = requests.get(TEMPMAIL_API, params={"email": email, "limit": 1}, timeout=10)
        if r.status_code != 200:
            return None
        return {
            "email":    email,
            "provider": "tempmail.plus",
        }
    except Exception as e:
        log.warning("[tempmail.plus] create failed: %s", e)
        return None


def _poll_tempmail(account: dict, timeout: int) -> str | None:
    email    = account["email"]
    deadline = time.time() + timeout
    seen     = set()
    first    = True
    while time.time() < deadline:
        if first:
            first = False
        else:
            time.sleep(2)
        try:
            r = requests.get(TEMPMAIL_API, params={"email": email, "limit": 20}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                for msg in data.get("mail_list", []):
                    mid = str(msg.get("mail_id") or msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    fr = requests.get(TEMPMAIL_API,
                                      params={"email": email, "id": mid}, timeout=10)
                    if fr.status_code == 200:
                        fd   = fr.json()
                        body = (
                            str(fd.get("mail_text") or "")
                            + str(fd.get("mail_html") or "")
                            + str(fd.get("mail_subject") or "")
                            + str(fd)
                        )
                        otp = _extract_otp(body)
                        if otp:
                            return otp
        except:
            pass
    return None


# ── guerrillamail ─────────────────────────────────────────────────────────────

def _create_guerrillamail() -> dict | None:
    try:
        sess = requests.Session()
        r = sess.get(
            "https://api.guerrillamail.com/ajax.php?f=get_email_address&lang=en",
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data  = r.json()
        email = data.get("email_addr", "")
        if not email:
            return None
        return {
            "email":    email,
            "provider": "guerrillamail",
            "_sess":    sess,
            "_seq":     0,
        }
    except Exception as e:
        log.warning("[guerrillamail] create failed: %s", e)
        return None


def _poll_guerrillamail(account: dict, timeout: int) -> str | None:
    sess     = account["_sess"]
    seq      = account.get("_seq", 0)
    deadline = time.time() + timeout
    seen     = set()
    first    = True
    while time.time() < deadline:
        if first:
            first = False
        else:
            time.sleep(2)
        try:
            r = sess.get(
                f"https://api.guerrillamail.com/ajax.php?f=get_email_list&offset=0&seq={seq}",
                timeout=10,
            )
            if r.status_code == 200:
                for msg in r.json().get("list", []):
                    mid = msg.get("mail_id", "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    fr = sess.get(
                        f"https://api.guerrillamail.com/ajax.php?f=fetch_email&email_id={mid}",
                        timeout=10,
                    )
                    if fr.status_code == 200:
                        body = str(fr.json())
                        otp  = _extract_otp(body)
                        if otp:
                            return otp
        except:
            pass
    return None


# ── OTP extractor ─────────────────────────────────────────────────────────────

_INVALID_OTPS = {
    "000000", "111111", "222222", "333333", "444444",
    "555555", "666666", "777777", "888888", "999999",
    "123456", "654321",
}


def _is_valid_otp(otp: str) -> bool:
    """Return True only for OTPs that look like real Railway codes."""
    if not otp or len(otp) != 6 or not otp.isdigit():
        return False
    if otp in _INVALID_OTPS:
        return False
    # Require at least 2 distinct digits (rejects 000000, 111111, etc.)
    if len(set(otp)) < 2:
        return False
    return True


def _extract_otp(body: str) -> str | None:
    # Primary: keyword-adjacent 6-digit number (also validate it)
    m = re.search(
        r'(?:code|verify|otp|token|sign.?in|login|confirm)[^\d]{0,80}(\d{6})\b',
        body, re.IGNORECASE | re.DOTALL
    )
    if m:
        candidate = m.group(1)
        if _is_valid_otp(candidate):
            return candidate
        log.debug("[OTP] Primary regex matched %s but failed validity check — skipping", candidate)
    # Fallback: any standalone 6-digit number with >2 unique digits
    for c in re.findall(r'(?<!\d)(\d{6})(?!\d)', body):
        if _is_valid_otp(c):
            return c
    return None


# ── Public API ────────────────────────────────────────────────────────────────

_PROVIDERS = [
    ("mail.tm",        _create_mailtm,        _poll_mailtm),
    ("tempmail.plus",  _create_tempmail,       _poll_tempmail),
    ("guerrillamail",  _create_guerrillamail,  _poll_guerrillamail),
]


def get_random_account(
    exclude_providers: set | None = None,
    prefer_provider: str | None = None,
) -> dict | None:
    """
    Try providers to create a disposable inbox.
    prefer_provider: if given, try that provider first (before randomising the rest).
    exclude_providers: set of provider names to try last (already-tried ones).
    Returns account dict with 'email', 'provider', and private '_*' keys.
    """
    excl = exclude_providers or set()
    order = list(_PROVIDERS)

    # Build ordered list: preferred → fresh (shuffled) → excluded (shuffled)
    pinned   = [t for t in order if t[0] == prefer_provider] if prefer_provider else []
    fresh    = [t for t in order if t[0] != prefer_provider and t[0] not in excl]
    fallback = [t for t in order if t[0] != prefer_provider and t[0] in excl]
    random.shuffle(fresh)
    random.shuffle(fallback)
    final_order = pinned + fresh + fallback

    for name, creator, _ in final_order:
        log.info("[mail] Trying provider: %s", name)
        account = creator()
        if account:
            log.info("[mail] Created account via %s: %s", name, account["email"])
            return account
        log.warning("[mail] Provider %s failed, trying next", name)
    return None


def poll_otp(account: dict, timeout: int = 120) -> str | None:
    """Poll for OTP using the provider that created this account."""
    provider = account.get("provider", "mail.tm")
    for name, _, poller in _PROVIDERS:
        if name == provider:
            return poller(account, timeout)
    return _poll_mailtm(account, timeout)
