"""
Стратегия дельта-нейтрального сбора фандинга (Funding Rate Arbitrage).
Инкапсулирует поиск межбиржевых перекосов ставок, удержание до 72 часов,
дискретные начисления выплат и строгий контроль расхождения цен.
"""
from __future__ import annotations

import itertools
import logging
import time
from typing import TYPE_CHECKING, Any

from cryptoarb.calc import NetEdgeResult
from cryptoarb.risk import simulate_leg_fill
from cryptoarb.strategies.base import BaseStrategy

if TYPE_CHECKING:
    from cryptoarb.engine import Engine

log = logging.getLogger("strategies.funding")


class FundingArbitrageStrategy(BaseStrategy):
    """Дельта-нейтральный арбитраж ставок финансирования."""

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        super().__init__(engine, cfg)
        self.name = "funding_arb"
        self.display_name = "Сбор фандинга"

        fa = cfg.get("funding_arb", {})
        self.enabled = bool(fa.get("enabled", True))
        self.min_diff_8h_pct = float(fa.get("min_funding_diff_8h_pct", 0.08))
        self.max_raw_spread_pct = float(fa.get("max_raw_spread_pct", 0.20))
        self.position_size = float(fa.get("position_size_usdt", 10.0))
        self.max_hold_hours = float(fa.get("max_holding_hours", 72.0))
        self.spread_stop_pct = float(fa.get("spread_stop_pct", 2.0))

        self.last_scan_ts = 0.0

    async def on_funding_refresh(self) -> None:
        """Реакция на периодическое обновление ставок коннекторами."""
        if self.enabled:
            await self.scan_opportunities()

    async def on_scan_symbol(self, symbol: str, quotes: dict, now: float) -> None:
        """Периодическое сканирование арбитража фандинга."""
        pass  # Сканирование фандинга выполняется комплексно через scan_opportunities каждые 10с

    async def scan_opportunities(self) -> None:
        """Комплексный поиск возможностей межбиржевого сбора фандинга."""
        if not self.enabled or not self.engine.emulator_enabled:
            return

        now = time.time()
        self.last_scan_ts = now

        for sym in self.engine.symbols:
            quotes = self.engine.state.fresh_quotes(sym, self.engine.exchanges)
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
                if rate_diff_8h_pct < self.min_diff_8h_pct:
                    continue

                if q_long.best_ask <= 0 or q_short.best_bid <= 0:
                    continue
                raw_spread_pct = (q_short.best_bid - q_long.best_ask) / q_long.best_ask * 100.0
                if abs(raw_spread_pct) > self.max_raw_spread_pct:
                    continue

                key = (sym, ex_long, ex_short)
                if self.engine.emulator._cooldown_active(key, now):
                    continue

                ob_long = await self.engine._get_ob(ex_long, sym)
                ob_short = await self.engine._get_ob(ex_short, sym)
                if not ob_long or not ob_short:
                    continue

                fill_long = simulate_leg_fill(ob_long, "buy", self.position_size)
                fill_short = simulate_leg_fill(ob_short, "sell", self.position_size)
                if not (fill_long.filled and fill_short.filled):
                    continue

                fee_pct = (q_long.taker_fee + q_short.taker_fee) * 2.0 * 100.0
                res = NetEdgeResult(
                    symbol=sym, exch_long=ex_long, exch_short=ex_short,
                    raw_spread_pct=raw_spread_pct, funding_edge_pct=rate_diff_8h_pct,
                    fees_pct=fee_pct, slippage_pct=0.0, width_pct=0.0,
                    net_edge_pct=rate_diff_8h_pct, passed_threshold=True,
                )

                opened = self.engine.emulator.try_open(
                    res, q_long, q_short, ob_long, ob_short,
                    strategy="funding", size_usdt=self.position_size,
                )
                if opened:
                    self.engine._event(
                        "signal",
                        f"💰 СБОР ФАНДИНГА: {sym} SHORT {ex_short} ({q_short.funding_rate*100:+.3f}%) / "
                        f"LONG {ex_long} ({q_long.funding_rate*100:+.3f}%) Δ={rate_diff_8h_pct:+.3f}% (8ч) | ${self.position_size:.0f} поз."
                    )

    async def check_exits(self, symbol: str, quotes: dict, now: float) -> None:
        """Проверка условий закрытия связок фандинга (72ч, инверсия, стоп по спреду)."""
        active_positions = [
            p for p in self.engine.emulator.open_positions.values()
            if p.symbol == symbol and p.strategy == "funding"
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

            holding_hours = (now - pos.open_ts) / 3600.0

            # 1. Завершение планового горизонта (72 часа = 9 выплат)
            if holding_hours >= self.max_hold_hours:
                self.engine.emulator.close_position(
                    pos.trade_id, ob_lo, ob_sh, reason="funding_target_reached",
                    current_net_edge_pct=0.0, now=now,
                )
                self.engine._event("close", f"Сбор фандинга {pos.symbol}: плановое закрытие (72ч удержания)")
                continue

            # 2. Инверсия ставок
            if q_lo.funding_rate is not None and q_sh.funding_rate is not None:
                cur_rate_diff_8h = (q_sh.funding_rate - q_lo.funding_rate) * 100.0
                if cur_rate_diff_8h < 0.0:
                    self.engine.emulator.close_position(
                        pos.trade_id, ob_lo, ob_sh, reason="funding_inverted",
                        current_net_edge_pct=cur_rate_diff_8h, now=now,
                    )
                    self.engine._event("close", f"Сбор фандинга {pos.symbol}: закрыт по инверсии ставок ({cur_rate_diff_8h:+.3f}%)")
                    continue

            # 3. Защитный стоп-лосс при сильном расхождении спот-цен
            if ob_lo.bids and ob_sh.asks and ob_sh.asks[0].price > 0:
                cur_spread_pct = (ob_lo.bids[0].price - ob_sh.asks[0].price) / ob_sh.asks[0].price * 100.0
                if abs(cur_spread_pct - (pos.entry_raw_spread_pct or 0.0)) > self.spread_stop_pct:
                    self.engine.emulator.close_position(
                        pos.trade_id, ob_lo, ob_sh, reason="funding_spread_stop",
                        current_net_edge_pct=cur_spread_pct, now=now,
                    )
                    self.engine._event("close", f"Сбор фандинга {pos.symbol}: СТОП по расширению спреда до {cur_spread_pct:+.2f}%")
                    continue

    def snapshot(self) -> dict[str, Any]:
        """Формирование среза аналитики, KPI, возможностей и матрицы для дашборда."""
        now = time.time()
        funding_opps = []
        matrix_rows = []

        all_open_positions = list(self.engine.emulator.open_positions.values())
        funding_positions = [p for p in all_open_positions if p.strategy == "funding"]
        n_funding = len(funding_positions)

        daily_dripping_sum = 0.0
        payment_8h_dripping_sum = 0.0
        total_open_funding_accrued = 0.0

        for p in funding_positions:
            rate_sh = p.funding_rate_short or 0.0
            rate_lo = p.funding_rate_long or 0.0
            diff_8h = (rate_sh - rate_lo)
            sz = p.size_usdt or self.position_size
            inc_8h = sz * diff_8h
            inc_24h = inc_8h * 3.0
            daily_dripping_sum += inc_24h
            payment_8h_dripping_sum += inc_8h
            total_open_funding_accrued += (p.funding_accrued_usdt or 0.0)

        # Сканирование возможностей для радара и матрицы
        for sym in self.engine.symbols:
            quotes = self.engine.state.fresh_quotes(sym, self.engine.exchanges)
            if len(quotes) < 2:
                continue

            row_rates = {}
            for ex in self.engine.exchanges:
                q = quotes.get(ex)
                if q and q.funding_rate is not None:
                    row_rates[ex] = round(q.funding_rate * 100.0, 4)

            if len(row_rates) >= 2:
                max_r = max(row_rates.values())
                min_r = min(row_rates.values())
                matrix_rows.append({
                    "symbol": sym,
                    "base": sym.split("/")[0].split(":")[0],
                    "rates": row_rates,
                    "max_rate": max_r,
                    "min_rate": min_r,
                    "delta": round(max_r - min_r, 4),
                })

            for (e1, q1), (e2, q2) in itertools.combinations(quotes.items(), 2):
                if q1.funding_rate is None or q2.funding_rate is None:
                    continue

                if q1.funding_rate >= q2.funding_rate:
                    sh_ex, sh_q = e1, q1
                    lo_ex, lo_q = e2, q2
                else:
                    sh_ex, sh_q = e2, q2
                    lo_ex, lo_q = e1, q1

                diff_8h = (sh_q.funding_rate - lo_q.funding_rate) * 100.0
                if diff_8h < 0.01:
                    continue

                raw_spread = 0.0
                if lo_q.best_ask > 0 and sh_q.best_bid > 0:
                    raw_spread = (sh_q.best_bid - lo_q.best_ask) / lo_q.best_ask * 100.0

                fee_pct = (lo_q.taker_fee + sh_q.taker_fee) * 2.0 * 100.0
                inc_8h = self.position_size * (diff_8h / 100.0)
                inc_24h = inc_8h * 3.0
                inc_3d = inc_8h * 9.0
                net_3d = inc_3d - (self.position_size * fee_pct / 100.0 * 2.0)

                is_open = any(
                    p.strategy == "funding" and p.symbol == sym and
                    p.exch_short == sh_ex and p.exch_long == lo_ex
                    for p in funding_positions
                )
                passed = diff_8h >= self.min_diff_8h_pct and abs(raw_spread) <= self.max_raw_spread_pct

                funding_opps.append({
                    "symbol": sym,
                    "base": sym.split("/")[0].split(":")[0],
                    "exch_short": sh_ex,
                    "exch_long": lo_ex,
                    "rate_short_8h_pct": round(sh_q.funding_rate * 100.0, 4),
                    "rate_long_8h_pct": round(lo_q.funding_rate * 100.0, 4),
                    "diff_8h_pct": round(diff_8h, 4),
                    "apr_pct": round(diff_8h * 3.0 * 365.0, 1),
                    "raw_spread_pct": round(raw_spread, 3),
                    "inc_8h_usdt": round(inc_8h, 4),
                    "inc_24h_usdt": round(inc_24h, 4),
                    "inc_3d_usdt": round(inc_3d, 4),
                    "net_3d_usdt": round(net_3d, 4),
                    "is_open": is_open,
                    "passed": passed,
                })

        funding_opps.sort(key=lambda o: o["diff_8h_pct"], reverse=True)
        matrix_rows.sort(key=lambda m: m["delta"], reverse=True)

        emu_stats = self.engine.emulator.stats
        closed_funding_trades = emu_stats.get("funding", {}).get("closed", 0)
        closed_funding_pnl = emu_stats.get("funding", {}).get("pnl_usdt", 0.0)
        closed_funding_funding_usdt = emu_stats.get("funding", {}).get("funding_usdt", 0.0)
        total_funding_earned_usdt = closed_funding_funding_usdt + total_open_funding_accrued
        funding_wr = round(emu_stats.get("funding", {}).get("wins", 0) / closed_funding_trades * 100.0, 1) if closed_funding_trades else 0.0

        return {
            "kpi": {
                "total_funding_earned_usdt": round(total_funding_earned_usdt, 4),
                "daily_dripping_income_usdt": round(daily_dripping_sum, 4),
                "payment_8h_dripping_income_usdt": round(payment_8h_dripping_sum, 4),
                "open_positions_count": n_funding,
                "open_margin_usdt": round(n_funding * (self.position_size / 10.0), 2),
                "open_notional_usdt": round(n_funding * self.position_size, 2),
                "closed_trades_count": closed_funding_trades,
                "win_rate_pct": funding_wr,
                "closed_pnl_usdt": round(closed_funding_pnl, 4),
                "best_opportunity": funding_opps[0] if funding_opps else None,
                "min_diff_8h_pct": self.min_diff_8h_pct,
                "max_raw_spread_pct": self.max_raw_spread_pct,
                "position_size_usdt": self.position_size,
            },
            "opportunities": funding_opps[:100],
            "matrix": matrix_rows[:150],
        }
