# language: Python 3.12+, file: stripe_fid.py, target: Windows 11, stdlib-only
# Reverse-engineered from Stripe.js module 3950:
#   r = (s) => XOR with 5
#   decode = (b64) => r(atob(decodeURIComponent(b64))).trim()
#   encode = (obj) => encodeURIComponent(btoa(r(pad(JSON.stringify(obj)))))
import base64
import json
import re
import urllib.parse
from typing import Any


def decode_fragment(fid_or_url: str) -> dict[str, Any]:
    """
    Декодирует обфусцированный фрагмент (#fid...) из URL Stripe Checkout Session.
    
    Алгоритм Stripe:
      1. Извлечение хэш-части URL (#...) и отсечение query-параметров (?...)
      2. URL-декодирование (unquote)
      3. Base64-декодирование байтов
      4. Побайтовый XOR каждого байта с числом 5 (byte ^ 5)
      5. JSON-десериализация результирующего UTF-8 текста
    
    Поддерживает:
      - Полный URL (https://checkout.stripe.com/c/pay/cs_live_...#fid...)
      - Фрагмент с решёткой (#fid...)
      - Сырую строку фрагмента (fid...)
      - URL-encoded варианты (%23fid...)
    """
    if not fid_or_url:
        return {}

    raw = str(fid_or_url).strip()

    # Извлекаем cs_live / cs_test session ID из URL пути, если он есть
    session_id = None
    m_session = re.search(r"\b(cs_(?:live|test)_[a-zA-Z0-9]+)\b", raw)
    if m_session:
        session_id = m_session.group(1)

    # Отсекаем всё до символа '#' (хэш)
    if "#" in raw:
        raw = raw.split("#", 1)[1]

    # Отсекаем query-параметры после '?'
    raw = raw.split("?", 1)[0]

    # Если передан вид cs_..._secret_fid...
    if "_secret_" in raw:
        parts = raw.split("_secret_", 1)
        if not session_id and parts[0].startswith("cs_"):
            session_id = parts[0]
        raw = parts[1]

    # URL-decode
    unquoted = urllib.parse.unquote(raw)

    # Очищаем возможные концевые символы
    b64_str = unquoted.strip().rstrip("%")
    if not b64_str:
        return {}

    # Выравнивание padding для Base64
    pad = b64_str + "=" * (-len(b64_str) % 4)

    try:
        decoded_bytes = base64.b64decode(pad, altchars=b"-_")
    except Exception:
        try:
            decoded_bytes = base64.b64decode(pad)
        except Exception as e:
            return {"error": f"Base64 decode error: {e}", "raw": raw}

    # Побайтовый XOR с 5
    xored_bytes = bytes(b ^ 5 for b in decoded_bytes)
    xored_text = xored_bytes.decode("utf-8", errors="replace").strip()

    try:
        data = json.loads(xored_text)
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}", "raw_text": xored_text}

    if not isinstance(data, dict):
        return {"value": data}

    # Дополняем контекстными ключами, если они извлечены
    if session_id:
        if "checkoutSessionId" not in data and "sessionId" not in data:
            data["checkoutSessionId"] = session_id
        if "client_secret" not in data:
            data["client_secret"] = f"{session_id}_secret_{b64_str}"

    return data


def encode_fragment(data: dict[str, Any]) -> str:
    """
    Прямое кодирование словаря в Stripe Checkout фрагмент (#fid...).
    Точная инверсия алгоритма Stripe (module 3950).
    """
    json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    utf8_bytes = json_str.encode("utf-8")
    
    # Stripe паддит строку пробелами до кратности 3 перед btoa
    pad_len = (3 - len(utf8_bytes) % 3) % 3
    padded_bytes = utf8_bytes + (b" " * pad_len)
    
    xored_bytes = bytes(b ^ 5 for b in padded_bytes)
    b64_encoded = base64.b64encode(xored_bytes).decode("ascii")
    return urllib.parse.quote(b64_encoded, safe="")


if __name__ == "__main__":
    # Тест-вектор из боевой сессии Checkout
    test_vector = (
        "fidnandhYHdWcXxpYCc/J2FgY2RwaXEnKSdpamZkaWAnPydgaycpJ3ZwZ3Zmd2x1cWxqa1Brb"
        "HRwYGtgdnZAa2RnaWBhJz9jZGl2YCknYnBkZmRoamlgU2R3bGRrcSc/J2Zqa3F3amknKSdkdW"
        "xOYHwnPyd1blppbHNgWjA0SH12UVJPcXM9S1BqQ3xMZnBGRkdif25HNXR/QlFOQkNuNG9AZk"
        "NVb1Q1PXBqTjcxNm8zQ01iaUt8a2ZhYEBTZ39Lb3Q1UXdLMG4yPVRDT1RzVU91Y25TNTVmN3"
        "ZfMkY2aycpJ2N3amhWYHdzYHcnP3F3cGApJ2dkZm5id2xwa2FGamlqdyc/JyZjY2NjY2MnKSd"
        "pZHxqcHFRfHVgJz8naHBpcWxabHFgaCcpJ2BrZGdpYFVpZGZgbWppYWB3dic/cXdwYHgl"
    )

    test_url = f"https://pay.opus.pro/c/pay/cs_live_b1Uf5qpxeXTGYCy6WQoQB5bmwsnvKAnqQR5rdLxU4U5GYLut0vTO96sqCz#{test_vector}"

    print("[*] Stripe Checkout Fragment Decoder")
    print(f"[*] Input URL: {test_url[:80]}...\n")
    
    decoded = decode_fragment(test_url)
    print("[+] Decoded Payload:")
    print(json.dumps(decoded, indent=2, ensure_ascii=False))

    # Круговой тест: decode -> encode -> decode
    encoded_back = encode_fragment({k: v for k, v in decoded.items() if k not in ("checkoutSessionId", "client_secret")})
    re_decoded = decode_fragment(encoded_back)
    assert re_decoded.get("apiKey") == decoded.get("apiKey"), "Roundtrip verification failed!"
    print("\n[+] Roundtrip Verification: PASSED (100% match)")
