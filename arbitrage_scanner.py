# ================================================================
#  ARBITRAGE SCANNER v5.9-STABLE
#  Multi-Exchange Arbitrage Bot (MEXC + BITGET + BIGONE + OKX + BINANCE + KUCOIN + BYBIT + GATE + HTX + KRAKEN + CRYPTO)
#  Render + Telegram Webhook (PTB 21.6)
#  © 2025
# ================================================================
#
# 🔹 Описание:
#   Универсальный бот для поиска арбитражных возможностей между крупными биржами.
#   Работает через Telegram Webhook и полностью совместим с Render.
#   Отображает топ арбитражных пар с учётом фильтров по объёму и профиту.
#
# 🔹 Telegram-команды:
#   /start    — приветствие и параметры
#   /scan     — ручной скан (топ-10 сигналов)
#   /status   — состояние бирж
#   /scanlog  — включить/выключить debug
#   /info     — подробная справка
# ================================================================

import os
import sys
import asyncio
import nest_asyncio
from datetime import datetime
import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== CONFIG ==================
MIN_SPREAD = 1.2
MIN_VOLUME_1H = 500_000
SCAN_INTERVAL = 120
VERSION = "v5.9-stable"

# ================== ENV VARS ==================
env_vars = {
    # === Основные ===
    "MEXC_API_KEY": os.getenv("MEXC_API_KEY"),
    "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET"),

    "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
    "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
    "BITGET_API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE"),

    "BIGONE_API_KEY": os.getenv("BIGONE_API_KEY"),
    "BIGONE_API_SECRET": os.getenv("BIGONE_API_SECRET"),

    # === Дополнительные ===
    "BINANCE_API_KEY": os.getenv("BINANCE_API_KEY"),
    "BINANCE_API_SECRET": os.getenv("BINANCE_API_SECRET"),

    "OKX_API_KEY": os.getenv("OKX_API_KEY"),
    "OKX_API_SECRET": os.getenv("OKX_API_SECRET"),

    "KUCOIN_API_KEY": os.getenv("KUCOIN_API_KEY"),
    "KUCOIN_API_SECRET": os.getenv("KUCOIN_API_SECRET"),

    "BYBIT_API_KEY": os.getenv("BYBIT_API_KEY"),
    "BYBIT_API_SECRET": os.getenv("BYBIT_API_SECRET"),

    "GATE_API_KEY": os.getenv("GATE_API_KEY"),
    "GATE_API_SECRET": os.getenv("GATE_API_SECRET"),

    "HTX_API_KEY": os.getenv("HTX_API_KEY"),
    "HTX_API_SECRET": os.getenv("HTX_API_SECRET"),

    "KRAKEN_API_KEY": os.getenv("KRAKEN_API_KEY"),
    "KRAKEN_API_SECRET": os.getenv("KRAKEN_API_SECRET"),

    "CRYPTO_API_KEY": os.getenv("CRYPTO_API_KEY"),
    "CRYPTO_API_SECRET": os.getenv("CRYPTO_API_SECRET"),

    # === Telegram ===
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
scanlog_enabled = set()
app: Application | None = None

# ================== UTILS ==================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ================== EXCHANGES ==================
async def init_exchanges():
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
                "options": {"defaultType": "spot"}
            })
            await ex.load_markets()
            exchange_status[name] = {"status": "✅", "error": None, "ex": ex}
            log(f"{name.upper()} ✅ инициализирован 🟢")
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
        "bigone": (ccxt.bigone, {
            "apiKey": env_vars["BIGONE_API_KEY"],
            "secret": env_vars["BIGONE_API_SECRET"]
        }),
        "binance": (ccxt.binance, {
            "apiKey": env_vars["BINANCE_API_KEY"],
            "secret": env_vars["BINANCE_API_SECRET"]
        }),
        "okx": (ccxt.okx, {
            "apiKey": env_vars["OKX_API_KEY"],
            "secret": env_vars["OKX_API_SECRET"]
        }),
        "kucoin": (ccxt.kucoin, {
            "apiKey": env_vars["KUCOIN_API_KEY"],
            "secret": env_vars["KUCOIN_API_SECRET"]
        }),
        #"bybit": (ccxt.bybit, {
        #    "apiKey": env_vars["BYBIT_API_KEY"],
        #   "secret": env_vars["BYBIT_API_SECRET"]
        #}),
        "gate": (ccxt.gate, {
            "apiKey": env_vars["GATE_API_KEY"],
            "secret": env_vars["GATE_API_SECRET"]
        }),
        "htx": (ccxt.huobi, {
            "apiKey": env_vars["HTX_API_KEY"],
            "secret": env_vars["HTX_API_SECRET"]
        }),
        "kraken": (ccxt.kraken, {
            "apiKey": env_vars["KRAKEN_API_KEY"],
            "secret": env_vars["KRAKEN_API_SECRET"]
        }),
        "crypto": (ccxt.cryptocom, {
            "apiKey": env_vars["CRYPTO_API_KEY"],
            "secret": env_vars["CRYPTO_API_SECRET"]
        }),
    }

    for name, (cls, params) in candidates.items():
        ex = await try_init(name, cls, **params)
        if ex:
            exchanges[name] = ex

    active = [k for k, v in exchange_status.items() if v["status"] == "✅"]
    log(f"Активные биржи: {', '.join(active)} 🟩")
    log(f"Инициализация завершена: {len(active)}/{len(exchange_status)} активны.")

async def close_all_exchanges():
    for name, ex in exchanges.items():
        try:
            await ex.close()
            log(f"{name.upper()} закрыт ✅")
        except Exception as e:
            log(f"{name.upper()} ошибка закрытия: {e}")

# ================== TELEGRAM COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🤖 *ARBITRAGE SCANNER {VERSION}*\n\n"
        f"Подключено бирж: {len(exchanges)}\n"
        f"Фильтры:\n"
        f"• Мин. профит: {MIN_SPREAD:.1f}%\n"
        f"• Мин. объём 1ч: {MIN_VOLUME_1H/1000:.0f}k$\n"
        f"• Интервал автосканирования: {SCAN_INTERVAL} сек.\n\n"
        "Доступные команды:\n"
        "/scan — ручной поиск арбитражных пар\n"
        "/status — состояние подключений\n"
        "/info — подробная справка\n"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 *Статус подключений:*"]
    for name, st in exchange_status.items():
        emoji = "🟢" if st["status"] == "✅" else "🔴" if st["status"] == "❌" else "⚪"
        lines.append(f"{emoji} {name.upper()} — {st['status']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"*ARBITRAGE SCANNER {VERSION} — подробная справка*\n\n"
        "🔹 *Описание:*\n"
        "Бот сканирует биржи на предмет ценовых расхождений по USDT-парам и "
        "ищет арбитражные возможности с учётом объёма и комиссий.\n\n"
        "🔹 *Параметры:*\n"
        f"• Мин. профит: {MIN_SPREAD}%\n"
        f"• Мин. объём 1ч: {MIN_VOLUME_1H/1000:.0f}k$\n"
        f"• Интервал скана: {SCAN_INTERVAL} сек\n\n"
        "🔹 *Логика работы:*\n"
        "1. Получает топ ликвидных пар на каждой бирже\n"
        "2. Сравнивает средние цены (bid/ask)\n"
        "3. Вычисляет спред и реальный профит после комиссий\n"
        "4. Отбирает пары с профитом ≥ MIN_SPREAD и достаточным объёмом\n\n"
        "🔹 *Команды:*\n"
        "/start — параметры\n"
        "/scan — ручной поиск\n"
        "/status — подключение к биржам\n"
        "/info — справка\n\n"
        "🔹 *Пример:*\n"
        "`BTC/USDT | Купить на MEXC (67000.2) → Продать на Bitget (67750.3) | +1.12%`\n\n"
        "🔹 *Рекомендации:*\n"
        "• Ставь SCAN_INTERVAL ≥ 120 сек для стабильности\n"
        "• Добавляй только активные API ключи\n"
        "• Render может завершить процесс при избыточной нагрузке — оптимизируй top_n до 50\n"
    )
await update.message.reply_text(
    text,
    parse_mode="HTML",
    disable_web_page_preview=True
)

# ================== MAIN ==================
async def main():
    print("🚀 INIT START (Render + Telegram webhook)", flush=True)

    # === Инициализация бирж ===
    await init_exchanges()

    global app
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # === Команды ===
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("✅ Бот активен.")))
    app.add_handler(CommandHandler("info", info))  # функция info с описанием
    # при необходимости добавь остальные handlers (scan, status, balance, stop и т.д.)

    # === Планировщик ===
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: None, "interval", seconds=SCAN_INTERVAL)
    scheduler.start()

    # === Параметры webhook ===
    PORT = int(os.getenv("PORT", "10000"))
    EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL", "")
    if not EXTERNAL_URL:
        raise SystemExit("❌ Нет RENDER_EXTERNAL_URL / WEBHOOK_URL (Render HTTPS URL)")

    WEBHOOK_PATH = f"/{TELEGRAM_BOT_TOKEN}"
    WEBHOOK_URL = f"{EXTERNAL_URL.rstrip('/')}{WEBHOOK_PATH}"
    WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "") or None

    print(f"🌐 Webhook URL: {WEBHOOK_URL}", flush=True)
    print(f"🔒 Secret set: {'yes' if WEBHOOK_SECRET else 'no'}", flush=True)
    log(f"🌐 Listening on 0.0.0.0:{PORT} for Telegram webhook...")

    # === Лог запуска ===
    log("===========================================================")
    log(f"✅ Arbitrage Scanner {VERSION} запущен (Render webhook mode)")
    log(f"Порт: {PORT}")
    log(f"Фильтры: профит ≥ {MIN_SPREAD}% | объём ≥ {MIN_VOLUME_1H/1000:.0f}k$/1ч")
    log(f"Автоскан каждые {SCAN_INTERVAL} сек (если включён)")
    log("🌐 Webhook сервер запущен и слушает входящие обновления Telegram.")
    log("===========================================================")

    # === Запуск webhook с безопасным завершением ===
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
        log("🧹 Завершение работы — соединения закрыты.")


if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.run(main())
