# ================================================================
#  ARBITRAGE SCANNER v5.8-STABLE
#  Multi-Exchange Arbitrage Bot (MEXC + BITGET + BIGONE)
#  Render + Telegram Webhook (PTB 21.6)
#  © 2025
# ================================================================
#
# 🔹 Telegram-команды:
#   /start — краткая справка и запуск автосканирования
#   /scan — разовый скан (топ-10 сигналов)
#   /status — состояние подключений к биржам
#   /balance — балансы по биржам
#   /stop — остановить автоскан
#   /info — подробная справка
#   /ping — проверить связь
#
# ================================================================

import os
import sys
import asyncio
import nest_asyncio
from datetime import datetime
import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== CONFIG ==================
MIN_SPREAD = 1.2
MIN_VOLUME_1H = 500_000
SCAN_INTERVAL = 120
VERSION = "v5.8-stable"

# ================== ENV VARS ==================
env_vars = {
    "MEXC_API_KEY": os.getenv("MEXC_API_KEY"),
    "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET"),
    "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
    "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
    "BITGET_API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE"),
    "BIGONE_API_KEY": os.getenv("BIGONE_API_KEY"),
    "BIGONE_API_SECRET": os.getenv("BIGONE_API_SECRET"),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "CHAT_ID": os.getenv("CHAT_ID"),
}
TELEGRAM_BOT_TOKEN = env_vars["TELEGRAM_BOT_TOKEN"]
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ Нет TELEGRAM_BOT_TOKEN")

# ================== PREPARE LOOP ==================
nest_asyncio.apply()

# ================== GLOBALS ==================
exchanges = {}
exchange_status = {}
app: Application | None = None
scheduler: AsyncIOScheduler | None = None

# ================== LOG ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def color_status(symbol):
    if symbol == "✅":
        return "🟢"
    if symbol == "❌":
        return "🔴"
    if symbol == "⚪":
        return "⚪"
    return symbol

# ================== EXCHANGES ==================
async def init_exchanges():
    global exchanges, exchange_status
    exchanges, exchange_status = {}, {}

    async def try_init(name, ex_class, **kwargs):
        if not all(kwargs.values()):
            exchange_status[name] = {"status": "⚪", "error": "нет API-ключей", "ex": None}
            log(f"{name.upper()} ⚪ пропущен — нет API-ключей")
            return None
        try:
            ex = ex_class({
                **kwargs,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })
            await ex.load_markets()
            exchange_status[name] = {"status": "✅", "error": None, "ex": ex}
            log(f"{name.upper()} ✅ инициализирован {color_status('✅')}")
            return ex
        except Exception as e:
            err = str(e).split("\n")[0][:120]
            exchange_status[name] = {"status": "❌", "error": err, "ex": None}
            log(f"{name.upper()} ❌ {err} {color_status('❌')}")
            return None

    pairs = {
        "mexc": (ccxt.mexc, {
            "apiKey": env_vars["MEXC_API_KEY"],
            "secret": env_vars["MEXC_API_SECRET"]
        }),
        "bitget": (ccxt.bitget, {
            "apiKey": env_vars["BITGET_API_KEY"],
            "secret": env_vars["BITGET_API_SECRET"],
            "password": env_vars["BITGET_API_PASSPHRASE"]
        }),
        "bigone": (ccxt.bigone, {
            "apiKey": env_vars["BIGONE_API_KEY"],
            "secret": env_vars["BIGONE_API_SECRET"]
        }),
    }

    for name, (cls, params) in pairs.items():
        await try_init(name, cls, **params)

    active = [k for k, v in exchange_status.items() if v["status"] == "✅"]
    log(f"Активные биржи: {', '.join(active) if active else '—'} 🟩")
    log(f"Инициализация завершена: {len(active)}/{len(exchange_status)} активны.")

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

async def scan_all_pairs():
    results = []
    FEES = {"mexc": 0.001, "bitget": 0.001, "bigone": 0.001}
    symbols = set()

    for name, ex in exchanges.items():
        try:
            tops = await get_top_symbols(ex)
            symbols.update(tops)
        except Exception as e:
            log(f"{name} ошибка топ-листа: {e}")

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
    return results[:10]

# ================== TELEGRAM COMMANDS ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"*ARBITRAGE SCANNER {VERSION}*\n\n"
        f"Фильтры: профит ≥ {MIN_SPREAD}% | объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/1ч\n"
        f"Автоскан каждые {SCAN_INTERVAL} сек.\n\n"
        "Доступные команды:\n"
        "/scan — ручной скан\n"
        "/status — статус подключений\n"
        "/balance — балансы\n"
        "/stop — выключить автоскан\n"
        "/info — справка\n"
        "/ping — проверить связь",
        parse_mode="Markdown"
    )

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"*📘 ARBITRAGE SCANNER {VERSION}*\n\n"
        "1️⃣ *Описание:*\n"
        "Бот сканирует пары USDT на биржах *MEXC*, *Bitget* и *BigONE*, "
        "ищет арбитражные возможности между ними и рассчитывает потенциальную прибыль.\n\n"
        "2️⃣ *Параметры:*\n"
        f"• Минимальный профит: ≥ {MIN_SPREAD}%\n"
        f"• Минимальный объём (1ч): ≥ {MIN_VOLUME_1H/1000:.0f}k USD\n"
        f"• Интервал автосканирования: {SCAN_INTERVAL} сек\n"
        "• Поддерживаемые биржи: MEXC / Bitget / BigONE\n\n"
        "3️⃣ *Логика работы:*\n"
        "• Получает топ ликвидных USDT-пар\n"
        "• Сравнивает цены покупки и продажи между биржами\n"
        "• Проверяет, превышает ли разница заданный порог профита\n"
        "• Фильтрует пары с недостаточным объёмом\n"
        "• Формирует и отправляет сигналы в Telegram\n\n"
        "4️⃣ *Команды и формат ввода:*\n"
        "/start — запустить и показать параметры\n"
        "/scan — разовый поиск сигналов\n"
        "/status — подключенные биржи\n"
        "/balance — балансы\n"
        "/stop — остановить авто\n"
        "/info — полная справка\n"
        "/ping — проверить связь\n\n"
        "5️⃣ *Пример сигнала:*\n"
        "`BTC/USDT`\n"
        "Профит: 1.45%\n"
        "Купить: MEXC 67200.5\n"
        "Продать: Bitget 68180.2\n"
        "Объём 1ч: 12.3M$\n\n"
        "6️⃣ *Рекомендации:*\n"
        "• Держите профит ≥1%, объём ≥500k для реалистичных сделок\n"
        "• При большом числе сигналов ориентируйтесь на пары с max объёмом\n"
        "• Обновляйте API-ключи каждые 3–6 месяцев\n"
        "• Храните ключи только в Render Environment\n"
        "• Для продвинутых стратегий добавьте KuCoin или Binance\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Я на связи! Версия: {VERSION}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 *Статус подключений:*"]
    for name, st in exchange_status.items():
        lines.append(f"{name.upper()} {st['status']} {color_status(st['status'])} {st.get('error','')}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["💰 Балансы по биржам:"]
    for name, st in exchange_status.items():
        ex = st["ex"]
        if st["status"] == "✅" and ex:
            try:
                b = await ex.fetch_balance()
                free = b["USDT"]["free"]
                lines.append(f"{name.upper()} ✅ {free:.2f} USDT")
            except Exception as e:
                lines.append(f"{name.upper()} ⚠️ ошибка: {str(e)[:50]}")
        else:
            lines.append(f"{name.upper()} {st['status']} {st.get('error','')}")
    await update.message.reply_text("\n".join(lines))

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔎 Ищу арбитражные сигналы...")
    res = await scan_all_pairs()
    if not res:
        await update.message.reply_text("⏳ Сигналов нет.")
    else:
        for sig in res:
            txt = (
                f"*{sig['symbol']}*\n"
                f"Профит: *{sig['spread']}%*\n"
                f"Купить: {sig['cheap'].upper()} {sig['price_cheap']}\n"
                f"Продать: {sig['expensive'].upper()} {sig['price_expensive']}\n"
                f"Объём 1ч: {sig['volume_1h']}M$"
            )
            await update.message.reply_text(txt, parse_mode="Markdown")

# ================== AUTO SCAN ==================
async def auto_scan():
    chat_id = env_vars.get("CHAT_ID")
    if not chat_id:
        return
    results = await scan_all_pairs()
    if not results:
        await app.bot.send_message(chat_id, "⏳ Нет подходящих арбитражных пар.")
    else:
        msg = ["💹 *Топ-арбитражные сигналы:*"]
        for sig in results:
            msg.append(
                f"{sig['symbol']} — {sig['spread']}% | "
                f"{sig['cheap'].upper()} → {sig['expensive'].upper()}"
            )
        await app.bot.send_message(chat_id, "\n".join(msg), parse_mode="Markdown")

# ================== MAIN ==================
async def main():
    print("🚀 INIT START (Render + Telegram webhook)", flush=True)
    await init_exchanges()

    global app, scheduler
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_scan, "interval", seconds=SCAN_INTERVAL)
    scheduler.start()

    PORT = int(os.getenv("PORT", "10000"))
    EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL", "")
    if not EXTERNAL_URL:
        raise SystemExit("❌ Нет RENDER_EXTERNAL_URL / WEBHOOK_URL")

    WEBHOOK_PATH = f"/{TELEGRAM_BOT_TOKEN}"
    WEBHOOK_URL = f"{EXTERNAL_URL.rstrip('/')}{WEBHOOK_PATH}"

    print(f"🌐 Webhook URL: {WEBHOOK_URL}", flush=True)
    log(f"✅ Arbitrage Scanner {VERSION} запущен (Render webhook mode)")

    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except RuntimeError:
        import nest_asyncio
        nest_asyncio.apply()
        asyncio.run(main())

