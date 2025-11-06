# ================================================================
#  ARBITRAGE SCANNER v6.1-STABLE (Render + Telegram Webhook)
#  Multi-Exchange Spot Arbitrage (REAL ORDERS + AUTOSCAN)
#  Exchanges enabled by default: MEXC, BITGET, BIGONE, OKX, KUCOIN
#  Others left as examples and can be enabled by uncommenting.
#  PTB 21.6 | CCXT async_support | Python 3.13 compatible
# ================================================================

import os
import asyncio
import nest_asyncio
from datetime import datetime
from typing import Dict, Any, List, Tuple, Set

import ccxt.async_support as ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== CONFIG ==================
MIN_SPREAD = 1.2            # мин. чистый профит в %
MIN_VOLUME_1H = 500_000     # мин. объём/1ч ($) по худшей из двух бирж
SCAN_INTERVAL = 120         # автоскан каждые N сек
TOPN_PER_EXCHANGE = 80      # максимальное кол-во ликвидных пар на биржу
VERSION = "v6.1-stable"

TAKER_FEE_DEFAULT = 0.001   # 0.10% как грубая оценка
MAKER_FEE_DEFAULT = 0.0008

# ================== ENV ==================
env = {
    "MEXC_API_KEY": os.getenv("MEXC_API_KEY"),
    "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET"),

    "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
    "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
    "BITGET_API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE"),

    "BIGONE_API_KEY": os.getenv("BIGONE_API_KEY"),
    "BIGONE_API_SECRET": os.getenv("BIGONE_API_SECRET"),

    #"KUCOIN_API_KEY": os.getenv("KUCOIN_API_KEY"),
    #"KUCOIN_API_SECRET": os.getenv("KUCOIN_API_SECRET"),
    #"KUCOIN_PASSWORD": os.getenv("KUCOIN_PASSWORD"),
    
    #"BINANCE_API_KEY": os.getenv("BINANCE_API_KEY"),
    #"BINANCE_API_SECRET": os.getenv("BINANCE_API_SECRET"),

    #"OKX_API_KEY": os.getenv("OKX_API_KEY"),
    #"OKX_API_SECRET": os.getenv("OKX_API_SECRET"),
    #"OKX_API_PASSPHRASE": os.getenv("OKX_API_PASSPHRASE"),
    
    # BYBIT отключён (403 CloudFront с Render)
    # "BYBIT_API_KEY": os.getenv("BYBIT_API_KEY"),
    # "BYBIT_API_SECRET": os.getenv("BYBIT_API_SECRET"),

    "GATE_API_KEY": os.getenv("GATE_API_KEY"),
    "GATE_API_SECRET": os.getenv("GATE_API_SECRET"),

    #"HTX_API_KEY": os.getenv("HTX_API_KEY"),
    #"HTX_API_SECRET": os.getenv("HTX_API_SECRET"),

    #"KRAKEN_API_KEY": os.getenv("KRAKEN_API_KEY"),
    #"KRAKEN_API_SECRET": os.getenv("KRAKEN_API_SECRET"),

    #"CRYPTO_API_KEY": os.getenv("CRYPTO_API_KEY"),
    #"CRYPTO_API_SECRET": os.getenv("CRYPTO_API_SECRET"),

    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "CHAT_ID": os.getenv("CHAT_ID"),
}
BOT_TOKEN = env["TELEGRAM_BOT_TOKEN"]
if not BOT_TOKEN:
    raise SystemExit("❌ Нет TELEGRAM_BOT_TOKEN")

# ================== PREP ==================
nest_asyncio.apply()
START_TIME = datetime.now()
LAST_SCAN_AT: datetime | None = None  # время последнего успешного скана

# ================== GLOBALS ==================
app: Application | None = None
exchanges: Dict[str, ccxt.Exchange] = {}
exchange_status: Dict[str, Dict[str, Any]] = {}
scanlog_enabled: Set[int] = set()
pending_trades: Dict[int, Dict[str, Any]] = {}  # chat_id -> {cheap, expensive, symbol, usdt?}

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def fmt_pct(x: float) -> str:
    return f"{x:.2f}%"

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def uptime_str() -> str:
    delta = datetime.now() - START_TIME
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"

def last_scan_str() -> str:
    if not LAST_SCAN_AT:
        return "—"
    return LAST_SCAN_AT.strftime("%Y-%m-%d %H:%M:%S")

# ================== EXCH INIT/CLOSE ==================
async def init_exchanges():
    """
    Инициализация подключенных бирж.
    Чтобы временно отключить биржу — просто закомментируй её в 'candidates'.
    """
    global exchanges, exchange_status
    exchanges, exchange_status = {}, {}

    async def try_init(name, ex_class, **kwargs):
        if not all(kwargs.values()):
            exchange_status[name] = {"status": "⚪", "error": "нет API", "ex": None}
            log(f"{name.upper()} ⚪ пропущен — нет API ключей")
            return None
        try:
            ex = ex_class({
                **kwargs,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
                "timeout": 30000,
            })
            await ex.load_markets()
            exchange_status[name] = {"status": "✅", "error": None, "ex": ex}
            log(f"{name.upper()} ✅ инициализирован 🟢")
            return ex
        except Exception as e:
            err = str(e).split("\n")[0][:200]
            exchange_status[name] = {"status": "❌", "error": err, "ex": None}
            log(f"{name.upper()} ❌ {err}")
            return None

    candidates = {
        # === ENABLED BY DEFAULT ===
        "mexc": (ccxt.mexc, {
            "apiKey": env["MEXC_API_KEY"], "secret": env["MEXC_API_SECRET"]
        }),
        "bitget": (ccxt.bitget, {
            "apiKey": env["BITGET_API_KEY"], "secret": env["BITGET_API_SECRET"], "password": env["BITGET_API_PASSPHRASE"]
        }),
        "bigone": (ccxt.bigone, {
            "apiKey": env["BIGONE_API_KEY"], "secret": env["BIGONE_API_SECRET"]
        }),
        #"kucoin": (ccxt.kucoin, {
        #    "apiKey": env["KUCOIN_API_KEY"], "secret": env["KUCOIN_API_SECRET"], "password": env["KUCOIN_PASSWORD"]
        #}),
        
        #"okx": (ccxt.okx, {
        #    "apiKey": env["OKX_API_KEY"], "secret": env["OKX_API_SECRET"],
        #    "password": env["OKX_API_PASSPHRASE"]
        #}),
        # === OPTIONAL — UNCOMMENT TO ENABLE ===
        # "binance": (ccxt.binance, {
        #     "apiKey": env["BINANCE_API_KEY"], "secret": env["BINANCE_API_SECRET"]
        # }),
         "gate": (ccxt.gate, {
             "apiKey": env["GATE_API_KEY"], "secret": env["GATE_API_SECRET"]
        # }),
        # "htx": (ccxt.huobi, {
        #     "apiKey": env["HTX_API_KEY"], "secret": env["HTX_API_SECRET"]
        # }),
        # "kraken": (ccxt.kraken, {
        #     "apiKey": env["KRAKEN_API_KEY"], "secret": env["KRAKEN_API_SECRET"]
        # }),
        # "crypto": (ccxt.cryptocom, {
        #     "apiKey": env["CRYPTO_API_KEY"], "secret": env["CRYPTO_API_SECRET"]
        # }),
        # "bybit": (ccxt.bybit, {   # CloudFront 403 с Render — отключено
        #     "apiKey": env.get("BYBIT_API_KEY"), "secret": env.get("BYBIT_API_SECRET")
        # }),
    }

    for name, (cls, params) in candidates.items():
        ex = await try_init(name, cls, **params)
        if ex:
            exchanges[name] = ex

    active = [k for k, v in exchange_status.items() if v["status"] == "✅"]
    log(f"Активные биржи: {', '.join(active) if active else '—'} 🟩")
    log(f"Инициализация завершена: {len(active)}/{len(exchange_status)} активны.")

async def close_all_exchanges():
    for name, ex in list(exchanges.items()):
        try:
            await ex.close()
            log(f"{name.upper()} закрыт ✅")
        except Exception as e:
            log(f"{name.upper()} ошибка закрытия: {e}")

# ================== MARKET DATA / SCAN ==================
async def get_top_symbols(ex: ccxt.Exchange, top_n=TOPN_PER_EXCHANGE) -> List[str]:
    tickers = await ex.fetch_tickers()
    rows = []
    for s, t in tickers.items():
        if ":" in s or not s.endswith("/USDT"):
            continue
        qv = safe_float(t.get("quoteVolume") or (t.get("info") or {}).get("quoteVolume"))
        rows.append((s, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:top_n]]

async def scan_all_pairs(chat_id: int | None = None) -> List[Dict[str, Any]]:
    global LAST_SCAN_AT
    # собрать универcальный список символов
    symbol_set: Set[str] = set()
    for name, ex in exchanges.items():
        try:
            tops = await get_top_symbols(ex)
            symbol_set.update(tops)
        except Exception as e:
            if chat_id in scanlog_enabled and app:
                await app.bot.send_message(chat_id, f"<i>{name} ошибка топ-листа: {e}</i>", parse_mode="HTML")

    results: List[Dict[str, Any]] = []
    FEES = {name: TAKER_FEE_DEFAULT for name in exchanges.keys()}

    # пройти все символы, собрать цены и объёмы
    for symbol in symbol_set:
        prices: Dict[str, float] = {}
        vols: Dict[str, float] = {}
        for name, ex in exchanges.items():
            try:
                t = await ex.fetch_ticker(symbol)
                bid = safe_float(t.get("bid"))
                ask = safe_float(t.get("ask"))
                if bid and ask:
                    mid = (bid + ask) / 2.0
                    prices[name] = mid
                    vols[name] = safe_float(t.get("quoteVolume") or (t.get("info") or {}).get("quoteVolume"))
            except Exception:
                continue

        if len(prices) < 2:
            continue

        min_p = min(prices.values())
        max_p = max(prices.values())
        spread_pct = (max_p - min_p) / min_p * 100.0
        if spread_pct < MIN_SPREAD:
            continue

        # проверяем минимальный объём 1ч среди доступных бирж по этому символу
        if vols:
            min_vol = min(v for v in vols.values() if v is not None)
            if min_vol < MIN_VOLUME_1H:
                continue

        cheap = min(prices, key=prices.get)
        expensive = max(prices, key=prices.get)

        gross = (max_p / min_p - 1.0) * 100.0
        fees = (FEES.get(cheap, TAKER_FEE_DEFAULT) + FEES.get(expensive, TAKER_FEE_DEFAULT)) * 100.0
        net = gross - fees

        if net < MIN_SPREAD:
            continue

        results.append({
            "symbol": symbol,
            "cheap": cheap,
            "expensive": expensive,
            "price_cheap": round(prices[cheap], 6),
            "price_expensive": round(prices[expensive], 6),
            "spread": round(net, 2),
            "volume_1h": round((min_vol if vols else 0) / 1_000_000, 2),
        })

    results.sort(key=lambda x: x["spread"], reverse=True)
    LAST_SCAN_AT = datetime.now()
    return results[:10]

# ================== BUY FLOW (SINGLE BUY BUTTON) ==================
def build_buy_keyboard(sig: Dict[str, Any]) -> InlineKeyboardMarkup:
    # одна кнопка BUY — спрашиваем сумму после нажатия
    data = f"{sig['cheap']}|{sig['expensive']}|{sig['symbol']}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("BUY", callback_data=f"buy:{data}")
    ]])

async def on_buy_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, payload = q.data.split(":")
    cheap, expensive, symbol = payload.split("|")
    chat_id = q.message.chat.id
    pending_trades[chat_id] = {"cheap": cheap, "expensive": expensive, "symbol": symbol}
    await q.edit_message_text(
        f"💰 Введите <b>сумму USDT</b> для сделки.\n"
        f"Сделка: <code>{symbol}</code> — BUY на <b>{cheap.upper()}</b>, SELL на <b>{expensive.upper()}</b>",
        parse_mode="HTML"
    )

async def on_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    step = pending_trades.get(chat_id)
    if not step:
        return
    txt = (update.message.text or "").strip().replace(",", ".")
    try:
        amt = float(txt)
        if amt <= 0:
            raise ValueError
        step["usdt"] = amt
    except Exception:
        return await update.message.reply_text("❌ Введите положительное число, например: 25")

    symbol = step["symbol"]
    cheap = step["cheap"]
    sell = step["expensive"]
    ex_buy = exchanges.get(cheap)
    ex_sell = exchanges.get(sell)

    try:
        t_buy = await ex_buy.fetch_ticker(symbol)
        t_sell = await ex_sell.fetch_ticker(symbol)
        ask = safe_float(t_buy.get("ask"))
        bid = safe_float(t_sell.get("bid"))
    except Exception as e:
        return await update.message.reply_text(f"❌ Не смог получить цены: {e}")

    profit_pct = (bid / ask - 1.0) * 100.0 - (TAKER_FEE_DEFAULT + TAKER_FEE_DEFAULT) * 100.0
    profit_usd = step["usdt"] * profit_pct / 100.0
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{cheap}|{sell}|{symbol}|{step['usdt']}"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel")
    ]])

    text = (
        f"<b>{symbol}</b>\n"
        f"BUY {cheap.upper()} @ <code>{ask}</code>\n"
        f"SELL {sell.upper()} @ <code>{bid}</code>\n"
        f"Сумма: <b>{step['usdt']:.2f} USDT</b>\n"
        f"Оценка профита: <b>{fmt_pct(profit_pct)}</b> (~{profit_usd:.2f} USDT)\n"
        f"⚠️ Балансы должны быть: USDT на {cheap.upper()} и базовая монета на {sell.upper()}.\n"
        f"Продолжить?"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, payload = q.data.split(":")
    cheap, sell, symbol, usdt_s = payload.split("|")
    usdt = float(usdt_s)
    base = symbol.split("/")[0]

    ex_buy = exchanges.get(cheap)
    ex_sell = exchanges.get(sell)

    # 1) BUY на дешёвой бирже (market)
    try:
        bal_buy = await ex_buy.fetch_balance()
        usdt_free = safe_float((bal_buy.get("USDT") or {}).get("free"))
        if usdt_free <= 0:
            raise RuntimeError(f"{cheap.upper()}: нет свободных USDT")
        t = await ex_buy.fetch_ticker(symbol)
        ask = safe_float(t.get("ask"))
        if ask <= 0:
            raise RuntimeError("bad ask")
        spend = min(usdt, usdt_free)
        base_amount_est = (spend * (1 - TAKER_FEE_DEFAULT)) / ask
        amount = safe_float(ex_buy.amount_to_precision(symbol, base_amount_est), base_amount_est)
        order_buy = await ex_buy.create_order(symbol, "market", "buy", amount)
    except Exception as e:
        await q.edit_message_text(f"❌ BUY ошибка на {cheap.upper()}: {e}")
        return

    # 2) SELL на дорогой бирже (market)
    try:
        bal_sell = await ex_sell.fetch_balance()
        base_free = safe_float((bal_sell.get(base) or {}).get("free"))
        if base_free <= 0:
            raise RuntimeError(f"{sell.upper()}: нет свободного {base}")
        sell_amount = min(base_free, amount)
        sell_amount = safe_float(ex_sell.amount_to_precision(symbol, sell_amount), sell_amount)
        order_sell = await ex_sell.create_order(symbol, "market", "sell", sell_amount)
    except Exception as e:
        await q.edit_message_text(
            f"⚠️ BUY выполнен, но SELL не удалось на {sell.upper()}: {e}\n"
            f"Проверь баланс и ордера вручную."
        )
        pending_trades.pop(q.message.chat.id, None)
        return

    await q.edit_message_text(
        f"✅ Готово:\n"
        f"• BUY {symbol} на {cheap.upper()} ~<code>{amount}</code>\n"
        f"• SELL {symbol} на {sell.upper()} ~<code>{sell_amount}</code>\n"
        f"Проверь историю ордеров на биржах.",
        parse_mode="HTML"
    )
    pending_trades.pop(q.message.chat.id, None)

async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending_trades.pop(q.message.chat.id, None)
    await q.edit_message_text("❌ Отменено.")

# ================== COMMANDS ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # включаем автоскан для этого чата
    context.chat_data["chat_id"] = update.effective_chat.id
    context.chat_data["autoscan"] = True

    active = [k.upper() for k, v in exchange_status.items() if v["status"] == "✅"]
    total = len(exchange_status)
    active_str = ", ".join(active) if active else "—"
    text = (
        f"🤖 <b>ARBITRAGE SCANNER {VERSION}</b>\n\n"
        f"Аптайм: <code>{uptime_str()}</code> (с {START_TIME.strftime('%Y-%m-%d %H:%M:%S')})\n"
        f"🕓 Последнее обновление: <code>{last_scan_str()}</code>\n"
        f"Активные биржи: 🟢 <b>{len(active)}</b> / {total}\n"
        f"{active_str}\n\n"
        f"Фильтры:\n"
        f"• Мин. профит: <code>{MIN_SPREAD:.1f}%</code>\n"
        f"• Мин. объём (1ч): <code>{MIN_VOLUME_1H/1000:.0f}k$</code>\n"
        f"• Автоскан: каждые <code>{SCAN_INTERVAL}</code> сек\n\n"
        "Команды:\n"
        "/scan — разовый скан (топ-10)\n"
        "/balance — баланс по биржам\n"
        "/status — статус подключений\n"
        "/scanlog — включить/выключить live-лог\n"
        "/stop — отключить автоскан\n"
        "/info — справка\n"
        "/ping — пинг"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"<b>ARBITRAGE SCANNER {VERSION} — справка</b>\n\n"
        "1) <b>Описание</b>\n"
        "Скан USDT-пар на нескольких спотовых биржах и поиск кросс-биржевого арбитража.\n\n"
        "2) <b>Параметры</b>\n"
        f"• Мин. профит: <code>{MIN_SPREAD}%</code>\n"
        f"• Мин. объём: <code>{MIN_VOLUME_1H/1000:.0f}k$</code>\n"
        f"• Автоскан: <code>{SCAN_INTERVAL} сек</code>\n\n"
        "3) <b>Логика</b>\n"
        "— собираем топ-ликвидные пары; считаем mid-price; фильтруем по объёму; считаем спред и чистую маржу после комиссий;\n"
        "— выдаём топ сигналов; по клику — BUY/SELL на разных биржах (нужны балансы USDT и базовой монеты). \n\n"
        "4) <b>Команды</b>\n"
        "/start, /scan, /balance, /status, /scanlog, /stop, /info, /ping\n\n"
        "5) <b>Пример</b>\n"
        "<code>BTC/USDT</code>: купить на MEXC 67000.2 → продать на Bitget 67750.3 → <b>+1.12%</b>\n\n"
        "6) <b>Рекомендации</b>\n"
        "— Держи USDT на дешёвой бирже и базовую монету на дорогой;\n"
        "— Не опускай SCAN_INTERVAL ниже 120 сек;\n"
        "— TOPN_PER_EXCHANGE ≈ 50–100 для стабильности на Render."
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Я здесь.", parse_mode="HTML")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = [k.upper() for k, v in exchange_status.items() if v["status"] == "✅"]
    total = len(exchange_status)
    active_str = ", ".join(active) if active else "—"

    lines = [
        "📊 <b>Статус подключений</b>\n",
        f"Аптайм: <code>{uptime_str()}</code>",
        f"🕓 Последнее обновление: <code>{last_scan_str()}</code>",
        f"Активные биржи: 🟢 <b>{len(active)}</b> / {total}",
        f"{active_str}\n",
        "Подробности:"
    ]
    for name, st in exchange_status.items():
        emoji = "🟢" if st["status"] == "✅" else "🔴" if st["status"] == "❌" else "⚪"
        msg = f"{emoji} {name.upper()} — {'OK' if st['status'] == '✅' else st['error'] or st['status']}"
        lines.append(msg)

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["<b>Баланс (USDT):</b>"]
    for name, st in exchange_status.items():
        ex = st.get("ex")
        if st["status"] == "✅" and ex:
            try:
                b = await ex.fetch_balance()
                free = safe_float((b.get("USDT") or {}).get("free"))
                lines.append(f"{name.upper()}: <code>{free:.2f}</code> USDT")
            except Exception as e:
                lines.append(f"{name.upper()}: ошибка — {e}")
        else:
            lines.append(f"{name.upper()}: {st['status']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def scanlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in scanlog_enabled:
        scanlog_enabled.remove(chat_id)
        await update.message.reply_text("🟡 Лог сканирования выключен.")
    else:
        scanlog_enabled.add(chat_id)
        await update.message.reply_text("🟢 Лог сканирования включён.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["autoscan"] = False
    await update.message.reply_text("⏸️ Автоскан отключён.")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    res = await scan_all_pairs(chat_id)
    if not res:
        return await update.message.reply_text("Сигналов нет.")
    for sig in res:
        txt = (
            f"<b>{sig['symbol']}</b>\n"
            f"Профит: <b>{sig['spread']}%</b>\n"
            f"Купить: {sig['cheap'].upper()} <code>{sig['price_cheap']}</code>\n"
            f"Продать: {sig['expensive'].upper()} <code>{sig['price_expensive']}</code>\n"
            f"Объём 1ч: <code>{sig['volume_1h']}M</code>$"
        )
        await update.message.reply_text(txt, parse_mode="HTML", reply_markup=build_buy_keyboard(sig))

# ================== AUTOSCAN ==================
async def autoscan_tick():
    if not app:
        return
    for data in app.chat_data.values():
        if data.get("autoscan"):
            chat_id = data["chat_id"]
            try:
                res = await scan_all_pairs(chat_id)
                if not res:
                    continue
                for sig in res:
                    txt = (
                        f"<b>{sig['symbol']}</b>\n"
                        f"Профит: <b>{sig['spread']}%</b>\n"
                        f"Купить: {sig['cheap'].upper()} <code>{sig['price_cheap']}</code>\n"
                        f"Продать: {sig['expensive'].upper()} <code>{sig['price_expensive']}</code>\n"
                        f"Объём 1ч: <code>{sig['volume_1h']}M</code>$"
                    )
                    await app.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=build_buy_keyboard(sig))
            except Exception as e:
                try:
                    await app.bot.send_message(chat_id, f"⚠️ autoscan: {e}")
                except Exception:
                    pass

# ================== MAIN ==================
async def main():
    log("🚀 INIT START (Render + Telegram webhook)")
    await init_exchanges()

    global app
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("scanlog", scanlog_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CallbackQueryHandler(on_buy_click, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^cancel$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_amount_text))

    # Второй обработчик /start (группа 1) — включает автоскан для чата
    async def on_start_autoscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.chat_data["chat_id"] = update.effective_chat.id
        context.chat_data["autoscan"] = True
    app.add_handler(CommandHandler("start", on_start_autoscan), group=1)

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(autoscan_tick, "interval", seconds=SCAN_INTERVAL)
    scheduler.start()

    # Webhook params
    PORT = int(os.getenv("PORT", "10000"))
    EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL", "")
    if not EXTERNAL_URL:
        raise SystemExit("❌ Нет RENDER_EXTERNAL_URL / WEBHOOK_URL")

    WEBHOOK_PATH = f"/{BOT_TOKEN}"
    WEBHOOK_URL = f"{EXTERNAL_URL.rstrip('/')}{WEBHOOK_PATH}"
    WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "") or None

    log(f"🌐 Webhook URL: {WEBHOOK_URL}")
    log(f"🔒 Secret set: {'yes' if WEBHOOK_SECRET else 'no'}")
    log(f"🌐 Listening on 0.0.0.0:{PORT} ...")
    log("===========================================================")
    log(f"✅ Arbitrage Scanner {VERSION} запущен (Render webhook mode)")
    log(f"Фильтры: профит ≥ {MIN_SPREAD}% | объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/1ч | топ/биржу={TOPN_PER_EXCHANGE}")
    log(f"Автоскан каждые {SCAN_INTERVAL} сек")
    log("===========================================================")

    try:
        await app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    finally:
        await close_all_exchanges()
        log("🧹 Завершение — соединения закрыты.")

if __name__ == "__main__":
    nest_asyncio.apply()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
