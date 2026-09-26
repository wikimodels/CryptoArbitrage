"""
OBI Lead-Lag Engine (Memory-bounded, Render Free Tier Ready).
Ring buffers with deque(maxlen=200), strictly bounded RAM (< 40MB).
"""

from collections import deque
import logging
import time
from typing import Dict, List, Optional, Tuple
import uuid

from .models import OBISignal, OrderBookDepth5, PaperTrade

log = logging.getLogger("obi_engine")


class OBILeadLagEngine:
    def __init__(
        self,
        lead_exchange: str = "binance",
        lag_exchange: str = "asterdex",
        symbols: Optional[List[str]] = None,
        obi_threshold: float = 0.60,      # 0.60 = 80/20 ratio
        min_lead_lag_bps: float = 0.5,    # Min lag spread to trigger Maker limit
        cooldown_sec: float = 5.0,        # Min time between signals per symbol
        tp_bps: float = 15.0,             # Take-profit in basis points (+0.15%)
        sl_bps: float = 12.0,             # Stop-loss in basis points (-0.12%)
        order_timeout_sec: float = 8.0,   # Unfilled maker cancel timeout
        max_hold_sec: float = 20.0,       # Max holding time
        notional_usd: float = 50.0,       # Simulated trade notional
        maker_fee_bps: float = 0.0,       # Aster DEX / Lag maker fee (0.00% on promos)
    ):
        self.lead_exchange = lead_exchange
        self.lag_exchange = lag_exchange
        self.symbols = [s.upper() for s in (symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"])]
        
        self.obi_threshold = obi_threshold
        self.min_lead_lag_bps = min_lead_lag_bps
        self.cooldown_sec = cooldown_sec
        self.tp_bps = tp_bps
        self.sl_bps = sl_bps
        self.order_timeout_sec = order_timeout_sec
        self.max_hold_sec = max_hold_sec
        self.notional_usd = notional_usd
        self.maker_fee_bps = maker_fee_bps

        # In-memory storage bounded strictly by maxlen
        self._latest_books: Dict[Tuple[str, str], OrderBookDepth5] = {}
        self._signals: deque = deque(maxlen=200)
        self._trades: deque = deque(maxlen=200)
        self._cooldowns: Dict[Tuple[str, str], float] = {}

        # Aggregate Statistics
        self.total_signals = 0
        self.total_filled = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_net_pnl_usd = 0.0

    def on_depth_update(self, snapshot: OrderBookDepth5):
        key = (snapshot.exchange.lower(), snapshot.symbol.upper())
        self._latest_books[key] = snapshot

        # 1. Update active paper trades
        self._update_trades(snapshot)

        # 2. Check for OBI trigger on Lead venue
        if snapshot.exchange.lower() == self.lead_exchange.lower():
            self._check_lead_signal(snapshot)

    def _check_lead_signal(self, lead_snap: OrderBookDepth5):
        sym = lead_snap.symbol.upper()
        now = lead_snap.ts if lead_snap.ts > 0 else time.time()
        obi = lead_snap.obi

        # Determine signal side
        if obi >= self.obi_threshold:
            side = "BUY"
        elif obi <= -self.obi_threshold:
            side = "SELL"
        else:
            return

        # Check cooldown
        last_sig_time = self._cooldowns.get((sym, side), 0.0)
        if now - last_sig_time < self.cooldown_sec:
            return

        # Retrieve lag venue book
        lag_snap = self._latest_books.get((self.lag_exchange.lower(), sym))
        if not lag_snap or not lag_snap.bids or not lag_snap.asks:
            return

        # Check staleness: difference between books must be <= 2.0s
        if abs(now - lag_snap.ts) > 2.0:
            return

        lead_mid = lead_snap.mid_price
        lag_mid = lag_snap.mid_price
        if lead_mid <= 0 or lag_mid <= 0:
            return

        # Calculate lead-lag displacement in basis points
        lead_lag_bps = (lead_mid - lag_mid) / lag_mid * 10000.0

        # We want Aster DEX (lag) to be behind:
        # For BUY: Binance is surging up (lead_mid > lag_mid -> lead_lag_bps > 0)
        # For SELL: Binance is surging down (lead_mid < lag_mid -> lead_lag_bps < 0)
        if side == "BUY" and lead_lag_bps < self.min_lead_lag_bps:
            return
        if side == "SELL" and lead_lag_bps > -self.min_lead_lag_bps:
            return

        # Passed all filters: Record Signal & Place Maker Paper Trade
        self._cooldowns[(sym, side)] = now
        self.total_signals += 1

        # Calculate suggested Maker limit price at BBO
        if side == "BUY":
            limit_price = lag_snap.best_bid  # Join best bid as Maker
        else:
            limit_price = lag_snap.best_ask  # Join best ask as Maker

        sig = OBISignal(
            id=str(uuid.uuid4())[:8],
            symbol=sym,
            side=side,
            lead_exchange=self.lead_exchange,
            lag_exchange=self.lag_exchange,
            lead_obi=round(obi, 3),
            lead_mid=lead_mid,
            lag_mid=lag_mid,
            lag_best_bid=lag_snap.best_bid,
            lag_best_ask=lag_snap.best_ask,
            lead_lag_bps=round(lead_lag_bps, 2),
            suggested_limit_price=limit_price,
            created_at=now,
        )
        self._signals.append(sig)

        # Spawn paper trade
        trade = PaperTrade(
            id=sig.id,
            symbol=sym,
            side=side,
            exchange=self.lag_exchange,
            limit_price=limit_price,
            notional_usd=self.notional_usd,
            entry_ts=now,
            status="PENDING",
        )
        self._trades.append(trade)
        log.info(f"[OBI SIGNAL] {side} {sym} | Lead OBI={obi:+.2f} | Lag={lead_lag_bps:+.1f} bps | Maker Limit={limit_price}")

    def _update_trades(self, snap: OrderBookDepth5):
        if snap.exchange.lower() != self.lag_exchange.lower():
            return

        now = snap.ts if snap.ts > 0 else time.time()
        for trade in list(self._trades):
            if trade.symbol != snap.symbol:
                continue

            # 1. Fill check for PENDING maker order
            if trade.status == "PENDING":
                # Order expired without fill?
                if now - trade.entry_ts > self.order_timeout_sec:
                    trade.status = "EXPIRED"
                    trade.exit_reason = "timeout"
                    continue

                # Did price cross our limit?
                if trade.side == "BUY" and snap.best_ask <= trade.limit_price:
                    trade.status = "FILLED"
                    trade.fill_price = trade.limit_price
                    trade.fill_ts = now
                    self.total_filled += 1
                elif trade.side == "SELL" and snap.best_bid >= trade.limit_price:
                    trade.status = "FILLED"
                    trade.fill_price = trade.limit_price
                    trade.fill_ts = now
                    self.total_filled += 1
                continue

            # 2. Exit check for FILLED position
            if trade.status == "FILLED":
                held_sec = now - trade.fill_ts
                current_mid = snap.mid_price
                if current_mid <= 0:
                    continue

                # Gross return
                if trade.side == "BUY":
                    pnl_bps = (current_mid - trade.fill_price) / trade.fill_price * 10000.0
                else:
                    pnl_bps = (trade.fill_price - current_mid) / trade.fill_price * 10000.0

                should_close = False
                reason = ""

                # Take-Profit
                if pnl_bps >= self.tp_bps:
                    should_close = True
                    reason = "take_profit"
                # Stop-Loss
                elif pnl_bps <= -self.sl_bps:
                    should_close = True
                    reason = "stop_loss"
                # Max Hold Time
                elif held_sec >= self.max_hold_sec:
                    should_close = True
                    reason = "time_stop"

                if should_close:
                    trade.status = "CLOSED"
                    trade.exit_price = current_mid
                    trade.exit_ts = now
                    trade.exit_reason = reason
                    trade.gross_pnl_bps = round(pnl_bps, 2)

                    # Net PnL (accounting for 2x maker fee)
                    net_bps = pnl_bps - (2.0 * self.maker_fee_bps)
                    net_usd = (net_bps / 10000.0) * trade.notional_usd
                    trade.net_pnl_usd = round(net_usd, 4)

                    self.total_net_pnl_usd += trade.net_pnl_usd
                    if net_usd > 0:
                        self.winning_trades += 1
                    else:
                        self.losing_trades += 1

                    log.info(f"[TRADE CLOSED] {trade.side} {trade.symbol} | Reason={reason} | Net PnL={net_usd:+.3f}$ ({pnl_bps:+.1f} bps)")

    def get_snapshot(self) -> dict:
        total_closed = self.winning_trades + self.losing_trades
        win_rate = (self.winning_trades / total_closed * 100.0) if total_closed > 0 else 0.0
        fill_rate = (self.total_filled / self.total_signals * 100.0) if self.total_signals > 0 else 0.0

        books_data = []
        for (ex, sym), b in sorted(self._latest_books.items()):
            books_data.append({
                "exchange": ex,
                "symbol": sym,
                "mid": round(b.mid_price, 4),
                "spread_bps": round(b.spread_bps, 2),
                "obi": round(b.obi, 3),
                "age_ms": round((time.time() - b.ts) * 1000, 0),
            })

        return {
            "lead_exchange": self.lead_exchange,
            "lag_exchange": self.lag_exchange,
            "symbols": self.symbols,
            "stats": {
                "total_signals": self.total_signals,
                "total_filled": self.total_filled,
                "fill_rate_pct": round(fill_rate, 1),
                "total_closed": total_closed,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "win_rate_pct": round(win_rate, 1),
                "net_pnl_usd": round(self.total_net_pnl_usd, 3),
            },
            "books": books_data,
            "latest_signals": [
                {
                    "id": s.id,
                    "symbol": s.symbol,
                    "side": s.side,
                    "lead_obi": s.lead_obi,
                    "lag_bps": s.lead_lag_bps,
                    "limit_price": s.suggested_limit_price,
                    "time": time.strftime("%H:%M:%S", time.localtime(s.created_at)),
                }
                for s in reversed(self._signals)
            ][:15],
            "latest_trades": [
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "side": t.side,
                    "status": t.status,
                    "pnl_bps": t.gross_pnl_bps,
                    "net_usd": t.net_pnl_usd,
                    "reason": t.exit_reason,
                    "time": time.strftime("%H:%M:%S", time.localtime(t.entry_ts)),
                }
                for t in reversed(self._trades)
            ][:15],
        }
