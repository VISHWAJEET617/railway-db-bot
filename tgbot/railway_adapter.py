"""
Railway automation adapter for the Telegram bot.
Wraps the core automation logic from railway_bot.py with:
- Progress callback support (called at each step)
- Multiple DB type support (PostgreSQL, MySQL, MongoDB, Redis)
- Per-user isolated browser sessions
- Returns credentials dict instead of saving to file
"""
import asyncio
import logging
import re
import json
import requests
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

from playwright.async_api import async_playwright
from tgbot.config import CHROMIUM_PATH, DB_TYPES
from tgbot.mail_providers import get_random_account, poll_otp
from tgbot.railway_api import get_or_create_tcp_proxy
from tgbot.database import is_email_used, mark_email_used

STEPS = [
    ("creating_email",    "Creating temp email"),
    ("submitting_email",  "Submitting email to Railway"),
    ("waiting_otp",       "Waiting for OTP"),
    ("entering_otp",      "Entering OTP"),
    ("accepting_tos",     "Accepting Terms of Service"),
    ("creating_project",  "Creating project"),
    ("provisioning_db",   "Provisioning database"),
    ("extracting_creds",  "Extracting credentials"),
]


def _get_first(d: dict, keys: list, default="") -> str:
    for k in keys:
        if d.get(k):
            return d[k]
    return default



async def _try_click(page, selectors: list, timeout=1500) -> bool:
    for sel in selectors:
        try:
            elem = page.locator(sel).first
            await elem.wait_for(state="visible", timeout=timeout)
            await elem.click()
            return True
        except:
            pass
    return False


def _has_tos_modal(text: str) -> bool:
    """
    Only returns True when Railway's actual TOS *modal* is open.
    'Terms of Service' alone is a footer link present on every page — ignore it.
    We look for phrases that only appear inside the modal itself.
    """
    modal_phrases = [
        "keep it cool for everyone",
        "you must accept our terms",
        "agree to our terms",
        "privacy and data policy",
        "i will not deploy",
        "scroll to read",
    ]
    tl = text.lower()
    return any(p in tl for p in modal_phrases)


async def _js_click_button(page, texts: list) -> str | None:
    """Click a button by its text using JS (bypasses Playwright visibility checks)."""
    for text in texts:
        try:
            result = await page.evaluate(f"""
                (searchText) => {{
                    const all = [...document.querySelectorAll('button, [role="button"], label, input')];
                    const el = all.find(e => (e.innerText || e.value || '').includes(searchText));
                    if (el) {{ el.click(); return el.innerText || el.value || 'clicked'; }}
                    return null;
                }}
            """, text)
            if result:
                log.info("[Railway] JS click found & clicked: '%s' -> '%s'", text[:40], str(result)[:40])
                return result
        except Exception as e:
            log.info("[Railway] JS click error for '%s': %s", text[:30], e)
    return None


async def _dismiss_tos(page):
    """
    Railway TOS modal — 3-step process (using JS clicks to bypass visibility/overlay issues):
      1. Click 'Please review the list of banned items' to expand the banned list
      2. Click 'I will not deploy any of that' checkbox/confirmation
      3. Click the final 'I Agree' button
    """
    # Log ALL buttons in DOM (including hidden ones behind overlay)
    try:
        all_btns = await page.evaluate("""
            () => [...document.querySelectorAll('button, [role="button"]')]
                .map(e => e.innerText.trim().slice(0, 60) + ' [vis=' + (e.offsetParent !== null) + ']')
                .filter(s => s.trim())
        """)
        log.info("[Railway] ALL buttons in DOM: %s", all_btns)
    except:
        pass

    async def _scroll_modal():
        try:
            await page.evaluate("""
                () => {
                    const els = [...document.querySelectorAll('div, section, article, ul, ol')];
                    els.filter(e => e.scrollHeight > e.clientHeight + 20)
                       .forEach(e => { e.scrollTop = e.scrollHeight; });
                }
            """)
            await page.wait_for_timeout(1000)
        except:
            pass

    # ── STAGE 1: Scroll → "I agree with Railway's Terms of Service" ──────────
    log.info("[Railway] TOS stage1: scrolling to reveal agree button")
    await _scroll_modal()

    try:
        all_btns2 = await page.evaluate("""
            () => [...document.querySelectorAll('button, [role="button"]')]
                .map(e => e.innerText.trim().slice(0, 60) + ' [vis=' + (e.offsetParent !== null) + ']')
                .filter(s => s.trim())
        """)
        log.info("[Railway] ALL buttons after scroll: %s", all_btns2)
    except:
        pass

    r_stage1 = await _js_click_button(page, [
        "I agree with Railway",
        "I Agree",
        "I agree",
    ])
    log.info("[Railway] TOS stage1 agree click: %s", r_stage1)
    await page.wait_for_timeout(2000)

    # ── STAGE 2: "Please review" → checkbox → final confirm ──────────────────
    try:
        all_btns3 = await page.evaluate("""
            () => [...document.querySelectorAll('button, [role="button"]')]
                .map(e => e.innerText.trim().slice(0, 60) + ' [vis=' + (e.offsetParent !== null) + ']')
                .filter(s => s.trim())
        """)
        log.info("[Railway] ALL buttons stage2: %s", all_btns3)
    except:
        pass

    # Step 2a — click "Please review the list of banned items"
    r2a = await _js_click_button(page, ["Please review the list of banned items", "Please review"])
    log.info("[Railway] TOS stage2a 'Please review' click: %s", r2a)
    await page.wait_for_timeout(1200)

    # Scroll again after expanding banned items list
    await _scroll_modal()

    # Step 2b — click "I will not deploy any of that" checkbox/button
    r2b = await _js_click_button(page, ["I will not deploy any of that", "I will not deploy"])
    log.info("[Railway] TOS stage2b 'I will not deploy' click: %s", r2b)
    await page.wait_for_timeout(800)

    # Step 2c — final confirm button
    r2c = await _js_click_button(page, [
        "I agree with Railway",
        "I Agree",
        "I agree",
        "Agree",
        "Accept",
        "Confirm",
        "Continue",
    ])
    log.info("[Railway] TOS stage2c final click: %s", r2c)

    await page.wait_for_timeout(2500)
    body_after = await page.inner_text("body")
    still_tos = _has_tos_modal(body_after)
    log.info("[Railway] TOS dismiss complete. Modal still visible: %s", still_tos)

    # Log buttons still present
    try:
        btn_texts = await page.evaluate("""
            () => [...document.querySelectorAll('button, a, [role="button"]')]
                .filter(e => e.innerText.trim())
                .map(e => e.tagName + ':' + e.innerText.trim().slice(0, 40))
                .slice(0, 15)
        """)
        log.info("[Railway] Buttons after TOS dismiss: %s", btn_texts)
    except:
        pass


async def _is_logged_in(page) -> bool:
    await page.goto("https://railway.com/dashboard", wait_until="domcontentloaded")
    # Poll up to 12s for the SPA to hydrate — avoids false "not logged in" on slow renders
    for _i in range(6):
        await page.wait_for_timeout(2000)
        content = await page.inner_text("body")
        url = page.url
        log.info("[Railway] Auth check — url=%s content_len=%d (poll %d/6)",
                 url, len(content.strip()), _i + 1)
        if "login" in url or "signin" in url:
            log.info("[Railway] Not logged in — redirected to login")
            return False
        if "Continue with GitHub" in content or "Log in using email" in content:
            log.info("[Railway] Not logged in — login UI detected")
            return False
        if len(content.strip()) >= 200:
            log.info("[Railway] Logged in confirmed")
            return True
    log.info("[Railway] Not logged in — page too short after 12s")
    return False


async def _handle_onboarding(page):
    for step in range(12):
        await asyncio.sleep(2)
        url  = page.url
        text = await page.inner_text("body")
        log.info("[Railway] Onboarding step=%d url=%s", step, url[:60])

        # Already reached a project or a clean dashboard — done
        if "/project/" in url:
            return
        if "/dashboard" in url and "Welcome to Railway" not in text and "get started" not in text.lower():
            return

        # Still on login page — just wait for the redirect, don't try to click anything
        if "login" in url or "signin" in url:
            log.info("[Railway] Onboarding — still on login/signin page, waiting for redirect…")
            continue

        clicked = await _try_click(page, [
            'button:has-text("Continue with Email")',
            'button:has-text("Get Started")',
            'button:has-text("Get started")',
            'button:has-text("Skip")',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'a:has-text("Skip")',
        ], timeout=2000)
        if not clicked:
            log.info("[Railway] Onboarding — no button found at step %d", step)
            break


async def _submit_email(page, email: str) -> bool:
    await page.goto("https://railway.com/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    try: await page.click('button:has-text("Accept All")', timeout=3000)
    except: pass

    await page.wait_for_selector('button:has-text("Log in using email")', timeout=15000)
    await page.click('button:has-text("Log in using email")')
    await page.wait_for_selector('input[type="email"]', timeout=10000)
    await page.fill('input[type="email"]', email)
    await page.click('button:has-text("Continue with Email")')
    await page.wait_for_timeout(5000)

    body = await page.inner_text("body")
    indicators = ["please enter the code", "enter the code sent", "verification code",
                  "check your email", "code sent to", "magic link"]
    if any(i in body.lower() for i in indicators):
        return True

    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            ft = await frame.inner_text("body")
            if any(i in ft.lower() for i in indicators):
                return True
            pin = await frame.query_selector('#pin-code-input-0, input[type="text"], input[inputmode="numeric"]')
            if pin:
                return True
        except:
            pass
    return False


async def _enter_otp(page, otp: str):
    """Type the OTP into Railway's verification form.

    Railway renders individual digit boxes. We try several selector strategies,
    then fall back to raw keyboard input on whatever input is focused.
    """
    # Strategy 1 — individual digit boxes (Railway's standard layout)
    individual_sels = [
        '#pin-code-input-0',
        'input[inputmode="numeric"]',
        'input[type="number"]',
        'input[type="text"][maxlength="1"]',
        'input[autocomplete="one-time-code"]',
    ]
    for sel in individual_sels:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=3000)
            await el.click()
            # Type digit-by-digit; Railway auto-advances focus between boxes
            await page.keyboard.type(otp, delay=120)
            log.info("[Railway] OTP entered via selector: %s", sel)
            return
        except Exception:
            pass

    # Strategy 2 — search inside iframes
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            for sel in ['#pin-code-input-0', 'input[inputmode="numeric"]',
                        'input[type="text"]', 'input[maxlength="1"]']:
                pin = await frame.query_selector(sel)
                if pin:
                    await frame.click(sel)
                    await frame.type(sel, otp, delay=120)
                    log.info("[Railway] OTP entered via iframe selector: %s", sel)
                    return
        except Exception:
            pass

    # Strategy 3 — fill each digit into its own box
    try:
        all_inputs = page.locator('input[type="text"], input[inputmode="numeric"]')
        cnt = await all_inputs.count()
        if cnt >= 6:
            for i, digit in enumerate(otp[:cnt]):
                await all_inputs.nth(i).fill(digit)
                await asyncio.sleep(0.1)
            log.info("[Railway] OTP entered via %d individual fill()s", cnt)
            return
    except Exception:
        pass

    # Strategy 4 — last resort: type into whatever is focused
    log.warning("[Railway] OTP fallback: raw keyboard type")
    await page.keyboard.type(otp, delay=150)


async def _add_tcp_proxy_browser(
    page, project_id: str, service_id: str, environment_id: str
) -> dict | None:
    """
    Add a TCP proxy for port 27017 via Railway's dashboard UI.
    Used when the GraphQL API is blocked for trial accounts ('Not Authorized').
    Navigates to Service → Settings → Networking and clicks 'Add TCP Proxy'.
    """
    try:
        service_url = (
            f"https://railway.com/project/{project_id}"
            f"/service/{service_id}"
            f"?environmentId={environment_id}"
        )
        log.info("[TCP Proxy UI] Navigating to service settings: %s", service_url[:90])
        await page.goto(service_url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2500)

        # ── Click the Settings tab ───────────────────────────────────────────
        await page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('a, button, [role="tab"]'));
            const tab = all.find(el =>
                el.textContent.trim() === 'Settings' && el.offsetParent !== null
            );
            if (tab) tab.click();
        }""")
        await page.wait_for_timeout(1500)

        # ── Find & click the "Add TCP Proxy" / "+ TCP Proxy" button ─────────
        tcp_btn_text = await page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('button, [role="button"], a'));
            const btn = all.find(el =>
                el.offsetParent !== null && el.textContent.toLowerCase().includes('tcp proxy')
            );
            if (btn) { btn.click(); return btn.textContent.trim(); }
            return null;
        }""")
        log.info("[TCP Proxy UI] TCP proxy button: %s", tcp_btn_text)

        if not tcp_btn_text:
            log.warning("[TCP Proxy UI] 'Add TCP Proxy' button not found — plan may block it")
            return None

        await page.wait_for_timeout(1000)

        # ── Fill port number 27017 ───────────────────────────────────────────
        filled = await page.evaluate("""() => {
            const inp = document.querySelector(
                'input[type="number"], input[placeholder*="port" i], input[placeholder*="27017"]'
            );
            if (inp) {
                inp.focus();
                inp.value = '';
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                return true;
            }
            return false;
        }""")
        await page.keyboard.type("27017")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(800)

        # ── Click Create / Save / Confirm ────────────────────────────────────
        await page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('button, [role="button"]'));
            const btn = all.find(el =>
                el.offsetParent !== null && (
                    el.textContent.trim() === 'Create' ||
                    el.textContent.trim() === 'Save'   ||
                    el.textContent.trim() === 'Add'    ||
                    el.textContent.trim() === 'Confirm'
                )
            );
            if (btn) btn.click();
        }""")

        # ── Poll the page until the proxy domain appears (up to 20s) ─────────
        for attempt in range(6):
            await page.wait_for_timeout(3500)
            proxy_info = await page.evaluate("""() => {
                const text = document.body.innerText;
                const dm = text.match(/([a-z0-9-]+\\.proxy\\.rlwy\\.net)/i);
                const pt = text.match(/\\.proxy\\.rlwy\\.net[^\\d]*(\\d{3,5})/i);
                if (dm && pt) return {domain: dm[1], port: pt[1]};
                return null;
            }""")
            if proxy_info:
                domain = proxy_info["domain"]
                port   = int(proxy_info["port"])
                log.info("[TCP Proxy UI] Proxy live: %s:%d", domain, port)
                return {"domain": domain, "proxyPort": port}
            log.info("[TCP Proxy UI] Waiting for proxy domain (attempt %d/6)…", attempt + 1)

        log.warning("[TCP Proxy UI] Proxy domain never appeared in UI")
        return None

    except Exception as exc:
        log.warning("[TCP Proxy UI] Browser TCP proxy error: %s", exc)
        return None


async def _extract_via_api(
    context, project_url: str, db_type: str,
    captured_token: str | None = None,
    progress_cb=None,
) -> dict | None:
    m = re.search(r'/project/([a-f0-9-]+)', project_url)
    if not m:
        return None
    project_id = m.group(1)

    cookies = await context.cookies()
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies
                             if "railway" in c.get("domain", "").lower()])

    pages = context.pages
    if not pages:
        return None

    # Prefer the token captured from network requests (reliable).
    # Fall back to localStorage extraction only if network capture missed it.
    token = captured_token
    if token:
        log.info("[Railway] Using network-captured Bearer token (len=%d)", len(token))
    else:
        page = pages[0]
        try:
            token = await page.evaluate("""() => {
                for (let k of Object.keys(localStorage)) {
                    let v = localStorage.getItem(k);
                    if (v && v.length > 20 && (k.includes('token') || k.includes('auth') || k.includes('key')))
                        return v;
                }
                for (let k of Object.keys(sessionStorage)) {
                    let v = sessionStorage.getItem(k);
                    if (v && v.length > 20) return v;
                }
                try {
                    const cookieToken = document.cookie.split(';')
                        .map(c => c.trim())
                        .find(c => c.startsWith('token=') || c.startsWith('railway_token='));
                    if (cookieToken) return cookieToken.split('=').slice(1).join('=');
                } catch(e) {}
                return null;
            }""")
            log.info("[Railway] localStorage token: %s",
                     "YES (len=%d)" % len(token) if token else "NO")
        except Exception as e:
            log.info("[Railway] Token extract error: %s", e)

    headers = {"Content-Type": "application/json"}
    if cookie_str:
        headers["Cookie"] = cookie_str
    if token:
        headers["Authorization"] = f"Bearer {token}"

    project_query = """
    query GetProject($projectId: String!) {
      project(id: $projectId) {
        id name
        services { edges { node { id name
            serviceInstances { edges { node { environmentId serviceId } } }
        } } }
        environments { edges { node { id name } } }
      }
    }"""

    service_id = environment_id = None
    try:
        resp = requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={"query": project_query, "variables": {"projectId": project_id}},
            headers=headers, timeout=15,
        )
        if resp.status_code == 200:
            proj = resp.json()["data"]["project"]
            services = proj["services"]["edges"]
            envs     = proj["environments"]["edges"]
            if services:
                node = services[0]["node"]
                service_id = node["id"]
                instances  = node["serviceInstances"]["edges"]
                if instances:
                    environment_id = instances[0]["node"]["environmentId"]
            if not environment_id and envs:
                environment_id = envs[0]["node"]["id"]
    except:
        pass

    m2 = re.search(r'/service/([a-f0-9-]+)', project_url)
    if m2 and not service_id:
        service_id = m2.group(1)

    if not service_id or not environment_id:
        return None

    vars_query = """
    query GetVariables($projectId: String!, $serviceId: String!, $environmentId: String!) {
      variables(projectId: $projectId, serviceId: $serviceId, environmentId: $environmentId)
    }"""

    cfg = DB_TYPES.get(db_type, DB_TYPES["postgresql"])

    # MongoDB can take longer to provision (service deploy + public TCP proxy vars).
    # Give it up to ~10 minutes (60 * 10s).
    max_attempts = 60 if db_type == "mongodb" else 18

    for attempt in range(max_attempts):
        try:
            vr = requests.post(
                "https://backboard.railway.app/graphql/v2",
                json={"query": vars_query, "variables": {
                    "projectId": project_id,
                    "serviceId": service_id,
                    "environmentId": environment_id,
                }},
                headers=headers, timeout=15,
            )
            if vr.status_code == 200:
                variables = vr.json().get("data", {}).get("variables", {})
                if isinstance(variables, dict) and variables:
                    db_url     = _get_first(variables, cfg["url_keys"])
                    private    = _get_first(variables, cfg["private_keys"])
                    host       = _get_first(variables, cfg["host_keys"])
                    port       = _get_first(variables, cfg["port_keys"])
                    username   = _get_first(variables, cfg["user_keys"])
                    password   = _get_first(variables, cfg["pass_keys"])
                    db_name    = _get_first(variables, cfg["db_keys"])
                    if db_url or private or password:
                        # ── MongoDB: wait for Railway to publish MONGO_PUBLIC_URL ─────────
                        # Railway creates the public TCP proxy asynchronously. The public
                        # URL shows up in Variables as MONGO_PUBLIC_URL after deploy.
                        if db_type == "mongodb":
                            # If we only have an internal URL, keep polling until the public
                            # URL appears (or until we hit max_attempts).
                            is_internal = ".railway.internal" in (db_url or "")
                            has_public = bool(variables.get("MONGO_PUBLIC_URL"))

                            if is_internal and not has_public and attempt < max_attempts - 1:
                                if progress_cb:
                                    waited = (attempt + 1) * 10
                                    await progress_cb(
                                        "waiting_tcp_proxy",
                                        "current",
                                        f"Waiting for Public URL (MONGO_PUBLIC_URL)… ({waited}s / {max_attempts*10}s)",
                                    )
                                await asyncio.sleep(10)
                                continue

                        return {
                            "db_url":         db_url,
                            "private_url":    private,
                            "host":           host,
                            "port":           port,
                            "username":       username,
                            "password":       password,
                            "db_name":        db_name,
                            "all_vars":       variables,
                            "railway_token":  token or "",
                        }
                log.info("[Railway] Variables not ready yet (attempt %d/%d) for %s",
                         attempt + 1, max_attempts, db_type)

                if db_type == "mongodb" and progress_cb:
                    waited = (attempt + 1) * 10
                    await progress_cb(
                        "deploying_mongo",
                        "current",
                        f"Waiting for Railway to deploy MongoDB… ({waited}s / {max_attempts*10}s)",
                    )
        except Exception as exc:
            log.warning("[Railway] _extract_via_api attempt %d error: %s", attempt + 1, exc)
        if attempt < max_attempts - 1:
            await asyncio.sleep(10)

    return None


async def create_railway_db(
    db_type: str,
    progress_cb,
    cancel_event: asyncio.Event | None = None,
    fresh: bool = True,
    proxy: dict | None = None,
) -> dict:
    """
    Main entry point for the Telegram bot.
    progress_cb(step_id, status) — status: "current" | "done" | "error"
    fresh=True (default): Always creates a fresh Railway account + email.
         Passing fresh=True explicitly (from /newdb) guarantees no session
         state, cookies, or credentials from any previous run are reused.
    Returns credentials dict on success, raises RuntimeError on failure.
    """
    import logging
    log = logging.getLogger(__name__)
    if fresh:
        log.info("[Railway] Starting FRESH session for db_type=%s (no session reuse)", db_type)
    cfg = DB_TYPES.get(db_type, DB_TYPES["postgresql"])

    def cb(step_id, status="current", meta: str | None = None):
        try:
            asyncio.get_event_loop().call_soon_threadsafe(
                lambda: asyncio.ensure_future(progress_cb(step_id, status, meta))
            )
        except:
            pass

    async def acb(step_id, status="current", meta: str | None = None):
        try:
            await progress_cb(step_id, status, meta)
        except:
            pass

    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=CHROMIUM_PATH or None,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--window-size=1400,900"],
        )
        ctx_kwargs = dict(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900},
        )
        if proxy:
            playwright_proxy = {"server": proxy["server"]}
            if proxy.get("username"):
                playwright_proxy["username"] = proxy["username"]
                playwright_proxy["password"] = proxy.get("password", "")
            ctx_kwargs["proxy"] = playwright_proxy
            log.info("[Railway] Using proxy: %s", proxy["server"])

        context = await browser.new_context(**ctx_kwargs)

        try:
            if cancelled():
                raise RuntimeError("Cancelled")

            # ── Email + OTP — up to 3 attempts, rotating providers on timeout ──────
            # Per-attempt OTP timeout: 60s. If no OTP arrives, re-navigate to the
            # Railway login page with a fresh email from a DIFFERENT provider.
            OTP_TIMEOUT_PER_ATTEMPT = 60
            MAX_EMAIL_ATTEMPTS = 3

            await acb("creating_email", "current")

            rp = await context.new_page()

            # ── Intercept outgoing requests to capture Railway Bearer token ──────────
            # Railway's frontend sends Authorization: Bearer <token> on every API call.
            # Capturing it here is 100% reliable; localStorage extraction is not.
            _captured_token: dict = {"value": None}

            async def _on_request(req):
                if "backboard.railway.app" in req.url and not _captured_token["value"]:
                    auth = req.headers.get("authorization", "")
                    if auth.startswith("Bearer ") and len(auth) > 27:
                        _captured_token["value"] = auth[7:]
                        log.info("[Railway] Bearer token captured from network (len=%d)",
                                 len(_captured_token["value"]))

            rp.on("request", _on_request)

            otp = None
            account = None
            tried_providers: set = set()

            for email_attempt in range(MAX_EMAIL_ATTEMPTS):
                if cancelled():
                    raise RuntimeError("Cancelled")

                # Pick a fresh unused email.
                # On attempt 1 always prefer mail.tm (most reliable for Railway OTPs).
                # On retries, prefer a provider not yet tried.
                _prefer = "mail.tm" if email_attempt == 0 else None
                candidate = None
                for _ in range(15):
                    c = get_random_account(
                        exclude_providers=tried_providers,
                        prefer_provider=_prefer,
                    )
                    if not c:
                        await asyncio.sleep(1)
                        continue
                    if is_email_used(c["email"]):
                        log.info("[Railway] Email used — skipping %s", c["email"])
                        await asyncio.sleep(0.5)
                        continue
                    candidate = c
                    break

                if not candidate:
                    log.warning("[Railway] Could not find fresh email on attempt %d", email_attempt + 1)
                    break

                temp_email = candidate["email"]
                mark_email_used(temp_email, provider=candidate.get("provider", ""))
                tried_providers.add(candidate["provider"])
                account = candidate
                log.info(
                    "[Railway] Email attempt %d/%d — provider=%s email=%s",
                    email_attempt + 1, MAX_EMAIL_ATTEMPTS, account["provider"], temp_email,
                )

                if email_attempt == 0:
                    await acb("creating_email", "done")
                    await acb("submitting_email", "current")

                otp_sent = await _submit_email(rp, temp_email)
                if not otp_sent:
                    log.warning("[Railway] Railway rejected email %s — trying next", temp_email)
                    if email_attempt == 0:
                        await acb("submitting_email", "done")
                    continue

                if email_attempt == 0:
                    await acb("submitting_email", "done")
                    await acb("waiting_otp", "current")

                log.info("[Railway] Polling OTP for %s (timeout=%ds)…", temp_email, OTP_TIMEOUT_PER_ATTEMPT)
                loop = asyncio.get_event_loop()
                otp = await loop.run_in_executor(None, poll_otp, account, OTP_TIMEOUT_PER_ATTEMPT)

                if otp:
                    log.info("[Railway] OTP received on attempt %d: %s", email_attempt + 1, otp)
                    break
                else:
                    log.warning(
                        "[Railway] OTP timeout on attempt %d (provider=%s) — switching provider",
                        email_attempt + 1, account["provider"],
                    )

            if not otp:
                raise RuntimeError(
                    f"OTP not received after {MAX_EMAIL_ATTEMPTS} attempts "
                    f"(tried: {', '.join(tried_providers)}) — try again"
                )
            if cancelled():
                raise RuntimeError("Cancelled")
            await acb("waiting_otp", "done")

            await acb("entering_otp", "current")
            await rp.bring_to_front()
            await _enter_otp(rp, otp)
            # Wait for Railway to accept the OTP and navigate away from the login page.
            # We poll every 1s for up to 25s — much more reliable than a fixed sleep.
            log.info("[Railway] Waiting for post-OTP redirect (up to 25s)…")
            for _w in range(25):
                await asyncio.sleep(1)
                _cur = rp.url
                if "login" not in _cur and "signin" not in _cur:
                    log.info("[Railway] Post-OTP redirect detected: %s (after %ds)", _cur[:60], _w + 1)
                    break
                if _w == 24:
                    log.warning("[Railway] Still on login page after 25s — OTP may have failed")
            await _handle_onboarding(rp)
            await rp.close()
            if cancelled():
                raise RuntimeError("Cancelled")
            await acb("entering_otp", "done")

            verify = await context.new_page()
            logged = await _is_logged_in(verify)
            await verify.close()
            if not logged:
                raise RuntimeError("Login failed — OTP may have been wrong. Try again.")

            # ── Phase 1: Handle TOS then create an empty project ─────────────────
            await acb("accepting_tos", "current")

            rp = await context.new_page()
            await rp.goto("https://railway.com/new", wait_until="domcontentloaded")
            await rp.wait_for_timeout(4000)

            body_text = await rp.inner_text("body")
            log.info("[Railway] /new page loaded. URL=%s text_len=%d preview=%s",
                     rp.url, len(body_text), body_text[:300].replace("\n", " "))

            # Handle TOS modal if it appeared immediately
            if _has_tos_modal(body_text):
                log.info("[Railway] TOS modal detected on /new — handling")
                await _dismiss_tos(rp)

            await acb("accepting_tos", "done")

            # ── Phase 2: Click "Empty Project" to enter the project canvas ────────
            await acb("creating_project", "current")
            if cancelled():
                raise RuntimeError("Cancelled")

            # Log all visible buttons/links to understand the page structure
            try:
                btn_texts = await rp.evaluate("""
                    () => [...document.querySelectorAll('button, a, [role="button"]')]
                        .filter(e => e.innerText.trim())
                        .map(e => e.tagName + ':' + e.innerText.trim().slice(0, 40))
                        .slice(0, 20)
                """)
                log.info("[Railway] Clickable elements on /new: %s", btn_texts)
            except:
                pass

            async def _click_empty_project():
                """Click 'Empty Project' using exact JS text match on smallest matching element."""
                result = await rp.evaluate("""
                    () => {
                        // Find all elements whose trimmed innerText is exactly 'Empty Project'
                        const candidates = [...document.querySelectorAll('*')]
                            .filter(e => {
                                const t = (e.innerText || '').trim();
                                return t === 'Empty Project';
                            });
                        if (!candidates.length) return null;
                        // Click the deepest/most specific element (last in DOM order = most nested)
                        const el = candidates[candidates.length - 1];
                        el.click();
                        return el.tagName + ':' + (el.className || '').slice(0, 40);
                    }
                """)
                log.info("[Railway] Empty project JS click result: %s", result)
                return result

            log.info("[Railway] Looking for 'Empty Project' option")
            empty_result = await _click_empty_project()
            log.info("[Railway] Empty project clicked: %s", bool(empty_result))

            if not empty_result:
                body2 = await rp.inner_text("body")
                log.info("[Railway] Empty click failed — page text: %s", body2[:400].replace("\n", " "))

            # Wait for redirect to /project/
            project_url = None
            for attempt in range(40):
                if cancelled():
                    raise RuntimeError("Cancelled")
                await rp.wait_for_timeout(3000)
                cur  = rp.url
                body = await rp.inner_text("body")
                log.info("[Railway] Project wait attempt=%d url=%s", attempt, cur[:80])

                if "/project/" in cur:
                    project_url = cur
                    log.info("[Railway] Got project URL: %s", project_url)
                    break

                # TOS modal may have appeared after clicking
                if _has_tos_modal(body):
                    log.info("[Railway] TOS modal appeared mid-flow — dismissing")
                    await _dismiss_tos(rp)
                    await _click_empty_project()
                    continue

                # If we ended up on /new/database (wrong option clicked), go back to /new
                if "/new/database" in cur:
                    log.info("[Railway] Ended up on /new/database — going back to /new")
                    await rp.goto("https://railway.com/new", wait_until="domcontentloaded")
                    await rp.wait_for_timeout(2000)
                    # Dismiss TOS if it reappears
                    body_new = await rp.inner_text("body")
                    if _has_tos_modal(body_new):
                        await _dismiss_tos(rp)
                    await _click_empty_project()
                    continue

                # If we're still on /new (but not /new/database), retry exact click
                if "/new" in cur:
                    log.info("[Railway] Still on /new, retrying empty project exact click")
                    await _click_empty_project()

            if not project_url or "/project/" not in project_url:
                final_url = rp.url
                final_body = await rp.inner_text("body")
                log.error("[Railway] Failed to reach project. Final URL=%s body=%s",
                          final_url, final_body[:400].replace("\n", " "))
                raise RuntimeError(
                    f"Could not create project — Railway UI may have changed. "
                    f"Last URL: {final_url[:80]}"
                )

            await acb("creating_project", "done")
            await acb("provisioning_db", "current")
            if cancelled():
                raise RuntimeError("Cancelled")

            # ── Phase 3: Add DB via Ctrl+K command palette inside the project ─────
            log.info("[Railway] Opening command palette to add %s", cfg["search"])
            await rp.wait_for_timeout(2000)
            await rp.keyboard.press("Control+k")
            await rp.wait_for_timeout(2000)

            # Type DB name
            await rp.keyboard.type(cfg["search"], delay=80)
            await rp.wait_for_timeout(2000)

            db_option_clicked = await _try_click(rp, [
                f'[role="option"]:has-text("{cfg["search"]}")',
                f'li:has-text("{cfg["search"]}")',
                f'[class*="item"]:has-text("{cfg["search"]}")',
                f'[class*="result"]:has-text("{cfg["search"]}")',
                f'[data-value*="{cfg["search"].lower()}"]',
            ], timeout=4000)
            log.info("[Railway] DB option clicked: %s", db_option_clicked)

            if not db_option_clicked:
                log.info("[Railway] DB option not found, pressing Enter")
                await rp.keyboard.press("Enter")

            await rp.wait_for_timeout(5000)
            log.info("[Railway] After DB selection. URL=%s", rp.url[:80])

            # ── Phase 4: Extract credentials ──────────────────────────────────────
            await acb("extracting_creds", "current")
            if cancelled():
                raise RuntimeError("Cancelled")

            # MongoDB UX: show deploy/proxy sub-steps while waiting.
            if db_type == "mongodb":
                await acb("deploying_mongo", "current", "Starting deployment…")

            creds = await _extract_via_api(
                context,
                project_url,
                db_type,
                captured_token=_captured_token["value"],
                progress_cb=acb,
            )
            if not creds:
                raise RuntimeError(
                    "Could not extract credentials — the database service may still be "
                    "initializing. Please try again in a moment."
                )

            # Mark MongoDB sub-steps complete once creds are available.
            if db_type == "mongodb":
                await acb("deploying_mongo", "done")
                await acb("waiting_tcp_proxy", "done")

            await acb("extracting_creds", "done")
            await acb("provisioning_db", "done")

            creds["email"]       = temp_email
            creds["project_url"] = project_url
            creds["db_type"]     = db_type

            log.info("[Railway] Done! email=%s db_type=%s project=%s",
                     temp_email, db_type, project_url[:60])

            await browser.close()
            return creds

        except Exception as e:
            log.error("[Railway] Fatal error: %s", e)
            await browser.close()
            raise RuntimeError(str(e))
