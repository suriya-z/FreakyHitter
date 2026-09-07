"""
Captcha Solver Module — Multi-provider async captcha solving.
Supports: NopeCHA, CaptchaAI, 2Captcha, CapSolver
Solves:   hCaptcha, hCaptcha Enterprise, reCAPTCHA v2, Cloudflare Turnstile
"""
import aiohttp
import asyncio
import os
import re
import json
import logging
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)

NOPECHA_SUBMIT = "https://api.nopecha.com/token"
NOPECHA_RESULT = "https://api.nopecha.com/token"

CAPTCHAAI_IN = "https://ocr.captchaai.com/in.php"
CAPTCHAAI_RES = "https://ocr.captchaai.com/res.php"

TWOCAPTCHA_IN  = "https://2captcha.com/in.php"
TWOCAPTCHA_RES = "https://2captcha.com/res.php"

CAPSOLVER_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_RES = "https://api.capsolver.com/getTaskResult"

MAX_POLL_ATTEMPTS = 40
POLL_INTERVAL = 5

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def _get_config_key(key: str, env_var: str) -> str:
    try:
        with open(_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        val = cfg.get(key, "")
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env_var, "")


def get_nopecha_key() -> str:
    return _get_config_key("nopecha_api_key", "NOPECHA_API_KEY")

def get_captchaai_key() -> str:
    return _get_config_key("captchaai_api_key", "CAPTCHAAI_API_KEY")

def get_twocaptcha_key() -> str:
    return _get_config_key("twocaptcha_api_key", "TWOCAPTCHA_API_KEY")

def get_capsolver_key() -> str:
    return _get_config_key("capsolver_api_key", "CAPSOLVER_API_KEY")

def has_any_solver_key() -> bool:
    return bool(get_nopecha_key() or get_captchaai_key() or get_twocaptcha_key() or get_capsolver_key())


# ============= PROVIDER IMPLEMENTATIONS =============

async def _nopecha_solve(task_type, sitekey, pageurl, session=None, extra=None):
    key = get_nopecha_key()
    if not key:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        payload = {"type": task_type, "sitekey": sitekey, "url": pageurl, "key": key}
        if extra:
            payload.update(extra)
        logger.info(f"NopeCHA submit: type={task_type}, sitekey={sitekey[:20]}...")
        async with session.post(NOPECHA_SUBMIT, json=payload) as resp:
            result = await resp.json(content_type=None)
            if "error" in result:
                logger.warning(f"NopeCHA submit error {result.get('error')}: {result.get('message', '')}")
                return None
            task_id = result.get("data")
            if not task_id:
                return None

        await asyncio.sleep(10)
        for _ in range(MAX_POLL_ATTEMPTS):
            params = {"id": task_id, "key": key}
            async with session.get(NOPECHA_RESULT, params=params) as resp:
                result = await resp.json(content_type=None)
                if "error" in result:
                    err = result["error"]
                    if err in (9, 14) or err == "Incomplete" or "Incomplete" in str(result.get("message", "")):
                        pass
                    else:
                        return None
                else:
                    data = result.get("data")
                    token = data[0] if isinstance(data, list) and data else data
                    if token and isinstance(token, str) and len(token) > 20:
                        logger.info(f"NopeCHA solved! Token: {str(token)[:30]}...")
                        return token
            await asyncio.sleep(POLL_INTERVAL)
        return None
    except Exception as e:
        logger.error(f"NopeCHA solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


async def _captchaai_solve(method, sitekey, pageurl, session=None, extra=None):
    key = get_captchaai_key()
    if not key:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        submit_data = {"key": key, "method": method, "sitekey": sitekey, "pageurl": pageurl, "json": "1"}
        if extra:
            submit_data.update(extra)
        async with session.post(CAPTCHAAI_IN, data=submit_data) as resp:
            result = await resp.json(content_type=None)
            if result.get("status") != 1:
                return None
            task_id = result["request"]

        await asyncio.sleep(10)
        for _ in range(MAX_POLL_ATTEMPTS):
            params = {"key": key, "action": "get", "id": task_id, "json": "1"}
            async with session.get(CAPTCHAAI_RES, params=params) as resp:
                result = await resp.json(content_type=None)
                if result.get("status") == 1:
                    return result["request"]
                if result.get("request") != "CAPCHA_NOT_READY":
                    return None
            await asyncio.sleep(POLL_INTERVAL)
        return None
    except Exception as e:
        logger.error(f"CaptchaAI solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


async def _twocaptcha_solve(method, sitekey, pageurl, session=None, extra=None):
    key = get_twocaptcha_key()
    if not key:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        submit_data = {"key": key, "method": method, "sitekey": sitekey, "pageurl": pageurl, "json": "1"}
        if extra:
            submit_data.update(extra)
        async with session.post(TWOCAPTCHA_IN, data=submit_data) as resp:
            result = await resp.json(content_type=None)
            if result.get("status") != 1:
                return None
            task_id = result["request"]

        await asyncio.sleep(15)
        for _ in range(MAX_POLL_ATTEMPTS):
            params = {"key": key, "action": "get", "id": task_id, "json": "1"}
            async with session.get(TWOCAPTCHA_RES, params=params) as resp:
                result = await resp.json(content_type=None)
                if result.get("status") == 1:
                    return result["request"]
                if result.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                    return None
            await asyncio.sleep(POLL_INTERVAL)
        return None
    except Exception as e:
        logger.error(f"2captcha solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


async def _capsolver_solve(task_type, sitekey, pageurl, session=None, extra=None):
    key = get_capsolver_key()
    if not key:
        return None
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        task = {"type": task_type, "websiteURL": pageurl, "websiteKey": sitekey}
        if extra:
            task.update(extra)
        payload = {"clientKey": key, "task": task}
        async with session.post(CAPSOLVER_URL, json=payload) as resp:
            result = await resp.json(content_type=None)
            if result.get("errorId") != 0:
                return None
            task_id = result.get("taskId")
            if not task_id:
                return None

        await asyncio.sleep(5)
        for _ in range(MAX_POLL_ATTEMPTS):
            poll_payload = {"clientKey": key, "taskId": task_id}
            async with session.post(CAPSOLVER_RES, json=poll_payload) as resp:
                result = await resp.json(content_type=None)
                if result.get("errorId") != 0:
                    return None
                status = result.get("status")
                if status == "ready":
                    solution = result.get("solution", {})
                    token = solution.get("gRecaptchaResponse") or solution.get("token")
                    if token:
                        return token
                    return None
                elif status != "processing":
                    return None
            await asyncio.sleep(POLL_INTERVAL)
        return None
    except Exception as e:
        logger.error(f"CapSolver solve error: {e}")
        return None
    finally:
        if own_session:
            await session.close()


# ============= PUBLIC API =============

async def solve_hcaptcha(sitekey, pageurl, session=None):
    if get_nopecha_key():
        token = await _nopecha_solve("hcaptcha", sitekey, pageurl, session)
        if token:
            return token
    if get_captchaai_key():
        token = await _captchaai_solve("hcaptcha", sitekey, pageurl, session)
        if token:
            return token
    if get_twocaptcha_key():
        token = await _twocaptcha_solve("hcaptcha", sitekey, pageurl, session)
        if token:
            return token
    return await _capsolver_solve("HCaptchaTaskProxyless", sitekey, pageurl, session)


async def solve_hcaptcha_enterprise(sitekey, pageurl, rqdata=None, session=None):
    extra_cs = {}
    if rqdata:
        extra_cs["enterprisePayload"] = {"rqdata": rqdata}

    if get_nopecha_key():
        nopecha_extra = {}
        if rqdata:
            nopecha_extra["rqdata"] = rqdata
        token = await _nopecha_solve("hcaptcha", sitekey, pageurl, session, extra=nopecha_extra or None)
        if token:
            return token
    if get_captchaai_key():
        captchaai_extra = {}
        if rqdata:
            captchaai_extra["data"] = rqdata
        token = await _captchaai_solve("hcaptcha", sitekey, pageurl, session, extra=captchaai_extra or None)
        if token:
            return token
    if get_twocaptcha_key():
        extra_2c = {"enterprise": "1"}
        if rqdata:
            extra_2c["data"] = rqdata
        token = await _twocaptcha_solve("hcaptcha", sitekey, pageurl, session, extra=extra_2c)
        if token:
            return token
    return await _capsolver_solve("HCaptchaEnterpriseTaskProxyless", sitekey, pageurl, session, extra=extra_cs or None)


async def solve_recaptcha_v2(sitekey, pageurl, session=None):
    if get_nopecha_key():
        token = await _nopecha_solve("recaptcha2", sitekey, pageurl, session)
        if token:
            return token
    if get_captchaai_key():
        token = await _captchaai_solve("userrecaptcha", sitekey, pageurl, session)
        if token:
            return token
    if get_twocaptcha_key():
        token = await _twocaptcha_solve("userrecaptcha", sitekey, pageurl, session)
        if token:
            return token
    return await _capsolver_solve("ReCaptchaV2TaskProxyless", sitekey, pageurl, session)


async def solve_turnstile(sitekey, pageurl, session=None, action=None, cdata=None):
    if get_nopecha_key():
        extra = {}
        if action: extra["action"] = action
        if cdata: extra["cdata"] = cdata
        token = await _nopecha_solve("turnstile", sitekey, pageurl, session, extra)
        if token:
            return token
    if get_captchaai_key():
        extra = {}
        if action: extra["action"] = action
        if cdata: extra["data"] = cdata
        token = await _captchaai_solve("turnstile", sitekey, pageurl, session, extra)
        if token:
            return token
    if get_twocaptcha_key():
        extra = {}
        if action: extra["action"] = action
        if cdata: extra["data"] = cdata
        token = await _twocaptcha_solve("turnstile", sitekey, pageurl, session, extra)
        if token:
            return token
    cs_extra = {}
    if action:
        cs_extra["action"] = action
    return await _capsolver_solve("AntiTurnstileTaskProxyless", sitekey, pageurl, session, extra=cs_extra or None)
