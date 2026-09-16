import asyncio
from gateways.stripe.stripe_3ds_bypasser import (
    Stripe3DSBypasser,
    _flatten,
    _parse_form,
    _as_dict,
    _html_challenge,
)

async def test_suite():
    print("--- 1. Testing Telemetry & Timezone Zero Handling ---")
    telemetry, tz, w, h, lang, ua = Stripe3DSBypasser._build_browser_telemetry('GB', profile={'tz_offset': 0})
    assert tz == 0, f"Expected tz 0 for GB profile, got {tz}"
    print(f"[PASS] tz_offset: {tz}, resolution: {w}x{h}, lang: {lang}")

    print("--- 2. Testing Flatten Helper for Nested browser[*] Form Keys ---")
    nested = {
        "key": "pk_live_123",
        "browser": {
            "threeDSCompInd": "Y",
            "fingerprintAttempted": True,
            "browserJavaEnabled": False,
        },
        "payment_user_agent": "stripe.js/test"
    }
    flattened = _flatten(nested)
    assert flattened["browser[threeDSCompInd]"] == "Y"
    assert flattened["browser[fingerprintAttempted]"] == "true"
    assert flattened["browser[browserJavaEnabled]"] == "false"
    assert flattened["payment_user_agent"] == "stripe.js/test"
    print("[PASS] Flattened structure:", flattened)

    print("--- 3. Testing Form Action '#' and PaReq Entity Unescape ---")
    html_sample = '<form action="#"><input name="PaReq" value="code123&amp;xyz"><input name="MD" value="merchant"></form>'
    act, fields = _parse_form(html_sample, "https://bank.com/acs/entry")
    assert act == "https://bank.com/acs/entry", f"Expected base_url fallback for '#', got {act}"
    assert fields["PaReq"] == "code123&xyz", f"Expected unescaped PaReq, got {fields['PaReq']}"
    print(f"[PASS] Action: {act}, Fields: {fields}")

    print("--- 4. Testing _as_id String Filtering (pi_, seti_, pm_) ---")
    _as_id = Stripe3DSBypasser._as_id
    assert _as_id("src_1O8qWE2eZvKYlo2C") == "src_1O8qWE2eZvKYlo2C"
    assert _as_id("pi_3O8qWE2eZvKYlo2C") == ""
    assert _as_id("seti_3O8qWE2eZvKYlo2C") == ""
    assert _as_id("pm_3O8qWE2eZvKYlo2C") == ""
    assert _as_id({"id": "src_dict_valid"}) == "src_dict_valid"
    assert _as_id({"id": "pi_dict_invalid"}) == ""
    print("[PASS] _as_id clean filters verified")

    print("--- 5. Testing _merge_sdk String Source Id Preservation ---")
    na_sample = {
        "three_d_secure_2_source": "src_preserved_999",
        "use_stripe_sdk": {
            "type": "stripe_3ds2_fingerprint",
            "three_ds_method_url": "https://hooks.stripe.com/method"
        }
    }
    merged = Stripe3DSBypasser._merge_sdk(na_sample)
    assert merged.get("three_d_secure_2_source") == "src_preserved_999"
    print("[PASS] String source ID preserved in _merge_sdk:", merged.get("three_d_secure_2_source"))

    print("--- 6. Testing _html_challenge Token/Code False Positive Immunity ---")
    csrf_html = '<html><input type="hidden" name="csrf_token" value="abc"><input type="hidden" name="code" value="123"></html>'
    assert not _html_challenge(csrf_html), "Expected CSRF/code not to trigger challenge wall"
    otp_html = '<html><div>Enter one time password below</div><input name="otp"></html>'
    assert _html_challenge(otp_html), "Expected OTP to trigger challenge wall"
    print("[PASS] _html_challenge precision verified")

    print("\nALL 6 SMOKE CHECKS PASSED.")

if __name__ == "__main__":
    asyncio.run(test_suite())
