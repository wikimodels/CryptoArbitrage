"""
Стратегия классического спредового арбитража по книге ордеров (L2 Order Book Spread Arbitrage).
Инкапсулирует вычисление чистого перевеса (net edge) с учетом проскальзывания, ширины спреда,
отслеживание спайков для скальпинга и направленный фронт-раннинг лагов.
"""
from __future__ import annotations

import itertools
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from cryptoarb.calc import compute_net_edge

if TYPE_CHECKING:
    from cryptoarb.engine import Engine

from cryptoarb.strategies.base import BaseStrategy

log = logging.getLogger("strategies.spread_arb")


class SpreadArbitrageStrategy(BaseStrategy):
    """Классический спредовый арбитраж и скальпинг спайков."""

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        super().__init__(engine, cfg)
        self.name = "spread_arb"
        self.display_name = "Спредовый арбитраж"

        sc = cfg.get("scoring", {})
        self.holding_hours = sc.get("assumed_holding_hours", 0.5)
        self.min_threshold = sc.get("min_threshold_pct", 0.15)
        self.slippage_buffer = sc.get("slippage_buffer_pct", 0.05)
        self.max_book_width = sc.get("max_book_width_pct", 0.3)

        sp = cfg.get("scan", {})
        self.prefilter = sp.get("prefilter_pct", 0.15)

        scalp = cfg.get("scalp", {})
        self.scalp_enabled = bool(scalp.get("enabled", False))
        self.scalp_exit_frac = scalp.get("exit_spread_frac", 0.3)
        self.scalp_max_holding_sec = scalp.get("max_holding_sec", 90)
        self.scalp_max_entry_spread = scalp.get("max_entry_spread_pct", 1.0)
        self.scalp_min_capture = scalp.get("min_capture_pct", 0.30)
        self.spike_min_spread = scalp.get("spike_min_spread_pct", 0.3)
        self.conv_frac = scalp.get("convergence_frac", 0.5)
        self.conv_window = scalp.get("convergence_window_sec", 120)
        self.watchlist_mode = scalp.get("watchlist_mode", "auto")
        self.watchlist_top = scalp.get("watchlist_top", 15)
        self.watchlist_min_spikes = scalp.get("watchlist_min_spikes", 5)

        self._spikes: dict[str, deque] = {}
        self._scalp_stats: dict[str, dict] = {}
        self._watchlist: list[str] = []
        self._watchlist_ts = 0.0
        self._last_scalp_persist = 0.0
        self._open_spikes: dict[tuple[str, tuple[str, str]], tuple[float, float]] = {}
        self._signal_last_log: dict[tuple[str, str, str], float] = {}

        if engine is not None and getattr(engine, "storage", None) is not None:
            loaded = engine.storage.load_scalp_stats()
            if loaded:
                self._scalp_stats.update(loaded)

    async def start(self) -> None:
        pass

    async def on_scan_symbol(self, symbol: str, quotes: dict, now: float) -> None:
        if not (self.scalp_enabled or self.engine.cfg.get("dir", {}).get("enabled", False)):
            return

        best = self._best_pair(quotes)
        if best is None:
            return

        if self.scalp_enabled and self._open_spikes:
            self._check_convergence(symbol, best[0], (best[1], best[2]), now)

        if best[0] < self.prefilter:
            return

        raw, lo, sh = best
        q_lo, q_sh = quotes[lo], quotes[sh]
        dt = abs(q_lo.ts - q_sh.ts)

        if raw > self.engine.max_sane_spread or dt > self.engine.max_leg_dt:
            return

        ob_lo = await self.engine._get_ob(lo, symbol)
        ob_sh = await self.engine._get_ob(sh, symbol)
        if not ob_lo or not ob_sh:
            return

        dyn_size = None
        if self.engine.emulator_enabled and self.engine.size_mode == "dynamic":
            dyn_size = self.engine.emulator.calc_dynamic_size(ob_lo, ob_sh)
            if dyn_size <= 0:
                return

        r = compute_net_edge(
            q_lo, q_sh,
            holding_hours=self.holding_hours,
            min_threshold_pct=self.min_threshold,
            slippage_buffer_pct=self.slippage_buffer,
            ob_a=ob_lo, ob_b=ob_sh,
            position_size_usdt=dyn_size or self.engine.size_usdt,
        )

        key = (symbol, r.exch_long, r.exch_short)
        throttled = now - self._signal_last_log.get(key, 0.0) >= self.engine.signal_throttle

        if self.scalp_enabled and raw >= self.spike_min_spread:
            self._track_spike(symbol, raw, (r.exch_long, r.exch_short), r.width_pct, now)
            self._refresh_watchlist(now)

        if throttled:
            self._signal_last_log[key] = now
            self.engine.loggers.signals.write({
                "ts": now, "symbol": symbol, "exch_long": r.exch_long, "exch_short": r.exch_short,
                "raw_spread_pct": round(r.raw_spread_pct, 4), "fees_pct": round(r.fees_pct, 4),
                "slippage_pct": round(r.slippage_pct, 4), "net_edge_pct": round(r.net_edge_pct, 4),
                "passed_threshold": r.passed_threshold,
            })

    def _best_pair(self, quotes: dict) -> tuple[float, str, str] | None:
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

    def _track_spike(self, symbol: str, raw_spread: float, pair_key: tuple[str, str],
                     width_pct: float | None, now: float):
        st = self._scalp_stats.setdefault(
            symbol, {"spikes": 0, "converged": 0, "capture_sum": 0.0,
                     "width_sum": 0.0, "width_n": 0}
        )
        self._spikes.setdefault(symbol, deque(maxlen=60)).append((now, raw_spread, pair_key))
        st["spikes"] += 1
        self._open_spikes[(symbol, pair_key)] = (now, raw_spread)
        if width_pct is not None:
            st["width_sum"] += width_pct
            st["width_n"] += 1

    def _check_convergence(self, symbol: str, cur_raw: float, pair_key: tuple[str, str], now: float):
        key = (symbol, pair_key)
        if key not in self._open_spikes:
            return
        t_open, s_open = self._open_spikes[key]
        st = self._scalp_stats.setdefault(symbol, {"spikes": 0, "converged": 0, "capture_sum": 0.0, "width_sum": 0.0, "width_n": 0})

        if cur_raw <= s_open * self.conv_frac:
            capture = s_open - cur_raw
            st["converged"] += 1
            st["capture_sum"] += capture
            del self._open_spikes[key]
        elif now - t_open > self.conv_window:
            del self._open_spikes[key]

    def _refresh_watchlist(self, now: float):
        if now - self._watchlist_ts < 30.0:
            return
        self._watchlist_ts = now
        ranked = self._scalp_scores()
        self._watchlist = [r["symbol"] for r in ranked[: self.watchlist_top]]

    def _scalp_scores(self) -> list[dict]:
        out = []
        for sym, st in self._scalp_stats.items():
            if st["spikes"] < self.watchlist_min_spikes:
                continue
            conv_rate = st["converged"] / st["spikes"]
            avg_cap = st["capture_sum"] / max(st["converged"], 1)
            avg_width = st["width_sum"] / st["width_n"] if st["width_n"] else 0.0
            freq = min(st["spikes"], 30) / 30.0
            score = conv_rate * avg_cap * freq
            out.append({
                "symbol": sym, "spikes": st["spikes"], "converged": st["converged"],
                "conv_rate": round(conv_rate * 100, 1), "avg_capture": round(avg_cap, 3),
                "avg_width": round(avg_width, 3), "score": round(score, 3),
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    async def check_exits(self, symbol: str, quotes: dict, now: float) -> None:
        pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "scalp_rank": self._scalp_scores()[:20],
            "watchlist": list(self._watchlist),
        }
