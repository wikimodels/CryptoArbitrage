"""Алерты (Блок 4 ТЗ). Консоль + system.log. Telegram добавляется тем же
интерфейсом send_* без изменения логики скоринга/эмулятора."""
from __future__ import annotations

from .calc import NetEdgeResult


class ConsoleAlertChannel:
    def send_signal(self, r: NetEdgeResult):
        print(
            f"[SIGNAL] {r.symbol:<20} LONG {r.exch_long:<9} / SHORT {r.exch_short:<9} "
            f"net={r.net_edge_pct:+.3f}%  "
            f"(spread={r.raw_spread_pct:+.3f}% fund={r.funding_edge_pct:+.3f}% "
            f"fees=-{r.fees_pct:.3f}% slip=-{r.slippage_pct:.3f}%)"
        )

    def send_orphan_leg_alert(self, symbol: str, exch_filled: str, exch_failed: str, trade_id: str):
        print(
            f"[!!! ORPHAN LEG !!!] {symbol} — нога на {exch_filled} исполнилась, "
            f"на {exch_failed} нет. trade_id={trade_id}"
        )

    def send_system(self, message: str):
        print(f"[SYSTEM] {message}")

    def send_error(self, message: str):
        print(f"[ERROR] {message}")
