# -*- coding: utf-8 -*-
"""
UNIVERSAL ARBITRAGE TOOL — Telegram Edition v5.1
Bybit, MEXC, Bitget | USDT-пары | Объём 1ч ≥500k | Спред ≥1.2%
🔵 >1.5% | Стабильность ≥3 мин | Прибыль, таймер, /buy (только покупка)
"""

import os, io, time, asyncio, json, hashlib
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
from collections import defaultdict, deque
from typing import List, Dict, Tuple, Optional
import ccxt
import matplotlib.pyplot as plt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

getcontext().prec = 18

# =============== CONFIG ===============
MODE = "telegram"
TOP_N_TELEGRAM = 5
LOG_PATH = "spreads_log.txt"
SIGNALS_DB = "signals_cache.json"

MIN_VOLUME_USD_1H = 500_000
MIN_SPREAD_PCT = 1.2
QUOTE_SYMBOL = "USDT"

PRICE_STABILITY_WINDOW = 180     # 3 мин
PRICE_STABILITY_THRESHOLD = 0.3  # ±0.3 %
ORDERBOOK_DEPTH_AMOUNT = 10_000  # $10k
SPREAD_STABILITY_LIMIT = 0.5     # <0.5 %
AUTO_SCAN_INTERVAL = 120         # каждые 2 мин
SIGNAL_TTL = 600                 # 10 мин
SEND_DELAY = 30                  # задержка перед отправкой
BUY_DEFAULT_AMOUNT_BTC = Decimal('0.01')

# Комиссии (maker/taker)
FEES_TRADE_PCT = {
    'Bybit': Decimal('0.0010'),
    'MEXC': Decimal('0.0010'),
    'Bitget': Decimal('0.0010')
}

# =============== KEYS ===============
try:
    from keys import (
        BYBIT_API_KEY, BYBIT_API_SECRET,
        MEXC_API_KEY, MEXC_API_SECRET,
        BITGET_API_KEY, BITGET_API_SECRET,
        TELEGRAM_BOT_TOKEN,
    )
except Exception:
    print("keys.py не найден или содержит ошибки.")
    raise SystemExit

# =============== INIT & UTILS ===============
open(LOG_PATH, "a", encoding="utf-8").close()

def log(msg: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def d(x) -> Decimal:
    return Decimal(str(x)) if x is not None else Decimal('0')

def save_signals_cache(cache: dict):
    try:
        with open(SIGNALS_DB, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Ошибка сохранения кэша: {e}")

def load_signals_cache() -> dict:
    if not os.path.exists(SIGNALS_DB):
        return {}
    try:
        with open(SIGNALS_DB, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# =============== EXCHANGES ===============
def init_bybit():
    ex = ccxt.bybit({
        'apiKey': BYBIT_API_KEY,
        'secret': BYBIT_API_SECRET,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True
    })
    ex.load_markets()
    return ex

def init_mexc():
    ex = ccxt.mexc({
        'apiKey': MEXC_API_KEY,
        'secret': MEXC_API_SECRET,
        'enableRateLimit': True
    })
    ex.load_markets()
    return ex

def init_bitget():
    ex = ccxt.bitget({
        'apiKey': BITGET_API_KEY,
        'secret': BITGET_API_SECRET,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True
    })
    ex.load_markets()
    return ex

bybit = init_bybit()
mexc = init_mexc()
bitget = init_bitget()

EXCHANGES = [
    ("Bybit", bybit),
    ("MEXC", mexc),
    ("Bitget", bitget)
]

# Кэши
price_cache = defaultdict(lambda: deque(maxlen=10))
spread_history = defaultdict(lambda: deque(maxlen=4))
signal_cache = load_signals_cache()
active_signals = {}  # message_id -> signal_data
sent_messages = set()

# =============== CORE FUNCTIONS ===============
def market_active(exchange, symbol) -> bool:
    m = exchange.markets.get(symbol)
    return bool(m and m.get("active", True))

def fetch_1h_volume(exchange, symbol) -> float:
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=1)
        return ohlcv[0][5] if ohllcv else 0
    except:
        return 0

def fetch_orderbook_depth(exchange, symbol, side: str, amount_usd: float):
    try:
        ob = exchange.fetch_order_book(symbol, limit=20)
        orders = ob['bids'] if side == 'buy' else ob['asks']
        filled = 0
        total_cost = 0
        for price, vol in orders:
            cost = price * vol
            if filled + cost >= amount_usd:
                needed = amount_usd - filled
                total_cost += needed
                filled += needed
                break
            else:
                total_cost += cost
                filled += cost
        slippage_pct = ((total_cost / amount_usd) - 1) * 100 if filled > 0 else 0
        return abs(slippage_pct)
    except:
        return 2.0

def is_price_stable(name, symbol, price_now):
    now = time.time()
    q = price_cache[(name, symbol)]
    q.append((now, float(price_now)))
    recent = [(t, p) for t, p in q if now - t <= PRICE_STABILITY_WINDOW]
    if len(recent) < 2:
        return True, 0
    first_price = recent[0][1]
    delta = abs((price_now - first_price) / first_price * 100)
    return delta < PRICE_STABILITY_THRESHOLD, delta

def is_spread_persistent(pair_key, spread_now):
    history = spread_history[pair_key]
    history.append(float(spread_now))
    if len(history) < 4:
        return False
    return sum(1 for s in history if s >= MIN_SPREAD_PCT) >= 3

def estimate_profit(spread_pct: float, amount_btc: Decimal, slippage: float = 0.1):
    gross = amount_btc * d(spread_pct / 100)
    total_fees = sum(FEES_TRADE_PCT.values()) * 2 * amount_btc  # buy + sell
    net = gross - total_fees - (amount_btc * d(slippage / 100))
    return net

async def scan_all_pairs():
    results = []
    now = time.time()
    for i, (name_a, ex_a) in enumerate(EXCHANGES):
        for j, (name_b, ex_b) in enumerate(EXCHANGES[i+1:], i+1):
            for sym in gather_common_pairs(ex_a, ex_b):
                try:
                    ta = ex_a.fetch_ticker(sym)
                    tb = ex_b.fetch_ticker(sym)
                    if not ta.get('last') or not tb.get('last'):
                        continue

                    vol_a = fetch_1h_volume(ex_a, sym)
                    vol_b = fetch_1h_volume(ex_b, sym)
                    avg_vol = (vol_a + vol_b) / 2
                    if avg_vol < MIN_VOLUME_USD_1H:
                        continue

                    pa, pb = d(ta["last"]), d(tb["last"])
                    if pa <= 0 or pb <= 0:
                        continue

                    spread = (pb - pa) / pa * 100
                    if spread < MIN_SPREAD_PCT:
                        continue

                    pair_key = f"{name_a}-{name_b}-{sym}"
                    if not is_spread_persistent(pair_key, spread):
                        continue

                    stable_a, _ = is_price_stable(name_a, sym, pa)
                    stable_b, _ = is_price_stable(name_b, sym, pb)
                    if not (stable_a and stable_b):
                        continue

                    depth_slippage = max(
                        fetch_orderbook_depth(ex_a, sym, 'buy', ORDERBOOK_DEPTH_AMOUNT),
                        fetch_orderbook_depth(ex_b, sym, 'sell', ORDERBOOK_DEPTH_AMOUNT)
                    )

                    profit = estimate_profit(spread, BUY_DEFAULT_AMOUNT_BTC, depth_slippage)

                    signal = {
                        "symbol": sym,
                        "cheap_ex": name_a,
                        "expensive_ex": name_b,
                        "price_cheap": float(pa),
                        "price_exp": float(pb),
                        "spread_pct": round(spread, 2),
                        "volume_1h": round(avg_vol / 1_000_000, 1),
                        "slippage": round(depth_slippage, 2),
                        "profit_est": round(float(profit), 2),
                        "first_seen": now,
                        "last_seen": now,
                        "stability_min": 0
                    }
                    results.append(signal)
                except Exception as e:
                    log(f"Scan error {sym}: {e}")
    results.sort(key=lambda x: x["spread_pct"], reverse=True)
    return results[:TOP_N_TELEGRAM]

def gather_common_pairs(ex_a, ex_b, quote=QUOTE_SYMBOL, max_symbols=100):
    a_pairs = [s for s in ex_a.markets.keys() if s.endswith(f"/{quote}") and market_active(ex_a, s)]
    return [s for s in a_pairs if s in ex_b.markets and market_active(ex_b, s)][:max_symbols]

def update_signal_timers(signals):
    now = time.time()
    for sig in signals:
        if sig["symbol"] in signal_cache:
            first = signal_cache[sig["symbol"]]["first_seen"]
            sig["stability_min"] = int((now - first) // 60)
        else:
            sig["stability_min"] = 0
    return signals

def generate_signal_text(signals, numbered=True):
    buf = io.StringIO()
    for idx, sig in enumerate(signals, 1):
        tag = "High" if sig["spread_pct"] > 1.5 else "Medium"
        timer = f"{sig['stability_min']} мин" if sig['stability_min'] > 0 else "новый"
        num = f"#{idx} " if numbered else ""
        buf.write(
            f"{num}{tag} {sig['symbol']} ({sig['cheap_ex']} to {sig['expensive_ex']})\n"
            f"   Спред: {sig['spread_pct']}% | {timer} | vol {sig['volume_1h']}M$\n"
            f"   {sig['cheap_ex']}: ${sig['price_cheap']:,.2f} to {sig['expensive_ex']}: ${sig['price_exp']:,.2f}\n"
            f"   Slippage: {sig['slippage']}%, Прибыль на 0.01 BTC: +${sig['profit_est']}\n\n"
        )
    return buf.getvalue().strip() or "Нет сигналов."

# =============== TRADING ===============
async def execute_buy(exchange_obj, symbol, amount_btc: Decimal):
    try:
        balance = exchange_obj.fetch_balance()
        usdt_free = d(balance.get('USDT', {}).get('free', 0))
        price = d(exchange_obj.fetch_ticker(symbol)['last'])
        cost = amount_btc * price
        if cost > usdt_free:
            return False, f"Недостаточно USDT: нужно {cost:.2f}, есть {usdt_free:.2f}"
        order = exchange_obj.create_market_buy_order(symbol, float(amount_btc))
        return True, f"Куплено {amount_btc} {symbol.split('/')[0]} за {cost:.2f} USDT"
    except Exception as e:
        return False, f"Ошибка покупки: {e}"

# =============== TELEGRAM ===============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Arbitrage Scanner v5.1\n\n"
        "Автоскан каждые 2 мин\n"
        "Пары: /USDT | Объём 1ч ≥500k\n"
        "Спред ≥1.2% | High >1.5%\n"
        "Только покупка на дешёвой бирже\n\n"
        "Команды:\n"
        "/scan — скан сейчас\n"
        "/analyze BTC/USDT — детальный анализ\n"
        "/buy 1 — купить по сигналу #1\n"
        "/buy BTC/USDT 0.02 — купить 0.02 BTC\n"
        "/balance — баланс USDT\n"
        "/log — последние логи\n"
        "/stop — выход"
    )
    await update.message.reply_text(msg)
    context.chat_data["chat_id"] = update.effective_chat.id
    log(f"/start by {update.effective_user.id}")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сканирую...")
    signals = await scan_all_pairs()
    signals = update_signal_timers(signals)
    text = generate_signal_text(signals)
    await update.message.reply_text(text or "Нет сигналов.")
    log(f"/scan by {update.effective_user.id}")

async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /analyze BTC/USDT")
        return
    symbol = context.args[0].upper()
    if not symbol.endswith("/USDT"):
        symbol += "/USDT"
    # Поиск по всем биржам
    prices = {}
    for name, ex in EXCHANGES:
        try:
            ticker = ex.fetch_ticker(symbol)
            if ticker.get('last'):
                prices[name] = d(ticker['last'])
        except:
            pass
    if len(prices) < 2:
        await update.message.reply_text(f"Пара {symbol} не найдена или неактивна.")
        return

    cheap_ex = min(prices, key=prices.get)
    exp_ex = max(prices, key=prices.get)
    spread = (prices[exp_ex] - prices[cheap_ex]) / prices[cheap_ex] * 100
    slippage = 0.1
    profit = estimate_profit(spread, BUY_DEFAULT_AMOUNT_BTC, slippage)

    analysis = (
        f"АНАЛИЗ {symbol}\n"
        f"Дешёвая: {cheap_ex} — ${prices[cheap_ex]:,.2f}\n"
        f"Дорогая: {exp_ex} — ${prices[exp_ex]:,.2f}\n"
        f"Спред: {spread:.2f}%\n"
        f"Комиссия: 0.20% (0.10% на биржу)\n"
        f"Slippage: {slippage:.2f}% (на $10k)\n"
        f"Прибыль на 0.01 BTC: +${profit:.2f}\n"
        f"Рекомендация: {'Купить сейчас' if spread > 1.5 else 'Мониторить'}"
    )
    await update.message.reply_text(analysis)

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /buy 1 или /buy BTC/USDT [0.02]")
        return

    signals = await scan_all_pairs()
    signals = update_signal_timers(signals)
    if not signals:
        await update.message.reply_text("Нет активных сигналов.")
        return

    arg = context.args[0]
    amount = BUY_DEFAULT_AMOUNT_BTC
    if len(context.args) > 1:
        try:
            amount = d(context.args[1])
        except:
            await update.message.reply_text("Неверная сумма.")
            return

    signal = None
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(signals):
            signal = signals[idx]
    else:
        sym = arg.upper()
        if not sym.endswith("/USDT"): sym += "/USDT"
        signal = next((s for s in signals if s["symbol"] == sym), None)

    if not signal:
        await update.message.reply_text("Сигнал не найден.")
        return

    ex_name = signal["cheap_ex"]
    ex_obj = next(ex for name, ex in EXCHANGES if name == ex_name)
    symbol = signal["symbol"]

    success, msg = await execute_buy(ex_obj, symbol, amount)
    await update.message.reply_text(msg)
    if success:
        log(f"BUY {amount} {symbol} on {ex_name}")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balances = []
    for name, ex in EXCHANGES:
        try:
            bal = ex.fetch_balance()
            usdt = bal.get('USDT', {}).get('free', 0)
            balances.append(f"{name}: {usdt:.2f} USDT")
        except:
            balances.append(f"{name}: ошибка")
    await update.message.reply_text("Баланс USDT:\n" + "\n".join(balances))

async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(LOG_PATH):
        await update.message.reply_text("Лог пуст.")
        return
    with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()[-30:]
    text = "".join(lines) or "Лог пуст."
    if len(text) > 3800:
        text = text[-3800:]
    await update.message.reply_text(f"Последние записи:\n\n{text}")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот остановлен.")
    log(f"/stop by {update.effective_user.id}")
    os._exit(0)

# =============== AUTO SCAN & CLEANUP ===============
async def auto_scan(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = [d.get("chat_id") for d in context.application.chat_data.values() if d.get("chat_id")]
    if not chat_ids:
        return

    signals = await scan_all_pairs()
    signals = update_signal_timers(signals)

    # Обновление кэша
    now = time.time()
    new_cache = {}
    for sig in signals:
        key = sig["symbol"]
        if key in signal_cache:
            sig["first_seen"] = signal_cache[key]["first_seen"]
        new_cache[key] = sig
    global signal_cache
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

async def cleanup_old_messages(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    to_delete = [mid for mid, data in active_signals.items() if now - data["time"] > SIGNAL_TTL]
    for mid in to_delete:
        for chat_id in [d.get("chat_id") for d in context.application.chat_data.values() if d.get("chat_id")]:
            try:
                await context.application.bot.delete_message(chat_id=chat_id, message_id=mid)
            except:
                pass
        del active_signals[mid]

# =============== SCHEDULER ===============
async def scheduler(app):
    while True:
        try:
            job_queue = app.job_queue
            job_queue.run_once(auto_scan, 1, data={})
            job_queue.run_once(cleanup_old_messages, 60, data={})
        except Exception as e:
            log(f"Scheduler error: {e}")
        await asyncio.sleep(AUTO_SCAN_INTERVAL)

# =============== ENTRY ===============
if __name__ == "__main__":
    if MODE == "telegram":
        import nest_asyncio
        nest_asyncio.apply()

        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("scan", cmd_scan))
        app.add_handler(CommandHandler("analyze", cmd_analyze))
        app.add_handler(CommandHandler("buy", cmd_buy))
        app.add_handler(CommandHandler("balance", cmd_balance))
        app.add_handler(CommandHandler("log", cmd_log))
        app.add_handler(CommandHandler("stop", cmd_stop))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                     lambda u, c: u.message.reply_text("Команды: /scan /analyze /buy /balance /log /stop")))

        asyncio.create_task(scheduler(app))

        print("Telegram-бот v5.1 запущен. Автоскан каждые 2 мин.")
        log("Bot started")
        asyncio.run(app.run_polling())
    else:
        print("Установите MODE = 'telegram'")