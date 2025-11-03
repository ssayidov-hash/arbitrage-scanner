# ================================================================
# ARBITRAGE SCANNER v5.6 — Render Edition (Stable)
# Exchanges: MEXC / BITGET
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
MIN_SPREAD = 1.2
MIN_VOLUME_1H = 500_000
SCAN_INTERVAL = 120
VERSION = "v5.6-stable"

# ================== ENV ==================
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
app = None
scanlog_enabled = set()

# ================== TEXTS ==================
INFO_TEXT = f"""*Arbitrage Scanner {VERSION}*

Сканирует *MEXC / BITGET* по USDT-парам.
Фильтры: профит ≥ {MIN_SPREAD}% и объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/1ч.
Автоскан каждые {SCAN_INTERVAL} сек.

Команды:
/start — запустить автоскан
/scan — ручной поиск
/balance — баланс
/status — статус подключений
/scanlog — лог сканирования (вкл/выкл)
/stop — выключить автоскан
/info — помощь
/ping — проверить связь
"""

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

async def send_log(chat_id, msg):
    if app and chat_id in scanlog_enabled:
        try:
            await app.bot.send_message(chat_id, f"🩶 {msg}")
        except:
            pass

# ================== HEALTH ==================
async def start_health_server():
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
    global exchanges, exchange_status
    exchanges, exchange_status = {}, {}

    async def try_init(name, ex_class, **kwargs):
        if not any(kwargs.values()):
            exchange_status[name] = {"status": "⚪", "error": "нет API-ключей"}
            log(f"{name.upper()} ⚪ пропущен — нет ключей")
            return None
        try:
            ex = ex_class(kwargs)
            await ex.load_markets()
            exchange_status[name] = {"status": "✅", "error": None}
            log(f"{name.upper()} ✅ инициализирован")
            return ex
        except Exception as e:
            err = str(e).split("\n")[0][:180]
            exchange_status[name] = {"status": "❌", "error": err}
            log(f"{name.upper()} ❌ {err}")
            return None

    candidates = {
        "mexc": (ccxt.mexc, {"apiKey": env_vars["MEXC_API_KEY"], "secret": env_vars["MEXC_API_SECRET"]}),
        "bitget": (ccxt.bitget, {"apiKey": env_vars["BITGET_API_KEY"], "secret": env_vars["BITGET_API_SECRET"], "password": env_vars["BITGET_API_PASSPHRASE"]}),
    }

    for name, (cls, params) in candidates.items():
        ex = await try_init(name, cls, **params)
        if ex:
            exchanges[name] = ex

    active = [k for k, v in exchange_status.items() if v["status"] == "✅"]
    log(f"Активные биржи: {', '.join(active) if active else '—'}")

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
    pairs = [(s, t.get("quoteVolume", 0)) for s, t in tickers.items() if s.endswith("/USDT") and ":" not in s]
    pairs.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pairs[:top_n]]

async def scan_all_pairs(chat_id=None):
    results = []
    symbols = set()
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

    results.sort(key=lambda x: x["spread"], reverse=True)
    await send_log(chat_id, f"Готово. Найдено {len(results)} сигналов.")
    return results[:10]

# ================== COMMANDS ==================
async def start_cmd(update, context):
    context.chat_data["chat_id"] = update.effective_chat.id
    context.chat_data["autoscan"] = True
    await update.message.reply_text(INFO_TEXT, parse_mode="Markdown")

async def scan_cmd(update, context):
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
        await update.message.reply_text(text, parse_mode="Markdown")

async def balance_cmd(update, context):
    lines = ["💰 Баланс по биржам:"]
    for name, ex in exchanges.items():
        try:
            b = await ex.fetch_balance()
            free = b["USDT"]["free"]
            total = b["USDT"]["total"]
            lines.append(f"{name.upper()} ✅ {free:.2f}/{total:.2f}")
        except Exception as e:
            lines.append(f"{name.upper()} ❌ {e}")
    await update.message.reply_text("\n".join(lines))

async def status_cmd(update, context):
    lines = ["📊 *Статус бирж:*"]
    for name, st in exchange_status.items():
        lines.append(f"{name.upper()} {st['status']} {st.get('error','') or ''}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def ping_cmd(update, context):
    await update.message.reply_text("✅ Я на связи!")

async def stop_cmd(update, context):
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
                text = (f"*{sig['symbol']}*\nПрофит: *{sig['spread']}%*\n"
                        f"Купить: {sig['cheap'].upper()} {sig['price_cheap']}\n"
                        f"Продать: {sig['expensive'].upper()} {sig['price_expensive']}\n"
                        f"Объём 1ч: {sig['volume_1h']}M$")
                await app.bot.send_message(chat_id, text, parse_mode="Markdown")

# ================== MAIN ==================
async def main_async():
    try:
        await start_health_server()
        await init_exchanges()

        global app
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # === Handlers ===
        for cmd, func in [
            ("start", start_cmd), ("scan", scan_cmd),
            ("balance", balance_cmd), ("status", status_cmd),
            ("ping", ping_cmd), ("stop", stop_cmd),
        ]:
            app.add_handler(CommandHandler(cmd, func))

        scheduler = AsyncIOScheduler()
        scheduler.add_job(auto_scan, "interval", seconds=SCAN_INTERVAL)
        scheduler.start()

        port = int(os.environ.get("PORT", "10000"))
        host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        if not host:
            raise RuntimeError("Нет RENDER_EXTERNAL_HOSTNAME")

        webhook_url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"
        await app.bot.set_webhook(webhook_url, drop_pending_updates=True)

        log(f"✅ Arbitrage Scanner {VERSION} запущен. Порт: {port}")
        log(f"Webhook установлен: {webhook_url}")

        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(close_all_exchanges()))
        except Exception:
            log("⚠️ Signal handlers не поддерживаются в этой среде.")

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
