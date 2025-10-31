# main.py — Arbitrage Scanner v5.1 (Render.com FINAL + APScheduler)
import os
import time
import asyncio
import hashlib
import ccxt.async_support as ccxt
from datetime import datetime
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
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
app = None  # Глобальное приложение

# =============== ИНИЦИАЛИЗАЦИЯ БИРЖ ===============
async def init_bybit():
    return ccxt.bybit({
        'apiKey': BYBIT_API_KEY,
        'secret': BYBIT_API_SECRET,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True
    })

async def init_mexc():
    return ccxt.mexc({
        'apiKey': MEXC_API_KEY,
        'secret': MEXC_API_SECRET,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True
    })

async def init_bitget():
    return ccxt.bitget({
        'apiKey': BITGET_API_KEY,
        'secret': BITGET_API_SECRET,
        'password': BITGET_API_PASSPHRASE,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True
    })

async def init_exchanges():
    global exchanges
    exchanges = {
        'bybit': await init_bybit(),
        'mexc': await init_mexc(),
        'bitget': await init_bitget()
    }

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

        results.append({
            'symbol': symbol,
            'spread': round(spread, 2),
            'cheap': cheap_ex,
            'expensive': expensive_ex,
            'price_cheap': round(prices[cheap_ex], 6),
            'price_expensive': round(prices[expensive_ex], 6),
            'volume_1h': round(min_vol / 1_000_000, 2),
            'first_seen': time.time()
        })

    results.sort(key=lambda x: x['spread'], reverse=True)
    log(f"Найдено сигналов: {len(results)}")
    return results[:10]

# =============== ТАЙМЕР ===============
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
    return signals

# =============== ТЕКСТ ===============
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

# =============== КЭШ ===============
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
    signal_hash = hashlib.md5(text.encode()).hexdigest()
    if signal_hash in sent_messages:
        return
    await asyncio.sleep(SEND_DELAY)

    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
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
        "/start — главное меню\n"
        "/ping — пинг бота\n"
        "/scan — скан сейчас\n"
        "/analyze BTC/USDT — детальный отчёт\n"
        "/buy 1 — купить по сигналу #1\n"
        "/balance — баланс USDT\n"
        "/stop — остановить автоскан"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    msg = await update.message.reply_text("Пингую...")
    end_time = time.time()
    ping_ms = round((end_time - start_time) * 1000, 2)
    await msg.edit_text(f"Понг! {ping_ms} мс")

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
        try:
            if not ex.markets:
                await ex.load_markets()
            ticker = await ex.fetch_ticker(symbol)
            bid = ticker.get('bid')
            ask = ticker.get('ask')
            if bid and ask:
                prices[name] = (bid + ask) / 2
        except:
            continue

    if len(prices) < 2:
        await update.message.reply_text("Недостаточно данных.")
        return

    min_ex = min(prices, key=prices.get)
    max_ex = max(prices, key=prices.get)
    spread = (prices[max_ex] - prices[min_ex]) / prices[min_ex] * 100

    text = f"*{symbol}*\n\nДешёвая: {min_ex.upper()} → {prices[min_ex]:.6f}\nДорогая: {max_ex.upper()} → {prices[max_ex]:.6f}\nСпред: {spread:.2f}%"
    await update.message.reply_text(text, parse_mode='Markdown')

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Покупка выполнена (заглушка).")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Баланс: 1000 USDT (пример)")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in context.chat_data:
        del context.chat_data[chat_id]
    await update.message.reply_text("Автоскан остановлен.")

# =============== ЗАПУСК ===============
async def main():
    global app
    await init_exchanges()
    load_signals_cache()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("help", start))  # по желанию

    # === APScheduler ===
    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_scan, 'interval', seconds=SCAN_INTERVAL)
    scheduler.start()

    # === ОТПРАВКА УВЕДОМЛЕНИЯ ПРИ ЗАПУСКЕ ===
    ADMIN_CHAT_ID = 986793552  # замени на свой Telegram ID
    try:
        await app.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="🤖 Бот перезапущен и работает на Render ✅"
        )
    except Exception as e:
        log(f"Не удалось отправить сообщение админу: {e}")

    log("Telegram-бот v5.1 запущен. Автоскан каждые 2 мин.")
    await app.run_polling()


# === ЗАПУСК ===
if __name__ == "__main__":
    import nest_asyncio
    import asyncio

    nest_asyncio.apply()  # разрешает вложенные event loop (Render)
    asyncio.get_event_loop().run_until_complete(main())







