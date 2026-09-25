"""
Движок сканера: поднимает WS-стримы цен по всем биржам, периодический
REST funding, и цикл сканера, который на каждом проходе считает спреды
из свежих котировок, тянет стакан только для пар выше предфильтра
(экономия REST), логирует сигналы (с троттлингом), открывает/закрывает
виртуальные позиции. Всё изолировано try/except — один сбой не роняет
сканер.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import json
import time
from collections import deque
from pathlib import Path

from .calc import compute_net_edge, NetEdgeResult
from .connectors import CCXTConnector
from .market_state import MarketState
from .risk import simulate_leg_fill
from .strategies import build_strategies, BaseStrategy
from .zscore_tracker import ZScoreTracker

log = logging.getLogger("engine")

class Engine:
    def __init__(self, cfg, loggers, storage, alerts, emulator):
        self.cfg = cfg
        self.loggers = loggers
        self.storage = storage
        self.alerts = alerts
        self.emulator = emulator

        m = cfg["market"]
        self.state = MarketState(staleness_sec=m["staleness_sec"])
        self.ob_ttl = m["orderbook_ttl_sec"]
        self.ob_depth = m["orderbook_depth"]
        self._ob_sem = asyncio.Semaphore(m["orderbook_concurrency"])
        self.max_leg_dt = m["max_leg_dt_sec"]
        self.max_sane_spread = m["max_sane_spread_pct"]
        self.min_top_notional = m["min_top_notional_usdt"]
        self.price_sanity_pct = m.get("price_sanity_deviation_pct", 50)

        s = cfg["scan"]
        self.scan_interval = s["interval_ms"] / 1000.0
        self.prefilter = s["prefilter_pct"]
        self.signal_throttle = s["signal_log_throttle_sec"]

        sc = cfg["scoring"]
        self.holding_hours = sc["assumed_holding_hours"]
        self.min_threshold = sc["min_threshold_pct"]
        self.slippage_buffer = sc["slippage_buffer_pct"]
        self.funding_max_share = sc.get("funding_max_share_of_spread", 0.15)
        self.max_book_width = sc.get("max_book_width_pct", 0.3)

        self.size_usdt = cfg["emulator"]["virtual_position_size_usdt"] if "virtual_position_size_usdt" in cfg["emulator"] else cfg["emulator"].get("fixed_position_size_usdt", 1000)
        self.exit_frac = cfg["emulator"]["exit_threshold_frac"]
        self.max_holding_h = cfg["emulator"]["max_holding_hours"]
        self.emulator_enabled = cfg["emulator"]["enabled"]
        self.size_mode = cfg["emulator"].get("position_size_mode", "dynamic")
        self.min_size = cfg["emulator"].get("min_position_size_usdt", 50)
        self.max_size = cfg["emulator"].get("max_position_size_usdt", 1000)

        # ---- Z-Score конфигурация ----
        zc = cfg.get("zscore", {})
        self.zscore_enabled = bool(zc.get("enabled", True))
        self.entry_z = float(zc.get("entry_z", 4.0))
        self.exit_z = float(zc.get("exit_z", 0.0))
        self.stop_mult = float(zc.get("stop_mult", 2.0))
        self.timestop_sec = float(zc.get("timestop_sec", 1800))
        self.min_profit_margin_pct = float(zc.get("min_profit_margin_pct", 0.25))
        self.zscore_period = int(zc.get("period", 1440))
        self.z_tracker = ZScoreTracker(period=self.zscore_period)

        sc = cfg.get("scalp", {})
        self.scalp_enabled = bool(sc.get("enabled", False))
        self.scalp_exit_frac = sc.get("exit_spread_frac", 0.3)
        self.scalp_max_holding_sec = sc.get("max_holding_sec", 90)
        self.scalp_max_entry_spread = sc.get("max_entry_spread_pct", 1.0)
        self.scalp_min_capture = sc.get("min_capture_pct", 0.30)

        dr = cfg.get("dir", {})
        self.dir_enabled = bool(dr.get("enabled", False))
        self.dir_min_spread = dr.get("min_spread_pct", 0.3)
        self.dir_min_lag = dr.get("min_lag_sec", 0.2)
        self.dir_exit_frac = dr.get("exit_frac", 0.3)
        self.dir_stop_mult = dr.get("stop_mult", 2.0)
        self.dir_max_holding_sec = dr.get("max_holding_sec", 90)
        # ---- авто-watchlist: измеряем, какие монеты скальпятся ----
        self.spike_min_spread = sc.get("spike_min_spread_pct", 0.3)
        self.conv_frac = sc.get("convergence_frac", 0.5)
        self.conv_window = sc.get("convergence_window_sec", 120)
        self.watchlist_mode = sc.get("watchlist_mode", "auto")
        self.watchlist_top = sc.get("watchlist_top", 15)
        self.watchlist_min_spikes = sc.get("watchlist_min_spikes", 5)
        # symbol -> deque[(ts, raw_spread, pair_key)]
        self._spikes: Dict[str, deque] = {}
        # symbol -> {"spikes": n, "converged": n, "capture_sum": x, "width_sum": x, "width_n": n}
        self._scalp_stats: Dict[str, dict] = {}
        self._watchlist: list[str] = []
        self._watchlist_ts = 0.0
        self._last_scalp_persist = 0.0
        # открытые спайки: (symbol, pair) -> (ts, spread) — ждут сходимости
        self._open_spikes: Dict[tuple[str, tuple[str, str]], tuple[float, float]] = {}

        # ---- Сбор фандинга (Funding Arbitrage) ----
        fa = cfg.get("funding_arb", {})
        self.funding_arb_enabled = bool(fa.get("enabled", True))
        self.funding_min_diff_8h_pct = float(fa.get("min_funding_diff_8h_pct", 0.08))
        self.funding_max_raw_spread_pct = float(fa.get("max_raw_spread_pct", 0.20))
        self.funding_position_size = float(fa.get("position_size_usdt", 10.0))
        self.funding_max_hold_hours = float(fa.get("max_holding_hours", 72.0))
        self.funding_spread_stop_pct = float(fa.get("spread_stop_pct", 2.0))
        self._last_funding_scan_ts = 0.0
        # персистентность: подхватываем накопленную статистику из SQLite
        if storage is not None:
            loaded = storage.load_scalp_stats()
            if loaded:
                self._scalp_stats.update(loaded)
                log.info("Скальп-статистика загружена: %d монет", len(loaded))

        self.connectors: dict[str, CCXTConnector] = {}
        self.symbols: list[str] = []
        self.symbols_by_exchange: dict[str, set[str]] = {}
        self.exchanges: list[str] = []

        self._ob_cache: dict[tuple[str, str], tuple[float, object]] = {}
        self._signal_last_log: dict[tuple[str, str, str], float] = {}
        self._tasks: list[asyncio.Task] = []
        self._ws_tasks: dict[str, asyncio.Task] = {}
        self._stream_restarts: dict[str, int] = {}
        self.events: deque = deque(maxlen=120)
        self.started_at = time.time()
        self.last_scan_ts = 0.0
        self._running = False

        # Модульный реестр торговых стратегий (Strategy Pattern)
        self.strategies = build_strategies(self, cfg)
        self.z_strat = self.strategies.get("zscore")
        self.funding_strat = self.strategies.get("funding_arb")
        self.z_tracker = getattr(self.z_strat, "tracker", None)

    def _event(self, level: str, msg: str):
        self.events.appendleft({"ts": time.time(), "level": level, "msg": msg})

    # -------------------- lifecycle --------------------

    async def start(self):
        self._running = True
        fees = self.cfg["default_fees"]
        for exch_id in self.cfg["exchanges"]:
            try:
                c = CCXTConnector(exch_id, fees["taker"], fees["maker"])
                await c.connect()
                self.connectors[exch_id] = c
                self._event("system", f"Подключено: {exch_id}")
                log.info("Подключено: %s", exch_id)
            except Exception as e:
                try:
                    await c.close()
                except Exception:
                    pass
                self._event("error", f"Не удалось подключить {exch_id}: {e}")
                log.warning("Не удалось подключить %s: %s", exch_id, e)

        if len(self.connectors) < 2:
            raise RuntimeError("Нужно минимум 2 живых коннектора для арбитража.")

        self.exchanges = list(self.connectors.keys())
        self.state.set_fee_lookup(self._fee_lookup)
        await self._refresh_symbols()

        # Прогрев ZScoreTracker и синхронизация свечей
        if self.zscore_enabled:
            try:
                from .candle_updater import sync_candles
                sync_candles(symbols=self.symbols, exchanges=self.exchanges,
                             max_gap_minutes=90, retention_days=30)
            except Exception as e:
                log.warning("Автоматическая проверка свечей пропущена: %s", e)

        # Старт всех модульных стратегий
        for name, strat in self.strategies.items():
            if strat.enabled:
                try:
                    await strat.start()
                except Exception as e:
                    log.warning("Ошибка старта стратегии %s: %s", name, e)

        # WS-стримы цен (с супервизором: мёртвый/молчащий стрим перезапускается)
        for name, conn in self.connectors.items():
            self._ws_tasks[name] = asyncio.create_task(self._price_stream(name, conn), name=f"ws-{name}")
        # Funding + сканер + периодический рефреш символов + watchdog + ротация свечей
        self._tasks.append(asyncio.create_task(self._funding_loop(), name="funding"))
        self._tasks.append(asyncio.create_task(self._scanner_loop(), name="scanner"))
        self._tasks.append(asyncio.create_task(self._symbols_refresh_loop(), name="symbols-refresh"))
        self._tasks.append(asyncio.create_task(self._stream_watchdog(), name="stream-watchdog"))
        self._tasks.append(asyncio.create_task(self._candle_maintenance_loop(), name="candle-maintenance"))
        self.loggers.system.write({"event": "startup", "exchanges": self.exchanges,
                                   "symbols": len(self.symbols)})

    async def stop(self):
        self._running = False
        for strat in self.strategies.values():
            try:
                await strat.stop()
            except Exception:
                pass
        # финальный сброс скальп-статистики перед остановкой
        if self._scalp_stats:
            try:
                self.storage.save_scalp_stats(self._scalp_stats)
            except Exception:
                pass
        for t in list(self._ws_tasks.values()) + self._tasks:
            t.cancel()
        for conn in self.connectors.values():
            await conn.close()
        self.loggers.system.write({"event": "shutdown"})

    def _fee_lookup(self, exchange: str, symbol: str) -> tuple[float, float]:
        conn = self.connectors.get(exchange)
        return conn.fees_for(symbol) if conn else (0.0006, 0.0002)

    def _on_price_update(self, exchange: str, symbol: str, bid: float, ask: float,
                          bid_size: float, ask_size: float):
        self.state.update_price(exchange, symbol, bid, ask, bid_size, ask_size)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            now = time.time()
            for strat in self.strategies.values():
                if strat.enabled:
                    strat.on_price_tick(symbol, exchange, mid, now)

    # -------------------- symbols --------------------

    async def _refresh_symbols(self):
        from collections import Counter
        counter: Counter = Counter()
        for name, conn in self.connectors.items():
            try:
                syms = await conn.fetch_symbols()
                self.symbols_by_exchange[name] = set(syms)
                for s in set(syms):
                    counter[s] += 1
            except Exception as e:
                self.loggers.errors.write({"event": "fetch_symbols_failed", "exchange": name, "error": str(e)})

        flt = self.cfg.get("filters", {})
        min_common = int(flt.get("min_common_exchanges", 2))
        exclude_bases = set(flt.get("exclude_bases", []))
        exclude_symbols = set(flt.get("exclude_symbols", []))

        # common = только на >= min_common биржах
        common = {s for s, n in counter.items() if n >= min_common}

        # применяем фильтры исключений
        def _excluded(sym: str) -> bool:
            if sym in exclude_symbols:
                return True
            base = sym.split("/")[0]
            return base in exclude_bases

        self.symbols = sorted(s for s in common if not _excluded(s))

        self._event("system", (
            f"Каталог: {len(self.symbols)} символов (2+ биржи: {len(common)}, "
            f"исключено: {len(common) - len(self.symbols)})"
        ))
        log.info("Каталог: %d символов", len(self.symbols))

        # видимый каталог для контроля
        try:
            out_dir = Path(self.cfg.get("output_dir", "output"))
            out_dir.mkdir(parents=True, exist_ok=True)
            catalog = {
                "generated_ts": time.time(),
                "min_common_exchanges": min_common,
                "total_symbols": len(self.symbols),
                "symbols": [
                    {
                        "symbol": s,
                        "exchanges": sorted(x for x in self.connectors if s in self.symbols_by_exchange.get(x, set())),
                        "n_exchanges": len([x for x in self.connectors if s in self.symbols_by_exchange.get(x, set())]),
                    }
                    for s in self.symbols
                ],
                "coverage": {
                    name: len([s for s in self.symbols if s in self.symbols_by_exchange.get(name, set())])
                    for name in self.connectors
                },
            }
            (out_dir / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.loggers.errors.write({"event": "catalog_write_failed", "error": str(e)})

    async def _symbols_refresh_loop(self):
        interval = self.cfg["symbols_refresh_hours"] * 3600
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._refresh_symbols()
                # перезапустить WS-подписки под новый список
                for name, conn in self.connectors.items():
                    if name in self._ws_tasks:
                        self._ws_tasks[name].cancel()
                    self._ws_tasks[name] = asyncio.create_task(self._price_stream(name, conn), name=f"ws-{name}")
                self.loggers.system.write({"event": "symbols_refreshed", "count": len(self.symbols)})
            except Exception as e:
                self.loggers.errors.write({"event": "symbols_refresh_failed", "error": str(e)})

    async def _candle_maintenance_loop(self):
        """Периодическая фоновая ротация архива свечей раз в 12 часов."""
        while self._running:
            await asyncio.sleep(12 * 3600)
            try:
                from .candle_updater import sync_candles
                await asyncio.to_thread(sync_candles, symbols=self.symbols,
                                        exchanges=self.exchanges, force=False,
                                        max_gap_minutes=180, retention_days=30)
            except Exception as e:
                log.debug("Фоновая ротация свечей: %s", e)

    def _symbols_for(self, name: str) -> list[str]:
        have = self.symbols_by_exchange.get(name, set())
        return [s for s in self.symbols if s in have]

    # -------------------- WS price stream --------------------

    async def _price_stream(self, name: str, conn: CCXTConnector):
        """Супервизор стрима: watch_prices живёт вечно, но сеть флапает —
        если стрим вернулся/умер, перезапускаем через паузу."""
        while self._running:
            syms = self._symbols_for(name)
            if not syms:
                await asyncio.sleep(30)
                continue
            # точка отсчёта для watchdog: даём стриму 90с на первый тик
            self.state.last_price_ts[name] = time.time()
            try:
                await conn.watch_prices(syms, self._on_price_update)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.loggers.errors.write({"event": "price_stream_error", "exchange": name, "error": str(e)})
            if not self._running:
                return
            self.state.last_price_ts[name] = time.time()
            await asyncio.sleep(5)

    async def _stream_watchdog(self):
        """Раз в 30с: если от биржи нет ни одного тика > 90с (или таск умер)
        — перезапускаем её стрим. 3 тихих рестарта подряд -> полная
        пересборка коннектора (свежий WS-клиент)."""
        while self._running:
            await asyncio.sleep(30)
            now = time.time()
            for name in list(self._ws_tasks.keys()):
                task = self._ws_tasks.get(name)
                silent = now - self.state.last_price_ts.get(name, 0.0) > 90
                dead = task is None or task.done()
                if not (silent or dead):
                    self._stream_restarts[name] = 0
                    continue
                if name not in self.connectors:
                    continue
                self._stream_restarts[name] = self._stream_restarts.get(name, 0) + 1
                try:
                    if self._stream_restarts[name] >= 3:
                        self._event("system", f"{name}: пересборка коннектора (3 тихих рестарта)")
                        log.warning("[%s] full connector rebuild", name)
                        try:
                            await self.connectors[name].close()
                        except Exception:
                            pass
                        fees = self.cfg["default_fees"]
                        c = CCXTConnector(name, fees["taker"], fees["maker"])
                        await c.connect()
                        self.connectors[name] = c
                        self._stream_restarts[name] = 0
                    else:
                        why = "task dead" if dead else "нет тиков >90с"
                        self._event("system", f"{name}: стрим перезапущен ({why})")
                        log.warning("[%s] watchdog restart stream (%s)", name, why)
                    if task and not task.done():
                        task.cancel()
                    conn = self.connectors[name]
                    self._ws_tasks[name] = asyncio.create_task(
                        self._price_stream(name, conn), name=f"ws-{name}")
                except Exception as e:
                    self.loggers.errors.write({"event": "watchdog_restart_failed",
                                               "exchange": name, "error": str(e)})

    # -------------------- funding --------------------

    async def _funding_loop(self):
        interval = self.cfg["market"]["funding_refresh_sec"]
        while self._running:
            for name, conn in self.connectors.items():
                try:
                    mapping = await conn.refresh_funding(self._symbols_for(name))
                    self.state.update_funding(name, mapping)
                except Exception as e:
                    self.loggers.errors.write({"event": "funding_refresh_failed", "exchange": name, "error": str(e)})
            if self.funding_arb_enabled:
                try:
                    await self._scan_funding_arbitrage()
                except Exception as e:
                    log.warning("Ошибка сканирования фандинг-арбитража: %s", e)
            await asyncio.sleep(interval)

    async def _scan_funding_arbitrage(self):
        if not self.funding_arb_enabled or not self.emulator_enabled:
            return
        now = time.time()
        self._last_funding_scan_ts = now

        for sym in self.symbols:
            quotes = self.state.fresh_quotes(sym, self.exchanges)
            if len(quotes) < 2:
                continue

            for (e1, q1), (e2, q2) in itertools.combinations(quotes.items(), 2):
                if q1.funding_rate is None or q2.funding_rate is None:
                    continue

                if q1.funding_rate >= q2.funding_rate:
                    ex_short, q_short = e1, q1
                    ex_long, q_long = e2, q2
                else:
                    ex_short, q_short = e2, q2
                    ex_long, q_long = e1, q1

                rate_diff_8h_pct = (q_short.funding_rate - q_long.funding_rate) * 100.0
                if rate_diff_8h_pct < self.funding_min_diff_8h_pct:
                    continue

                if q_long.best_ask <= 0 or q_short.best_bid <= 0:
                    continue
                raw_spread_pct = (q_short.best_bid - q_long.best_ask) / q_long.best_ask * 100.0
                if abs(raw_spread_pct) > self.funding_max_raw_spread_pct:
                    continue

                key = (sym, ex_long, ex_short)
                if self.emulator._cooldown_active(key, now):
                    continue

                ob_long = await self._get_ob(ex_long, sym)
                ob_short = await self._get_ob(ex_short, sym)
                if not ob_long or not ob_short:
                    continue

                fill_long = simulate_leg_fill(ob_long, "buy", self.funding_position_size)
                fill_short = simulate_leg_fill(ob_short, "sell", self.funding_position_size)
                if not (fill_long.filled and fill_short.filled):
                    continue

                fee_pct = (q_long.taker_fee + q_short.taker_fee) * 2 * 100.0
                res = NetEdgeResult(
                    symbol=sym, exch_long=ex_long, exch_short=ex_short,
                    raw_spread_pct=raw_spread_pct, funding_edge_pct=rate_diff_8h_pct,
                    fees_pct=fee_pct, slippage_pct=0.0, width_pct=0.0,
                    net_edge_pct=rate_diff_8h_pct, passed_threshold=True,
                )

                opened = self.emulator.try_open(
                    res, q_long, q_short, ob_long, ob_short,
                    strategy="funding", size_usdt=self.funding_position_size,
                )
                if opened:
                    self._event(
                        "signal",
                        f"💰 СБОР ФАНДИНГА: {sym} SHORT {ex_short} ({q_short.funding_rate*100:+.3f}%) / "
                        f"LONG {ex_long} ({q_long.funding_rate*100:+.3f}%) Δ={rate_diff_8h_pct:+.3f}% (8ч) | ${self.funding_position_size:.0f} поз."
                    )

    # -------------------- order book (cached) --------------------

    async def _get_ob(self, exchange: str, symbol: str):
        now = time.time()
        cached = self._ob_cache.get((exchange, symbol))
        if cached and now - cached[0] <= self.ob_ttl:
            return cached[1]
        conn = self.connectors.get(exchange)
        if not conn:
            return None
        async with self._ob_sem:
            ob = await conn.fetch_orderbook(symbol, self.ob_depth)
        self._ob_cache[(exchange, symbol)] = (now, ob)
        return ob

    # -------------------- scanner --------------------

    async def _scanner_loop(self):
        while self._running:
            t0 = time.time()
            try:
                await self._scan_once()
            except Exception as e:
                self.loggers.errors.write({"event": "scan_error", "error": repr(e)})
                log.exception("scan error")
            self.last_scan_ts = time.time()
            elapsed = self.last_scan_ts - t0
            # персистентность скальп-рейтинга: раз в 60с на диск
            if self._scalp_stats and self.last_scan_ts - self._last_scalp_persist > 60:
                self._last_scalp_persist = self.last_scan_ts
                try:
                    self.storage.save_scalp_stats(self._scalp_stats)
                except Exception:
                    pass
            # Периодическое сканирование арбитража фандинга (раз в 10с)
            if self.funding_strat and self.funding_strat.enabled and self.last_scan_ts - self.funding_strat.last_scan_ts > 10.0:
                try:
                    await self.funding_strat.scan_opportunities()
                except Exception as e:
                    log.warning("Ошибка сканирования фандинг-арбитража: %s", e)
            await asyncio.sleep(max(0.0, self.scan_interval - elapsed))

    async def _scan_once(self):
        now = time.time()
        for symbol in self.symbols:
            quotes = self.state.fresh_quotes(symbol, self.exchanges)
            quotes = self._drop_price_outliers(quotes)
            if len(quotes) < 2:
                continue

            # 1) Проверка выходов по всем стратегиям
            for strat in self.strategies.values():
                if strat.enabled:
                    await strat.check_exits(symbol, quotes, now)

            # 2) Сканирование точек входа по всем стратегиям
            for strat in self.strategies.values():
                if strat.enabled:
                    await strat.on_scan_symbol(symbol, quotes, now)

            # 3) Быстрый предфильтр: лучшая пара по сырому спреду (legacy скальп/dir)
            if not (self.scalp_enabled or self.dir_enabled):
                continue

            best = self._best_pair(quotes)
            if best is None:
                continue
            # 1a) Сходимость открытых спайков — проверяется на КАЖДОМ скане
            # по текущему спреду (схождение уходит ниже порога спайка)
            if self.scalp_enabled and self._open_spikes:
                self._check_convergence(symbol, best[0], (best[1], best[2]), now)
            if best[0] < self.prefilter:
                continue
            _, lo, sh = best
            q_lo, q_sh = quotes[lo], quotes[sh]
            raw = best[0]
            dt = abs(q_lo.ts - q_sh.ts)

            # 2b) Отсечка выбросов (ДО dir!): аномальный спред = битая
            # котировка или разные токены — направленно не торгуем.
            if raw > self.max_sane_spread:
                self.loggers.errors.write({
                    "event": "suspect_spread", "symbol": symbol,
                    "exch_long": lo, "exch_short": sh, "raw_spread_pct": round(raw, 4),
                })
                continue

            # 1b) DIR: большой dt = видимый лаг -> направленный вход одной
            # ногой на отстающей бирже (фронт-ран лаггера)
            if self.dir_enabled and dt > self.max_leg_dt and raw >= self.dir_min_spread:
                await self._try_dir_entry(symbol, raw, lo, sh, q_lo, q_sh, dt, now)
                continue

            # 2a) Выравнивание ног по времени: свежесть по отдельности мало,
            # важно чтобы обе котировки были сняты близко друг к другу.
            if dt > self.max_leg_dt:
                continue

            # 2c) Ликвидность вершины книги: спред неторгуем, если на лучших
            # уровнях мало объёма. Если объёмы неизвестны (биржа отдаёт только
            # last) — гейт пропускает, защиту делает проверка по стакану.
            top_long = q_lo.best_ask * q_lo.ask_size
            top_short = q_sh.best_bid * q_sh.bid_size
            sizes_known = q_lo.ask_size > 0 and q_sh.bid_size > 0
            if sizes_known and min(top_long, top_short) < self.min_top_notional:
                continue

            # 3) Полный расчёт со стаканом только для кандидата
            ob_lo = await self._get_ob(lo, symbol)
            ob_sh = await self._get_ob(sh, symbol)

            # 3a) Динамический размер: не двигаем стакан. 0 = слишком тонко.
            if self.emulator_enabled and self.size_mode == "dynamic":
                dyn_size = self.emulator.calc_dynamic_size(ob_lo, ob_sh)
                if dyn_size <= 0:
                    continue
            else:
                dyn_size = None  # fixed-режим: размер по умолчанию

            r = compute_net_edge(
                quotes[lo], quotes[sh],
                holding_hours=self.holding_hours,
                min_threshold_pct=self.min_threshold,
                slippage_buffer_pct=self.slippage_buffer,
                ob_a=ob_lo, ob_b=ob_sh,
                position_size_usdt=dyn_size or self.size_usdt,
            )

            # 4) Троттлинг логирования сигнала на пару (и алертов — иначе
            # консоль заливает каждый проход сканера)
            key = (symbol, r.exch_long, r.exch_short)
            throttled = now - self._signal_last_log.get(key, 0.0) >= self.signal_throttle

            # 4a) Скальп-рейтинг: спайк + сходимость (по сырым спредам пары)
            if self.scalp_enabled and raw >= self.spike_min_spread:
                self._track_spike(symbol, raw, (r.exch_long, r.exch_short),
                                  r.width_pct, now)
                self._refresh_watchlist(now)

            if throttled:
                self._signal_last_log[key] = now
                self.loggers.signals.write({
                    "symbol": r.symbol, "exch_long": r.exch_long, "exch_short": r.exch_short,
                    "raw_spread_pct": r.raw_spread_pct, "funding_edge_pct": r.funding_edge_pct,
                    "fees_pct": r.fees_pct, "slippage_pct": r.slippage_pct,
                    "width_pct": r.width_pct,
                    "net_edge_pct": r.net_edge_pct, "passed_threshold": r.passed_threshold,
                })
                self.storage.save_signal({"ts": now, **r.__dict__})
                self.storage.save_quote(quotes[lo])
                self.storage.save_quote(quotes[sh])

            # 5) Прошедший порог -> алерт (не чаще троттлинга) + попытка
            # открыть позицию (кулдаун внутри эмулятора)
            if r.passed_threshold:
                # 5a) ПРАВИЛО СООТНОШЕНИЯ: funding не должен быть главным
                # драйвером. Спред 1% + funding 1% = отказ (спред сам есть
                # цена funding-дифференциала, сходиться не будет).
                if (r.funding_edge_pct > 0 and r.raw_spread_pct > 0
                        and r.funding_edge_pct > self.funding_max_share * r.raw_spread_pct):
                    if throttled:
                        self._event("skip", f"{r.symbol} funding-dominated "
                                    f"({r.funding_edge_pct:.2f}% > {self.funding_max_share:.0%} of {r.raw_spread_pct:.2f}%)")
                    continue

                # 5b) ГЕЙТ ШИРИНЫ КНИГ: суммарная ширина обеих книг больше
                # порога -> реальный кост-флор (вход-walk + ширина выхода)
                # не отбивается порогом. Тонкие/широкие монеты отсекаются:
                # на них зелёного закрытия не бывает в принципе.
                if r.width_pct > self.max_book_width:
                    if throttled:
                        self._event("skip", f"{r.symbol} ширина книг "
                                    f"{r.width_pct:.2f}% > {self.max_book_width}%")
                    continue

                # 5c) ВХОД ТОЛЬКО ПО РЕАЛЬНОМУ СТАКАНУ: спред должен
                # существовать на ценах исполнения прямо сейчас. Сигнал
                # считался по тикерам (могли устареть) — фантомный спред
                # здесь отсеивается, иначе позиция становится ставкой на
                # направление цены.
                book_lo = ob_lo if r.exch_long == lo else ob_sh
                book_sh = ob_sh if r.exch_short == sh else ob_lo
                if (book_lo and book_lo.asks and book_sh and book_sh.bids):
                    entry_long = book_lo.asks[0].price
                    entry_short = book_sh.bids[0].price
                    book_spread = (entry_short - entry_long) / entry_long * 100.0 if entry_long > 0 else -999
                    if book_spread < self.min_threshold:
                        if throttled:
                            self._event("skip", f"{r.symbol} фантом: спред на стакане "
                                        f"{book_spread:.3f}% < {self.min_threshold}%")
                        continue

                if throttled:
                    self.alerts.send_signal(r)
                    self._event("signal", f"{r.symbol} {r.exch_long}->{r.exch_short} net={r.net_edge_pct:+.3f}%")
                if self.emulator_enabled:
                    q_long = quotes[r.exch_long]
                    q_short = quotes[r.exch_short]
                    # Сплит стратегий: мелькающий спред < max_entry -> СКАЛЬП
                    # (быстрый выход на сжатии), жирный -> ПОЗИЦИОННЫЙ АРБИТРАЖ
                    strategy = ("scalp" if (self.scalp_enabled
                                            and r.raw_spread_pct < self.scalp_max_entry_spread)
                                else "arb")
                    # Гейт: в auto-режиме скальпим только топ-N монет по score
                    if strategy == "scalp" and self.watchlist_mode == "auto" and \
                            symbol not in self._watchlist:
                        if throttled:
                            self._event("skip", f"{symbol} не в скальп-watchlist (топ-{self.watchlist_top})")
                        continue
                    # Гейт: экономика. Средний захват монеты должен покрывать
                    # кост-флор (комиссии+slippage+ширина ~0.30%) с запасом.
                    if strategy == "scalp":
                        st = self._scalp_stats.get(symbol)
                        if st and st.get("converged", 0) > 0:
                            avg_cap = st["capture_sum"] / st["converged"]
                            if avg_cap < self.scalp_min_capture:
                                if throttled:
                                    self._event("skip", f"{symbol} захват {avg_cap:.2f}% < "
                                                f"{self.scalp_min_capture}% (после комиссий минус)")
                                continue
                    self.emulator.try_open(r, q_long, q_short,
                                           ob_lo if r.exch_long == lo else ob_sh,
                                           ob_sh if r.exch_short == sh else ob_lo,
                                           strategy=strategy, size_usdt=dyn_size)

    # ---- DIR: направленный вход на отстающей бирже ----

    async def _try_dir_entry(self, symbol: str, raw: float, lo: str, sh: str,
                             q_lo, q_sh, dt: float, now: float):
        """Фронт-ран лаггера: лидер двинулся, лаггер ещё нет.
        Лаг = более старая котировка. Вход одной ногой на лаггере по
        направлению к лидеру. Перед входом — верификация по свежему
        стакану лаггера (лаг мог уже закрыться)."""
        if dt < self.dir_min_lag:
            return
        # отстающая = более старая котировка; направление — к лидеру
        if q_lo.ts <= q_sh.ts:
            lag_ex, side, lead_q = lo, "long", q_sh   # отстала дешёвая -> догонит вверх
        else:
            lag_ex, side = sh, "short"                # отстала дорогая -> догонит вниз
            lead_q = q_lo

        key = (symbol, lag_ex, side)
        ob = await self._get_ob(lag_ex, symbol)
        if not ob or not ob.asks or not ob.bids:
            return

        # Верификация: отклонение должно существовать на СВЕЖЕМ стакане
        # лаггера против свежего лидера. Иначе лаг закрыт — вход без эджа.
        lead_mid = (lead_q.best_bid + lead_q.best_ask) / 2.0
        if lead_mid <= 0:
            return
        if side == "long":
            lag_ask = ob.asks[0].price
            dev = (lead_mid - lag_ask) / lag_ask * 100.0
        else:
            lag_bid = ob.bids[0].price
            dev = (lag_bid - lead_mid) / lead_mid * 100.0
        if dev < self.dir_min_spread:
            return  # лаг уже закрылся на реальных ценах

        size = self.emulator.calc_dir_size(ob, side)
        if size <= 0:
            return

        q_lag = q_lo if lag_ex == lo else q_sh
        pos = self.emulator.try_open_dir(symbol, lag_ex, side, size, dev, q_lag, ob, now)
        if pos:
            self._event("dir_open", f"{symbol} {side.upper()} {lag_ex} dev={dev:.2f}% size={size:.0f}")

    # ---- скальп-рейтинг: спайки и их сходимость ----

    def _track_spike(self, symbol: str, raw_spread: float, pair_key: tuple[str, str],
                     width_pct: float | None, now: float):
        """Регистрирует спайк. САМА сходимость проверяется на каждом скане
        (_check_convergence): спред обычно схлдывается НИЖЕ порога спайка,
        и сравнивать спайк-со-спайком нельзя — схождение не видно."""
        st = self._scalp_stats.setdefault(
            symbol, {"spikes": 0, "converged": 0, "capture_sum": 0.0,
                     "width_sum": 0.0, "width_n": 0})
        self._spikes.setdefault(symbol, deque(maxlen=60)).append((now, raw_spread, pair_key))
        st["spikes"] += 1
        self._open_spikes[(symbol, pair_key)] = (now, raw_spread)
        if width_pct is not None:
            st["width_sum"] += width_pct
            st["width_n"] += 1

    def _check_convergence(self, symbol: str, cur_raw: float, pair_key: tuple[str, str], now: float):
        """Вызывается на КАЖДОМ скане по текущему сырому спреду пары:
        открытый спайк сжался до conv_frac за conv_window -> СОШЁЛСЯ
        (+захват). Окно истекло без сжатия -> не сошёлся (списываем)."""
        key = (symbol, pair_key)
        sp = self._open_spikes.get(key)
        if not sp:
            return
        ts0, sp0 = sp
        dt = now - ts0
        if cur_raw > 0 and cur_raw <= sp0 * self.conv_frac:
            st = self._scalp_stats.setdefault(
                symbol, {"spikes": 0, "converged": 0, "capture_sum": 0.0,
                         "width_sum": 0.0, "width_n": 0})
            st["converged"] += 1
            st["capture_sum"] += (sp0 - cur_raw)
            del self._open_spikes[key]
        elif dt > self.conv_window:
            del self._open_spikes[key]

    def _scalp_scores(self) -> list[dict]:
        """Рейтинг монет для скальпа: сходимость × захват × частота."""
        out = []
        now = time.time()
        for sym, st in self._scalp_stats.items():
            if st["spikes"] < self.watchlist_min_spikes:
                continue
            conv_rate = st["converged"] / st["spikes"]
            avg_cap = st["capture_sum"] / max(st["converged"], 1)
            avg_width = st["width_sum"] / st["width_n"] if st["width_n"] else 0.0
            freq = min(st["spikes"], 30) / 30.0
            score = conv_rate * avg_cap * freq
            out.append({"symbol": sym, "spikes": st["spikes"],
                        "converged": st["converged"],
                        "conv_rate": round(conv_rate * 100, 1),
                        "avg_capture": round(avg_cap, 3),
                        "avg_width": round(avg_width, 3),
                        "score": round(score, 3)})
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    def _refresh_watchlist(self, now: float):
        if now - self._watchlist_ts < 30:
            return
        self._watchlist_ts = now
        ranked = self._scalp_scores()
        self._watchlist = [r["symbol"] for r in ranked[: self.watchlist_top]]

    def _drop_price_outliers(self, quotes: dict) -> dict:
        """Одноимённые, но РАЗНЫЕ токены (один тикер на разных биржах):
        их цена отличается в разы/в тысячи раз. Настоящий токен так
        расходиться не может — арбитраж свёл бы цену мгновенно.
        Выбрасываем котировки, чья средняя цена дальше медианы по биржам,
        чем на price_sanity_deviation_pct."""
        if len(quotes) < 2:
            return quotes
        mids = sorted((q.best_bid + q.best_ask) / 2.0 for q in quotes.values())
        n = len(mids)
        median = mids[n // 2] if n % 2 else (mids[n // 2 - 1] + mids[n // 2]) / 2.0
        if median <= 0:
            return quotes
        lo = median * (1.0 - self.price_sanity_pct / 100.0)
        hi = median * (1.0 + self.price_sanity_pct / 100.0)
        return {ex: q for ex, q in quotes.items()
                if lo <= (q.best_bid + q.best_ask) / 2.0 <= hi}

    def _best_pair(self, quotes: dict) -> tuple[float, str, str] | None:
        """Лучшая пара по сырому спреду (корректно по ask/bid, оба направления)."""
        best = None
        exs = list(quotes.keys())
        for a, b in itertools.combinations(exs, 2):
            qa, qb = quotes[a], quotes[b]
            if qa.best_ask > 0:
                s_ab = (qb.best_bid - qa.best_ask) / qa.best_ask * 100.0
                if best is None or s_ab > best[0]:
                    best = (s_ab, a, b)
            if qb.best_ask > 0:
                s_ba = (qa.best_bid - qb.best_ask) / qb.best_ask * 100.0
                if best is None or s_ba > best[0]:
                    best = (s_ba, b, a)
        return best

    async def _scan_zscore_symbol(self, symbol: str, quotes: dict, now: float):
        """Поиск возможностей Z-Score арбитража (|Z| >= entry_z) с валидацией стакана."""
        exs = list(quotes.keys())
        for ea, eb in itertools.combinations(exs, 2):
            qa, qb = quotes[ea], quotes[eb]
            mid_a = (qa.best_bid + qa.best_ask) / 2.0
            mid_b = (qb.best_bid + qb.best_ask) / 2.0
            z = self.z_tracker.get_z(symbol, ea, eb, mid_a, mid_b)
            if z is None or abs(z) < self.entry_z:
                continue

            # Направление: z > 0 => ea дороже, eb дешевле; z < 0 => ea дешевле, eb дороже
            if z > 0:
                ex_short, ex_long = ea, eb
                z_in = z
            else:
                ex_short, ex_long = eb, ea
                z_in = -z

            q_long, q_short = quotes[ex_long], quotes[ex_short]
            key = (symbol, ex_long, ex_short)
            throttled = now - self._signal_last_log.get(key, 0.0) >= self.signal_throttle

            # 1. Защита от рассинхрона по времени котировок
            dt = abs(q_long.ts - q_short.ts)
            if dt > self.max_leg_dt:
                if throttled:
                    self._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} dt={dt:.1f}с > {self.max_leg_dt}с (рассинхрон котировок)")
                continue

            # 2. Защита от битых/аномальных спредов
            raw_top_spread = (q_short.best_bid - q_long.best_ask) / q_long.best_ask * 100.0 if q_long.best_ask > 0 else 0.0
            if raw_top_spread > self.max_sane_spread:
                if throttled:
                    self._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} спред={raw_top_spread:.1f}% > {self.max_sane_spread}% (выброс)")
                continue

            # 3. Ликвидность лучших цен (минимальный порог, глубокий расчет идет через VWAP)
            top_long = q_long.best_ask * q_long.ask_size
            top_short = q_short.best_bid * q_short.bid_size
            sizes_known = q_long.ask_size > 0 and q_short.bid_size > 0
            if sizes_known and min(top_long, top_short) < 10.0:
                if throttled:
                    self._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} пустой топ стакана: ${min(top_long, top_short):.0f}")
                continue

            # 4. Проверка стакана (L2 Order Book)
            ob_long = await self._get_ob(ex_long, symbol)
            ob_short = await self._get_ob(ex_short, symbol)
            if not ob_long or not ob_short:
                if throttled:
                    self._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} стаканы недоступны")
                continue

            # 5. Размер позиции
            if self.size_mode == "dynamic":
                trade_size = self.emulator.calc_dynamic_size(ob_long, ob_short)
            else:
                trade_size = self.size_usdt

            if trade_size < self.min_size:
                if throttled:
                    self._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} размер ${trade_size:.0f} < ${self.min_size}")
                continue

            # 6. Расчет честного исполнения через VWAP
            fill_long = simulate_leg_fill(ob_long, "buy", trade_size)
            fill_short = simulate_leg_fill(ob_short, "sell", trade_size)
            if not (fill_long.filled and fill_short.filled):
                if throttled:
                    self._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} не хватает глубины на ${trade_size:.0f}")
                continue

            vwap_long = fill_long.fill_price
            vwap_short = fill_short.fill_price
            real_spread = (vwap_short - vwap_long) / vwap_long * 100.0
            total_fees_pct = (q_long.taker_fee + q_short.taker_fee) * 2 * 100.0
            net_edge = real_spread - total_fees_pct

            # Гейт безопасности: реальный чистый спред должен покрывать все 4 комиссии с запасом
            if net_edge < self.min_profit_margin_pct:
                if throttled:
                    self._signal_last_log[key] = now
                    self._event("skip", f"Z-Score {symbol} Z={z:+.1f} VWAP-спред={real_spread:+.2f}% - 4x ком.={total_fees_pct:.2f}% -> edge={net_edge:+.3f}% < {self.min_profit_margin_pct}%")
                continue

            # 7. Логирование и алерт
            res = NetEdgeResult(
                symbol=symbol, exch_long=ex_long, exch_short=ex_short,
                raw_spread_pct=real_spread, funding_edge_pct=0.0,
                fees_pct=total_fees_pct, slippage_pct=0.0, width_pct=0.0,
                net_edge_pct=net_edge, passed_threshold=True,
            )
            if throttled:
                self._signal_last_log[key] = now
                self.alerts.send_signal(res)
                self._event("signal", f"🔥 ВХОД Z-Score {symbol} {ex_long}->{ex_short} Z={z:+.2f} edge={net_edge:+.3f}%")
                self.loggers.signals.write({
                    "symbol": symbol, "exch_long": ex_long, "exch_short": ex_short,
                    "z_score": round(z, 2), "raw_spread_pct": round(real_spread, 4),
                    "fees_pct": round(total_fees_pct, 4), "net_edge_pct": round(net_edge, 4),
                    "passed_threshold": True,
                })
                self.storage.save_signal({"ts": now, "strategy": "zscore", **res.__dict__})

            # 8. Открытие позиции через эмулятор
            if self.emulator_enabled:
                self.emulator.try_open(res, q_long, q_short, ob_long, ob_short,
                                       strategy="arb", size_usdt=trade_size, z_in=z_in)

    async def _check_exits(self, symbol: str, quotes: dict, now: float):
        for trade_id, pos in list(self.emulator.open_positions.items()):
            if pos.symbol != symbol:
                continue

            strategy = getattr(pos, "strategy", "arb")

            # ---- DIR: тейк/стоп/тайм-стоп по PnL самой ноги ----
            if strategy == "dir":
                q = quotes.get(pos.exch_long)
                if q is None:
                    continue
                dev = getattr(pos, "entry_raw_spread_pct", 0.0) or 0.0
                if dev <= 0:
                    continue
                if pos.side == "long":
                    pnl_pct = (q.best_bid - pos.entry_price_long) / pos.entry_price_long * 100.0
                else:
                    pnl_pct = (pos.entry_price_long - q.best_ask) / pos.entry_price_long * 100.0

                reason = None
                if pnl_pct >= self.dir_exit_frac * dev:
                    reason = "dir_tp"
                elif pnl_pct <= -self.dir_stop_mult * dev:
                    reason = "dir_stop"
                elif (now - pos.open_ts) >= self.dir_max_holding_sec:
                    reason = "dir_timeout"
                if reason:
                    ob = await self._get_ob(pos.exch_long, symbol)
                    self.emulator.try_close_dir(trade_id, q, ob, reason)
                continue

            # ---- СКАЛЬП: выход на сжатии спреда или по тайм-стопу ----
            # Триггер по тикерам, но закрытие только если ЧЕСТНЫЙ PnL
            # выхода (walk по стаканам + комиссии) >= 0: иначе сжатие
            # тикера — ловушка, ширина книг съедает остаток спреда.
            if strategy == "scalp" and self.scalp_enabled:
                if pos.exch_long in quotes and pos.exch_short in quotes:
                    ql, qs = quotes[pos.exch_long], quotes[pos.exch_short]
                    if ql.best_ask > 0:
                        cur_spread = (qs.best_bid - ql.best_ask) / ql.best_ask * 100.0
                        entry = getattr(pos, "entry_raw_spread_pct", 0.0) or 0.0
                        if entry > 0 and cur_spread > 0 and cur_spread <= entry * self.scalp_exit_frac:
                            ob_l = await self._get_ob(pos.exch_long, symbol)
                            ob_s = await self._get_ob(pos.exch_short, symbol)
                            est = self.emulator.estimate_close_pnl(pos, ql, qs, ob_l, ob_s)
                            if est is not None and est >= 0:
                                self.emulator.try_close(trade_id, ql, qs, cur_spread,
                                                        reason="scalp_converged", ob_long=ob_l, ob_short=ob_s)
                            continue
                age_sec = now - pos.open_ts
                if age_sec >= self.scalp_max_holding_sec:
                    if pos.exch_long in quotes and pos.exch_short in quotes:
                        ob_l = await self._get_ob(pos.exch_long, symbol)
                        ob_s = await self._get_ob(pos.exch_short, symbol)
                        self.emulator.try_close(trade_id, quotes[pos.exch_long], quotes[pos.exch_short],
                                                0.0, reason="scalp_timeout", ob_long=ob_l, ob_short=ob_s)
                    continue

            # ---- СБОР ФАНДИНГА: удержание 72ч / инверсия ставок / стоп-лосс спреда ----
            if strategy == "funding":
                hold_hours = (now - pos.open_ts) / 3600.0
                ql = quotes.get(pos.exch_long)
                qs = quotes.get(pos.exch_short)

                # 1. Завершение планового горизонта удержания (по умолчанию 72ч = 9 начислений)
                if hold_hours >= self.funding_max_hold_hours:
                    if ql and qs:
                        ob_l = await self._get_ob(pos.exch_long, symbol)
                        ob_s = await self._get_ob(pos.exch_short, symbol)
                        self.emulator.try_close(trade_id, ql, qs, 0.0,
                                                reason="funding_max_hold", ob_long=ob_l, ob_short=ob_s)
                    continue

                if not ql or not qs:
                    continue

                # 2. Инверсия ставок фандинга: ставка на шорте упала ниже лонга
                if qs.funding_rate < ql.funding_rate:
                    ob_l = await self._get_ob(pos.exch_long, symbol)
                    ob_s = await self._get_ob(pos.exch_short, symbol)
                    self.emulator.try_close(trade_id, ql, qs, 0.0,
                                            reason="funding_flipped", ob_long=ob_l, ob_short=ob_s)
                    continue

                # 3. Защитный стоп-лосс при сильной раздвижке спреда (> 2.0%)
                if ql.best_ask > 0:
                    spread_now = abs((qs.best_bid - ql.best_ask) / ql.best_ask * 100.0)
                    if spread_now > self.funding_spread_stop_pct:
                        ob_l = await self._get_ob(pos.exch_long, symbol)
                        ob_s = await self._get_ob(pos.exch_short, symbol)
                        self.emulator.try_close(trade_id, ql, qs, spread_now,
                                                reason="funding_spread_stop", ob_long=ob_l, ob_short=ob_s)
                        continue
                continue

            # ---- ПОЗИЦИОННЫЙ Z-SCORE АРБИТРАЖ: ждём РЕАЛЬНО прибыльного закрытия ----
            hold_sec = now - pos.open_ts
            if hold_sec / 3600.0 >= self.max_holding_h:
                if pos.exch_long in quotes and pos.exch_short in quotes:
                    ob_l = await self._get_ob(pos.exch_long, symbol)
                    ob_s = await self._get_ob(pos.exch_short, symbol)
                    self.emulator.try_close(trade_id, quotes[pos.exch_long], quotes[pos.exch_short],
                                            0.0, reason="max_holding", ob_long=ob_l, ob_short=ob_s)
                continue

            if pos.exch_long not in quotes or pos.exch_short not in quotes:
                continue

            ql, qs = quotes[pos.exch_long], quotes[pos.exch_short]
            ob_l = await self._get_ob(pos.exch_long, symbol)
            ob_s = await self._get_ob(pos.exch_short, symbol)

            # Честная оценка PnL закрытия (обе комиссии + стаканы + funding)
            est = self.emulator.estimate_close_pnl(pos, ql, qs, ob_l, ob_s)

            # Z-схождение
            converged = False
            cur_z = None
            if self.zscore_enabled:
                mid_l = (ql.best_bid + ql.best_ask) / 2.0
                mid_s = (qs.best_bid + qs.best_ask) / 2.0
                cur_z = self.z_tracker.get_z(symbol, pos.exch_short, pos.exch_long, mid_s, mid_l)
                if cur_z is not None:
                    # Входили: pos.exch_short дороже, pos.exch_long дешевле => z_in > 0
                    if cur_z <= self.exit_z or abs(cur_z) <= self.exit_z:
                        converged = True
            else:
                converged = True

            cur_spread = ((qs.best_bid - ql.best_ask) / ql.best_ask * 100.0
                          if ql.best_ask > 0 else 0.0)

            # Стоп-лосс по расширению аномалии (|Z| > stop_mult * z_in)
            if cur_z is not None and getattr(pos, "z_in", 0.0) > 0 and cur_z >= self.stop_mult * pos.z_in:
                self.emulator.try_close(trade_id, ql, qs, cur_spread,
                                        reason="stop", ob_long=ob_l, ob_short=ob_s)
                continue

            # Тайм-стоп (например, 30 минут)
            if hold_sec >= self.timestop_sec:
                self.emulator.try_close(trade_id, ql, qs, cur_spread,
                                        reason="timestop", ob_long=ob_l, ob_short=ob_s)
                continue

            # Схождение: сошлись по Z И реальный результат закрытия в плюс
            if converged and est is not None and est >= 0:
                self.emulator.try_close(trade_id, ql, qs, cur_spread,
                                        reason="converged",
                                        ob_long=ob_l, ob_short=ob_s)

    # -------------------- dashboard snapshot --------------------

    def snapshot(self) -> dict:
        now = time.time()
        spreads = []
        for symbol in self.symbols:
            quotes = self.state.fresh_quotes(symbol, self.exchanges)
            quotes = self._drop_price_outliers(quotes)
            if len(quotes) < 2:
                continue
            best = self._best_pair(quotes)
            if best is None or best[0] <= 0:
                continue
            _, lo, sh = best
            q_lo, q_sh = quotes[lo], quotes[sh]
            leg_dt = abs(q_lo.ts - q_sh.ts)
            top_long = q_lo.best_ask * q_lo.ask_size
            top_short = q_sh.best_bid * q_sh.bid_size
            top_notional = min(top_long, top_short)
            sizes_known = q_lo.ask_size > 0 and q_sh.bid_size > 0
            suspect = (
                best[0] > self.max_sane_spread
                or leg_dt > self.max_leg_dt
                or (sizes_known and top_notional < self.min_top_notional)
            )
            r = compute_net_edge(
                q_lo, q_sh,
                holding_hours=self.holding_hours,
                min_threshold_pct=self.min_threshold,
                slippage_buffer_pct=self.slippage_buffer,
                position_size_usdt=self.size_usdt,
            )
            spreads.append({
                "symbol": r.symbol, "exch_long": r.exch_long, "exch_short": r.exch_short,
                "raw_spread_pct": round(r.raw_spread_pct, 4),
                "funding_edge_pct": round(r.funding_edge_pct, 4),
                "fees_pct": round(r.fees_pct, 4), "slippage_pct": round(r.slippage_pct, 4),
                "width_pct": round(r.width_pct, 4),
                "net_edge_pct": round(r.net_edge_pct, 4),
                "passed": r.passed_threshold and not suspect,
                "suspect": suspect,
                "leg_dt_sec": round(leg_dt, 2),
                "top_notional_usdt": round(top_notional, 0),
            })
        spreads.sort(key=lambda x: x["net_edge_pct"], reverse=True)

        def snap_stats(st: dict, open_n: int) -> dict:
            closed = st["closed"]
            return {
                "opened": st["opened"], "closed": closed,
                "wins": st["wins"], "losses": st["losses"],
                "orphan_aborts": st["orphan_aborts"],
                "win_rate_pct": round(st["wins"] / closed * 100.0, 1) if closed else 0.0,
                "pnl_usdt": round(st["pnl_usdt"], 2),
                "fees_usdt": round(st["fees_usdt"], 2),
                "funding_usdt": round(st["funding_usdt"], 2),
                "avg_holding_sec": round(st["holding_sec_sum"] / closed, 1) if closed else 0.0,
                "open_positions": open_n,
            }

        emu_stats = self.emulator.stats
        positions = self.emulator.positions_snapshot()
        n_arb = sum(1 for p in positions if p.get("strategy") == "arb")
        n_scalp = sum(1 for p in positions if p.get("strategy") == "scalp")
        n_dir = sum(1 for p in positions if p.get("strategy") == "dir")
        n_funding = sum(1 for p in positions if p.get("strategy") == "funding")

        # Z-Score Радар и список монет для дашборда
        z_radar = []
        max_abs_z = 0.0
        max_z_info = "—"
        arb_coins_map: dict[str, dict] = {}
        if self.zscore_enabled:
            for (sym, ea, eb), state in self.z_tracker.states.items():
                if sym not in arb_coins_map:
                    arb_coins_map[sym] = {
                        "symbol": sym,
                        "base": sym.split("/")[0],
                        "exchanges": set(),
                        "pairs_count": 0,
                        "max_abs_z": 0.0,
                        "max_spread_pct": 0.0,
                    }
                arb_item = arb_coins_map[sym]
                arb_item["exchanges"].add(ea)
                arb_item["exchanges"].add(eb)
                arb_item["pairs_count"] += 1

                if state.ma is None or state.sd is None or state.sd <= 0:
                    continue
                q = self.state.fresh_quotes(sym, [ea, eb])
                qa, qb = q.get(ea), q.get(eb)
                if not qa or not qb:
                    continue
                mid_a = (qa.best_bid + qa.best_ask) / 2.0
                mid_b = (qb.best_bid + qb.best_ask) / 2.0
                z = state.get_z(mid_a, mid_b)
                if z is None:
                    continue
                abs_z = abs(z)
                if abs_z > max_abs_z:
                    max_abs_z = abs_z
                    max_z_info = f"{sym.split('/')[0]} ({z:+.2f})"

                if abs_z > arb_item["max_abs_z"]:
                    arb_item["max_abs_z"] = round(abs_z, 2)

                ratio = mid_a / mid_b if mid_b > 0 else 1.0
                spread = (mid_a - mid_b) / mid_b * 100.0 if mid_b > 0 else 0.0
                if abs(spread) > arb_item["max_spread_pct"]:
                    arb_item["max_spread_pct"] = round(abs(spread), 3)

                fees = (qa.taker_fee + qb.taker_fee) * 2 * 100.0
                z_radar.append({
                    "symbol": sym,
                    "ex_a": ea,
                    "ex_b": eb,
                    "z": round(z, 2),
                    "abs_z": round(abs_z, 2),
                    "ratio": round(ratio, 5),
                    "ma": round(state.ma, 5),
                    "sd": round(state.sd, 5),
                    "spread_pct": round(spread, 3),
                    "fees_pct": round(fees, 3),
                    "is_signal": abs_z >= self.entry_z,
                })
            z_radar.sort(key=lambda x: x["abs_z"], reverse=True)

        arbitrage_coins = []
        for sym, d in arb_coins_map.items():
            arbitrage_coins.append({
                "symbol": d["symbol"],
                "base": d["base"],
                "exchanges": sorted(d["exchanges"]),
                "pairs_count": d["pairs_count"],
                "max_abs_z": d["max_abs_z"],
                "max_spread_pct": d["max_spread_pct"],
                "status": "SIGNAL" if d["max_abs_z"] >= self.entry_z else ("WATCH" if d["max_abs_z"] >= 2.5 else "ACTIVE"),
            })
        arbitrage_coins.sort(key=lambda x: x["symbol"])

        # mark-to-market по открытым позициям + капающий фандинг
        total_open_funding_accrued = 0.0
        daily_dripping_sum = 0.0
        payment_8h_dripping_sum = 0.0

        for p in positions:
            p["z_in"] = round(float(p.get("z_in") or 0.0), 2)
            p["cur_z"] = None
            p["cur_spread_pct"] = None
            p["unrealized_pnl_usdt"] = None
            p["funding_payments_count"] = 0
            p["funding_accrued_usdt"] = 0.0
            p["next_payment_in_sec"] = None

            q = self.state.fresh_quotes(p["symbol"], self.exchanges)
            ql = q.get(p["exch_long"])
            qs = q.get(p["exch_short"])
            if not ql or not qs or ql.best_ask <= 0:
                continue

            if self.zscore_enabled:
                mid_l = (ql.best_bid + ql.best_ask) / 2.0
                mid_s = (qs.best_bid + qs.best_ask) / 2.0
                cz = self.z_tracker.get_z(p["symbol"], p["exch_short"], p["exch_long"], mid_s, mid_l)
                if cz is not None:
                    p["cur_z"] = round(cz, 2)

            cur = (qs.best_bid - ql.best_ask) / ql.best_ask * 100.0
            size = p["size_usdt"]
            price_pnl = ((ql.best_bid - p["entry_price_long"]) / p["entry_price_long"] * size
                         + (p["entry_price_short"] - qs.best_ask) / p["entry_price_short"] * size)
            exit_fees = (p["taker_long"] + p["taker_short"]) * size
            n_l = self.emulator._funding_payments(p["next_funding_ts_long"], p["funding_interval_long"],
                                                  p["open_ts"], now)
            n_s = self.emulator._funding_payments(p["next_funding_ts_short"], p["funding_interval_short"],
                                                  p["open_ts"], now)
            funding = (p["funding_rate_short"] * n_s - p["funding_rate_long"] * n_l) * size
            p["cur_spread_pct"] = round(cur, 3)
            p["unrealized_pnl_usdt"] = round(
                price_pnl - p["entry_fees_usdt"] - exit_fees + funding, 2)
            p["funding_payments_count"] = int(max(n_s, n_l))
            p["funding_accrued_usdt"] = round(funding, 4)

            next_ts = p.get("next_funding_ts_short") or p.get("next_funding_ts_long")
            if next_ts:
                if next_ts > 1e11:
                    next_ts /= 1000.0
                p["next_payment_in_sec"] = max(0, int(next_ts - now))

            if p.get("strategy") == "funding":
                total_open_funding_accrued += funding
                diff_pct = (p.get("funding_rate_short", 0.0) - p.get("funding_rate_long", 0.0)) * 100.0
                inc_8h_pos = p["size_usdt"] * (diff_pct / 100.0)
                payment_8h_dripping_sum += inc_8h_pos
                daily_dripping_sum += inc_8h_pos * 3.0

        # Срезы фандинга и Z-Score формируются модульными стратегиями
        fresh_counts = self.state.fresh_count_by_exchange(self.exchanges)

        return {
            "ts": now,
            "uptime_sec": now - self.started_at,
            "last_scan_age_sec": now - self.last_scan_ts if self.last_scan_ts else None,
            "exchanges": [
                {"name": e, "fresh_symbols": fresh_counts.get(e, 0),
                 "ws_alive": (e in self._ws_tasks and not self._ws_tasks[e].done())}
                for e in self.exchanges
            ],
            "symbols_total": len(self.symbols),
            "min_threshold_pct": self.min_threshold,
            "zscore": self.z_strat.snapshot() if self.z_strat else {},
            "funding_arb": self.funding_strat.snapshot() if self.funding_strat else {},
            "cross_coin": self.strategies["cross_coin"].snapshot() if "cross_coin" in self.strategies else {},
            "spreads": spreads[:60],
            "positions": positions,
            "closed_trades": self.emulator.closed_trades_snapshot()[:100],
            "arbitrage_coins": arbitrage_coins,
            "scalp_rank": self._scalp_scores()[:20],
            "watchlist": list(self._watchlist),
            "stats": {
                "arb": snap_stats(emu_stats.get("arb", {}), n_arb),
                "arb_z4": snap_stats(emu_stats.get("arb_z4", {}), sum(1 for p in positions if p.get("strategy") == "arb" and p.get("is_z4"))),
                "arb_z35": snap_stats(emu_stats.get("arb_z35", {}), sum(1 for p in positions if p.get("strategy") == "arb" and not p.get("is_z4"))),
                "scalp": snap_stats(emu_stats.get("scalp", {}), n_scalp),
                "dir": snap_stats(emu_stats.get("dir", {}), n_dir),
                "funding": snap_stats(emu_stats.get("funding", {}), n_funding),
                "open_positions": len(positions),
            },
            "events": list(self.events)[:40],
        }
