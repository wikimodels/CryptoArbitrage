# CryptoArbitrage — CEX arbitrage screener (Phase 1, WS edition)

Скринер арбитражных возможностей между CEX-биржами по бессрочным
USDT-фьючерсам: считает `net_edge` (спред + funding − комиссии −
проскальзывание), пишет полную статистику, эмулирует сделки на **реальных**
данных и показывает всё в тёмном дашборде.

Данные приходят по **WebSocket** (`ccxt.pro`, входит в бесплатный MIT-пакет
ccxt с версии 1.95+) — push вместо polling. Funding тянется REST-ом раз в
несколько минут (он меняется редко). Стакан — REST по требованию, только
для пар, прошедших предфильтр.

## Установка (Poetry)

```bash
cd D:\GitHub\CryptoArbitrage
poetry install
```

## Запуск

```bash
poetry run cryptoarb
# или
poetry run python -m cryptoarb.main --config config.yaml
```

Дашборд: **http://127.0.0.1:8080** (порт меняется в `config.yaml`).

## Архитектура

```
cryptoarb/
├── main.py              точка входа (engine + dashboard в одном loop)
├── config.py            загрузка конфига с дефолтами
├── base.py              контракт ExchangeConnector + Quote/OrderBook
├── connectors/
│   └── ccxt_connector.py  WS-коннектор ccxt.pro (watch_tickers/bids_asks)
├── market_state.py      общее состояние (цены по WS + funding по REST)
├── calc.py              net_edge: спред/funding/fees/slippage
├── scorer.py            перебор всех пар бирж по символу
├── risk.py              orphan-leg на реальной глубине стакана
├── emulator.py          виртуальные позиции (fees + funding + кулдаун)
├── storage.py           SQLite (WAL, фоновый писатель, ротация)
├── logger_setup.py      JSONL-логи с ротацией по дням
├── alerts.py            консольные алерты
├── engine.py            WS-циклы, сканер, выходы, снапшот для дашборда
└── dashboard/
    ├── app.py           FastAPI + WebSocket
    └── templates/index.html  тёмная тема, тёмные скроллбары
```

Ядро (`calc/scorer/emulator`) не знает про конкретные биржи — только через
`ExchangeConnector`. Заменить коннектор = реализовать тот же ABC.

## Что исправлено против первой версии

| Было | Стало |
|------|-------|
| `ImportError: connectors` (файлы в корне) | нормальный пакет `cryptoarb/` |
| `kucoin` = спот, свопы не тянулись | `kucoinfutures`, `binanceusdm`, `gate` (карта id используется) |
| PnL эмулятора без комиссий и funding | вычитаются 4×taker + начисляется funding по факту |
| один спред плодил тысячи дублей позиций | кулдаун `cooldown_sec` на тройку (символ, лонг, шорт) |
| последовательный polling → цикл минуты | WS push (`ccxt.pro`) + предфильтр + стакан по требованию |
| `commit()` на каждую строку в async | WAL + фоновый писатель + батч-коммиты |
| одна ошибка роняла весь сканер | `try/except` вокруг каждого прохода/символа |
| БД росла на всех парах каждый цикл | троттлинг записи сигналов + ротация `retention_days` |
| позиции висели вечно | таймаут `max_holding_hours` |
| сравнение направления по одному условию | корректный выбор обоих направлений по ask/bid |

## Конфигурация

Все параметры — в `config.yaml`. Ключевое:

- `scan.prefilter_pct` — сырой спред, ниже которого пара даже не идёт за
  стаканом (экономит REST).
- `scan.signal_log_throttle_sec` — как часто писать сигнал в лог/БД на пару.
- `emulator.cooldown_sec` — антидубль повторных входов.
- `emulator.max_holding_hours` — принудительное закрытие.
- `scoring.min_threshold_pct` — порог валидного сигнала.

## Комиссии

`default_fees` — заглушки. Реальные taker/maker берутся из market-info
биржи, если она их отдаёт; иначе применяется дефолт. Для точной статистики
подставь свои тарифы под тир аккаунта.

## Данные и логи

- `data/scanner.db` — SQLite (quotes / signals / emulator_trades).
- `logs/*.jsonl` — signals / emulator_trades / errors / system, ротация по дням.

Файлы логов — самодостаточный источник истины и переживают сбой БД.
