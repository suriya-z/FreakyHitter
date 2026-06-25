# GokuHitter Bot — Project Context

> **Use this file to bootstrap any new Antigravity chat.**
> Open this file in your editor, start a new chat, and say: "Read PROJECT_CONTEXT.md and continue where we left off."

---

## What This Is

**GokuHitter** is a Telegram bot that performs Stripe payment gateway hitting via pure Python HTTP requests. It replaced an older architecture that relied on a Chrome extension ("Dot Bypasser") which had anti-tamper / license issues.

**Architecture:** `Telegram Bot (aiogram) → Python Backend (hitter_core.py) → Stripe API`

**Deployment:** Render (via `render.yaml` + `Dockerfile`), PostgreSQL for persistence (proxies, user data).

**Stack:** Python 3.11, aiogram, curl_cffi (TLS fingerprint spoofing), aiohttp, asyncpg, Playwright (fallback).

---

## File Structure

| File | Purpose | Size |
|------|---------|------|
| `bot.py` | Telegram bot — commands, UI, session management | 993 lines |
| `hitter_core.py` | Core hitting engine — Stripe API interaction, proxy management, BIN lookup, card gen | 1477 lines |
| `hitter_core_backup.py` | Previous version backup | — |
| `.env` | BOT_TOKEN, DATABASE_URL, LOG_GROUP_ID, OWNER_ID | — |
| `Dockerfile` + `render.yaml` | Deployment config | — |
| `requirements.txt` | Python dependencies | — |

---

## Key Classes (hitter_core.py)

| Class | Responsibility |
|-------|---------------|
| `StripeAPIExtractor` | Extracts `cs_live` / `pk_live` from checkout URLs, fetches payment data |
| `StripeAPIHitter` | Core hitting logic — creates PaymentMethod, confirms PaymentIntent, handles 3DS |
| `ConcurrentHitter` | Async worker pool — parallel card hitting with stop-on-success |
| `ProxyManager` | Per-user proxy CRUD, geo-matching via IP-API, PostgreSQL-backed |
| `RandomData` | Generates fake identity data (name, email, phone, address) geo-matched to proxy |
| `BINLookup` | Free BIN database lookup with caching (antipublic.cc) |
| `CardGenerator` | Luhn-valid card generation from BIN patterns |

---

## Bot Commands (bot.py)

| Command | What it does |
|---------|-------------|
| `/hit <link> <card\|cards>` | Hit a Stripe checkout URL with card(s) |
| `/stop` | Cancel active hitting session |
| `/proxy` | Proxy management (add/remove/test) |
| `/allproxies` | Bulk proxy operations |
| `/proxystatus` | Check proxy pool status |
| `/offproxy` | Disable proxy usage |
| `/setlog` | Set logging group |
| `/cmds` | Show all commands |

---

## Features Already Implemented

1. **Pure Python Stripe hitting** — no browser/extension dependency
2. **TLS fingerprint spoofing** — curl_cffi impersonating Chrome 120/131
3. **Concurrent hitting** — async worker pool with configurable batch size
4. **Stop-on-success** — instant task cancellation when a card charges
5. **Receipt URL extraction** — recursive finder with retry/polling
6. **BIN lookup** — card country/bank/type via antipublic.cc API
7. **Proxy geo-matching** — matches proxy country to card issuing country
8. **Stripe device fingerprint tokens** — `muid`/`sid`/`guid` generation via `m.stripe.com/6`
9. **Per-user proxy management** — PostgreSQL-backed, with health checking
10. **Auto-proxy checker loop** — background task that periodically validates proxies
11. **Auto-delete messages** — 30s cleanup for clean chat UX
12. **Progress animations** — live-updating hitting status in Telegram

---

## God-Tier Roadmap (Priority Order)

### ✅ DONE — Tier 1
1. ✅ Stripe fingerprint tokens (`muid`/`sid`/`guid`)
2. ✅ BIN-to-proxy geo-matching
3. ⬜ Proxy scoring system (track success rates, auto-retire burnt proxies)

### ⬜ TODO — Tier 2
4. ⬜ Radar signal spoofing (full RadarProfile generator)
5. ⬜ Smart retry engine (decline-code-aware retries)
6. ⬜ Low-value SCA exemption exploitation

### ⬜ TODO — Tier 3
7. ⬜ Full Stripe.js emulation
8. ⬜ Playwright headless fallback
9. ⬜ Multi-gateway support (Braintree, Adyen, Square)
10. ⬜ Card validation tiers (Luhn → $0 auth → micro-charge → full)

### ⬜ TODO — Tier 4 (Infrastructure)
11. ⬜ Distributed worker architecture (Redis queue + horizontal scaling)
12. ⬜ Real-time analytics dashboard

---

## Key Technical Notes

- **Stripe flow:** Extract `cs_live` + `pk_live` from checkout page → create PaymentMethod via `/v1/payment_methods` → confirm via `/v1/payment_pages/{cs}/confirm` → handle 3DS if needed → poll PaymentIntent for result
- **TLS spoofing is critical** — curl_cffi `chrome120` impersonation prevents JA3 fingerprint bans
- **muid/sid/guid generation** — POST to `m.stripe.com/6` with full device telemetry JSON (v2 protocol, screen dims, timezone, language, etc.)
- **BIN lookup** — `https://bins.antipublic.cc/bins/{bin6}`, cached in-memory
- **Proxy geo** — IP-API lookup, cached, used for address/timezone generation
- **Concurrency** — asyncio task pool, workers pull from queue, first success cancels all others
- **Race condition guard** — workers check `self.is_running` after each hit to prevent trailing failures from overwriting success messages

---

## Git History

The project has full `.git` history. Use `git log --oneline -20` to see recent changes.

---

## How to Run Locally

```bash
cd gokuhitter_bot
pip install -r requirements.txt
# Set up .env with BOT_TOKEN, DATABASE_URL, LOG_GROUP_ID, OWNER_ID
python bot.py
```

## How to Deploy

Push to GitHub → Render auto-deploys from `render.yaml`.
