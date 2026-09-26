"""
Юнит-тесты для модульной системы арбитража фандинга:
- TwoLegSyncProtocol (синхронизация двух ног)
- LegFailureHandler (обработка зависшей ноги, выравнивание дельты, слиппедж)
- UTexFundingStrategy (асимметрия овернайтов, скальпинг базиса, часы торгов)
- CryptoFundingStrategy
"""
from datetime import datetime, timezone
import unittest

from cryptoarb.strategies.funding.common import (
    DeltaNeutralPair,
    LegFailureHandler,
    LegFillResult,
    LegSide,
    LegState,
    TwoLegSyncProtocol,
)
from cryptoarb.strategies.funding.utex_funding import UTexFundingStrategy


class TestFundingStrategies(unittest.TestCase):

    def test_two_leg_sync_planning(self):
        proto = TwoLegSyncProtocol(timeout_ms=500, max_slippage_bps=15.0)
        plan = proto.plan_entry(
            symbol_primary="NOK",
            exchange_primary="utex",
            side_primary=LegSide.BUY,
            symbol_hedge="NOKUSDT",
            exchange_hedge="binance",
            target_notional_usd=5000.0,
            price_primary=16.0,
            price_hedge=16.05,
        )

        self.assertEqual(plan["primary"]["side"], LegSide.BUY)
        self.assertEqual(plan["hedge"]["side"], LegSide.SELL)
        self.assertAlmostEqual(plan["primary"]["qty"] * 16.0, 5000.0)
        self.assertAlmostEqual(plan["hedge"]["qty"] * 16.05, 5000.0)
        self.assertEqual(plan["max_slippage_bps"], 15.0)

    def test_leg_failure_partial_fill_equalization(self):
        # Primary исполнилась на $5,000 (312.5 акций по $16)
        primary = LegFillResult(
            exchange="utex",
            symbol="NOK",
            side=LegSide.BUY,
            requested_qty=312.5,
            filled_qty=312.5,
            avg_price=16.0,
            state=LegState.FILLED,
        )
        # Hedge исполнился только частично на $3,000 (187.5 акций по $16)
        hedge = LegFillResult(
            exchange="binance",
            symbol="NOKUSDT",
            side=LegSide.SELL,
            requested_qty=312.5,
            filled_qty=187.5,
            avg_price=16.0,
            state=LegState.PARTIALLY_FILLED,
        )

        action, excess_qty = LegFailureHandler.evaluate_partial_fill(primary, hedge)
        self.assertEqual(action, "REDUCE_PRIMARY")
        # Избыток: ($5000 - $3000) / 16.0 = 125 акций
        self.assertAlmostEqual(excess_qty, 125.0)

    def test_leg_failure_balanced_fill(self):
        primary = LegFillResult(
            exchange="utex",
            symbol="NOK",
            side=LegSide.BUY,
            requested_qty=100.0,
            filled_qty=100.0,
            avg_price=10.0,
            state=LegState.FILLED,
        )
        hedge = LegFillResult(
            exchange="binance",
            symbol="NOKUSDT",
            side=LegSide.SELL,
            requested_qty=100.0,
            filled_qty=100.0,
            avg_price=10.0,
            state=LegState.FILLED,
        )
        action, excess_qty = LegFailureHandler.evaluate_partial_fill(primary, hedge)
        self.assertEqual(action, "BALANCED")
        self.assertEqual(excess_qty, 0.0)

    def test_slippage_guard(self):
        # Ожидали покупку по 100.0
        # Фактическая цена 100.10 (+10 bps) при лимите 15 bps -> OK
        self.assertTrue(LegFailureHandler.check_slippage(100.0, 100.10, LegSide.BUY, max_slippage_bps=15.0))
        # Фактическая цена 100.25 (+25 bps) при лимите 15 bps -> превышение (False)
        self.assertFalse(LegFailureHandler.check_slippage(100.0, 100.25, LegSide.BUY, max_slippage_bps=15.0))

    def test_utex_overnight_asymmetry(self):
        strat = UTexFundingStrategy(engine=None, cfg={"funding_arb": {}})

        # 1. Акции США (NOK) без плеча: овернайт = 0.0%
        equity_calc = strat.calculate_net_daily_yield("NOK", funding_rate=0.0020, is_unleveraged=True)
        self.assertEqual(equity_calc["overnight_daily_pct"], 0.0)
        # Суточный доход при 8ч фандинге (3 выплаты): 0.20% * 3 = 0.60%
        self.assertAlmostEqual(equity_calc["daily_funding_pct"], 0.60)
        self.assertAlmostEqual(equity_calc["net_daily_yield_pct"], 0.60)

        # 2. CME Фьючерс (NATGAS) на весь объем: овернайт = 0.0194%
        futures_calc = strat.calculate_net_daily_yield("NATGAS", funding_rate=0.0020)
        self.assertAlmostEqual(futures_calc["overnight_daily_pct"], 0.0194)
        # Газ имеет 6 выплат в сутки (каждые 4 часа): 0.20% * 6 = 1.20%
        self.assertAlmostEqual(futures_calc["daily_funding_pct"], 1.20)
        # Чистый суточный доход: 1.20% - 0.0194% = 1.1806%
        self.assertAlmostEqual(futures_calc["net_daily_yield_pct"], 1.1806, places=3)

    def test_utex_basis_scalp_detector(self):
        strat = UTexFundingStrategy(engine=None, cfg={"funding_arb": {"tradfi_basis_scalp_bps": 30.0}})

        # Спред раздвинулся: CEX=10.05 vs UTEX=10.00 (+50 bps)
        is_opp, bps, action = strat.evaluate_basis_scalp(cex_price=10.05, utex_price=10.00)
        self.assertTrue(is_opp)
        self.assertEqual(action, "SCALP_SHORT_CEX_LONG_UTEX")
        self.assertAlmostEqual(bps, 50.0)

        # Узкий спред: CEX=10.01 vs UTEX=10.00 (+10 bps < 30 bps)
        is_opp, bps, action = strat.evaluate_basis_scalp(cex_price=10.01, utex_price=10.00)
        self.assertFalse(is_opp)
        self.assertEqual(action, "HOLD")

    def test_utex_market_hours_check(self):
        # Суббота (weekday=5) -> закрыто
        saturday = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
        self.assertFalse(UTexFundingStrategy.is_market_open("US_EQUITY", saturday))
        self.assertFalse(UTexFundingStrategy.is_market_open("COMMODITY_FUTURES", saturday))

        # Вторник 15:00 UTC (сессия NYSE в разгаре) -> открыто
        tuesday_midday = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        self.assertTrue(UTexFundingStrategy.is_market_open("US_EQUITY", tuesday_midday))
        self.assertTrue(UTexFundingStrategy.is_market_open("COMMODITY_FUTURES", tuesday_midday))

    def test_crypto_funding_strategy_config_and_universe(self):
        from unittest.mock import MagicMock
        from cryptoarb.strategies.funding.crypto_funding import CryptoFundingStrategy

        mock_engine = MagicMock()
        mock_engine.symbols = ["BTC/USDT:USDT", "ENA/USDT:USDT"]
        mock_engine.exchanges = [
            "binance", "bybit", "okx", "bitget", "mexc",
            "gateio", "bingx", "hyperliquid", "kucoin", "htx"
        ]
        mock_engine.emulator_enabled = True

        cfg = {
            "funding_arb": {
                "enabled": True,
                "min_funding_diff_8h_pct": 0.10,
                "exit_diff_8h_pct": 0.02,
                "max_raw_spread_pct": 0.20,
                "position_size_usdt": 10.0,
                "max_holding_hours": 336.0,
                "spread_stop_pct": 2.0,
            }
        }
        strat = CryptoFundingStrategy(mock_engine, cfg)
        self.assertEqual(strat.min_diff_8h_pct, 0.10)
        self.assertEqual(strat.exit_diff_8h_pct, 0.02)
        self.assertEqual(strat.max_hold_hours, 336.0)
        self.assertEqual(strat.spread_stop_pct, 2.0)
        self.assertEqual(len(mock_engine.exchanges), 10)

    def test_orderbook_sizer_balanced(self):
        from cryptoarb.base import OrderBookLevel, OrderBookSnapshot
        from cryptoarb.strategies.funding.common import OrderBookSizer

        # Long нога (слабая, биржа MEXC): в топ-3 уровнях Ask лежит по 100 монет по $10 = $3,000
        ob_mexc = OrderBookSnapshot(
            exchange="mexc", symbol="TEST/USDT:USDT", ts=100.0,
            bids=[OrderBookLevel(9.99, 100.0), OrderBookLevel(9.98, 100.0)],
            asks=[OrderBookLevel(10.01, 100.0), OrderBookLevel(10.02, 100.0), OrderBookLevel(10.03, 100.0)],
        )
        # Short нога (сильная, биржа Binance): в топ-3 уровнях Bid лежит по 500 монет по $10 = $15,000
        ob_binance = OrderBookSnapshot(
            exchange="binance", symbol="TEST/USDT:USDT", ts=100.0,
            bids=[OrderBookLevel(10.05, 500.0), OrderBookLevel(10.04, 500.0), OrderBookLevel(10.03, 500.0)],
            asks=[OrderBookLevel(10.06, 500.0)],
        )

        res = OrderBookSizer.calculate_optimal_entry_size(
            ob_long=ob_mexc, ob_short=ob_binance,
            exch_long="mexc", exch_short="binance",
            participation_rate=0.10, top_n_levels=3,
            max_level_slippage_bps=25.0,
        )

        self.assertTrue(res.is_tradable)
        self.assertEqual(res.bottleneck_leg, "long")
        self.assertEqual(res.bottleneck_exchange, "mexc")
        # Ноционал топ-3: 10.01*100 + 10.02*100 + 10.03*100 = 3006. 10% = 300.60
        self.assertAlmostEqual(res.optimal_size_usd, 300.60, places=1)
        self.assertLess(res.estimated_slippage_bps, 2.0)  # Сдвиг менее 2 bps!

    def test_orderbook_sizer_too_thin(self):
        from cryptoarb.base import OrderBookLevel, OrderBookSnapshot
        from cryptoarb.strategies.funding.common import OrderBookSizer

        # Ультра-тонкий стакан: всего $50 в топ-3 уровнях (10% = $5 < $10 min)
        ob_thin = OrderBookSnapshot(
            exchange="thin_cex", symbol="SHIT/USDT:USDT", ts=100.0,
            bids=[OrderBookLevel(1.0, 10.0)],
            asks=[OrderBookLevel(1.01, 10.0), OrderBookLevel(1.02, 10.0), OrderBookLevel(1.03, 10.0)],
        )
        ob_deep = OrderBookSnapshot(
            exchange="binance", symbol="SHIT/USDT:USDT", ts=100.0,
            bids=[OrderBookLevel(1.05, 1000.0)],
            asks=[OrderBookLevel(1.06, 1000.0)],
        )

        res = OrderBookSizer.calculate_optimal_entry_size(
            ob_long=ob_thin, ob_short=ob_deep,
            exch_long="thin_cex", exch_short="binance",
            participation_rate=0.10, min_notional_usd=10.0,
        )
        self.assertFalse(res.is_tradable)
        self.assertEqual(res.optimal_size_usd, 0.0)

    def test_orderbook_sizer_chunking_plan(self):
        from cryptoarb.base import OrderBookLevel, OrderBookSnapshot
        from cryptoarb.strategies.funding.common import OrderBookSizer

        # В топ-3 уровнях $5,000 (10% = $500 безопасный слайс)
        ob_weak = OrderBookSnapshot(
            exchange="utex", symbol="ALT/USDT:USDT", ts=100.0,
            bids=[OrderBookLevel(10.0, 500.0)],
            asks=[OrderBookLevel(10.0, 500.0)],
        )
        ob_deep = OrderBookSnapshot(
            exchange="binance", symbol="ALT/USDT:USDT", ts=100.0,
            bids=[OrderBookLevel(10.0, 5000.0)],
            asks=[OrderBookLevel(10.0, 5000.0)],
        )

        # Целевой объем портфеля $2,000 при безопасном слайсе $500 -> 4 куска
        res = OrderBookSizer.calculate_optimal_entry_size(
            ob_long=ob_weak, ob_short=ob_deep,
            exch_long="utex", exch_short="binance",
            participation_rate=0.10, target_portfolio_usd=2000.0,
        )
        self.assertTrue(res.is_tradable)
        self.assertEqual(res.optimal_size_usd, 500.0)
        self.assertIsNotNone(res.chunk_plan)
        self.assertEqual(res.chunk_plan["recommended_chunks"], 4)
        self.assertEqual(res.chunk_plan["slice_size_usd"], 500.0)


if __name__ == "__main__":
    unittest.main()

