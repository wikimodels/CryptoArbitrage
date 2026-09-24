"""
Тестирование всех компонентов обновленной Z-Score архитектуры:
1. VWAP расчет в risk.py
2. Вычет входных комиссий в emulator.py
3. ZScoreTracker: прогрев, тики, расчет Z-Score
4. Проверка Engine конфигурации и импортов
"""
import sys
import unittest
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from cryptoarb.base import OrderBookLevel, OrderBookSnapshot, Quote
from cryptoarb.risk import simulate_leg_fill
from cryptoarb.emulator import Emulator, VirtualPosition
from cryptoarb.zscore_tracker import ZScoreTracker
from cryptoarb.config import load_config
from cryptoarb.calc import NetEdgeResult


class TestZScoreArchitecture(unittest.TestCase):
    def test_vwap_calculation(self):
        """Проверка честного расчета VWAP по стакану."""
        # Стакан asks:
        # 100 базовых монет по 10.0 (нотионал 1000$)
        # 100 базовых монет по 10.5 (нотионал 1050$)
        bids = [OrderBookLevel(9.9, 100.0)]
        asks = [OrderBookLevel(10.0, 100.0), OrderBookLevel(10.5, 100.0)]
        ob = OrderBookSnapshot("test_ex", "TEST/USDT", 1000.0, bids, asks)

        # 1. Покупка на $500: берется только 1-й уровень
        res1 = simulate_leg_fill(ob, "buy", 500.0)
        self.assertTrue(res1.filled)
        self.assertAlmostEqual(res1.fill_price, 10.0, places=4)

        # 2. Покупка на $1500: $1000 по 10.0 (100 монет) + $500 по 10.5 (47.619 монет)
        # Итого монет = 147.619, VWAP = 1500 / 147.6190476 = 10.16129
        res2 = simulate_leg_fill(ob, "buy", 1500.0)
        self.assertTrue(res2.filled)
        expected_vwap = 1500.0 / (1000.0 / 10.0 + 500.0 / 10.5)
        self.assertAlmostEqual(res2.fill_price, expected_vwap, places=4)
        print("OK: VWAP рассчитывается корректно:", res2.fill_price)

    def test_emulator_fee_deduction(self):
        """Проверка, что estimate_close_pnl честно вычитает входную комиссию."""
        class MockLoggers:
            class MockLog:
                def write(self, data): pass
            errors = MockLog()
            emulator_trades = MockLog()

        class MockStorage:
            def save_emulator_trade(self, data): pass

        class MockAlerts:
            def send_orphan_leg_alert(self, *args): pass

        em = Emulator(MockLoggers(), MockStorage(), MockAlerts(), 1000.0, 3.0)

        # Позиция: входной размер 1000 USDT, комиссии 0.0006 (0.06% на ногу = 1.2 USDT на вход)
        pos = VirtualPosition(
            trade_id="test-1", symbol="TEST/USDT", strategy="arb",
            exch_long="okx", exch_short="bitget", entry_net_edge_pct=1.0,
            entry_price_long=100.0, entry_price_short=102.0,
            open_ts=1000.0, size_usdt=1000.0,
            taker_long=0.0006, taker_short=0.0006,
            funding_rate_long=0.0, funding_interval_long=8.0,
            funding_rate_short=0.0, funding_interval_short=8.0,
            next_funding_ts_long=None, next_funding_ts_short=None,
            entry_fees_usdt=1.2, z_in=4.0
        )

        q_long = Quote("okx", "TEST/USDT", 1010.0, 100.0, 100.1, 10.0, 10.0, 0.0, 8.0, None, 0.0006, 0.0002)
        q_short = Quote("bitget", "TEST/USDT", 1010.0, 101.9, 102.0, 10.0, 10.0, 0.0, 8.0, None, 0.0006, 0.0002)

        # Предположим стаканы идеальные, цена закрытия лонга 100.0, шорта 102.0 (цена не изменилась, price_pnl = 0)
        # Комиссия выхода = (0.0006 + 0.0006) * 1000 = 1.2 USDT
        # Комиссия входа = 1.2 USDT
        # Итого PnL должен быть: price_pnl (0) - total_fees (2.4) = -2.4 USDT!
        pnl = em.estimate_close_pnl(pos, q_long, q_short)
        self.assertIsNotNone(pnl)
        self.assertAlmostEqual(pnl, -2.4, places=4)
        print("OK: estimate_close_pnl честно вычитает полную комиссию входа+выхода:", pnl)

    def test_zscore_tracker(self):
        """Проверка работы ZScoreTracker."""
        zt = ZScoreTracker(period=100)
        # Инициализируем синтетическую историю с ma=1.0, sd=0.01
        for i in range(100):
            zt._get_or_create("BTC/USDT", "ex1", "ex2").push_bar(1.0 + (i % 5 - 2) * 0.005)

        # При соотношении 1.04 Z должен быть положительным и большим
        z = zt.get_z("BTC/USDT", "ex1", "ex2", 1.04, 1.0)
        self.assertIsNotNone(z)
        self.assertGreater(z, 3.0)
        print("OK: ZScoreTracker корректно рассчитывает Z-Score:", round(z, 2))

    def test_config_loading(self):
        """Проверка загрузки zscore параметров из config.yaml."""
        cfg = load_config("config.yaml")
        self.assertIn("zscore", cfg)
        self.assertTrue(cfg["zscore"]["enabled"])
        self.assertEqual(cfg["zscore"]["entry_z"], 4.0)
        self.assertEqual(cfg["zscore"]["exit_z"], 0.0)
        self.assertEqual(cfg["zscore"]["timestop_sec"], 1800)
        print("OK: Конфигурация zscore успешно загружена из config.yaml")


if __name__ == "__main__":
    unittest.main()
