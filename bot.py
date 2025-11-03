# ================================================================
# ARBITRAGE SCANNER v5.6 — Interactive Edition (Webhook, Render)
# © 2025 — Multi-Exchange Arbitrage Bot for Telegram
# Exchanges: MEXC / BITGET / KUCOIN / OKX / HUOBI / BIGONE
# ================================================================
import os
import asyncio
import ccxt.async_support as ccxt
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== CONFIG ==================
MIN_SPREAD = 1.2
MIN_VOLUME_1H = 500_000
SCAN_INTERVAL = 120
VERSION = "v5.6"

# ================== ENV VARS ==================
env_vars = {
    "MEXC_API_KEY": os.getenv("MEXC_API_KEY"),
    "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET"),
    "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
    "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
    "BITGET_API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE"),
    "KUCOIN_API_KEY": os.getenv("KUCOIN_API_KEY"),
    "KUCOIN_API_SECRET": os.getenv("KUCOIN_API_SECRET"),
    "KUCOIN_API_PASS": os.getenv("KUCOIN_API_PASS"),
    "OKX_API_KEY": os.getenv("OKX_API_KEY"),
    "OKX_API_SECRET": os.getenv("OKX_API_SECRET"),
    "OKX_API_PASS": os.getenv("OKX_API_PASS"),
    "HUOBI_API_KEY": os.getenv("HUOBI_API_KEY"),
    "HUOBI_API_SECRET": os.getenv("HUOBI_API_SECRET"),
    "BIGONE_API_KEY": os.getenv("BIGONE_API_KEY"),
    "BIGONE_API_SECRET": os.getenv("BIGONE_API_SECRET"),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "CHAT_ID": os.getenv("CHAT_ID"),
}
TELEGRAM_BOT_TOKEN = env_vars["TELEGRAM_BOT_TOKEN"]

# ================== GLOBALS ==================
exchanges = {}
exchange_status = {}
pending_trades = {}
app = None
scanlog_enabled = set()

# ================== TEXT ==================
INFO_TEXT = f"""*Arbitrage Scanner {VERSION}*
Бот сканирует *MEXC / BITGET / KUCOIN / OKX / HUOBI / BIGONE* по USDT-парам.
Анализирует топ-100 монет и ищет арбитраж ≥ {MIN_SPREAD}% с объёмом ≥ {MIN_VOLUME_1H/1000:.0f}k$.
Работа:
— Автоскан каждые {SCAN_INTERVAL} сек (если включён)
— Команды не блокируются
— BUY без номинала — бот сам спросит сумму
— Реальное время логов по /scanlog
Команды:
/start — запустить и подписаться на автоскан
/scan — ручной запуск сканирования
/balance — баланс по биржам
/status — статус подключения бирж
/scanlog — включить или выключить лог сканирования
/stop — остановить автоскан
/info — параметры и помощь
/ping — проверить связь
"""

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

async def send_log(chat_id, msg):
    if app and chat_id in scanlog_enabled:
        try:
            await app.bot.send_message(chat_id, f"{msg}")
        except:
            pass

# ================== INIT EXCHANGES ==================
async def init_exchanges():
    global exchanges, exchange_status
    exchanges, exchange_status = {}, {}
    async def try_init(name, ex_class, **kwargs):
        if not any(kwargs.values()):
            exchange_status[name] = {"status": "off", "error": "нет API-ключей", "ex": None}
            log(f"{name.upper()} off пропущен — нет API-ключей")
            return None
        try:
            ex = ex_class(kwargs)
            await ex.load_markets()
            exchange_status[name] = {"status": "on", "error": None, "ex": ex}
            log(f"{name.upper()} on инициализирован")
            return ex
        except Exception as e:
            err = str(e).split('\n')[0][:180]
            exchange_status[name] = {"status": "error", "error": err, "ex": None}
            log(f"{name.upper()} error {err}")
            return None

    candidates = {
        "mexc": (ccxt.mexc, {"apiKey": env_vars.get("MEXC_API_KEY"), "secret": env_vars.get("MEXC_API_SECRET")}),
        "bitget": (ccxt.bitget, {"apiKey": env_vars.get("BITGET_API_KEY"), "secret": env_vars.get("BITGET_API_SECRET"), "password": env_vars.get("BITGET_API_PASSPHRASE")}),
        "kucoin": (ccxt.kucoin, {"apiKey": env_vars.get("KUCOIN_API_KEY"), "secret": env_vars.get("KUCOIN_API_SECRET"), "password": env_vars.get("KUCOIN_API_PASS")}),
        "okx": (ccxt.okx, {"apiKey": env_vars.get("OKX_API_KEY"), "secret": env_vars.get("OKX_API_SECRET"), "password": env_vars.get("OKX_API_PASS")}),
        "huobi": (ccxt.huobi, {"apiKey": env_vars.get("HUOBI_API_KEY"), "secret": env_vars.get("HUOBI_API_SECRET")}),
        "bigone": (ccxt.bigone, {"apiKey": env_vars.get("BIGONE_API_KEY"), "secret": env_vars.get("BIGONE_API_SECRET")}),
    }
    for name, (cls, params) in candidates.items():
        ex = await try_init(name, cls, **params)
        if ex:
            exchanges[name] = ex
    active = [k for k, v in exchange_status.items() if v["status"] == "on"]
    log(f"Активные биржи: {', '.join(active) if active else '—'}")

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
        profit = (max_p / min_p - 1) * 100 - (FEES.get(cheap, 0.001) + FEES.get(expensive, 0.001)) * 100
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
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"BUY_{sig['cheap'].upper()}",
            callback_data=f"buy:{sig['cheap']}:{sig['expensive']}:{sig['symbol']}"
        )
    ]])

async def handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data.split(":")
    if len(data) != 4:
        return
    _, cheap, sell, symbol = data
    if cheap not in exchanges or sell not in exchanges:
        return await q.edit_message_text("error Биржа недоступна.")
    chat_id = q.message.chat_id
    pending_trades[chat_id] = {"cheap": cheap, "sell": sell, "symbol": symbol}
    await q.edit_message_text(
        f"Введите сумму сделки в USDT для {symbol} на {cheap.upper()} (например: 25)"
    )

async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    step = pending_trades.get(chat_id)
    if not step:
        return
    text = (update.message.text or "").strip()
    try:
        usdt = float(text.replace(",", "."))
        if usdt <= 0:
            raise ValueError
    except:
        return await update.message.reply_text("error Введите положительное число, например: 25")
    cheap, sell, symbol = step["cheap"], step["sell"], step["symbol"]
    ex_buy = exchanges.get(cheap)
    ex_sell = exchanges.get(sell)
    try:
        t_buy = await ex_buy.fetch_ticker(symbol)
        t_sell = await ex_sell.fetch_ticker(symbol)
        buy_price = t_buy["ask"]
        sell_price = t_sell["bid"]
        FEES = {"mexc": 0.001, "bitget": 0.001, "kucoin": 0.001}
        profit_pct = (sell_price / buy_price - 1) * 100 - (FEES.get(cheap,0.001)+FEES.get(sell,0.001))*100
        profit_usd = round(usdt * profit_pct / 100, 2)
        amount = round(usdt / buy_price, 6)
        step["usdt"] = usdt
        pending_trades[chat_id] = step
        msg = (f"*{symbol}*\n"
               f"Покупка: {cheap.upper()} по {buy_price}\n"
               f"Продажа: {sell.upper()} по {sell_price}\n"
               f"Сумма: {usdt} USDT → ≈ {amount} {symbol.split('/')[0]}\n"
               f"Примерный профит: *{profit_pct:.2f}% (~{profit_usd} USDT)*\n\n"
               f"Подтвердить покупку?")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Подтвердить", callback_data=f"confirm:{cheap}:{symbol}:{usdt}"),
            InlineKeyboardButton("Отмена", callback_data="cancel")
        ]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        await update.message.reply_text(f"error Ошибка при расчёте: {e}")

async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, exch, symbol, usdt = q.data.split(":")
        usdt = float(usdt)
        ex = exchanges[exch]
        bal = await ex.fetch_balance()
        free = bal["USDT"]["free"]
        if free < usdt:
            return await q.edit_message_text(f"Недостаточно средств ({free:.2f} USDT).")
        t = await ex.fetch_ticker(symbol)
        amount = round(usdt / t["ask"], 6)
        order = await ex.create_market_buy_order(symbol, amount)
        await q.edit_message_text(
            f"Куплено {amount} {symbol.split('/')[0]} на {exch.upper()} ({usdt} USDT)\nID: {order.get('id','—')}"
        )
    except Exception as e:
        await q.edit_message_text(f"error Ошибка покупки: {e}")

async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending_trades.pop(q.message.chat_id, None)
    await q.edit_message_text("Покупка отменена.")

# ================== COMMANDS ==================
async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Я на связи! Версия: {VERSION}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not exchange_status:
        await update.message.reply_text("warning Биржи ещё не инициализированы.")
        return
    lines = ["*Статус подключений:*"]
    for name, data in exchange_status.items():
        status = data.get("status", "off")
        error = data.get("error")
        if error:
            lines.append(f"{name.upper()}: {status} — {error}")
        else:
            lines.append(f"{name.upper()}: {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["chat_id"] = update.effective_chat.id
    context.chat_data["autoscan"] = True
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")

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
    lines = ["*Баланс по биржам:*"]
    for name, st in exchange_status.items():
        ex = st["ex"]
        if st["status"] == "on" and ex:
            try:
                b = await ex.fetch_balance()
                lines.append(f"{name.upper()} on {b['USDT']['free']:.2f} / {b['USDT']['total']:.2f}")
            except Exception as e:
                lines.append(f"{name.upper()} warning ошибка: {e}")
        else:
            reason = st["error"] or "неактивна"
            lines.append(f"{name.upper()} {st['status']} {reason}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def scanlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in scanlog_enabled:
        scanlog_enabled.remove(chat_id)
        await update.message.reply_text("Лог сканирования выключен.")
    else:
        scanlog_enabled.add(chat_id)
        await update.message.reply_text("Лог сканирования включён (реальное время).")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["autoscan"] = False
    await update.message.reply_text("Автоскан выключен для этого чата.")

# ================== AUTOSCAN ==================
async def auto_scan():
    if not app:
        return
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

# ================== MAIN ==================
async def close_all_exchanges():
    for name, ex in exchanges.items():
        try:
            await ex.close()
            log(f"{name.upper()} закрыт on")
        except Exception as e:
            log(f"{name.upper()} ошибка закрытия: {e}")

async def main():
    try:
        await init_exchanges()
        global app
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # === ХЕНДЛЕРЫ ===
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("info", info))
        app.add_handler(CommandHandler("scan", scan_cmd))
        app.add_handler(CommandHandler("balance", balance_cmd))
        app.add_handler(CommandHandler("scanlog", scanlog_cmd))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("ping", ping_cmd))
        app.add_handler(CommandHandler("stop", stop_cmd))
        app.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^buy:"))
        app.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^confirm:"))
        app.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=r"^cancel$"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input))

        scheduler = AsyncIOScheduler()
        scheduler.add_job(auto_scan, "interval", seconds=SCAN_INTERVAL)
        scheduler.start()

        # === RENDER WEBHOOK ===
        webhook_url = os.environ.get("RENDER_EXTERNAL_URL")
        if not webhook_url:
            raise RuntimeError("RENDER_EXTERNAL_URL не найден.")
        webhook_url = f"{webhook_url.rstrip('/')}/webhook"
        port = int(os.environ.get("PORT", 10000))

        await app.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        log(f"Webhook установлен: {webhook_url}")

        # Уведомление
        CHAT_ID = env_vars.get("CHAT_ID")
        if CHAT_ID:
            try:
                await app.bot.send_message(int(CHAT_ID), f"Arbitrage Scanner {VERSION} запущен!")
            except Exception as e:
                log(f"Не удалось уведомить: {e}")

        log(f"Слушаю порт {port}...")

        # ← ВОЗВРАЩАЕМ ПЕРЕМЕННЫЕ ДЛЯ ЗАПУСКА ВНЕ main()
        return app, port, webhook_url

    except Exception as e:
        log(f"КРИТИЧЕСКАЯ ОШИБКА: {e}")
        raise
    finally:
        await close_all_exchanges()

# ================== ЗАПУСК ==================
if __name__ == "__main__":
    # Инициализация
    app, port, webhook_url = asyncio.run(main())

    # Запуск webhook отдельно — без двойного event loop
    asyncio.run(
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="/webhook",
            webhook_url=webhook_url
        )
    )
