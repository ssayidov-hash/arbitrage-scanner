# ================================================================
# ARBITRAGE SCANNER v5.6-STABLE (Render + Telegram Webhook)
# © 2025 — Multi-Exchange Arbitrage Bot
# Exchanges: MEXC / BITGET
# ================================================================

import os
import asyncio
from datetime import datetime
from aiohttp import web
import ccxt.async_support as ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.error import TimedOut, RetryAfter, NetworkError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== CONFIG ==================
MIN_SPREAD = 1.2        # минимальный профит в %
MIN_VOLUME_1H = 500_000 # минимальный объем за 1ч ($)
SCAN_INTERVAL = 120     # автоскан каждые X сек
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

# ================== GLOBALS ==================
exchanges = {}
exchange_status = {}
pending_trades = {}
scanlog_enabled = set()
app: Application | None = None

# ================== TEXT ==================
INFO_TEXT = f"""*Arbitrage Scanner {VERSION}*

Бот сканирует *MEXC / BITGET* по USDT-парам.
Фильтры: профит ≥ {MIN_SPREAD}% и объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/1ч.
Автоскан каждые {SCAN_INTERVAL} сек (если включён).

*Команды:*
/start — инфо + включить автоскан  
/scan — разовый скан  
/balance — баланс по биржам  
/status — статус подключений  
/scanlog — вкл/выкл лог  
/stop — выключить автоскан  
/info — справка  
/ping — проверить связь
"""

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

async def send_log(chat_id: int, msg: str):
    if app and chat_id in scanlog_enabled:
        try:
            await app.bot.send_message(chat_id, f"🩶 {msg}")
        except:
            pass

# ================== HEALTH ==================
async def start_health_server():
    """Health-check HTTP для Render"""
    port = int(os.environ.get("PORT", "10000"))
    health_app = web.Application()
    health_app.add_routes([web.get("/", lambda _: web.Response(text="OK"))])
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log(f"[Init] Health server listening on port {port}")
    log("🌐 Health server готов.")

# ================== EXCHANGES ==================
async def init_exchanges():
    """Инициализация всех бирж"""
    global exchanges, exchange_status
    exchanges, exchange_status = {}, {}

    async def try_init(name, ex_class, **kwargs):
        if not all(kwargs.values()):
            exchange_status[name] = {"status": "⚪", "error": "нет API-ключей", "ex": None}
            log(f"{name.upper()} ⚪ пропущен — нет API-ключей")
            return None
        try:
            ex = ex_class(kwargs)
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
    log(f"Инициализация завершена: {len(active)}/{len(exchange_status)} бирж активны.")

async def close_all_exchanges():
    for name, ex in exchanges.items():
        try:
            await ex.close()
            log(f"{name.upper()} закрыт ✅")
        except Exception as e:
            log(f"{name.upper()} ошибка закрытия: {e}")

# ================== SCANNER ==================
async def get_top_symbols(exchange, top_n=100):
    tickers = await exchange.fetch_tickers()
    pairs = [(s, t.get("quoteVolume", 0)) for s, t in tickers.items()
             if s.endswith("/USDT") and ":" not in s]
    pairs.sort(key=lambda x: x[1] or 0, reverse=True)
    return [s for s, _ in pairs[:top_n]]

async def scan_all_pairs(chat_id=None):
    results, symbols = [], set()
    FEES = {"mexc": 0.001, "bitget": 0.001}

    for name, ex in exchanges.items():
        try:
            tops = await get_top_symbols(ex)
            symbols.update(tops)
        except Exception as e:
            await send_log(chat_id, f"{name} ошибка топ-листа: {e}")

    await send_log(chat_id, f"Начал скан {len(symbols)} пар...")

    for symbol in symbols:
        prices, vols = {}, {}
        for name, ex in exchanges.items():
            try:
                t = await ex.fetch_ticker(symbol)
                if t.get("bid") and t.get("ask"):
                    prices[name] = (t["bid"] + t["ask"]) / 2
                    vols[name] = t.get("quoteVolume", 0) or 0
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

        if profit >= MIN_SPREAD:
            results.append({
                "symbol": symbol,
                "cheap": cheap,
                "expensive": expensive,
                "price_cheap": round(prices[cheap], 6),
                "price_expensive": round(prices[expensive], 6),
                "spread": round(profit, 2),
                "volume_1h": round(min_vol / 1_000_000, 2)
            })

    results.sort(key=lambda x: x["spread"], reverse=True)
    await send_log(chat_id, f"Готово. Найдено {len(results)} сигналов.")
    return results[:10]

# ================== BUY FLOW ==================
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
    _, cheap, sell, symbol = q.data.split(":")
    chat_id = q.message.chat.id
    pending_trades[chat_id] = {"cheap": cheap, "sell": sell, "symbol": symbol}
    await q.edit_message_text(f"💰 Введите сумму сделки в USDT для {symbol} на {cheap.upper()} (например: 25)")

async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    step = pending_trades.get(chat_id)
    if not step:
        return
    try:
        usdt = float(update.message.text.strip())
    except:
        return await update.message.reply_text("❌ Введите число, например 25")

    cheap, sell, symbol = step["cheap"], step["sell"], step["symbol"]
    ex_buy, ex_sell = exchanges[cheap], exchanges[sell]
    t_buy, t_sell = await ex_buy.fetch_ticker(symbol), await ex_sell.fetch_ticker(symbol)
    buy_price, sell_price = t_buy["ask"], t_sell["bid"]

    profit_pct = (sell_price / buy_price - 1) * 100 - 0.2
    profit_usd = round(usdt * profit_pct / 100, 2)
    msg = (f"*{symbol}*\n"
           f"Покупка: {cheap.upper()} по {buy_price}\n"
           f"Продажа: {sell.upper()} по {sell_price}\n"
           f"Сумма: {usdt} USDT\n"
           f"💹 Примерный профит: *{profit_pct:.2f}% (~{profit_usd} USDT)*\n"
           f"Подтвердить покупку?")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{cheap}:{symbol}:{usdt}"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel")
    ]])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, exch, symbol, usdt = q.data.split(":")
    await q.edit_message_text(f"✅ Ордер подтверждён ({exch.upper()}, {symbol}, {usdt} USDT)\n⚙️ (реальный трейд отключён в демо)")

async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending_trades.pop(q.message.chat.id, None)
    await q.edit_message_text("❌ Покупка отменена.")

# ================== COMMANDS ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["chat_id"] = update.effective_chat.id
    context.chat_data["autoscan"] = True
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Я на связи! Версия: {VERSION}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 *Статус подключений:*"]
    for name, st in exchange_status.items():
        status, err = st["status"], st["error"]
        lines.append(f"{name.upper()} {status} {err or ''}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["💰 Баланс по биржам:"]
    for name, st in exchange_status.items():
        ex = st["ex"]
        if st["status"] == "✅" and ex:
            try:
                b = await ex.fetch_balance()
                free = b["USDT"]["free"]
                lines.append(f"{name.upper()} ✅ {free:.2f} USDT")
            except:
                lines.append(f"{name.upper()} ⚠️ ошибка запроса баланса")
        else:
            lines.append(f"{name.upper()} {st['status']} {st.get('error','')}")
    await update.message.reply_text("\n".join(lines))

async def scanlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in scanlog_enabled:
        scanlog_enabled.remove(chat_id)
        await update.message.reply_text("🧱 Лог сканирования выключен.")
    else:
        scanlog_enabled.add(chat_id)
        await update.message.reply_text("📡 Лог сканирования включён (реальное время).")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["autoscan"] = False
    await update.message.reply_text("Автоскан ❌ выключен.")

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
                txt = (f"*{sig['symbol']}*\nПрофит: *{sig['spread']}%*\n"
                       f"Купить: {sig['cheap'].upper()} {sig['price_cheap']}\n"
                       f"Продать: {sig['expensive'].upper()} {sig['price_expensive']}\n"
                       f"Объём 1ч: {sig['volume_1h']}M$")
                await app.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=get_buy_keyboard(sig))

# ================== MAIN ==================
async def main_async():
    try:
        await start_health_server()
        await init_exchanges()
        global app
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Команды
        cmds = [
            ("start", start_cmd), ("info", info_cmd),
            ("scan", scan_all_pairs), ("balance", balance_cmd),
            ("status", status_cmd), ("scanlog", scanlog_cmd),
            ("ping", ping_cmd), ("stop", stop_cmd),
        ]
        for cmd, func in cmds:
            app.add_handler(CommandHandler(cmd, func))
        app.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^buy:"))
        app.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^confirm:"))
        app.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=r"^cancel$"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input))

        # Планировщик
        scheduler = AsyncIOScheduler()
        scheduler.add_job(auto_scan, "interval", seconds=SCAN_INTERVAL)
        scheduler.start()

        # Webhook
        port = int(os.environ.get("PORT", "10000"))
        host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        webhook_url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"

        # Повторные попытки установки webhook
        for attempt in range(3):
            try:
                await app.bot.set_webhook(webhook_url, drop_pending_updates=True, timeout=30)
                break
            except (TimedOut, RetryAfter, NetworkError) as e:
                log(f"⚠️ Попытка {attempt+1}/3 установки webhook не удалась ({e}). Повтор через 5 сек...")
                await asyncio.sleep(5)
        else:
            log("❌ Не удалось установить webhook после 3 попыток.")
            return

        log(f"✅ Arbitrage Scanner {VERSION} запущен. Порт: {port}")
        log(f"Webhook установлен: {webhook_url}")

        # === Инструкция при запуске ===
        log("===========================================================")
        log("📘 ИНСТРУКЦИЯ:")
        log(f"Фильтры: профит ≥ {MIN_SPREAD}% | объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/1ч")
        log(f"Автоскан каждые {SCAN_INTERVAL} сек (если включён)")
        log("")
        log("🔹 Команды Telegram:")
        log("/start — инфо + включить автоскан")
        log("/scan — разовый скан (топ-10 сигналов)")
        log("/balance — баланс по биржам")
        log("/status — статус подключений")
        log("/scanlog — вкл/выкл лог сканирования")
        log("/stop — выключить автоскан")
        log("/info — справка")
        log("/ping — проверить связь")
        log("===========================================================")

        await app.run_webhook(listen="0.0.0.0", port=port, url_path=TELEGRAM_BOT_TOKEN, webhook_url=webhook_url)
    except Exception as e:
        log(f"❌ Ошибка в main_async: {e}")
    finally:
        await close_all_exchanges()
        log("🧹 Завершение работы.")


if __name__ == "__main__":
    try:
        # избегаем DeprecationWarning: "There is no current event loop"
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(main_async())
    except KeyboardInterrupt:
        pass