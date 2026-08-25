"""
Эмулятор сделок на реальных данных (Блок 1.1 п.2 ТЗ).

Фиксы против прошлой версии:
- PnL честный: вычитаются комиссии обеих ног на вход И выход, начисляется
  funding по фактическому времени удержания (раньше это игнорировалось и
  завышало статистику).
- Кулдаун: одна тройка (символ, лонг, шорт) не открывается повторно чаще
  cooldown_sec — иначе один живущий спред плодил бы тысячи дублей.
- Таймаут: позиция старше max_holding_hours закрывается принудительно.
- Orphan-leg моделируется на реальной глубине стакана (risk.py).
- Вход/выход только по реальным bid/ask, без синтетики/рандома.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from typing import Dict

from .base import OrderBookSnapshot, Quote
from .calc import NetEdgeResult
from .risk import check_orphan_leg, simulate_leg_fill


@dataclass
class VirtualPosition:
    trade_id: str
    symbol: str
    strategy: str          # "arb" | "scalp"
    exch_long: str
    exch_short: str
    entry_net_edge_pct: float
    entry_price_long: float
    entry_price_short: float
    open_ts: float
    size_usdt: float
    taker_long: float
    taker_short: float
    funding_rate_long: float
    funding_interval_long: float
    funding_rate_short: float
    funding_interval_short: float
    next_funding_ts_long: float | None
    next_funding_ts_short: float | None
    entry_fees_usdt: float
    entry_raw_spread_pct: float = 0.0
    status: str = "open"


class Emulator:
    def __init__(self, loggers, storage, alerts, position_size_usdt: float,
                 orphan_timeout_sec: float, cooldown_sec: float = 60.0,
                 max_holding_hours: float = 72.0, loss_cooldown_sec: float = 900.0,
                 position_size_mode: str = "dynamic", dynamic_size_pct_of_book: float = 10.0,
                 dynamic_size_top_levels: int = 3, min_position_size_usdt: float = 50.0,
                 max_position_size_usdt: float = 1000.0, max_open_per_pair: int = 1,
                 max_entry_slippage_pct: float = 0.15):
        self.loggers = loggers
        self.storage = storage
        self.alerts = alerts
        self.position_size_usdt = position_size_usdt      # fixed-режим / референс
        self.orphan_timeout_sec = orphan_timeout_sec
        self.cooldown_sec = cooldown_sec
        self.loss_cooldown_sec = loss_cooldown_sec
        self.max_open_per_pair = max(1, max_open_per_pair)
        self.max_holding_hours = max_holding_hours
        self.position_size_mode = position_size_mode
        self.dyn_pct = dynamic_size_pct_of_book
        self.dyn_top_levels = max(1, dynamic_size_top_levels)
        self.min_size = min_position_size_usdt
        self.max_size = max_position_size_usdt
        self.max_entry_slippage_pct = max_entry_slippage_pct
        self.open_positions: dict[str, VirtualPosition] = {}
        self._last_open: dict[tuple[str, str, str], float] = {}
        self._last_loss: dict[tuple[str, str, str], float] = {}
        # stats раздельные: arb и scalp — каждое направление со своей отчётностью
        self.stats: Dict[str, dict] = {"arb": self._new_stats(), "scalp": self._new_stats()}

    @staticmethod
    def _new_stats() -> dict:
        return {"opened": 0, "closed": 0, "wins": 0, "losses": 0, "orphan_aborts": 0,
                "pnl_usdt": 0.0, "fees_usdt": 0.0, "funding_usdt": 0.0,
                "holding_sec_sum": 0.0}

    def calc_dynamic_size(self, ob_long, ob_short) -> float:
        """Размер, не двигающий стакан И не проходящий глубоко в книгу:
        1) набираем только уровни в пределах max_entry_slippage_pct от
           лучшей цены (дальше — тонкие книги съедят спред);
        2) берём dyn_pct% от этого на ХУДШЕЙ из двух ног;
        3) зажимаем в [min_size, max_size]. 0.0 = торговать нельзя."""
        def fillable(levels) -> float:
            if not levels:
                return 0.0
            best = levels[0].price
            if best <= 0:
                return 0.0
            acc = 0.0
            for lvl in levels:
                if abs(lvl.price - best) / best * 100.0 > self.max_entry_slippage_pct:
                    break
                acc += lvl.price * lvl.size
            return acc
        if not ob_long or not ob_long.asks or not ob_short or not ob_short.bids:
            return 0.0
        worst = min(fillable(ob_long.asks), fillable(ob_short.bids))
        if worst <= 0:
            return 0.0
        size = worst * self.dyn_pct / 100.0
        size = min(size, self.max_size)
        return size if size >= self.min_size else 0.0

    # -------------------- OPEN --------------------

    def _cooldown_active(self, key: tuple[str, str, str], now: float) -> bool:
        # лимит одновременных позиций на тройку: уже открыта -> не дублируем
        open_n = sum(1 for p in self.open_positions.values()
                     if (p.symbol, p.exch_long, p.exch_short) == key)
        if open_n >= self.max_open_per_pair:
            return True
        last = self._last_open.get(key)
        if last is not None and (now - last) < self.cooldown_sec:
            return True
        # после убыточного закрытия — расширенный запрет на перезаход
        last_loss = self._last_loss.get(key)
        if last_loss is not None and (now - last_loss) < self.loss_cooldown_sec:
            return True
        return False

    def try_open(self, result: NetEdgeResult, q_long: Quote, q_short: Quote,
                 ob_long: OrderBookSnapshot | None, ob_short: OrderBookSnapshot | None,
                 strategy: str = "arb", size_usdt: float | None = None) -> VirtualPosition | None:
        now = time.time()
        key = (result.symbol, result.exch_long, result.exch_short)
        if self._cooldown_active(key, now):
            return None

        size = size_usdt or self.position_size_usdt
        trade_id = str(uuid.uuid4())
        leg_long = simulate_leg_fill(ob_long, "buy", size)
        leg_short = simulate_leg_fill(ob_short, "sell", size)

        if check_orphan_leg(leg_long, leg_short):
            filled_exch = result.exch_long if leg_long.filled else result.exch_short
            failed_exch = result.exch_short if leg_long.filled else result.exch_long
            self.stats[strategy]["orphan_aborts"] += 1
            self.alerts.send_orphan_leg_alert(result.symbol, filled_exch, failed_exch, trade_id)
            self.loggers.errors.write({
                "event": "orphan_leg", "trade_id": trade_id, "symbol": result.symbol,
                "filled_exchange": filled_exch, "failed_exchange": failed_exch,
            })
            self.storage.save_emulator_trade({
                "trade_id": trade_id, "symbol": result.symbol,
                "exch_long": result.exch_long, "exch_short": result.exch_short,
                "open_ts": now, "entry_net_edge_pct": result.net_edge_pct,
                "orphan_leg": True, "status": "orphan_aborted",
            })
            self._last_open[key] = now  # чтобы не долбить одну и ту же битую пару
            return None

        if not (leg_long.filled and leg_short.filled):
            return None  # обе не прошли — просто не открываем

        entry_fees = (q_long.taker_fee + q_short.taker_fee) * size

        pos = VirtualPosition(
            trade_id=trade_id, symbol=result.symbol, strategy=strategy,
            exch_long=result.exch_long, exch_short=result.exch_short,
            entry_net_edge_pct=result.net_edge_pct,
            entry_price_long=leg_long.fill_price, entry_price_short=leg_short.fill_price,
            open_ts=now, size_usdt=size,
            taker_long=q_long.taker_fee, taker_short=q_short.taker_fee,
            funding_rate_long=q_long.funding_rate, funding_interval_long=q_long.funding_interval_hours,
            funding_rate_short=q_short.funding_rate, funding_interval_short=q_short.funding_interval_hours,
            next_funding_ts_long=q_long.next_funding_ts, next_funding_ts_short=q_short.next_funding_ts,
            entry_fees_usdt=entry_fees,
            entry_raw_spread_pct=result.raw_spread_pct,
        )
        self.open_positions[trade_id] = pos
        self._last_open[key] = now
        self.stats[strategy]["opened"] += 1

        self.loggers.emulator_trades.write({
            "event": "open", "trade_id": trade_id, "symbol": pos.symbol,
            "exch_long": pos.exch_long, "exch_short": pos.exch_short,
            "entry_net_edge_pct": pos.entry_net_edge_pct,
            "entry_price_long": pos.entry_price_long, "entry_price_short": pos.entry_price_short,
            "entry_fees_usdt": entry_fees,
        })
        self.storage.save_emulator_trade({
            "trade_id": trade_id, "symbol": pos.symbol,
            "exch_long": pos.exch_long, "exch_short": pos.exch_short,
            "open_ts": pos.open_ts, "entry_net_edge_pct": pos.entry_net_edge_pct,
            "orphan_leg": False, "status": "open",
        })
        return pos

    # -------------------- CLOSE --------------------

    @staticmethod
    def _funding_payments(next_ts: float | None, interval_h: float, open_ts: float, close_ts: float) -> float:
        """Сколько начислений funding реально прошло за удержание.
        Funding платится дискретно: только если держишь позицию в момент
        начисления. Точный подсчёт — по календарю от next_ts (известен на
        момент входа). Если биржа next_ts не отдала — фолбэк на непрерывное
        начисление (старое поведение, помечено как приближение)."""
        interval_sec = max(interval_h, 1e-6) * 3600.0
        if next_ts and next_ts > 0:
            if close_ts <= next_ts:
                return 0.0
            return float(int((close_ts - next_ts) // interval_sec) + 1)
        return max(0.0, (close_ts - open_ts) / 3600.0 / max(interval_h, 1e-6))

    def try_close(self, trade_id: str, q_long: Quote, q_short: Quote,
                  current_net_edge_pct: float, reason: str = "signal",
                  ob_long: OrderBookSnapshot | None = None,
                  ob_short: OrderBookSnapshot | None = None) -> float | None:
        pos = self.open_positions.get(trade_id)
        if not pos:
            return None

        now = time.time()

        # Выход по реальной глубине (как вход): продаём лонг в bid-стакан,
        # выкупаем шорт из ask-стакана. Если стаканы переданы и глубины
        # не хватает — закрыться НЕЛЬЗЯ (нет ликвидности), позиция остаётся.
        if ob_long is not None and ob_short is not None:
            leg_close_long = simulate_leg_fill(ob_long, "sell", pos.size_usdt)
            leg_close_short = simulate_leg_fill(ob_short, "buy", pos.size_usdt)
            if not (leg_close_long.filled and leg_close_short.filled):
                self.loggers.errors.write({
                    "event": "exit_no_liquidity", "trade_id": trade_id,
                    "symbol": pos.symbol,
                    "long_ok": leg_close_long.filled, "short_ok": leg_close_short.filled,
                })
                return None
            exit_price_long = leg_close_long.fill_price
            exit_price_short = leg_close_short.fill_price
        else:
            # Фолбэк (стаканы недоступны): по лучшим ценам, приближение.
            exit_price_long = q_long.best_bid
            exit_price_short = q_short.best_ask

        price_pnl_long = (exit_price_long - pos.entry_price_long) / pos.entry_price_long * pos.size_usdt
        price_pnl_short = (pos.entry_price_short - exit_price_short) / pos.entry_price_short * pos.size_usdt
        price_pnl = price_pnl_long + price_pnl_short

        exit_fees = (pos.taker_long + pos.taker_short) * pos.size_usdt
        total_fees = pos.entry_fees_usdt + exit_fees

        holding_sec = now - pos.open_ts
        n_long = self._funding_payments(pos.next_funding_ts_long, pos.funding_interval_long,
                                        pos.open_ts, now)
        n_short = self._funding_payments(pos.next_funding_ts_short, pos.funding_interval_short,
                                         pos.open_ts, now)
        funding_usdt = (pos.funding_rate_short * n_short - pos.funding_rate_long * n_long) * pos.size_usdt

        realized_pnl = price_pnl - total_fees + funding_usdt

        self.open_positions.pop(trade_id, None)
        st = self.stats.get(pos.strategy, self.stats["arb"])
        st["closed"] += 1
        st["wins" if realized_pnl >= 0 else "losses"] += 1
        st["pnl_usdt"] += realized_pnl
        st["fees_usdt"] += total_fees
        st["funding_usdt"] += funding_usdt
        st["holding_sec_sum"] += holding_sec

        if realized_pnl < 0:
            self._last_loss[(pos.symbol, pos.exch_long, pos.exch_short)] = now

        self.loggers.emulator_trades.write({
            "event": "close", "trade_id": trade_id, "symbol": pos.symbol,
            "reason": reason, "exit_net_edge_pct": current_net_edge_pct,
            "price_pnl_usdt": price_pnl, "fees_usdt": total_fees,
            "funding_usdt": funding_usdt, "realized_pnl_usdt": realized_pnl,
            "holding_seconds": holding_sec,
        })
        self.storage.save_emulator_trade({
            "trade_id": trade_id, "symbol": pos.symbol, "strategy": pos.strategy,
            "exch_long": pos.exch_long, "exch_short": pos.exch_short,
            "open_ts": pos.open_ts, "close_ts": now,
            "entry_net_edge_pct": pos.entry_net_edge_pct, "exit_net_edge_pct": current_net_edge_pct,
            "price_pnl_usdt": price_pnl, "fees_usdt": total_fees, "funding_usdt": funding_usdt,
            "realized_pnl_usdt": realized_pnl, "holding_seconds": holding_sec,
            "orphan_leg": False, "status": "closed",
        })
        return realized_pnl

    def positions_snapshot(self) -> list[dict]:
        now = time.time()
        return [{
            "trade_id": p.trade_id, "symbol": p.symbol, "strategy": getattr(p, "strategy", "arb"),
            "exch_long": p.exch_long, "exch_short": p.exch_short,
            "entry_net_edge_pct": p.entry_net_edge_pct,
            "entry_raw_spread_pct": getattr(p, "entry_raw_spread_pct", 0.0),
            "entry_price_long": p.entry_price_long, "entry_price_short": p.entry_price_short,
            "taker_long": p.taker_long, "taker_short": p.taker_short,
            "funding_rate_long": p.funding_rate_long, "funding_interval_long": p.funding_interval_long,
            "funding_rate_short": p.funding_rate_short, "funding_interval_short": p.funding_interval_short,
            "next_funding_ts_long": p.next_funding_ts_long, "next_funding_ts_short": p.next_funding_ts_short,
            "entry_fees_usdt": p.entry_fees_usdt,
            "holding_seconds": now - p.open_ts, "size_usdt": p.size_usdt,
        } for p in self.open_positions.values()]
