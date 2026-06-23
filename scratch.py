import requests
import json
import re

url = "https://invoice.stripe.com/i/acct_1HOrSwC6h1nxGoI3/live_YWNjdF8xSE9yU3dDNmgxbnhHb0kzLF9Va0c3MGhXdVlZWVZLelhNSGlkbkNVWTROSUFlNjNrLDE3MjY1Nzg3Nw0200RWfnf7eR?s=ap"
headers = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
resp = requests.get(url, headers=headers)
html = resp.text

print(f"Status: {resp.status_code}")
print(f"Len: {len(html)}")

# find all script tags
matches = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
for m in matches:
    if 'pk_live' in m:
        print("FOUND pk_live in script!")
        print(m[:200])

# check for any pk_live pattern
pks = re.findall(r'pk_live_[a-zA-Z0-9]+', html)
print("PKs:", pks)

# check for any checkout links
cs = re.findall(r'cs_live_[a-zA-Z0-9]+', html)
print("CSs:", cs)
