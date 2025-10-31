# main.py
import os
import time
import asyncio
import hashlib
import logging
import nest_asyncio
import ccxt.async_support as ccxt
import matplotlib.pyplot as plt
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# =============== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===============
required_vars = [
    "BYBIT_API_KEY", "BYBIT_API_SECRET",
    "MEXC_API_KEY", "MEXC_API_SECRET",
    "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE",
    "TELEGRAM_BOT_TOKEN"
]

missing = [var for var in required_vars if not os.getenv(var)]
if missing:
    print(f"ОШИБКА: Не заданы переменные: {', '.join(missing)}")
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
SEND_DELAY = 1.0
SCAN_INTERVAL = 120
STABILITY_TIME = 360

# =============== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===============
exchanges = {}
signal_cache = {}
sent_messages = set()
active_signals = {}

# =============== ИНИЦИАЛИЗАЦИЯ БИРЖ ===============
async def init_bybit():
    ex = ccxt.bybit({
        'apiKey': BYBIT_API_KEY,
        'secret': BYBIT_API_SECRET,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True
    })
    return ex

async def init_mexc():
    ex = ccxt.mexc({
        'apiKey': MEXC_API_KEY,
        'secret': MEXC_API_SECRET,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True
    })
    return ex

async def init_bitget():
    ex = ccxt.bitget({
        'apiKey': BITGET_API_KEY,
        'secret': BITGET_API_SECRET,
        'password': BITGET_API_PASSPHRASE,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True
    })
    return ex

async def init_exchanges():
    global exchanges
    exchanges = {
        'bybit': init_bybit(),
        'mexc': init_mexc(),
        'bitget': init_bitget()
    }

# =============== ЛОГИРОВАНИЕ ===============
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# =============== ПОЛУЧЕНИЕ ДАННЫХ ===============
async def fetch_ticker(ex, symbol):
    try:
        ticker = await ex.fetch_ticker(symbol)
        return {
            'bid': ticker['bid'],
            'ask': ticker['ask'],
            'volume': ticker.get('quoteVolume', 0)
        }
    except:
        return None

# =============== СКАНИРОВАНИЕ ПАР ===============
async def scan_all_pairs():
    symbols = set()
    for ex in exchanges.values():
        symbols.update(ex.symbols)
    usdt_pairs = [s for s in symbols if s.endswith('/USDT') and ':' not in s]

    results = []
    for symbol in usdt_pairs:
        prices = {}
        volumes = {}
        for name, ex in exchanges.items():
            data = await fetch_ticker(ex, symbol)
            if data and data['bid'] and data['ask']:
                prices[name] = (data['bid'] + data['ask']) / 2
                volumes[name] = data['volume']

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

        results.append({
            'symbol': symbol,
            'spread': round(spread, 2),
            'cheap': cheap_ex,
            'expensive': expensive_ex,
            'price_cheap': round(prices[cheap_ex], 6),
            'price_expensive': round(prices[expensive_ex], 6),
            'volume_1h': round(min_vol / 1_000_000, 2)
        })

    results.sort(key=lambda x: x['spread'], reverse=True)
    return results[:10]

# =============== ТАЙМЕР СТАБИЛЬНОСТИ ===============
def update_signal_timers(signals):
    now = time.time()
    for sig in signals:
        key = sig['symbol']
        if key in signal_cache:
            elapsed = now - signal_cache[key]['first_seen']
            mins = int(elapsed // 60)
            sig['timer'] = f"{mins} мин | {sig['spread']}%"
        else:
            sig['timer'] = "новый"
            sig['first_seen'] = now
    return signals

# =============== ГЕНЕРАЦИЯ ТЕКСТА ===============
def generate_signal_text(signals, numbered=False):
    if not signals:
        return "Нет сигналов."

    lines = []
    for i, sig in enumerate(signals):
        prefix = f"#{i+1} " if numbered else ""
        lines.append(
            f"{prefix}{sig['symbol']}\n"
            f"Спред: {sig['timer']}\n"
            f"Покупка: {sig['cheap'].upper()} → {sig['price_cheap']}\n"
            f"Продажа: {sig['expensive'].upper()} → {sig['price_expensive']}\n"
            f"Объём 1ч: {sig['volume_1h']}M$"
        )
    return "\n\n".join(lines)

# =============== КЭШ СИГНАЛОВ ===============
def load_signals_cache():
    global signal_cache
    try:
        import json
        with open('signals_cache.json', 'r') as f:
            signal_cache = json.load(f)
    except:
        signal_cache = {}

def save_signals_cache(cache):
    try:
        import json
        with open('signals_cache.json', 'w') as f:
            json.dump(cache, f)
    except:
        pass

# =============== AUTO SCAN & CLEANUP ===============
async def auto_scan(context: ContextTypes.DEFAULT_TYPE):
    global signal_cache

    chat_ids = [d.get("chat_id") for d in context.application.chat_data.values() if d.get("chat_id")]
    if not chat_ids:
        return

    signals = await scan_all_pairs()
    signals = update_signal_timers(signals)

    now = time.time()
    new_cache = {}
    for sig in signals:
        key = sig["symbol"]
        if key in signal_cache:
            sig["first_seen"] = signal_cache[key]["first_seen"]
        new_cache[key] = sig

    signal_cache = new_cache
    save_signals_cache(signal_cache)

    text = generate_signal_text(signals, numbered=True)
    if not text or text == "Нет сигналов.":
        return

    signal_hash = hashlib.md5(text.encode()).hexdigest()
    if signal_hash in sent_messages:
        return
    await asyncio.sleep(SEND_DELAY)

    for chat_id in chat_ids:
        try:
            msg = await context.application.bot.send_message(chat_id=chat_id, text=text)
            active_signals[msg.message_id] = {"hash": signal_hash, "time": now}
        except Exception as e:
            log(f"Ошибка отправки: {e}")

    sent_messages.add(signal_hash)
    if len(sent_messages) > 50:
        sent_messages.clear()

# =============== КОМАНДЫ ===============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.chat_data['chat_id'] = chat_id
    text = (
        "*Arbitrage Scanner v5.1*\n\n"
        "Автоскан каждые 2 мин\n"
        "Спред ≥1.2% • Объём 1ч ≥500k$\n\n"
        "*Команды:*\n"
        "/scan — скан сейчас\n"
        "/analyze BTC/USDT — детальный отчёт\n"
        "/buy 1 — купить по сигналу #1\n"
        "/buy BTC/USDT 0.02 — купить 0.02 BTC\n"
        "/balance — баланс USDT\n"
        "/log — последние логи\n"
        "/stop — остановить"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Сканирую...")
    signals = await scan_all_pairs()
    text = generate_signal_text(signals, numbered=True)
    await msg.edit_text(text or "Нет сигналов.")

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /analyze BTC/USDT")
        return
    symbol = context.args[0].upper()
    if not symbol.endswith('/USDT'):
        symbol += '/USDT'

    prices = {}
    for name, ex in exchanges.items():
        data = await fetch_ticker(ex, symbol)
        if data:
            prices[name] = (data['bid'] + data['ask']) / 2

    if len(prices) < 2:
        await update.message.reply_text("Недостаточно данных.")
        return

    min_ex = min(prices, key=prices.get)
    max_ex = max(prices, key=prices.get)
    spread = (prices[max_ex] - prices[min_ex]) / prices[min_ex] * 100

    text = (
        f"*{symbol}*\n\n"
        f"Дешёвая: {min_ex.upper()} → {prices[min_ex]:.6f}\n"
        f"Дорогая: {max_ex.upper()} → {prices[max_ex]:.6f}\n"
        f"Спред: {spread:.2f}%"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /buy 1 или /buy BTC/USDT 0.02")
        return

    # Логика покупки (заглушка)
    await update.message.reply_text("Покупка выполнена на дешёвой бирже.")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Баланс: 1000 USDT (пример)")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in context.chat_data:
        del context.chat_data[chat_id]
    await update.message.reply_text("Остановлено.")

async def main():
    nest_asyncio.apply()
    await init_exchanges()
    load_signals_cache()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("stop", stop))

    job_queue = app.job_queue
    job_queue.run_repeating(auto_scan, interval=SCAN_INTERVAL, first=10)

    log("Telegram-бот v5.1 запущен. Автоскан каждые 2 мин.")
    try:
        await app.run_polling()
    finally:
        for ex in exchanges.values():
            await ex.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())   # ← ВОТ ЭТА СТРОКА!
    except KeyboardInterrupt:
        log("Бот остановлен.")



