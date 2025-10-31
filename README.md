\# Arbitrage Scanner v5.1 (Bybit • MEXC • Bitget)



\*\*Автоматический сканер арбитража USDT-пар\*\*  

Спред ≥1.2% • Объём 1ч ≥500k$ • Только покупка на дешёвой бирже



---



\## Особенности

\- Автоскан каждые 2 мин

\- Таймер стабильности (`6 мин | 1.82%`)

\- Прибыль с учётом комиссий и slippage

\- `/buy 1` — покупка на самой дешёвой бирже

\- `/analyze BTC/USDT` — детальный отчёт

\- Безопасный spot-режим

\- Автоочистка сообщений через 10 мин



---



\## Команды

| Команда | Описание |

|-------|--------|

| `/start` | Запуск + справка |

| `/scan` | Скан сейчас |

| `/analyze BTC/USDT` | Анализ пары |

| `/buy 1` | Купить по сигналу #1 |

| `/buy BTC/USDT 0.02` | Купить 0.02 BTC |

| `/balance` | Баланс USDT |

| `/log` | Последние логи |

| `/stop` | Остановить |



---



\## Деплой на Render



1\. Форкни репозиторий

2\. Зайди на \[render.com](https://render.com)

3\. \*\*New → Web Service\*\* → подключи GitHub

4\. Создай \*\*PostgreSQL\*\* (или используй \*\*Key-Value\*\*)

5\. Добавь переменные в \*\*Environment Secrets\*\*:

BYBIT_API_KEY=...
BYBIT_API_SECRET=...
MEXC_API_KEY=...
MEXC_API_SECRET=...
BITGET_API_KEY=...
BITGET_API_SECRET=...
TELEGRAM_BOT_TOKEN=...

6. Используй `render.yaml` → **Deploy**

---

## Локальный запуск

```bash
pip install -r requirements.txt
cp keys.example.py keys.py  # ← ВСТАВЬ СВОИ КЛЮЧИ
python main.py

