# main.py — Arbitrage Scanner v5.2 (Render.com FINAL) + кнопка BUY_EXCH (10 USDT)
import os
import time
import asyncio
import hashlib
import ccxt.async_support as ccxt
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =============== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ===============
required = [
    "BYBIT_API_KEY", "BYBIT_API_SECRET",
    "MEXC_API_KEY", "MEXC_API_SECRET",
    "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE",
    "TELEGRAM_BOT_TOKEN"
]
missing = [v for v in required if not os.getenv(v)]
if missing:
    print(f"ОШИБКА: Нет переменных: {', '.join(missing)}")
    exit(1)

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET")
BITGET_API_PASSPHRASE = os.getenv("BITGET_API_PASSPHRASE")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# =============== КОНФИГ ===============
MIN_SPREAD = 1.2
MIN_VOLUME_1H = 500_000
SCAN_INTERVAL = 120
SEND_DELAY = 1.0

# =============== ГЛОБАЛЬНЫЕ ===============
exchanges = {}
signal_cache = {}
sent_messages = set()
app = None

# =============== ИНИЦИАЛИЗАЦИЯ БИРЖ ===============
async def init_bybit():
    return ccxt.bybit({'apiKey': BYBIT_API_KEY,'secret': BYBIT_API_SECRET,'options': {'defaultType': 'spot'},'enableRateLimit': True})
async def init_mexc():
    return ccxt.mexc({'apiKey': MEXC_API_KEY,'secret': MEXC_API_SECRET,'options': {'defaultType': 'spot'},'enableRateLimit': True})
async def init_bitget():
    return ccxt.bitget({'apiKey': BITGET_API_KEY,'secret': BITGET_API_SECRET,'password': BITGET_API_PASSPHRASE,'options': {'defaultType': 'spot'},'enableRateLimit': True})
async def init_exchanges():
    global exchanges
    exchanges = {'bybit': await init_bybit(),'mexc': await init_mexc(),'bitget': await init_bitget()}

# =============== ЛОГИ ===============
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# =============== СКАНИРОВАНИЕ ===============
async def scan_all_pairs():
    symbols = set()
    for name, ex in exchanges.items():
        if not ex.markets:
            try:
                log(f"Загружаю рынки для {name.upper()}...")
                await ex.load_markets()
            except Exception as e:
                log(f"Ошибка load_markets {name}: {e}")
                continue
        symbols.update(ex.markets.keys())
    usdt_pairs = [s for s in symbols if s.endswith('/USDT') and ':' not in s]
    if not usdt_pairs:
        log("Нет USDT-пар")
        return []
    log(f"Сканирую {len(usdt_pairs)} пар...")
    results = []
    for symbol in usdt_pairs:
        prices = {}
        volumes = {}
        for name, ex in exchanges.items():
            try:
                ticker = await ex.fetch_ticker(symbol)
                bid = ticker.get('bid')
                ask = ticker.get('ask')
                if bid and ask:
                    prices[name] = (bid + ask) / 2
                    volumes[name] = ticker.get('quoteVolume', 0)
            except Exception:
                continue
        if len(prices) < 2:
            continue
        min_price = min(prices.values())
        max_price = max(prices.values())
        spread = (max_price - min_price) / min_price * 100
        if spread < MIN_SPREAD:
            continue
        min_vol = min(volumes.values())
        if min_vol < MIN_VOLUME_1H:
            continue
        cheap_ex = min(prices, key=prices.get)
        expensive_ex = max(prices, key=prices.get)
        FEE = {"bybit": 0.001, "bitget": 0.001, "mexc": 0.001}
        fee_buy = FEE.get(cheap_ex, 0.001)
        fee_sell = FEE.get(expensive_ex, 0.001)
        net_profit = (max_price / min_price - 1) * 100 - (fee_buy + fee_sell) * 100
        results.append({
            'symbol': symbol,'spread': round(net_profit, 2),'cheap': cheap_ex,'expensive': expensive_ex,
            'price_cheap': round(prices[cheap_ex], 6),'price_expensive': round(prices[expensive_ex], 6),
            'volume_1h': round(min_vol / 1_000_000, 2),'first_seen': time.time()
        })
    results.sort(key=lambda x: x['spread'], reverse=True)
    log(f"Найдено сигналов: {len(results)}")
    return results[:10]

# =============== КНОПКА ПОКУПКИ ===============
def get_buy_keyboard(sig):
    btn = InlineKeyboardButton(
        text=f"BUY_{sig['cheap'].upper()} (10 USDT)",
        callback_data=f"buy:{sig['cheap']}:{sig['symbol']}:10"
    )
    return InlineKeyboardMarkup([[btn]])

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
    try:
        balance = await ex.fetch_balance()
        free_usdt = balance['USDT']['free']
        if free_usdt < usdt:
            await query.edit_message_text(f"💰 Доступный баланс: {free_usdt:.2f} USDT (недостаточно)")
            return
        ticker = await ex.fetch_ticker(symbol)
        price = ticker['ask']
        amount = round(usdt / price, 6)
        order = await ex.create_market_buy_order(symbol, amount)
        await query.edit_message_text(
            f"✅ Куплено {amount} {symbol.split('/')[0]} на {exch_name.upper()} по {price} ({usdt} USDT)\nTxID: {order.get('id','—')}"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка покупки: {e}")

# =============== AUTO SCAN ===============
async def auto_scan():
    global signal_cache, app
    if not app:
        return
    chat_ids = [d.get("chat_id") for d in app.chat_data.values() if d.get("chat_id")]
    if not chat_ids:
        return
    log("Автоскан запущен...")
    signals = await scan_all_pairs()
    if not signals:
        log("Сигналов нет.")
        return
    now = time.time()
    new_cache = {}
    for sig in signals:
        key = sig["symbol"]
        if key in signal_cache:
            sig["first_seen"] = signal_cache[key]["first_seen"]
        new_cache[key] = sig
    signal_cache = new_cache
    text = "\n\n".join([f"#{i+1} {s['symbol']}\nПрофит: {s['spread']}%\nПокупка: {s['cheap'].upper()} {s['price_cheap']}\nПродажа: {s['expensive'].upper()} {s['price_expensive']}\nОбъем 1ч: {s['volume_1h']}M$" for i,s in enumerate(signals)])
    for chat_id in chat_ids:
        for sig in signals:
            try:
                await app.bot.send_message(chat_id=chat_id, text=f"{sig['symbol']}\nПрофит: {sig['spread']}%", reply_markup=get_buy_keyboard(sig))
            except Exception as e:
                log(f"Ошибка отправки: {e}")

# =============== КОМАНДЫ ===============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data['chat_id'] = update.effective_chat.id
    await update.message.reply_text("Бот запущен. /scan для проверки.")

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Сканирую...")
    signals = await scan_all_pairs()
    if not signals:
        await msg.edit_text("Нет сигналов.")
        return
    for sig in signals:
        try:
            await update.message.reply_text(f"{sig['symbol']}\nПрофит: {sig['spread']}%", reply_markup=get_buy_keyboard(sig))
        except Exception as e:
            log(f"Ошибка отправки сигнала: {e}")

# =============== ЗАПУСК ===============
async def main():
    global app
    await init_exchanges()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CallbackQueryHandler(handle_buy_callback, pattern="^buy:"))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_scan, 'interval', seconds=SCAN_INTERVAL)
    scheduler.start()
    log("Telegram-бот v5.2 запущен. Автоскан каждые 2 мин.")
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())
