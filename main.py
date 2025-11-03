# main.py — Arbitrage Scanner v5.5 (Webhook, Render)
import os
import asyncio
import ccxt.async_support as ccxt
from datetime import datetime
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== CONFIG ==================
MIN_SPREAD = 1.2
MIN_VOLUME_1H = 100_000
SCAN_INTERVAL = 120
VERSION = "v5.5"

# ================== ENV ==================
env_vars = {
    "MEXC_API_KEY": os.getenv("MEXC_API_KEY"),
    "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET"),
    "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
    "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
    "BITGET_API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE"),
    "KUCOIN_API_KEY": os.getenv("KUCOIN_API_KEY"),
    "KUCOIN_API_SECRET": os.getenv("KUCOIN_API_SECRET"),
    "KUCOIN_API_PASS": os.getenv("KUCOIN_API_PASS"),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
}
TELEGRAM_BOT_TOKEN = env_vars["TELEGRAM_BOT_TOKEN"]

# ================== GLOBALS ==================
exchanges = {}
pending_trades = {}
app = None
scanlog_enabled = set()  # чаты, где включён лог сканирования

# ================== TEXT ==================
INFO_TEXT = f"""*Arbitrage Scanner {VERSION}*

Бот сканирует *MEXC / BITGET / KUCOIN* по USDT-парам.  
Анализирует *топ-100 монет* по объёму и ищет арбитраж ≥ {MIN_SPREAD}% с объёмом ≥ {MIN_VOLUME_1H/1000:.0f}k$.

*Работа:*
— Автоскан каждые {SCAN_INTERVAL} сек (если включён)  
— Команды не блокируются  
— BUY без номинала — бот сам спросит сумму  
— Реальное время логов по /scanlog  

*Команды:*  
/start — запустить и подписаться на автоскан  
/scan — ручной запуск сканирования  
/balance — баланс по биржам  
/scanlog — включить или выключить лог сканирования  
/info — параметры и помощь  
/stop — остановить автоскан  
/ping — проверить соединение
"""

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

async def send_log(chat_id, msg):
    """Отправляет логи в чат, если включён /scanlog"""
    if app and chat_id in scanlog_enabled:
        try:
            await app.bot.send_message(chat_id, f"🩶 {msg}")
        except:
            pass

# ================== INIT ==================
async def init_exchanges():
    async def try_init(name, ex_class, **kwargs):
        try:
            ex = ex_class(kwargs)
            await ex.load_markets()
            log(f"{name.upper()} ✅ инициализирован")
            return ex
        except Exception as e:
            log(f"{name.upper()} ❌ {e}")
            return None

    global exchanges
    exchanges = {
        "mexc": await try_init("mexc", ccxt.mexc, apiKey=env_vars["MEXC_API_KEY"], secret=env_vars["MEXC_API_SECRET"]),
        "bitget": await try_init("bitget", ccxt.bitget, apiKey=env_vars["BITGET_API_KEY"],
                                 secret=env_vars["BITGET_API_SECRET"], password=env_vars["BITGET_API_PASSPHRASE"]),
        "kucoin": await try_init("kucoin", ccxt.kucoin, apiKey=env_vars["KUCOIN_API_KEY"],
                                 secret=env_vars["KUCOIN_API_SECRET"], password=env_vars["KUCOIN_API_PASS"]),
    }
    exchanges = {k: v for k, v in exchanges.items() if v}
    if not exchanges:
        raise RuntimeError("❌ Нет активных бирж.")
    log(f"Активные биржи: {', '.join(exchanges.keys())}")

# ================== SCANNER ==================
async def get_top_symbols(exchange, top_n=100):
    tickers = await exchange.fetch_tickers()
    pairs = [(s, t.get("quoteVolume", 0)) for s, t in tickers.items() if s.endswith("/USDT") and ":" not in s]
    pairs.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pairs[:top_n]]

async def scan_all_pairs(chat_id=None):
    results = []
    symbols = set()
    FEES = {"mexc": 0.001, "bitget": 0.001, "kucoin": 0.001}

    for name, ex in exchanges.items():
        try:
            tops = await get_top_symbols(ex)
            symbols.update(tops)
        except Exception as e:
            await send_log(chat_id, f"{name} ошибка топ-листа: {e}")

    await send_log(chat_id, f"Начал скан {len(symbols)} пар...")

    for i, symbol in enumerate(symbols):
        prices, vols = {}, {}
        for name, ex in exchanges.items():
            try:
                t = await ex.fetch_ticker(symbol)
                if t.get("bid") and t.get("ask"):
                    prices[name] = (t["bid"] + t["ask"]) / 2
                    vols[name] = t.get("quoteVolume", 0)
            except:
                continue

        if len(prices) < 2:
            continue

        min_p, max_p = min(prices.values()), max(prices.values())
        spread = (max_p - min_p) / min_p * 100
        if spread < MIN_SPREAD:
            continue
        min_vol = min(vols.values())
        if min_vol < MIN_VOLUME_1H:
            continue

        cheap, expensive = min(prices, key=prices.get), max(prices, key=prices.get)
        profit = (max_p / min_p - 1) * 100 - (FEES[cheap] + FEES[expensive]) * 100
        results.append({
            "symbol": symbol, "cheap": cheap, "expensive": expensive,
            "price_cheap": round(prices[cheap], 6), "price_expensive": round(prices[expensive], 6),
            "spread": round(profit, 2), "volume_1h": round(min_vol / 1_000_000, 2)
        })

        if chat_id in scanlog_enabled and i % 10 == 0:
            await send_log(chat_id, f"Скан {i}/{len(symbols)}...")

    results.sort(key=lambda x: x["spread"], reverse=True)
    await send_log(chat_id, f"Готово. Найдено {len(results)} сигналов.")
    return results[:10]

# ================== BUY LOGIC ==================
def get_buy_keyboard(sig):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"BUY_{sig['cheap'].upper()}", callback_data=f"buy:{sig['cheap']}:{sig['expensive']}:{sig['symbol']}")]
    ])

# ================== CALLBACK: BUY ==================
async def handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")
    if len(data) != 3:
        return

    _, exch_name, symbol = data
    ex = exchanges.get(exch_name)
    if not ex:
        await query.edit_message_text(f"❌ Биржа {exch_name.upper()} не инициализирована")
        return

    # Запоминаем, на что нажали
    chat_id = query.message.chat_id
    pending_trades[chat_id] = {"exchange": exch_name, "symbol": symbol}

    await query.edit_message_text(
        f"💰 Введите сумму сделки в USDT для {symbol} на {exch_name.upper()} (например: 25)",
    )
# ================== CALLBACK: ВВОД СУММЫ ==================
async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in pending_trades:
        return  # не ждём сумму

    text = update.message.text.strip()
    if not text.replace('.', '', 1).isdigit():
        await update.message.reply_text("❌ Введите число, например: 25")
        return

    usdt = float(text)
    trade = pending_trades[chat_id]
    exch_name = trade["exchange"]
    symbol = trade["symbol"]
    ex = exchanges.get(exch_name)

    try:
        ticker = await ex.fetch_ticker(symbol)
        price = ticker["ask"]
        amount = round(usdt / price, 6)
        spread = trade.get("spread", 0)

        est_profit_usdt = round(usdt * spread / 100, 2)
        text = (
            f"Купить {amount} {symbol.split('/')[0]} на {exch_name.upper()} за {usdt} USDT\n"
            f"💹 Примерный профит: *{spread}% (~{est_profit_usdt} USDT)*"
        )

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{exch_name}:{symbol}:{usdt}"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
            ]
        ])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при расчёте: {e}")

async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("buy_step")
    if not step:
        return
    try:
        usdt = float(update.message.text)
        step["usdt"] = usdt
        context.user_data["buy_step"] = step
        msg = (f"Покупка *{step['symbol']}*\nБиржа: {step['cheap'].upper()}\n"
               f"Сумма: {usdt} USDT\nПодтвердить сделку?")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{step['cheap']}:{step['symbol']}:{usdt}"),
             InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
        ])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    except:
        await update.message.reply_text("Неверный формат суммы. Введите число.")

async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, exch, symbol, usdt = q.data.split(":")
    usdt = float(usdt)
    ex = exchanges.get(exch)
    try:
        bal = await ex.fetch_balance()
        free = bal["USDT"]["free"]
        if free < usdt:
            return await q.edit_message_text(f"Недостаточно средств ({free:.2f} USDT).")
        t = await ex.fetch_ticker(symbol)
        amount = round(usdt / t["ask"], 6)
        order = await ex.create_market_buy_order(symbol, amount)
        await q.edit_message_text(f"✅ Куплено {amount} {symbol.split('/')[0]} на {exch.upper()} ({usdt} USDT)\nID: {order.get('id','—')}")
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка покупки: {e}")

async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("❌ Покупка отменена.")

# ================== COMMANDS ==================

# ================== SUMMARY ==================
START_SUMMARY = f"""
🧭 *Arbitrage Scanner {VERSION}*

Сканирует топ-100 монет на *MEXC / BITGET / KUCOIN*
Фильтры: профит ≥ {MIN_SPREAD}% и объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/h
Автоскан каждые {SCAN_INTERVAL} сек

⚙️ Команды:
/scan — ручной скан
/balance — показать USDT
/scanlog — лог (вкл/выкл)
/stop — остановить автоскан
/info — параметры и помощь

💡 Рекомендация:
Сначала включи /scanlog и /scan — для проверки.
Если всё ок — оставь автоскан активным.
"""

async def send_start_summary(chat_id):
    try:
        await app.bot.send_message(chat_id, START_SUMMARY, parse_mode="Markdown")
    except:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["chat_id"] = update.effective_chat.id
    context.chat_data["autoscan"] = True
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")
    await send_start_summary(update.effective_chat.id)


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Сканирую пары...")
    results = await scan_all_pairs(update.effective_chat.id)
    if not results:
        return await msg.edit_text("Нет сигналов.")
    await msg.delete()
    for sig in results:
        text = (f"*{sig['symbol']}*\nПрофит: *{sig['spread']}%*\n"
                f"Купить: {sig['cheap'].upper()} {sig['price_cheap']}\n"
                f"Продать: {sig['expensive'].upper()} {sig['price_expensive']}\n"
                f"Объём 1ч: {sig['volume_1h']}M$")
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_buy_keyboard(sig))

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["💰 Баланс:"]
    for n, ex in exchanges.items():
        try:
            b = await ex.fetch_balance()
            lines.append(f"{n.upper()}: {b['USDT']['free']:.2f} / {b['USDT']['total']:.2f}")
        except Exception as e:
            lines.append(f"{n.upper()}: {e}")
    await update.message.reply_text("\n".join(lines))

async def scanlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in scanlog_enabled:
        scanlog_enabled.remove(chat_id)
        await update.message.reply_text("🧱 Лог сканирования выключен.")
    else:
        scanlog_enabled.add(chat_id)
        await update.message.reply_text("📡 Лог сканирования включён (реальное время).")

# ================== AUTOSCAN ==================
async def auto_scan():
    for data in app.chat_data.values():
        if data.get("autoscan"):
            chat_id = data["chat_id"]
            res = await scan_all_pairs(chat_id)
            if not res:
                continue
            for sig in res:
                text = (f"*{sig['symbol']}*\nПрофит: *{sig['spread']}%*\n"
                        f"Купить: {sig['cheap'].upper()} {sig['price_cheap']}\n"
                        f"Продать: {sig['expensive'].upper()} {sig['price_expensive']}\n"
                        f"Объём 1ч: {sig['volume_1h']}M$")
                await app.bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=get_buy_keyboard(sig))

# ================== HEALTH ==================
async def healthcheck(_): return web.Response(text="OK")

async def start_health_server():
    port = int(os.environ.get("PORT", "8443")) + 1
    app = web.Application()
    app.add_routes([web.get("/", healthcheck)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log(f"[Init] Health server listening on port {port}")

# ================== MAIN ==================
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_health_server())
    loop.run_until_complete(init_exchanges())

    global app
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    handlers = [
        ("start", start), ("info", info), ("scan", scan_cmd),
        ("balance", balance_cmd), ("scanlog", scanlog_cmd)
    ]
    for cmd, func in handlers:
        app.add_handler(CommandHandler(cmd, func))

    app.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=r"^cancel$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input))

    scheduler = AsyncIOScheduler(event_loop=loop)
    scheduler.add_job(auto_scan, "interval", seconds=SCAN_INTERVAL)
    scheduler.start()

    port = int(os.environ.get("PORT", "8443"))
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    webhook_url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"
    loop.run_until_complete(app.bot.set_webhook(webhook_url, drop_pending_updates=True))

    log(f"Arbitrage Scanner {VERSION} запущен. Порт: {port}")
    app.run_webhook(listen="0.0.0.0", port=port, url_path=TELEGRAM_BOT_TOKEN, webhook_url=webhook_url)

if __name__ == "__main__":
    main()


