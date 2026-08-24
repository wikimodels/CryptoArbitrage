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

from .base import OrderBookSnapshot, Quote
from .calc import NetEdgeResult
from .risk import check_orphan_leg, simulate_leg_fill


@dataclass
class VirtualPosition:
    trade_id: str
    symbol: str
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
    entry_fees_usdt: float
    status: str = "open"


class Emulator:
    def __init__(self, loggers, storage, alerts, position_size_usdt: float,
                 orphan_timeout_sec: float, cooldown_sec: float = 60.0,
                 max_holding_hours: float = 72.0, loss_cooldown_sec: float = 900.0):
        self.loggers = loggers
        self.storage = storage
        self.alerts = alerts
        self.position_size_usdt = position_size_usdt
        self.orphan_timeout_sec = orphan_timeout_sec
        self.cooldown_sec = cooldown_sec
        self.loss_cooldown_sec = loss_cooldown_sec
        self.max_holding_hours = max_holding_hours
        self.open_positions: dict[str, VirtualPosition] = {}
        self._last_open: dict[tuple[str, str, str], float] = {}
        self._last_loss: dict[tuple[str, str, str], float] = {}
        self.stats = {
            "trades_closed": 0, "wins": 0, "losses": 0,
            "orphan_aborts": 0, "opened": 0,
            "pnl_usdt": 0.0, "fees_usdt": 0.0, "funding_usdt": 0.0,
            "holding_sec_sum": 0.0,
        }

    # -------------------- OPEN --------------------

    def _cooldown_active(self, key: tuple[str, str, str], now: float) -> bool:
        last = self._last_open.get(key)
        if last is not None and (now - last) < self.cooldown_sec:
            return True
        # после убыточного закрытия — расширенный запрет на перезаход
        last_loss = self._last_loss.get(key)
        if last_loss is not None and (now - last_loss) < self.loss_cooldown_sec:
            return True
        return False

    def try_open(self, result: NetEdgeResult, q_long: Quote, q_short: Quote,
                 ob_long: OrderBookSnapshot | None, ob_short: OrderBookSnapshot | None) -> VirtualPosition | None:
        now = time.time()
        key = (result.symbol, result.exch_long, result.exch_short)
        if self._cooldown_active(key, now):
            return None

        trade_id = str(uuid.uuid4())
        leg_long = simulate_leg_fill(ob_long, "buy", self.position_size_usdt)
        leg_short = simulate_leg_fill(ob_short, "sell", self.position_size_usdt)

        if check_orphan_leg(leg_long, leg_short):
            filled_exch = result.exch_long if leg_long.filled else result.exch_short
            failed_exch = result.exch_short if leg_long.filled else result.exch_long
            self.stats["orphan_aborts"] += 1
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

        entry_fees = (q_long.taker_fee + q_short.taker_fee) * self.position_size_usdt

        pos = VirtualPosition(
            trade_id=trade_id, symbol=result.symbol,
            exch_long=result.exch_long, exch_short=result.exch_short,
            entry_net_edge_pct=result.net_edge_pct,
            entry_price_long=leg_long.fill_price, entry_price_short=leg_short.fill_price,
            open_ts=now, size_usdt=self.position_size_usdt,
            taker_long=q_long.taker_fee, taker_short=q_short.taker_fee,
            funding_rate_long=q_long.funding_rate, funding_interval_long=q_long.funding_interval_hours,
            funding_rate_short=q_short.funding_rate, funding_interval_short=q_short.funding_interval_hours,
            entry_fees_usdt=entry_fees,
        )
        self.open_positions[trade_id] = pos
        self._last_open[key] = now
        self.stats["opened"] += 1

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

    def try_close(self, trade_id: str, q_long: Quote, q_short: Quote,
                  current_net_edge_pct: float, reason: str = "signal") -> float | None:
        pos = self.open_positions.pop(trade_id, None)
        if not pos:
            return None

        now = time.time()
        exit_price_long = q_long.best_bid    # закрываем лонг продажей по bid
        exit_price_short = q_short.best_ask  # закрываем шорт покупкой по ask

        price_pnl_long = (exit_price_long - pos.entry_price_long) / pos.entry_price_long * pos.size_usdt
        price_pnl_short = (pos.entry_price_short - exit_price_short) / pos.entry_price_short * pos.size_usdt
        price_pnl = price_pnl_long + price_pnl_short

        exit_fees = (pos.taker_long + pos.taker_short) * pos.size_usdt
        total_fees = pos.entry_fees_usdt + exit_fees

        holding_sec = now - pos.open_ts
        holding_h = holding_sec / 3600.0
        n_long = holding_h / max(pos.funding_interval_long, 1e-6)
        n_short = holding_h / max(pos.funding_interval_short, 1e-6)
        funding_usdt = (pos.funding_rate_short * n_short - pos.funding_rate_long * n_long) * pos.size_usdt

        realized_pnl = price_pnl - total_fees + funding_usdt

        self.stats["trades_closed"] += 1
        self.stats["wins" if realized_pnl >= 0 else "losses"] += 1
        self.stats["pnl_usdt"] += realized_pnl
        self.stats["fees_usdt"] += total_fees
        self.stats["funding_usdt"] += funding_usdt
        self.stats["holding_sec_sum"] += holding_sec

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
            "trade_id": trade_id, "symbol": pos.symbol,
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
            "trade_id": p.trade_id, "symbol": p.symbol,
            "exch_long": p.exch_long, "exch_short": p.exch_short,
            "entry_net_edge_pct": p.entry_net_edge_pct,
            "holding_seconds": now - p.open_ts, "size_usdt": p.size_usdt,
        } for p in self.open_positions.values()]
