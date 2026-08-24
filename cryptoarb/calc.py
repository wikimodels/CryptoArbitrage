"""
Расчётный модуль (Блок 2 ТЗ): спред между биржами по реальным bid/ask,
нормализация funding к горизонту удержания, издержки (комиссии +
проскальзывание), итоговая формула net_edge_%.

Стратегия (ТЗ п.1): покупаем где дешевле (long), продаём где дороже
(short), зарабатываем на схождении спреда; направление цены актива не
важно — учитываем spread edge + funding edge за вычетом издержек
входа/выхода.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .base import OrderBookSnapshot, Quote


@dataclass
class NetEdgeResult:
    symbol: str
    exch_long: str
    exch_short: str
    raw_spread_pct: float
    funding_edge_pct: float
    fees_pct: float
    slippage_pct: float
    net_edge_pct: float
    passed_threshold: bool


def raw_spread_pct(q_long: Quote, q_short: Quote) -> float:
    """Сырой спред по цене реального входа: покупаем по ask дешёвой,
    продаём по bid дорогой (не по mid — mid завышает edge)."""
    entry_long = q_long.best_ask
    entry_short = q_short.best_bid
    if entry_long <= 0:
        return 0.0
    return (entry_short - entry_long) / entry_long * 100.0


def funding_edge_pct(q_long: Quote, q_short: Quote, holding_hours: float) -> float:
    """Funding edge за горизонт удержания.

    Мы одновременно long на q_long и short на q_short. При funding_rate>0
    лонги платят шортам. Значит на ноге long мы платим, на ноге short —
    получаем. edge = (получили на шорте) - (заплатили на лонге)."""
    n_long = holding_hours / max(q_long.funding_interval_hours, 1e-6)
    n_short = holding_hours / max(q_short.funding_interval_hours, 1e-6)
    cost_long = q_long.funding_rate * n_long
    gain_short = q_short.funding_rate * n_short
    return (gain_short - cost_long) * 100.0


def fees_pct(q_long: Quote, q_short: Quote) -> float:
    """Комиссии вход+выход на обеих ногах (taker/taker — арбитраж входит
    по рынку немедленно): 2 ноги × 2 (вход+выход)."""
    return (q_long.taker_fee * 2 + q_short.taker_fee * 2) * 100.0


def _walk_worst_price(levels, size_usdt: float) -> tuple[float, bool]:
    """Идём по стакану на объём size_usdt, возвращаем (worst_price, filled).
    worst_price — цена последнего задетого уровня (консервативно)."""
    if not levels:
        return (0.0, False)
    remaining = size_usdt
    worst = levels[0].price
    for lvl in levels:
        if remaining <= 0:
            break
        worst = lvl.price
        remaining -= lvl.price * lvl.size
    return (worst, remaining <= 0)


def slippage_pct(ob_long: Optional[OrderBookSnapshot], ob_short: Optional[OrderBookSnapshot],
                 position_size_usdt: float, buffer_pct: float) -> float:
    """Оценка проскальзывания по фактической глубине стакана + буфер.
    Если глубины не хватило — добавляем штраф (частичное исполнение хуже),
    чтобы не занижать издержки."""
    slip = buffer_pct

    if ob_long and ob_long.asks:
        best = ob_long.asks[0].price
        worst, filled = _walk_worst_price(ob_long.asks, position_size_usdt)
        if best > 0:
            slip += abs(worst - best) / best * 100.0
        if not filled:
            slip += buffer_pct  # штраф за нехватку глубины

    if ob_short and ob_short.bids:
        best = ob_short.bids[0].price
        worst, filled = _walk_worst_price(ob_short.bids, position_size_usdt)
        if best > 0:
            slip += abs(best - worst) / best * 100.0
        if not filled:
            slip += buffer_pct

    return slip


def compute_net_edge(
    q_a: Quote, q_b: Quote,
    holding_hours: float,
    min_threshold_pct: float,
    slippage_buffer_pct: float,
    ob_a: Optional[OrderBookSnapshot] = None,
    ob_b: Optional[OrderBookSnapshot] = None,
    position_size_usdt: float = 1000.0,
) -> NetEdgeResult:
    """Выбирает лучшее направление (кто дешевле для покупки) и считает net_edge_%.

    Сравниваем обе стороны корректно по ask/bid:
      dir1: long a / short b, spread = (bid_b - ask_a)/ask_a
      dir2: long b / short a, spread = (bid_a - ask_b)/ask_b
    Берём направление с большим сырым спредом."""
    s_ab = (q_b.best_bid - q_a.best_ask) / q_a.best_ask * 100.0 if q_a.best_ask > 0 else -1e9
    s_ba = (q_a.best_bid - q_b.best_ask) / q_b.best_ask * 100.0 if q_b.best_ask > 0 else -1e9

    if s_ab >= s_ba:
        q_long, q_short, ob_long, ob_short = q_a, q_b, ob_a, ob_b
    else:
        q_long, q_short, ob_long, ob_short = q_b, q_a, ob_b, ob_a

    r_spread = raw_spread_pct(q_long, q_short)
    r_funding = funding_edge_pct(q_long, q_short, holding_hours)
    r_fees = fees_pct(q_long, q_short)
    r_slip = slippage_pct(ob_long, ob_short, position_size_usdt, slippage_buffer_pct)
    net = r_spread + r_funding - r_fees - r_slip

    return NetEdgeResult(
        symbol=q_long.symbol,
        exch_long=q_long.exchange,
        exch_short=q_short.exchange,
        raw_spread_pct=r_spread,
        funding_edge_pct=r_funding,
        fees_pct=r_fees,
        slippage_pct=r_slip,
        net_edge_pct=net,
        passed_threshold=net >= min_threshold_pct,
    )
