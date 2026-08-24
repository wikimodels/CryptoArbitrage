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

from .calc import compute_net_edge
from .connectors import CCXTConnector
from .market_state import MarketState

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
        self.funding_max_share = sc.get("funding_max_share_of_spread", 0.3)

        self.size_usdt = cfg["emulator"]["virtual_position_size_usdt"] if "virtual_position_size_usdt" in cfg["emulator"] else cfg["emulator"].get("fixed_position_size_usdt", 1000)
        self.exit_frac = cfg["emulator"]["exit_threshold_frac"]
        self.max_holding_h = cfg["emulator"]["max_holding_hours"]
        self.emulator_enabled = cfg["emulator"]["enabled"]
        self.size_mode = cfg["emulator"].get("position_size_mode", "dynamic")
        self.min_size = cfg["emulator"].get("min_position_size_usdt", 50)
        self.max_size = cfg["emulator"].get("max_position_size_usdt", 1000)

        sc = cfg.get("scalp", {})
        self.scalp_enabled = bool(sc.get("enabled", False))
        self.scalp_exit_frac = sc.get("exit_spread_frac", 0.3)
        self.scalp_max_holding_sec = sc.get("max_holding_sec", 90)
        self.scalp_max_entry_spread = sc.get("max_entry_spread_pct", 1.0)
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

        self.connectors: dict[str, CCXTConnector] = {}
        self.symbols: list[str] = []
        self.symbols_by_exchange: dict[str, set[str]] = {}
        self.exchanges: list[str] = []

        self._ob_cache: dict[tuple[str, str], tuple[float, object]] = {}
        self._signal_last_log: dict[tuple[str, str, str], float] = {}
        self._tasks: list[asyncio.Task] = []
        self._ws_tasks: dict[str, asyncio.Task] = {}
        self.events: deque = deque(maxlen=120)
        self.started_at = time.time()
        self.last_scan_ts = 0.0
        self._running = False

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
                self._event("error", f"Не удалось подключить {exch_id}: {e}")
                log.warning("Не удалось подключить %s: %s", exch_id, e)

        if len(self.connectors) < 2:
            raise RuntimeError("Нужно минимум 2 живых коннектора для арбитража.")

        self.exchanges = list(self.connectors.keys())
        self.state.set_fee_lookup(self._fee_lookup)
        await self._refresh_symbols()

        # WS-стримы цен
        for name, conn in self.connectors.items():
            self._ws_tasks[name] = asyncio.create_task(self._price_stream(name, conn), name=f"ws-{name}")
        # Funding + сканер + периодический рефреш символов
        self._tasks.append(asyncio.create_task(self._funding_loop(), name="funding"))
        self._tasks.append(asyncio.create_task(self._scanner_loop(), name="scanner"))
        self._tasks.append(asyncio.create_task(self._symbols_refresh_loop(), name="symbols-refresh"))
        self.loggers.system.write({"event": "startup", "exchanges": self.exchanges,
                                   "symbols": len(self.symbols)})

    async def stop(self):
        self._running = False
        for t in list(self._ws_tasks.values()) + self._tasks:
            t.cancel()
        for conn in self.connectors.values():
            await conn.close()
        self.loggers.system.write({"event": "shutdown"})

    def _fee_lookup(self, exchange: str, symbol: str) -> tuple[float, float]:
        conn = self.connectors.get(exchange)
        return conn.fees_for(symbol) if conn else (0.0006, 0.0002)

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

    def _symbols_for(self, name: str) -> list[str]:
        have = self.symbols_by_exchange.get(name, set())
        return [s for s in self.symbols if s in have]

    # -------------------- WS price stream --------------------

    async def _price_stream(self, name: str, conn: CCXTConnector):
        syms = self._symbols_for(name)
        if not syms:
            return
        try:
            await conn.watch_prices(syms, self.state.update_price)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.loggers.errors.write({"event": "price_stream_died", "exchange": name, "error": str(e)})
            self._event("error", f"WS {name} остановлен: {e}")

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
            await asyncio.sleep(interval)

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
            await asyncio.sleep(max(0.0, self.scan_interval - elapsed))

    async def _scan_once(self):
        now = time.time()
        for symbol in self.symbols:
            quotes = self.state.fresh_quotes(symbol, self.exchanges)
            quotes = self._drop_price_outliers(quotes)
            if len(quotes) < 2:
                continue

            # 1) Проверка выходов по открытым позициям этого символа
            await self._check_exits(symbol, quotes, now)

            # 2) Быстрый предфильтр: лучшая пара по сырому спреду (без IO)
            best = self._best_pair(quotes)
            if best is None or best[0] < self.prefilter:
                continue
            _, lo, sh = best
            q_lo, q_sh = quotes[lo], quotes[sh]

            # 2a) Выравнивание ног по времени: свежесть по отдельности мало,
            # важно чтобы обе котировки были сняты близко друг к другу.
            if abs(q_lo.ts - q_sh.ts) > self.max_leg_dt:
                continue

            # 2b) Отсечка выбросов: аномальный спред = битая котировка.
            if best[0] > self.max_sane_spread:
                self.loggers.errors.write({
                    "event": "suspect_spread", "symbol": symbol,
                    "exch_long": lo, "exch_short": sh, "raw_spread_pct": round(best[0], 4),
                })
                continue

            # 2c) Ликвидность вершины книги: спред неторгуем, если на лучших
            # уровнях мало объёма (покупаем в ask lo, продаём в bid sh).
            top_long = q_lo.best_ask * q_lo.ask_size
            top_short = q_sh.best_bid * q_sh.bid_size
            if min(top_long, top_short) < self.min_top_notional:
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
            if self.scalp_enabled and r.raw_spread_pct >= self.spike_min_spread:
                self._track_spike(symbol, r.raw_spread_pct, (r.exch_long, r.exch_short),
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

                # 5b) ВХОД ТОЛЬКО ПО РЕАЛЬНОМУ СТАКАНУ: спред должен
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
                    self.emulator.try_open(r, q_long, q_short,
                                           ob_lo if r.exch_long == lo else ob_sh,
                                           ob_sh if r.exch_short == sh else ob_lo,
                                           strategy=strategy, size_usdt=dyn_size)

    # ---- скальп-рейтинг: спайки и их сходимость ----

    def _track_spike(self, symbol: str, raw_spread: float, pair_key: tuple[str, str],
                     width_pct: float | None, now: float):
        """Регистрирует спайк; сравнивает с предыдущим — если спред сжался
        до conv_frac за conv_window, предыдущий спайк = СОШЁДШИЙСЯ."""
        st = self._scalp_stats.setdefault(
            symbol, {"spikes": 0, "converged": 0, "capture_sum": 0.0,
                     "width_sum": 0.0, "width_n": 0})
        dq = self._spikes.setdefault(symbol, deque(maxlen=60))

        # сходимость предыдущего спайка той же пары
        for i in range(len(dq) - 1, -1, -1):
            ts0, sp0, pk0 = dq[i]
            if pk0 != pair_key:
                continue
            dt = now - ts0
            if dt <= self.conv_window and sp0 > 0:
                if raw_spread > 0 and raw_spread <= sp0 * self.conv_frac:
                    st["converged"] += 1
                    st["capture_sum"] += (sp0 - raw_spread)
            break

        dq.append((now, raw_spread, pair_key))
        st["spikes"] += 1
        if width_pct is not None:
            st["width_sum"] += width_pct
            st["width_n"] += 1

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

    async def _check_exits(self, symbol: str, quotes: dict, now: float):
        for trade_id, pos in list(self.emulator.open_positions.items()):
            if pos.symbol != symbol:
                continue

            strategy = getattr(pos, "strategy", "arb")

            # ---- СКАЛЬП: выход на сжатии спреда или по тайм-стопу ----
            if strategy == "scalp" and self.scalp_enabled:
                if pos.exch_long in quotes and pos.exch_short in quotes:
                    ql, qs = quotes[pos.exch_long], quotes[pos.exch_short]
                    if ql.best_ask > 0:
                        cur_spread = (qs.best_bid - ql.best_ask) / ql.best_ask * 100.0
                        entry = getattr(pos, "entry_raw_spread_pct", 0.0) or 0.0
                        if entry > 0 and cur_spread > 0 and cur_spread <= entry * self.scalp_exit_frac:
                            ob_l = await self._get_ob(pos.exch_long, symbol)
                            ob_s = await self._get_ob(pos.exch_short, symbol)
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

            # ---- ПОЗИЦИОННЫЙ АРБИТРАЖ: ждём схождения net_edge / таймаут часов ----
            if (now - pos.open_ts) / 3600.0 >= self.max_holding_h:
                if pos.exch_long in quotes and pos.exch_short in quotes:
                    ob_l = await self._get_ob(pos.exch_long, symbol)
                    ob_s = await self._get_ob(pos.exch_short, symbol)
                    self.emulator.try_close(trade_id, quotes[pos.exch_long], quotes[pos.exch_short],
                                            0.0, reason="max_holding", ob_long=ob_l, ob_short=ob_s)
                continue
            if pos.exch_long not in quotes or pos.exch_short not in quotes:
                continue
            check = compute_net_edge(
                quotes[pos.exch_long], quotes[pos.exch_short],
                holding_hours=self.holding_hours,
                min_threshold_pct=self.min_threshold,
                slippage_buffer_pct=self.slippage_buffer,
            )
            if check.net_edge_pct <= self.min_threshold * self.exit_frac:
                ob_l = await self._get_ob(pos.exch_long, symbol)
                ob_s = await self._get_ob(pos.exch_short, symbol)
                self.emulator.try_close(trade_id, quotes[pos.exch_long], quotes[pos.exch_short],
                                        check.net_edge_pct, reason="converged",
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
            suspect = (
                best[0] > self.max_sane_spread
                or leg_dt > self.max_leg_dt
                or top_notional < self.min_top_notional
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
            "spreads": spreads[:60],
            "positions": positions,
            "scalp_rank": self._scalp_scores()[:20],
            "watchlist": list(self._watchlist),
            "stats": {
                "arb": snap_stats(emu_stats.get("arb", {}), n_arb),
                "scalp": snap_stats(emu_stats.get("scalp", {}), n_scalp),
                "open_positions": len(positions),
            },
            "events": list(self.events)[:40],
        }
