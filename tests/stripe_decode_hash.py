"""Decode the Stripe checkout URL hash (XOR-5 blob) to see the real apiKey + account."""
import sys, urllib.parse, base64, json

URL = sys.argv[1]
hash_str = URL.split('#')[1] if '#' in URL else ''
decoded = urllib.parse.unquote(hash_str)
raw_bytes = base64.b64decode(decoded + '==')
json_str = ''.join(chr(b ^ 5) for b in raw_bytes)
data = json.loads(json_str)
print(json.dumps(data, indent=2))
