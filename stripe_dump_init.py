"""Dump the Stripe init JSON structure to find the amount field."""
import asyncio, sys, os, json
sys.path.insert(0, r"C:\Users\acer\Downloads\ai\gokuhitter_bot")
os.chdir(r"C:\Users\acer\Downloads\ai\gokuhitter_bot")
from curl_compat import ChromeSession

CS = "cs_live_a1PkDtJT6lTScwoqkJTHWZF4d6xdIunwEzeZYvHbck0q4to0I6rqbHjMdz"
PK = "pk_live_51Q2SePHy7UpDvrVi0TktN5SRCoiTNs5Ang2xyMtwtC5BZzgTm4AtcJS3aAl3ycCLD47z3E7ui0OqZo41qVhGtEmV00yBNEzfle"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


async def main():
    async with ChromeSession(impersonate="chrome131", timeout=15) as s:
        url = f"https://api.stripe.com/v1/payment_pages/{CS}/init"
        data = f"key={PK}&eid=NA&browser_locale=en-US"
        hdr = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA,
               "Origin": "https://checkout.stripe.com", "Referer": f"https://checkout.stripe.com/c/pay/{CS}"}
        async with s.post(url, data=data, headers=hdr) as r:
            d = r.json() if callable(r.json) else r.json
            print("status:", r.status_code)
            print("top-level keys:", list(d.keys()) if isinstance(d, dict) else type(d))
            for k in d:
                v = d[k]
                if isinstance(v, dict):
                    print(f"  {k}: {list(v.keys())}")
                else:
                    print(f"  {k}: {str(v)[:80]}")
            # hunt for amount-ish keys recursively
            def walk(o, path=""):
                if isinstance(o, dict):
                    for k2, v2 in o.items():
                        if any(x in k2.lower() for x in ("amount", "total", "price", "value")):
                            if not isinstance(v2, (dict, list)):
                                print(f"    CANDIDATE {path}.{k2} = {v2}")
                        walk(v2, f"{path}.{k2}")
                elif isinstance(o, list):
                    for i, it in enumerate(o[:3]):
                        walk(it, f"{path}[{i}]")
            walk(d)


asyncio.run(main())
