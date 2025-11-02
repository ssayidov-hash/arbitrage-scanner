# main.py — Arbitrage Scanner v5.3 (Webhook, Render)
import os
import time
import asyncio
import ccxt.async_support as ccxt
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== ENV ==================
required = [
    "BYBIT_API_KEY", "BYBIT_API_SECRET",
    "MEXC_API_KEY", "MEXC_API_SECRET",
    "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE",
    "TELEGRAM_BOT_TOKEN",
]
missing = [v for v in required if not os.getenv(v)]
if missing:
    print(f"ОШИБКА: нет переменных: {', '.join(missing)}")
    raise SystemExit(1)

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET")
BITGET_API_PASSPHRASE = os.getenv("BITGET_API_PASSPHRASE")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ================== CONFIG ==================
MIN_SPREAD = 1.2
MIN_VOLUME_1H = 500_000
SCAN_INTERVAL = 120  # сек
VERSION = "v5.3"

# ================== GLOBALS ==================
exchanges = {}
app: Application | None = None

INFO_TEXT = f"""*Arbitrage Scanner {VERSION}*

Бот сканирует BYBIT / MEXC / BITGET по USDT-парам.
Фильтр: профит ≥ {MIN_SPREAD}% и объём ≥ {MIN_VOLUME_1H/1000:.0f}k$ за 1ч.
Авто-рассылка каждые {SCAN_INTERVAL} сек для чатов, где включено.

*Команды:*
/start — инфо и подписка на автоскан
/info — инфо
/scan — разовый скан
/balance — показать USDT на всех биржах
/ping — проверить, что жив
/stop — отключить автоскан для этого чата
"""

# ================== EXCH INIT ==================
async def init_bybit():
    return ccxt.bybit({
        "apiKey": BYBIT_API_KEY,
        "secret": BYBIT_API_SECRET,
        "options": {"defaultType": "spot"},
        "enableRateLimit": True,
    })

async def init_mexc():
    return ccxt.mexc({
        "apiKey": MEXC_API_KEY,
        "secret": MEXC_API_SECRET,
        "options": {"defaultType": "spot"},
        "enableRateLimit": True,
    })

async def init_bitget():
    return ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSPHRASE,
        "options": {"defaultType": "spot"},
        "enableRateLimit": True,
    })

async def init_exchanges():
    global exchanges
    exchanges = {
        "bybit": await init_bybit(),
        "mexc": await init_mexc(),
        "bitget": await init_bitget(),
    }

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def get_buy_keyboard(sig: dict):
    btn = InlineKeyboardButton(
        text=f"BUY_{sig['cheap'].upper()} (10 USDT)",
        callback_data=f"buy:{sig['cheap']}:{sig['symbol']}:10",
    )
    return InlineKeyboardMarkup([[btn]])

# ================== SCANNER ==================
async def scan_all_pairs():
    """
    Возвращает топ сигналов:
    [{symbol, spread, cheap, expensive, price_cheap, price_expensive, volume_1h}]
    """
    symbols = set()
    for name, ex in exchanges.items():
        if not ex.markets:
            try:
                log(f"load_markets {name} ...")
                await ex.load_markets()
            except Exception as e:
                log(f"load_markets {name} ошибка: {e}")
                continue
        symbols.update(ex.markets.keys())

    usdt_pairs = [s for s in symbols if s.endswith("/USDT") and ":" not in s]
    if not usdt_pairs:
        log("USDT-пар не найдено")
        return []

    log(f"Сканирую {len(usdt_pairs)} пар...")
    results = []
    FEES = {"bybit": 0.001, "bitget": 0.001, "mexc": 0.001}

    for symbol in usdt_pairs:
        prices = {}
        volumes = {}
        for name, ex in exchanges.items():
            try:
                ticker = await ex.fetch_ticker(symbol)
                bid = ticker.get("bid")
                ask = ticker.get("ask")
                if bid and ask:
                    prices[name] = (bid + ask) / 2
                    volumes[name] = ticker.get("quoteVolume", 0) or 0
            except Exception:
                continue

        if len(prices) < 2:
            continue

        min_price = min(prices.values())
        max_price = max(prices.values())
        raw_spread = (max_price - min_price) / min_price * 100
        if raw_spread < MIN_SPREAD:
            continue

        min_vol = min(volumes.values())
        if min_vol < MIN_VOLUME_1H:
            continue

        cheap_ex = min(prices, key=prices.get)
        expensive_ex = max(prices, key=prices.get)
        fee_buy = FEES.get(cheap_ex, 0.001)
        fee_sell = FEES.get(expensive_ex, 0.001)
        net_profit = (max_price / min_price - 1) * 100 - (fee_buy + fee_sell) * 100

        results.append({
            "symbol": symbol,
            "spread": round(net_profit, 2),
            "cheap": cheap_ex,
            "expensive": expensive_ex,
            "price_cheap": round(prices[cheap_ex], 6),
            "price_expensive": round(prices[expensive_ex], 6),
            "volume_1h": round(min_vol / 1_000_000, 2),
        })

    results.sort(key=lambda x: x["spread"], reverse=True)
    log(f"Найдено сигналов: {len(results)}")
    return results[:10]

# ================== CALLBACKS (BUY) ==================
async def handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")
    if len(data) != 4:
        return
    _, exch_name, symbol, usdt = data
    usdt = float(usdt)

    ex = exchanges.get(exch_name)
    if not ex:
        await query.edit_message_text(f"❌ Биржа {exch_name.upper()} не инициализирована")
        return

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{exch_name}:{symbol}:{usdt}"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
        ]
    ])
    await query.edit_message_text(
        f"Подтвердить покупку {symbol} на {exch_name.upper()} на сумму {usdt} USDT?",
        reply_markup=kb,
    )

async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")
    if len(data) != 4:
        await query.edit_message_text("Ошибка данных подтверждения.")
        return

    _, exch_name, symbol, usdt = data
    usdt = float(usdt)
    ex = exchanges.get(exch_name)

    try:
        balance = await ex.fetch_balance()
        free_usdt = balance["USDT"]["free"]
        if free_usdt < usdt:
            await query.edit_message_text(f"💰 Доступно: {free_usdt:.2f} USDT — не хватает.")
            return

        ticker = await ex.fetch_ticker(symbol)
        price = ticker["ask"]
        amount = round(usdt / price, 6)

        order = await ex.create_market_buy_order(symbol, amount)
        await query.edit_message_text(
            f"✅ Куплено {amount} {symbol.split('/')[0]} на {exch_name.upper()} по {price} ({usdt} USDT)\n"
            f"ID: {order.get('id', '—')}"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка покупки: {e}")

async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Отменено")
    await query.edit_message_text("❌ Покупка отменена.")

# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cd = context.chat_data
    cd["chat_id"] = update.effective_chat.id
    cd["autoscan"] = True
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["autoscan"] = False
    await update.message.reply_text("Автоскан ❌ отключён для этого чата.")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Сканирую...")
    signals = await scan_all_pairs()
    if not signals:
        await msg.edit_text("Нет сигналов.")
        return
    await msg.delete()
    for sig in signals:
        text = (
            f"{sig['symbol']}\n"
            f"Профит: *{sig['spread']}%*\n"
            f"Дешевле: {sig['cheap'].upper()} {sig['price_cheap']}\n"
            f"Дороже: {sig['expensive'].upper()} {sig['price_expensive']}\n"
            f"Объём 1ч: {sig['volume_1h']}M$"
        )
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_buy_keyboard(sig))

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    for name, ex in exchanges.items():
        try:
            bal = await ex.fetch_balance()
            usdt_free = bal["USDT"]["free"]
            usdt_total = bal["USDT"]["total"]
            lines.append(f"{name.upper()}: {usdt_free:.2f} / {usdt_total:.2f} USDT")
        except Exception as e:
            lines.append(f"{name.upper()}: ошибка {e}")
    await update.message.reply_text("\n".join(lines))

# ================== AUTOSCAN ==================
async def auto_scan():
    global app
    if not app:
        return

    # чаты, где включен автоскан
    target_chats = []
    for chat_id, data in app.chat_data.items():
        if data.get("chat_id") and data.get("autoscan", False):
            target_chats.append(data["chat_id"])

    if not target_chats:
        return

    log("Автоскан ...")
    signals = await scan_all_pairs()
    if not signals:
        log("Сигналов нет")
        return

    for chat_id in target_chats:
        for sig in signals:
            text = (
                f"{sig['symbol']}\n"
                f"Профит: *{sig['spread']}%*\n"
                f"Дешевле: {sig['cheap'].upper()} {sig['price_cheap']}\n"
                f"Дороже: {sig['expensive'].upper()} {sig['price_expensive']}\n"
                f"Объём 1ч: {sig['volume_1h']}M$"
            )
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=get_buy_keyboard(sig),
                )
            except Exception as e:
                log(f"Ошибка отправки в {chat_id}: {e}")

# ================== MAIN ==================
async def main():
    global app
    await init_exchanges()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # кнопки
    app.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=r"^cancel$"))

  # ================== MAIN ==================
async def main():
    global app
    await init_exchanges()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # кнопки
    app.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=r"^cancel$"))

    # планировщик
    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_scan, "interval", seconds=SCAN_INTERVAL)
    scheduler.start()

# --- Render port stub: Health server для Render ---
from aiohttp import web

async def healthcheck(request):
    return web.Response(text="OK")

# --- Render port stub: Health server для Render ---
from aiohttp import web

async def healthcheck(request):
    return web.Response(text="OK")

async def start_health_server():
    """Мини-сервер для Render (порт PORT+1, чтобы не конфликтовал с Telegram)"""
    port = int(os.environ.get("PORT", "8443")) + 1
    app = web.Application()
    app.add_routes([web.get("/", healthcheck)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[Init] Health server listening on port {port}", flush=True)


# ================== MAIN ==================
def main():
    # Создаём event loop и делаем его текущим
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # --- старт health-сервера, чтобы Render видел порт ---
    loop.run_until_complete(start_health_server())

    # --- инициализация бирж ---
    loop.run_until_complete(init_exchanges())

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # кнопки
    app.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=r"^cancel$"))

    # планировщик теперь знает о нашем loop
    scheduler = AsyncIOScheduler(event_loop=loop)
    scheduler.add_job(auto_scan, "interval", seconds=SCAN_INTERVAL)
    scheduler.start()

    # --- Webhook ---
    port = int(os.environ.get("PORT", "8443"))
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not host:
        raise RuntimeError("Нет RENDER_EXTERNAL_HOSTNAME — переведи сервис в Web Service")

    webhook_url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"
    log(f"Ставлю webhook: {webhook_url}")
    loop.run_until_complete(app.bot.set_webhook(webhook_url, drop_pending_updates=True))

    log(f"Arbitrage Scanner {VERSION} запущен (webhook). Порт: {port}")

    # блокирующий вызов — запускает сервер Telegram
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=webhook_url,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()






