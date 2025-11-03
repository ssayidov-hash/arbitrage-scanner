# ================================================================
# ARBITRAGE SCANNER v5.6 — Interactive Edition (Webhook, Render)
# © 2025 — Multi-Exchange Arbitrage Bot for Telegram
# Exchanges: MEXC / BITGET / KUCOIN / OKX / HUOBI / BIGONE
# ================================================================
import os
import asyncio
import signal
from datetime import datetime

import ccxt.async_support as ccxt
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== CONFIG ==================
MIN_SPREAD = 1.2               # % мин. спред (после комиссий в логике ниже)
MIN_VOLUME_1H = 500_000        # $ мин. объём 1ч
SCAN_INTERVAL = 120            # сек, автоскан
VERSION = "v5.6"

# ================== ENV ==================
env_vars = {
    "BYBIT_API_KEY": os.getenv("BYBIT_API_KEY"),
    "BYBIT_API_SECRET": os.getenv("BYBIT_API_SECRET"),

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
    "CHAT_ID": os.getenv("CHAT_ID"),  # не обязательно, для уведомления при старте
}
TELEGRAM_BOT_TOKEN = env_vars["TELEGRAM_BOT_TOKEN"]

# ================== GLOBALS ==================
exchanges = {}          # активные биржи: name -> ccxt instance
exchange_status = {}    # name -> {"status": "✅/⚪/❌", "error": str|None, "ex": obj|None}
pending_trades = {}     # chat_id -> {"cheap","sell","symbol","usdt"?}
app: Application | None = None
scanlog_enabled = set() # chat ids

# ================== TEXTS ==================
INFO_TEXT = f"""*Arbitrage Scanner {VERSION}*

Бот сканирует *MEXC / BITGET / KUCOIN / OKX / HUOBI / BIGONE / BYBIT* по USDT-парам.
Фильтры: профит ≥ {MIN_SPREAD}% и объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/1ч.
Автоскан каждые {SCAN_INTERVAL} сек (если включён).

*Команды:*
/start — инфо + включить автоскан
/scan — разовый скан
/balance — баланс по биржам
/status — статус подключений
/scanlog — вкл/выкл ленту логов сканера
/stop — выключить автоскан
/info — справка
/ping — проверить связь
"""

START_SUMMARY = f"""
🧭 *Arbitrage Scanner {VERSION}*

Скан топ-100 монет на *MEXC / BITGET / KUCOIN / OKX / HUOBI / BIGONE*.
Фильтры: профит ≥ {MIN_SPREAD}%, объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/h.
Автоскан: каждые {SCAN_INTERVAL} сек.

⚙️ Команды:
/scan — ручной скан
/balance — баланс USDT
/scanlog — лог (вкл/выкл)
/status — статусы бирж
/stop — остановить автоскан
"""

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

async def send_log(chat_id: int, msg: str):
    if app and chat_id in scanlog_enabled:
        try:
            await app.bot.send_message(chat_id, f"🩶 {msg}")
        except Exception:
            pass

# ================== HEALTH SERVER ==================
async def start_health_server():
    """Health-check HTTP (Render должен видеть открытый PORT)."""
    port = int(os.environ.get("PORT", "10000"))  # ВАЖНО: тот же PORT
    health_app = web.Application()
    health_app.add_routes([web.get("/", lambda _: web.Response(text="OK"))])
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log(f"[Init] Health server listening on port {port}")

# ================== EXCH INIT/CLOSE ==================
async def init_exchanges():
    """Инициализирует биржи, заполняет exchanges и exchange_status."""
    global exchanges, exchange_status
    exchanges, exchange_status = {}, {}

    async def try_init(name, ex_class, **kwargs):
        if not any(kwargs.values()):
            exchange_status[name] = {"status": "⚪", "error": "нет API-ключей", "ex": None}
            log(f"{name.upper()} ⚪ пропущен — нет API-ключей")
            return None
        ex = None
        try:
            ex = ex_class(kwargs)
            await ex.load_markets()
            exchange_status[name] = {"status": "✅", "error": None, "ex": ex}
            log(f"{name.upper()} ✅ инициализирован")
            return ex
        except Exception as e:
            err = str(e).split("\n")[0][:180]
            exchange_status[name] = {"status": "❌", "error": err, "ex": None}
            log(f"{name.upper()} ❌ {err}")
            try:
                if ex:
                    await ex.close()
            except Exception:
                pass
            return None

    candidates = {
        "mexc":   (ccxt.mexc,   {"apiKey": env_vars.get("MEXC_API_KEY"),   "secret": env_vars.get("MEXC_API_SECRET")}),
        "bitget": (ccxt.bitget, {"apiKey": env_vars.get("BITGET_API_KEY"), "secret": env_vars.get("BITGET_API_SECRET"), "password": env_vars.get("BITGET_API_PASSPHRASE")}),
        "kucoin": (ccxt.kucoin, {"apiKey": env_vars.get("KUCOIN_API_KEY"), "secret": env_vars.get("KUCOIN_API_SECRET"), "password": env_vars.get("KUCOIN_API_PASS")}),
        "okx":    (ccxt.okx,    {"apiKey": env_vars.get("OKX_API_KEY"),    "secret": env_vars.get("OKX_API_SECRET"),    "password": env_vars.get("OKX_API_PASS")}),
        "huobi": (ccxt.huobi, {"apiKey": ..., "secret": ..., "options": {"defaultType": "spot"}}),
        "bigone": (ccxt.bigone, {"apiKey": env_vars.get("BIGONE_API_KEY"), "secret": env_vars.get("BIGONE_API_SECRET")}),
        "bybit":  (ccxt.bybit,  {"apiKey": env_vars.get("BYBIT_API_KEY"),  "secret": env_vars.get("BYBIT_API_SECRET")}),
    }

    for name, (cls, params) in candidates.items():
        ex = await try_init(name, cls, **params)
        if ex:
            exchanges[name] = ex

    active = [k for k, v in exchange_status.items() if v["status"] == "✅"]
    total = len(exchange_status)
    log(f"Активные биржи: {', '.join(active) if active else '—'}")
    log(f"Инициализация завершена: {len(active)}/{total} бирж активны.")

async def close_all_exchanges():
    for name, ex in exchanges.items():
        try:
            await ex.close()
            log(f"{name.upper()} закрыт ✅")
        except Exception as e:
            log(f"{name.upper()} ошибка закрытия: {e}")

# ================== SCANNER ==================
async def get_top_symbols(exchange, top_n=100):
    """ТОП по quoteVolume, только .../USDT пары, без символов c ':'."""
    tickers = await exchange.fetch_tickers()
    pairs = [(s, t.get("quoteVolume", 0)) for s, t in tickers.items()
             if s.endswith("/USDT") and ":" not in s]
    pairs.sort(key=lambda x: x[1] or 0, reverse=True)
    return [s for s, _ in pairs[:top_n]]

async def scan_all_pairs(chat_id: int | None = None):
    """Скан всех доступных бирж, возврат топ сигналов (до 10)."""
    results = []
    symbols = set()
    FEES = {
        "mexc": 0.001, "bitget": 0.001, "kucoin": 0.001,
        "okx": 0.001, "huobi": 0.001, "bigone": 0.001, "bybit": 0.001
    }

    # собрать унион топ-100 по каждой активной бирже
    for name, ex in exchanges.items():
        try:
            tops = await get_top_symbols(ex)
            symbols.update(tops)
        except Exception as e:
            await send_log(chat_id, f"{name} ошибка топ-листа: {e}")

    await send_log(chat_id, f"Начал скан {len(symbols)} пар...")

    # пробежаться по символам, собрать цены на биржах
    for i, symbol in enumerate(symbols):
        prices, vols = {}, {}
        for name, ex in exchanges.items():
            try:
                t = await ex.fetch_ticker(symbol)
                bid, ask = t.get("bid"), t.get("ask")
                if bid and ask:
                    prices[name] = (bid + ask) / 2
                    vols[name] = t.get("quoteVolume", 0) or 0
            except Exception:
                continue

        if len(prices) < 2:
            continue

        min_p, max_p = min(prices.values()), max(prices.values())
        raw_spread_pct = (max_p - min_p) / min_p * 100
        if raw_spread_pct < MIN_SPREAD * 0.6:  # лёгкий ранний отсев
            continue

        min_vol = min(vols.values()) if vols else 0
        if min_vol < MIN_VOLUME_1H:
            continue

        cheap = min(prices, key=prices.get)
        expensive = max(prices, key=prices.get)
        # оценка профита с учётом комсы TAKER на обеих
        profit_pct = (prices[expensive] / prices[cheap] - 1) * 100 \
                     - (FEES.get(cheap, 0.001) + FEES.get(expensive, 0.001)) * 100

        if profit_pct < MIN_SPREAD:
            continue

        results.append({
            "symbol": symbol,
            "cheap": cheap,
            "expensive": expensive,
            "price_cheap": round(prices[cheap], 6),
            "price_expensive": round(prices[expensive], 6),
            "spread": round(profit_pct, 2),
            "volume_1h": round(min_vol / 1_000_000, 2)  # M$
        })

        if chat_id in scanlog_enabled and i % 10 == 0:
            await send_log(chat_id, f"Скан {i}/{len(symbols)}...")

    results.sort(key=lambda x: x["spread"], reverse=True)
    await send_log(chat_id, f"Готово. Найдено {len(results)} сигналов.")
    return results[:10]

# ================== BUY FLOW ==================
def get_buy_keyboard(sig: dict) -> InlineKeyboardMarkup:
    # передаём обе биржи и символ — пересчитаем точно после ввода суммы
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
        return await q.edit_message_text("❌ Биржа недоступна.")

    chat_id = q.message.chat.id
    pending_trades[chat_id] = {"cheap": cheap, "sell": sell, "symbol": symbol}

    await q.edit_message_text(
        f"💰 Введите сумму сделки в USDT для {symbol} на {cheap.upper()} (например: 25)"
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
    except Exception:
        return await update.message.reply_text("❌ Введите положительное число, например: 25")

    cheap, sell, symbol = step["cheap"], step["sell"], step["symbol"]
    ex_buy = exchanges.get(cheap)
    ex_sell = exchanges.get(sell)

    try:
        t_buy = await ex_buy.fetch_ticker(symbol)    # ask
        t_sell = await ex_sell.fetch_ticker(symbol)  # bid
        buy_price = t_buy["ask"]
        sell_price = t_sell["bid"]
        if not buy_price or not sell_price:
            return await update.message.reply_text("⚠️ Недостаточно стакана для расчёта.")

        FEES = {"mexc": 0.001, "bitget": 0.001, "kucoin": 0.001, "okx": 0.001, "huobi": 0.001, "bigone": 0.001, "bybit": 0.001}
        profit_pct = (sell_price / buy_price - 1) * 100 \
                     - (FEES.get(cheap, 0.001) + FEES.get(sell, 0.001)) * 100
        profit_usd = round(usdt * profit_pct / 100, 2)

        amount = round(usdt / buy_price, 6)
        step["usdt"] = usdt
        pending_trades[chat_id] = step

        msg = (f"*{symbol}*\n"
               f"Покупка: {cheap.upper()} по {buy_price}\n"
               f"Продажа: {sell.upper()} по {sell_price}\n"
               f"Сумма: {usdt} USDT → ≈ {amount} {symbol.split('/')[0]}\n"
               f"💹 Примерный профит: *{profit_pct:.2f}% (~{profit_usd} USDT)*\n\n"
               f"Подтвердить покупку?")

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{cheap}:{symbol}:{usdt}"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel")
        ]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при расчёте: {e}")

async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, exch, symbol, usdt = q.data.split(":")
        usdt = float(usdt)
        ex = exchanges[exch]
        bal = await ex.fetch_balance()
        free = bal.get("USDT", {}).get("free", 0.0)
        if free < usdt:
            return await q.edit_message_text(f"❌ Недостаточно средств ({free:.2f} USDT).")

        t = await ex.fetch_ticker(symbol)
        if not t.get("ask"):
            return await q.edit_message_text("⚠️ Нет цены ASK для покупки.")
        amount = round(usdt / t["ask"], 6)

        order = await ex.create_market_buy_order(symbol, amount)
        await q.edit_message_text(
            f"✅ Куплено {amount} {symbol.split('/')[0]} на {exch.upper()} ({usdt} USDT)\nID: {order.get('id','—')}"
        )
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка покупки: {e}")

async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending_trades.pop(q.message.chat.id, None)
    await q.edit_message_text("❌ Покупка отменена.")

# ================== COMMANDS ==================
async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Я на связи! Версия: {VERSION}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not exchange_status:
        return await update.message.reply_text("⚠️ Биржи ещё не инициализированы.")
    lines = ["📊 *Статус подключений:*"]
    for name, data in exchange_status.items():
        status = data.get("status", "⚪")
        error = data.get("error")
        if error:
            lines.append(f"{name.upper()}: {status} — {error}")
        else:
            lines.append(f"{name.upper()}: {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["chat_id"] = update.effective_chat.id
    context.chat_data["autoscan"] = True
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")
    try:
        await app.bot.send_message(update.effective_chat.id, START_SUMMARY, parse_mode="Markdown")
    except Exception:
        pass

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Сканирую пары…")
    results = await scan_all_pairs(update.effective_chat.id)
    if not results:
        return await msg.edit_text("Нет сигналов.")
    await msg.delete()
    for sig in results:
        text = (f"*{sig['symbol']}*\n"
                f"Профит: *{sig['spread']}%*\n"
                f"Купить: {sig['cheap'].upper()} {sig['price_cheap']}\n"
                f"Продать: {sig['expensive'].upper()} {sig['price_expensive']}\n"
                f"Объём 1ч: {sig['volume_1h']}M$")
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_buy_keyboard(sig))

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["💰 Баланс по биржам:"]
    for name, st in exchange_status.items():
        ex = st.get("ex")
        if st.get("status") == "✅" and ex:
            try:
                b = await ex.fetch_balance()
                free = b.get("USDT", {}).get("free", 0.0)
                total = b.get("USDT", {}).get("total", free)
                lines.append(f"{name.upper()} ✅ {free:.2f} / {total:.2f}")
            except Exception as e:
                lines.append(f"{name.upper()} ⚠️ ошибка: {e}")
        else:
            reason = st.get("error") or "неактивна"
            lines.append(f"{name.upper()} {st.get('status','⚪')} {reason}")
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
    await update.message.reply_text("Автоскан ❌ выключен для этого чата.")

# ================== AUTOSCAN ==================
async def auto_scan():
    if not app:
        return
    # пройдёмся по всем чатам, у кого включён автоскан
    for data in app.chat_data.values():
        if data.get("autoscan"):
            chat_id = data["chat_id"]
            res = await scan_all_pairs(chat_id)
            if not res:
                continue
            for sig in res:
                text = (f"*{sig['symbol']}*\n"
                        f"Профит: *{sig['spread']}%*\n"
                        f"Купить: {sig['cheap'].upper()} {sig['price_cheap']}\n"
                        f"Продать: {sig['expensive'].upper()} {sig['price_expensive']}\n"
                        f"Объём 1ч: {sig['volume_1h']}M$")
                await app.bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=get_buy_keyboard(sig))

# ================== KEEP ALIVE ==================
async def keep_alive():
    while True:
        await asyncio.sleep(3600)

# ================== ENTRY POINT ==================
import signal

async def main_async():
    try:
        await start_health_server()
        await init_exchanges()

        global app
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # === Telegram уведомление ===
        CHAT_ID = env_vars.get("CHAT_ID")
        if CHAT_ID:
            try:
                await app.bot.send_message(int(CHAT_ID), f"✅ Arbitrage Scanner {VERSION} запущен на Render")
                log(f"Отправлено уведомление в Telegram ({CHAT_ID})")
            except Exception as e:
                log(f"⚠️ Не удалось отправить сообщение при старте: {e}")

        # === Хендлеры ===
        handlers = [
            ("start", start),
            ("info", info),
            ("scan", scan_cmd),
            ("balance", balance_cmd),
            ("scanlog", scanlog_cmd),
            ("status", status_cmd),
            ("ping", ping_cmd),
            ("stop", stop_cmd),
        ]
        for cmd, func in handlers:
            app.add_handler(CommandHandler(cmd, func))

        app.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^buy:"))
        app.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^confirm:"))
        app.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=r"^cancel$"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input))

        scheduler = AsyncIOScheduler()
        scheduler.add_job(auto_scan, "interval", seconds=SCAN_INTERVAL)
        scheduler.start()

        # === Webhook ===
        port = int(os.environ.get("PORT", "10000"))
        host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        webhook_url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"
        await app.bot.set_webhook(webhook_url, drop_pending_updates=True)

        log(f"✅ Arbitrage Scanner {VERSION} запущен. Порт: {port}")
        log(f"Webhook установлен: {webhook_url}")


        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(close_all_exchanges()))
        except NotImplementedError:
            
    # Windows / Render fallback (без signal handlers)
    log("⚠️ Signal handlers не поддерживаются в этой среде.")


        # === Запускаем webhook (главный цикл) ===
        await app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=webhook_url,
        )

    except Exception as e:
        log(f"❌ Ошибка в main_async: {e}")
    finally:
        await close_all_exchanges()
        log("🧹 Завершение работы.")


if __name__ == "__main__":
    asyncio.run(main_async())

