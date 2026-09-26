"""
Модуль исполнения и управления жизненным циклом ордеров и позиций (Executor).
Реализует пассивный вход на 30% откате тела свечи (Maker Limit Pullback),
контроль таймаута лимита (5 минут), подтягивающийся трейлинг-стоп (+0.8%/0.5%),
жесткий стоп-лосс (-1.0%), тайм-стоп (60 минут) и межимпульсный кулдаун (15 минут).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from .models import PositionState, VolumeOIConfig, VolumeOISignal
from .selector import VolumeOISelector

log = logging.getLogger("volume_oi.executor")


class VolumeOIExecutor:
    """Управление заявками, позициями и риск-менеджментом стратегии."""

    def __init__(self, config: VolumeOIConfig, selector: VolumeOISelector):
        self.config = config
        self.selector = selector

        # Активные и отложенные позиции: { position_id: PositionState }
        self.positions: dict[str, PositionState] = {}
        # Индекс активной позиции по монете: { coin: position_id }
        self._coin_to_pos: dict[str, str] = {}
        # Кулдауны после закрытия сделок: { coin: expire_ts }
        self.cooldowns: dict[str, float] = {}
        # История закрытых сделок
        self.closed_positions: list[PositionState] = []

    def _clean_base(self, symbol_or_coin: str) -> str:
        return symbol_or_coin.split("/")[0].split("_")[0].upper()

    def is_in_cooldown(self, coin: str, now_ts: float) -> bool:
        base = self._clean_base(coin)
        until = self.cooldowns.get(base, 0.0)
        return now_ts < until

    def has_active_position(self, coin: str) -> bool:
        base = self._clean_base(coin)
        pos_id = self._coin_to_pos.get(base)
        if not pos_id:
            return False
        pos = self.positions.get(pos_id)
        return pos is not None and pos.status in ("PENDING_LIMIT", "OPEN")

    def submit_signal(self, signal: VolumeOISignal, now_ts: float) -> PositionState | None:
        """
        Создание отложенного лимитного ордера на откате 30% от тела свечи.
        Если монета в кулдауне или уже есть открытая позиция — сигнал отклоняется.
        """
        base = self._clean_base(signal.coin)
        if self.is_in_cooldown(base, now_ts):
            log.debug("Сигнал по %s пропущен: активен кулдаун", base)
            return None

        if self.has_active_position(base):
            log.debug("Сигнал по %s пропущен: уже есть открытая/отложенная позиция", base)
            return None

        item = self.selector.get_item(base)
        pos_size = self.config.position_size_usd
        trail_act = item.trail_act_pct if item else self.config.trail_activation_pct
        trail_dist = item.trail_dist_pct if item else self.config.trail_distance_pct

        pos_id = f"voi_{base}_{int(now_ts)}_{uuid.uuid4().hex[:6]}"
        pos = PositionState(
            position_id=pos_id,
            coin=base,
            direction=signal.direction,
            entry_price=signal.limit_price,
            entry_ts=0.0,
            size_usd=pos_size,
            status="PENDING_LIMIT",
            limit_order_ts=now_ts,
            limit_price=signal.limit_price,
            tp_price=signal.tp_price,
            hard_sl_price=signal.hard_sl_price,
            trail_act_pct=trail_act,
            trail_dist_pct=trail_dist,
            trailing_active=False,
            peak_price=signal.limit_price,
            current_stop_price=signal.hard_sl_price,
        )

        self.positions[pos_id] = pos
        self._coin_to_pos[base] = pos_id
        log.info(
            "Выставлен лимитный ордер (Maker Pullback): %s %s @ %.5f (SL: %.5f, TP: %.5f)",
            "LONG" if pos.direction == 1 else "SHORT",
            base,
            pos.limit_price,
            pos.hard_sl_price,
            pos.tp_price,
        )
        return pos

    def on_market_update(
        self,
        coin: str,
        bid: float,
        ask: float,
        high: float,
        low: float,
        now_ts: float,
    ) -> list[dict[str, Any]]:
        """
        Обновление состояния ордеров и позиций по тику/свече:
        - Исполнение PENDING_LIMIT ордеров при касании цены лимита.
        - Отмена PENDING_LIMIT по таймауту (5 минут).
        - Контроль TP, SL, Trailing Stop и Time Stop для OPEN позиций.
        """
        base = self._clean_base(coin)
        pos_id = self._coin_to_pos.get(base)
        if not pos_id:
            return []

        pos = self.positions.get(pos_id)
        if not pos or pos.status == "CLOSED":
            return []

        events: list[dict[str, Any]] = []

        # 1. Обработка отложенного лимитного ордера (PENDING_LIMIT)
        if pos.status == "PENDING_LIMIT":
            # Проверка таймаута (5 минут / 300 секунд)
            if now_ts - pos.limit_order_ts > self.config.order_timeout_sec:
                pos.status = "CLOSED"
                pos.exit_ts = now_ts
                pos.exit_reason = "EXPIRED_UNFILLED"
                del self._coin_to_pos[base]
                self.closed_positions.append(pos)
                log.info("Отменен лимитный ордер по таймауту: %s", base)
                events.append({"event": "order_expired", "position_id": pos_id, "coin": base})
                return events

            # Проверка касания уровня лимитного ордера
            filled = False
            if pos.direction == 1 and low <= pos.limit_price:
                filled = True
            elif pos.direction == -1 and high >= pos.limit_price:
                filled = True

            if filled:
                pos.status = "OPEN"
                pos.entry_price = pos.limit_price
                pos.entry_ts = now_ts
                pos.peak_price = pos.limit_price
                pos.current_stop_price = pos.hard_sl_price
                log.info(
                    "Исполнен Maker лимит: %s %s @ %.5f",
                    "LONG" if pos.direction == 1 else "SHORT",
                    base,
                    pos.entry_price,
                )
                events.append({
                    "event": "order_filled",
                    "position_id": pos_id,
                    "coin": base,
                    "entry_price": pos.entry_price,
                    "direction": pos.direction,
                })

        # 2. Обработка открытой позиции (OPEN)
        if pos.status == "OPEN":
            should_close = False
            exit_price = pos.entry_price
            exit_reason = ""

            if pos.direction == 1:
                fav = (high - pos.entry_price) / pos.entry_price
                adv = (low - pos.entry_price) / pos.entry_price

                # Тейк-профит
                if high >= pos.tp_price:
                    should_close = True
                    exit_price = pos.tp_price
                    exit_reason = "TAKE_PROFIT"
                # Жесткий стоп-лосс
                elif low <= pos.hard_sl_price:
                    should_close = True
                    exit_price = pos.hard_sl_price
                    exit_reason = "HARD_STOP"
                else:
                    # Подтягивающийся трейлинг-стоп
                    if fav >= pos.trail_act_pct / 100.0:
                        pos.trailing_active = True
                        if high > pos.peak_price:
                            pos.peak_price = high
                            pos.current_stop_price = pos.peak_price * (1.0 - pos.trail_dist_pct / 100.0)

                    if pos.trailing_active and low <= pos.current_stop_price:
                        should_close = True
                        exit_price = pos.current_stop_price
                        exit_reason = "TRAILING_STOP"
                    # Тайм-стоп (60 минут)
                    elif now_ts - pos.entry_ts >= self.config.timestop_sec:
                        should_close = True
                        exit_price = bid if bid > 0 else pos.entry_price
                        exit_reason = "TIMESTOP"

            else:  # SHORT
                fav = (pos.entry_price - low) / pos.entry_price
                adv = (high - pos.entry_price) / pos.entry_price

                # Тейк-профит
                if low <= pos.tp_price:
                    should_close = True
                    exit_price = pos.tp_price
                    exit_reason = "TAKE_PROFIT"
                # Жесткий стоп-лосс
                elif high >= pos.hard_sl_price:
                    should_close = True
                    exit_price = pos.hard_sl_price
                    exit_reason = "HARD_STOP"
                else:
                    # Подтягивающийся трейлинг-стоп
                    if fav >= pos.trail_act_pct / 100.0:
                        pos.trailing_active = True
                        if pos.peak_price == 0.0 or low < pos.peak_price:
                            pos.peak_price = low
                            pos.current_stop_price = pos.peak_price * (1.0 + pos.trail_dist_pct / 100.0)

                    if pos.trailing_active and high >= pos.current_stop_price:
                        should_close = True
                        exit_price = pos.current_stop_price
                        exit_reason = "TRAILING_STOP"
                    # Тайм-стоп (60 минут)
                    elif now_ts - pos.entry_ts >= self.config.timestop_sec:
                        should_close = True
                        exit_price = ask if ask > 0 else pos.entry_price
                        exit_reason = "TIMESTOP"

            if should_close:
                pos.status = "CLOSED"
                pos.exit_ts = now_ts
                pos.exit_price = exit_price
                pos.exit_reason = exit_reason

                # Расчет PnL
                if pos.direction == 1:
                    gross_pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100.0
                else:
                    gross_pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100.0

                # Комиссия: 0.04% на круг ($0.004 на позицию $10)
                fee_roundtrip_usd = 0.0004 * pos.size_usd
                net_pnl_usd = (gross_pnl_pct / 100.0) * pos.size_usd - fee_roundtrip_usd

                pos.gross_pnl_pct = round(gross_pnl_pct, 3)
                pos.net_pnl_usd = round(net_pnl_usd, 4)

                del self._coin_to_pos[base]
                self.closed_positions.append(pos)

                # Установка 15-минутного кулдауна
                self.cooldowns[base] = now_ts + self.config.cooldown_sec

                # Передача результата в селектор для отслеживания стоп-крана
                is_win = net_pnl_usd > 0
                self.selector.record_trade_result(base, is_win, now_ts)

                log.info(
                    "Закрыта позиция %s (%s): Exit @ %.5f | Reason: %s | Gross: %+.2f%% | Net: $%+.3f",
                    base,
                    "LONG" if pos.direction == 1 else "SHORT",
                    exit_price,
                    exit_reason,
                    pos.gross_pnl_pct,
                    pos.net_pnl_usd,
                )
                events.append({
                    "event": "position_closed",
                    "position_id": pos_id,
                    "coin": base,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "gross_pnl_pct": pos.gross_pnl_pct,
                    "net_pnl_usd": pos.net_pnl_usd,
                })

        return events

    def get_open_positions(self) -> list[dict[str, Any]]:
        return [
            pos.to_dict()
            for pos in self.positions.values()
            if pos.status in ("PENDING_LIMIT", "OPEN")
        ]

    def get_kpi_summary(self) -> dict[str, Any]:
        """Расчет агрегированных метрик результативности стратегии."""
        closed = [p for p in self.closed_positions if p.exit_reason != "EXPIRED_UNFILLED"]
        total_trades = len(closed)
        if total_trades == 0:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "net_pnl_usd": 0.0,
                "wins": 0,
                "losses": 0,
            }

        wins = sum(1 for p in closed if p.net_pnl_usd > 0)
        losses = sum(1 for p in closed if p.net_pnl_usd <= 0)
        win_rate = (wins / total_trades) * 100.0

        gross_wins = sum(p.net_pnl_usd for p in closed if p.net_pnl_usd > 0)
        gross_losses = abs(sum(p.net_pnl_usd for p in closed if p.net_pnl_usd <= 0))
        pf = (gross_wins / gross_losses) if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)
        net_pnl = sum(p.net_pnl_usd for p in closed)

        return {
            "total_trades": total_trades,
            "win_rate": round(win_rate, 1),
            "profit_factor": round(pf, 2),
            "net_pnl_usd": round(net_pnl, 4),
            "wins": wins,
            "losses": losses,
        }
