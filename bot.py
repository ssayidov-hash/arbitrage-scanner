# ================================================================
# ARBITRAGE SCANNER v5.6-STABLE (Render + Telegram Webhook, fixed)
# ================================================================

import os
import sys
import asyncio
import nest_asyncio
from datetime import datetime
import ccxt.async_support as ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.error import TimedOut, RetryAfter, NetworkError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== CONFIG ==================
MIN_SPREAD = 1.2
MIN_VOLUME_1H = 500_000
SCAN_INTERVAL = 120
VERSION = "v5.6-stable"

# ================== ENV VARS ==================
env_vars = {
    "MEXC_API_KEY": os.getenv("MEXC_API_KEY"),
    "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET"),
    "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
    "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
    "BITGET_API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE"),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "CHAT_ID": os.getenv("CHAT_ID"),
}
TELEGRAM_BOT_TOKEN = env_vars["TELEGRAM_BOT_TOKEN"]
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ Нет TELEGRAM_BOT_TOKEN")

# ================== PREPARE LOOP ==================
nest_asyncio.apply()  # важно для Render / Python 3.13

# ================== GLOBALS ==================
exchanges = {}
exchange_status = {}
pending_trades = {}
scanlog_enabled = set()
app: Application | None = None

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

async def send_log(chat_id: int, msg: str):
    if app and chat_id in scanlog_enabled:
        try:
            await app.bot.send_message(chat_id, f"🩶 {msg}")
        except:
            pass

# ================== EXCHANGES ==================
async def init_exchanges():
    global exchanges, exchange_status
    exchanges, exchange_status = {}, {}

    async def try_init(name, ex_class, **kwargs):
        if not all(kwargs.values()):
            exchange_status[name] = {"status": "⚪", "error": "нет API-ключей", "ex": None}
            log(f"{name.upper()} ⚪ пропущен — нет API-ключей")
            return None
        try:
            ex = ex_class({
                **kwargs,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })
            await ex.load_markets()
            exchange_status[name] = {"status": "✅", "error": None, "ex": ex}
            log(f"{name.upper()} ✅ инициализирован")
            return ex
        except Exception as e:
            err = str(e).split("\n")[0][:120]
            exchange_status[name] = {"status": "❌", "error": err, "ex": None}
            log(f"{name.upper()} ❌ {err}")
            return None

    candidates = {
        "mexc": (ccxt.mexc, {
            "apiKey": env_vars["MEXC_API_KEY"],
            "secret": env_vars["MEXC_API_SECRET"]
        }),
        "bitget": (ccxt.bitget, {
            "apiKey": env_vars["BITGET_API_KEY"],
            "secret": env_vars["BITGET_API_SECRET"],
            "password": env_vars["BITGET_API_PASSPHRASE"]
        }),
    }

    for name, (cls, params) in candidates.items():
        ex = await try_init(name, cls, **params)
        if ex:
            exchanges[name] = ex

    active = [k for k, v in exchange_status.items() if v["status"] == "✅"]
    log(f"Активные биржи: {', '.join(active) if active else '—'}")
    log(f"Инициализация завершена: {len(active)}/{len(exchange_status)} активны.")

async def close_all_exchanges():
    for name, ex in exchanges.items():
        try:
            await ex.close()
            log(f"{name.upper()} закрыт ✅")
        except Exception as e:
            log(f"{name.upper()} ошибка закрытия: {e}")

# ================== PLACEHOLDER SCAN (оставлено без изменений) ==================
# (весь твой существующий код scan_all_pairs, handle_buy_callback, команды и т.д.)
# вставляется сюда без изменений — он не влияет на запуск Render/webhook
# ================================================================================

# ================== MAIN ==================
async def main():
    print("🚀 INIT START (Render + Telegram webhook)", flush=True)
    await init_exchanges()

    global app
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # === Команды ===
    # (оставлены без изменений, добавляются твои start, scan, status и т.д.)
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("✅ Бот активен.")))
    # Добавь остальные handlers отсюда ↓
    # app.add_handler(CommandHandler("scan", scan_cmd))
    # app.add_handler(CallbackQueryHandler(...))
    # и т.д.

    # === Планировщик ===
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: None, "interval", seconds=SCAN_INTERVAL)
    scheduler.start()

    # === Webhook URL ===
    PORT = int(os.getenv("PORT", "10000"))
    EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL", "")
    if not EXTERNAL_URL:
        raise SystemExit("❌ Нет RENDER_EXTERNAL_URL / WEBHOOK_URL (Render HTTPS URL)")

    WEBHOOK_PATH = f"/{TELEGRAM_BOT_TOKEN}"
    WEBHOOK_URL = f"{EXTERNAL_URL.rstrip('/')}{WEBHOOK_PATH}"
    WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "") or None

    print(f"🌐 Webhook URL: {WEBHOOK_URL}", flush=True)
    print(f"🔒 Secret set: {'yes' if WEBHOOK_SECRET else 'no'}", flush=True)

    log("===========================================================")
    log(f"✅ Arbitrage Scanner {VERSION} запущен на Render (webhook mode)")
    log(f"Порт: {PORT}")
    log(f"Фильтры: профит ≥ {MIN_SPREAD}% | объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/1ч")
    log(f"Автоскан каждые {SCAN_INTERVAL} сек (если включён)")
    log("===========================================================")

    # === Запуск Webhook ===
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    asyncio.run(main())
