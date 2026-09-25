"""
Стратегия статистического межбиржевого арбитража на возврате к средней (Z-Score Mean Reversion).
Инкапсулирует ZScoreTracker, VWAP расчет глубины стакана и строгие правила выхода.
"""
from __future__ import annotations

import itertools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptoarb.calc import NetEdgeResult
from cryptoarb.risk import simulate_leg_fill
from cryptoarb.strategies.base import BaseStrategy
from cryptoarb.zscore_tracker import ZScoreTracker

if TYPE_CHECKING:
    from cryptoarb.engine import Engine

log = logging.getLogger("strategies.zscore")


class ZScoreStrategy(BaseStrategy):
    """Межбиржевой статистический арбитраж на возврате спреда к средней."""

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        super().__init__(engine, cfg)
        self.name = "zscore"
        self.display_name = "Z-Score Арбитраж"

        zc = cfg.get("zscore", {})
        self.enabled = bool(zc.get("enabled", True))
        self.entry_z = float(zc.get("entry_z", 4.0))
        self.exit_z = float(zc.get("exit_z", 0.0))
        self.stop_mult = float(zc.get("stop_mult", 2.0))
        self.timestop_sec = float(zc.get("timestop_sec", 1800.0))
        self.min_profit_margin_pct = float(zc.get("min_profit_margin_pct", 0.25))
        self.period = int(zc.get("period", 1440))

        self.tracker = ZScoreTracker(period=self.period)
        self._signal_last_log: dict[tuple[str, str, str], float] = {}

    async def start(self) -> None:
        """Прогрев скользящих окон из локального архива parquet-свечей."""
        if not self.enabled:
            return
        try:
            prewarmed = self.tracker.prewarm(
                self.engine.symbols, self.engine.exchanges, data_dir=Path("data/raw_1m_30d")
            )
            self.engine._event("system", f"ZScoreTracker прогрет: {prewarmed} пар")
            log.info("ZScoreTracker успешно прогрет: %d пар", prewarmed)
        except Exception as e:
            log.warning("Ошибка прогрева ZScoreTracker: %s", e)

    def on_price_tick(self, symbol: str, exchange: str, mid_price: float, now_sec: float) -> None:
        if self.enabled and mid_price > 0:
            self.tracker.on_tick(symbol, exchange, mid_price, now_sec)

    async def on_scan_symbol(self, symbol: str, quotes: dict, now: float) -> None:
        if not self.enabled or not self.engine.emulator_enabled:
            return

        exs = list(quotes.keys())
        for ea, eb in itertools.combinations(exs, 2):
            qa, qb = quotes[ea], quotes[eb]
            mid_a = (qa.best_bid + qa.best_ask) / 2.0
            mid_b = (qb.best_bid + qb.best_ask) / 2.0
            z = self.tracker.get_z(symbol, ea, eb, mid_a, mid_b)
            if z is None or abs(z) < self.entry_z:
                continue

            if z > 0:
                ex_short, ex_long = ea, eb
                z_in = z
            else:
                ex_short, ex_long = eb, ea
                z_in = -z

            q_long, q_short = quotes[ex_long], quotes[ex_short]
            key = (symbol, ex_long, ex_short)
            throttled = now - self._signal_last_log.get(key, 0.0) >= self.engine.signal_throttle

            # 1. Защита от рассинхрона котировок
            dt = abs(q_long.ts - q_short.ts)
            if dt > self.engine.max_leg_dt:
                if throttled:
                    self.engine._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} dt={dt:.1f}с > {self.engine.max_leg_dt}с")
                continue

            # 2. Защита от битых котировок / выбросов
            raw_top_spread = (q_short.best_bid - q_long.best_ask) / q_long.best_ask * 100.0 if q_long.best_ask > 0 else 0.0
            if raw_top_spread > self.engine.max_sane_spread:
                if throttled:
                    self.engine._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} спред={raw_top_spread:.1f}% > {self.engine.max_sane_spread}%")
                continue

            # 3. Ликвидность
            top_long = q_long.best_ask * q_long.ask_size
            top_short = q_short.best_bid * q_short.bid_size
            sizes_known = q_long.ask_size > 0 and q_short.bid_size > 0
            if sizes_known and min(top_long, top_short) < 10.0:
                if throttled:
                    self.engine._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} пустой топ стакана: ${min(top_long, top_short):.0f}")
                continue

            # 4. Проверка L2 стаканов
            ob_long = await self.engine._get_ob(ex_long, symbol)
            ob_short = await self.engine._get_ob(ex_short, symbol)
            if not ob_long or not ob_short:
                if throttled:
                    self.engine._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} стаканы недоступны")
                continue

            # 5. Расчет размера позиции
            if self.engine.size_mode == "dynamic":
                dyn_size = self.engine.emulator.calc_dynamic_size(ob_long, ob_short)
                if dyn_size <= 0:
                    continue
            else:
                dyn_size = self.engine.size_usdt

            # 6. Расчет честного VWAP исполнения
            fill_long = simulate_leg_fill(ob_long, "buy", dyn_size)
            fill_short = simulate_leg_fill(ob_short, "sell", dyn_size)
            if not (fill_long.filled and fill_short.filled):
                if throttled:
                    self.engine._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} не хватает ликвидности на ${dyn_size:.0f}")
                continue

            exec_long = fill_long.vwap_price
            exec_short = fill_short.vwap_price
            real_spread_pct = (exec_short - exec_long) / exec_long * 100.0 if exec_long > 0 else -999.0

            # 7. Четыре комиссии тейкера
            total_fees_pct = (q_long.taker_fee + q_short.taker_fee) * 2.0 * 100.0
            net_edge_pct = real_spread_pct - total_fees_pct

            if net_edge_pct < self.min_profit_margin_pct:
                if throttled:
                    self.engine._event("skip", f"Z-Score {symbol} {ex_long}->{ex_short} чистый перевес {net_edge_pct:+.2f}% < {self.min_profit_margin_pct:.2f}% (комиссии 4x={total_fees_pct:.2f}%)")
                continue

            res = NetEdgeResult(
                symbol=symbol, exch_long=ex_long, exch_short=ex_short,
                raw_spread_pct=raw_top_spread, funding_edge_pct=0.0,
                fees_pct=total_fees_pct, slippage_pct=0.0, width_pct=0.0,
                net_edge_pct=net_edge_pct, passed_threshold=True,
            )

            if throttled:
                self._signal_last_log[key] = now
                self.engine.loggers.signals.write({
                    "ts": now, "symbol": symbol, "exch_long": ex_long, "exch_short": ex_short,
                    "z_score": round(z, 2), "raw_spread_pct": round(real_spread_pct, 4),
                    "fees_pct": round(total_fees_pct, 4), "net_edge_pct": round(net_edge_pct, 4),
                    "passed_threshold": True, "strategy": "arb",
                })
                self.engine._event(
                    "signal",
                    f"🔥 Z-SCORE СИГНАЛ: {symbol} LONG {ex_long} / SHORT {ex_short} "
                    f"Z={z:+.2f} реальный спред={real_spread_pct:+.2f}% чистыми={net_edge_pct:+.2f}%"
                )

            self.engine.emulator.try_open(
                res, q_long, q_short, ob_long, ob_short,
                strategy="arb", size_usdt=dyn_size, z_in=z_in,
            )

    async def check_exits(self, symbol: str, quotes: dict, now: float) -> None:
        """Проверка закрытия позиций Z-Score (сведение к 0, таймстоп, стоп-лосс)."""
        active_positions = [
            p for p in self.engine.emulator.open_positions.values()
            if p.symbol == symbol and p.strategy == "arb"
        ]
        if not active_positions:
            return

        for pos in active_positions:
            q_lo = quotes.get(pos.exch_long)
            q_sh = quotes.get(pos.exch_short)
            if not q_lo or not q_sh:
                continue

            ob_lo = await self.engine._get_ob(pos.exch_long, symbol)
            ob_sh = await self.engine._get_ob(pos.exch_short, symbol)
            if not ob_lo or not ob_sh or not ob_lo.bids or not ob_sh.asks:
                continue

            mid_long = (q_lo.best_bid + q_lo.best_ask) / 2.0
            mid_short = (q_sh.best_bid + q_sh.best_ask) / 2.0
            cur_z_raw = self.tracker.get_z(symbol, pos.exch_short, pos.exch_long, mid_short, mid_long)

            if cur_z_raw is not None:
                pos.cur_z = cur_z_raw
                # 1. Сведение аномалии к нулю при положительном PnL
                if cur_z_raw <= self.exit_z:
                    est = self.engine.emulator.estimate_close_pnl(pos, ob_lo, ob_sh)
                    if est is not None and est >= 0:
                        self.engine.emulator.close_position(
                            pos.trade_id, ob_lo, ob_sh, reason="converged",
                            current_net_edge_pct=cur_z_raw, now=now,
                        )
                        self.engine._event("close", f"Z-Score {pos.symbol} сошелся в ноль (Z={cur_z_raw:+.2f}): закрыт с прибылью +${est:.2f}")
                        continue

                # 2. Стоп-лосс по расширению аномалии
                z_entry = getattr(pos, "z_in", self.entry_z) or self.entry_z
                if cur_z_raw >= self.stop_mult * z_entry:
                    self.engine.emulator.close_position(
                        pos.trade_id, ob_lo, ob_sh, reason="stop",
                        current_net_edge_pct=cur_z_raw, now=now,
                    )
                    self.engine._event("close", f"Z-Score {pos.symbol} СТОП: аномалия расширилась до Z={cur_z_raw:+.2f}")
                    continue

            # 3. Тайм-стоп при положительном PnL
            holding_sec = now - pos.open_ts
            if holding_sec >= self.timestop_sec:
                est = self.engine.emulator.estimate_close_pnl(pos, ob_lo, ob_sh)
                if est is not None and est >= 0:
                    self.engine.emulator.close_position(
                        pos.trade_id, ob_lo, ob_sh, reason="timestop",
                        current_net_edge_pct=pos.cur_z or 0.0, now=now,
                    )
                    self.engine._event("close", f"Z-Score {pos.symbol} тайм-стоп 30м: закрыт с прибылью +${est:.2f}")
                    continue

    def snapshot(self) -> dict[str, Any]:
        """Возвращает срез данных Z-Score для WebSocket."""
        radar = []
        max_abs_z = 0.0
        max_z_info = "—"

        for (sym, ea, eb), st in self.tracker.states.items():
            qa = self.engine.state.get_quote(ea, sym)
            qb = self.engine.state.get_quote(eb, sym)
            if not qa or not qb or qa.best_bid <= 0 or qb.best_ask <= 0:
                continue

            mid_a = (qa.best_bid + qa.best_ask) / 2.0
            mid_b = (qb.best_bid + qb.best_ask) / 2.0
            if mid_a <= 0 or mid_b <= 0:
                continue

            z = st.get_z(mid_a, mid_b)
            if z is None:
                continue

            abs_z = abs(z)
            if abs_z > max_abs_z:
                max_abs_z = abs_z
                max_z_info = f"{sym.split('/')[0]} ({ea}/{eb}) Z={z:+.2f}"

            spread_mid_pct = (mid_a - mid_b) / mid_b * 100.0
            fees_4x = (qa.taker_fee + qb.taker_fee) * 2.0 * 100.0

            radar.append({
                "symbol": sym, "ex_a": ea, "ex_b": eb,
                "z": round(z, 2), "abs_z": round(abs_z, 2),
                "ratio": round(mid_a / mid_b, 5),
                "ma": round(st.ma, 5) if st.ma is not None else 0.0,
                "sd": round(st.sd, 5) if st.sd is not None else 0.0,
                "spread_pct": round(spread_mid_pct, 3),
                "fees_pct": round(fees_4x, 3),
            })

        radar.sort(key=lambda r: r["abs_z"], reverse=True)
        return {
            "enabled": self.enabled,
            "entry_z": self.entry_z,
            "exit_z": self.exit_z,
            "timestop_sec": self.timestop_sec,
            "min_profit_margin_pct": self.min_profit_margin_pct,
            "max_abs_z": round(max_abs_z, 2),
            "max_z_info": max_z_info,
            "radar": radar[:60],
        }
