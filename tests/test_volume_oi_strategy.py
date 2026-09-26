"""
Комплексные модульные тесты для стратегии Volume Spike & Open Interest Momentum (Sniper Maker).
"""
import tempfile
import time
import unittest
from pathlib import Path

from cryptoarb.strategies.volume_oi.detector import Candle1m, VolumeOIDetector
from cryptoarb.strategies.volume_oi.executor import VolumeOIExecutor
from cryptoarb.strategies.volume_oi.models import (
    CoinTier,
    PositionState,
    VolumeOIConfig,
    VolumeOISignal,
    WhitelistItem,
)
from cryptoarb.strategies.volume_oi.selector import VolumeOISelector
from cryptoarb.strategies.volume_oi.strategy import VolumeOIStrategy


class TestVolumeOIStrategy(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.whitelist_path = Path(self.tmp_dir.name) / "test_whitelist.json"
        self.config = VolumeOIConfig(
            whitelist_file=str(self.whitelist_path),
            position_size_usd=10.0,
            pullback_frac=0.30,
            order_timeout_sec=300.0,
            tp_pct=1.5,
            sl_pct=1.0,
            trail_activation_pct=0.8,
            trail_distance_pct=0.5,
            timestop_sec=3600.0,
            cooldown_sec=900.0,
            min_rvol=5.0,
            min_delta_oi_pct=1.5,
            min_body_pct=0.15,
        )
        self.selector = VolumeOISelector(self.whitelist_path)
        self.detector = VolumeOIDetector(self.config, self.selector)
        self.executor = VolumeOIExecutor(self.config, self.selector)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_selector_hysteresis_and_tiering(self):
        """Проверка правил гистерезиса: Admission, Retention, Eviction и Circuit Breaker."""
        now = time.time()

        # 1. Проверка базовой доступности актива из белого списка
        self.assertTrue(self.selector.is_eligible("BTC", now))
        self.assertTrue(self.selector.is_eligible("ENA", now))
        self.assertFalse(self.selector.is_eligible("PENDLE", now))  # В черном списке

        # 2. Правило включения (Admission Rule) для нового токена
        res = self.selector.update_metrics_hysteresis(
            coin="NEWCOIN",
            win_rate=75.0,
            profit_factor=2.2,
            total_trades=8,
            daily_turnover_usd=25_000_000.0,
            p95_1m_volume_usd=50_000.0,
        )
        self.assertEqual(res, "ADMITTED")
        item = self.selector.get_item("NEWCOIN")
        self.assertIsNotNone(item)
        self.assertEqual(item.tier, CoinTier.TIER_1_HEAVYWEIGHT)
        self.assertEqual(item.max_position_usd, 3000.0)

        # 3. Правило удержания (Retention Rule) при легкой просадке винрейта (65%)
        res = self.selector.update_metrics_hysteresis(
            coin="NEWCOIN",
            win_rate=65.0,
            profit_factor=1.3,
            total_trades=12,
            daily_turnover_usd=15_000_000.0,
            p95_1m_volume_usd=25_000.0,
        )
        self.assertEqual(res, "RETAINED")
        self.assertTrue(self.selector.is_eligible("NEWCOIN", now))

        # 4. Правило исключения (Eviction Rule) при деградации винрейта ниже 55%
        res = self.selector.update_metrics_hysteresis(
            coin="NEWCOIN",
            win_rate=52.0,
            profit_factor=0.85,
            total_trades=20,
            daily_turnover_usd=10_000_000.0,
            p95_1m_volume_usd=15_000.0,
        )
        self.assertEqual(res, "EVICTED")
        self.assertFalse(self.selector.is_eligible("NEWCOIN", now))

        # 5. Аварийный стоп-кран (Intraday Circuit Breaker): 3 стопа подряд -> пауза 7 дней
        self.selector.record_trade_result("ENA", is_win=False, now_ts=now)
        self.selector.record_trade_result("ENA", is_win=False, now_ts=now + 100)
        self.assertTrue(self.selector.is_eligible("ENA", now + 150))
        self.selector.record_trade_result("ENA", is_win=False, now_ts=now + 200)

        # После 3-го стопа (отметка now + 200) ENA должна быть заблокирована на 7 дней (до now + 605000)
        self.assertFalse(self.selector.is_eligible("ENA", now + 250))
        # Спустя 7 дней блокировка спадает (now + 605100 > now + 605000)
        self.assertTrue(self.selector.is_eligible("ENA", now + 605100.0))

    def test_detector_signals_and_filters(self):
        """Проверка детектора: отсечение ложных спайков и валидация правильного Sniper Maker сигнала."""
        now = 1700000000.0
        coin = "BTC"
        exchange = "binance"

        # Формируем исторический 24h объем со средним 100 единиц
        for i in range(1440):
            t = now - (1440 - i) * 60.0
            p = 60000.0 + i * 0.1
            self.detector.update_candle(coin, exchange, t, p, p + 5, p - 5, p, 100.0)

        # 1. Спайк объема без притока открытого интереса (ложный вынос стопов) -> СИГНАЛА НЕТ
        self.detector.update_oi(coin, now - 300, 1_000_000_000.0)
        self.detector.update_oi(coin, now, 1_000_000_100.0)  # Delta OI ~ 0.0%
        spike_candle = Candle1m(ts=now, o=60100.0, h=60300.0, l=60090.0, c=60280.0, v=600.0)
        sig = self.detector.check_signal(coin, exchange, now, spike_candle)
        self.assertIsNone(sig, "Спайк без притока OI должен быть отсечен")

        # 2. Спайк объема с притоком OI (+0.5% для BTC), но закрытие со шпилькой (close_pos < 0.70)
        self.detector.update_oi(coin, now, 1_005_000_000.0)  # Delta OI = +0.5%
        wick_candle = Candle1m(ts=now, o=60100.0, h=60500.0, l=60090.0, c=60200.0, v=600.0)
        sig = self.detector.check_signal(coin, exchange, now, wick_candle)
        self.assertIsNone(sig, "Свеча с длинной верхней тенью должна быть отсечена")

        # 3. Идеальный институциональный сигнал (RVOL 6x, Delta OI +0.5%, ClosePos 0.90, Close > EMA50)
        valid_candle = Candle1m(ts=now, o=60100.0, h=60300.0, l=60095.0, c=60290.0, v=600.0)
        sig = self.detector.check_signal(coin, exchange, now, valid_candle)
        self.assertIsNotNone(sig, "Идеальный сигнал должен быть сгенерирован")
        self.assertEqual(sig.direction, 1)  # LONG
        self.assertGreaterEqual(sig.rvol, 4.0)

        # Проверка 30% отката тела свечи: Body = 60290 - 60100 = 190. Pullback = 0.3 * 190 = 57.
        expected_limit = 60290.0 - 57.0  # 60233.0
        self.assertAlmostEqual(sig.limit_price, expected_limit, places=1)
        self.assertLess(sig.limit_price, sig.close_price)  # Вход ниже закрытия импульса

    def test_executor_order_lifecycle(self):
        """Проверка исполнения лимитного ордера, таймаута, подтягивания трейлинг-стопа и расчета PnL."""
        now = 1700000000.0
        sig = VolumeOISignal(
            coin="UNI",
            direction=1,
            timestamp=now,
            rvol=6.5,
            delta_oi_pct=2.1,
            trend_ema=10.0,
            close_price=10.50,
            open_price=10.00,
            limit_price=10.35,  # 30% откат от тела 0.50
            hard_sl_price=10.35 * 0.99,  # -1.0% = 10.2465
            tp_price=10.35 * 1.015,     # +1.5% = 10.50525
            exchanges_confirmed=["binance", "okx"],
        )

        # 1. Создание отложенного лимитного ордера
        pos = self.executor.submit_signal(sig, now)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.status, "PENDING_LIMIT")
        self.assertEqual(pos.size_usd, 10.0)

        # 2. Цена держится выше лимита — ордер остается в ожидании
        events = self.executor.on_market_update("UNI", bid=10.45, ask=10.46, high=10.48, low=10.40, now_ts=now + 60)
        self.assertEqual(len(events), 0)
        self.assertEqual(pos.status, "PENDING_LIMIT")

        # 3. Микро-откат: цена касается лимита 10.35 — ордер исполняется как Maker
        events = self.executor.on_market_update("UNI", bid=10.34, ask=10.35, high=10.38, low=10.33, now_ts=now + 120)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "order_filled")
        self.assertEqual(pos.status, "OPEN")
        self.assertEqual(pos.entry_price, 10.35)

        # 4. Цена растет на +0.96% (до 10.45) и держится вверху — активируется подтягивающийся трейлинг-стоп (+0.8%)
        self.executor.on_market_update("UNI", bid=10.44, ask=10.45, high=10.45, low=10.41, now_ts=now + 180)
        self.assertTrue(pos.trailing_active)
        self.assertEqual(pos.peak_price, 10.45)
        # Дистанция трейлинга 0.5% от пика 10.45 = 10.45 * (1 - 0.005) = 10.39775
        expected_trail_stop = 10.45 * 0.995
        self.assertAlmostEqual(pos.current_stop_price, expected_trail_stop, places=4)

        # 5. Откат цены и срабатывание трейлинг-стопа в плюсе (low=10.38 <= 10.39775)
        events = self.executor.on_market_update("UNI", bid=10.38, ask=10.39, high=10.41, low=10.38, now_ts=now + 240)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "position_closed")
        self.assertEqual(events[0]["exit_reason"], "TRAILING_STOP")
        self.assertGreater(events[0]["net_pnl_usd"], 0.0, "Сделка по трейлинг-стопу должна быть прибыльной")

        # Проверка 15-минутного кулдауна
        self.assertTrue(self.executor.is_in_cooldown("UNI", now + 300))
        self.assertFalse(self.executor.is_in_cooldown("UNI", now + 1200))

    def test_strategy_snapshot_structure(self):
        """Проверка формирования среза аналитики для передачи на фронтенд."""
        dummy_engine = None
        strategy = VolumeOIStrategy(dummy_engine, {"volume_oi": {"enabled": True}})
        snap = strategy.snapshot()

        self.assertTrue(snap["enabled"])
        self.assertGreaterEqual(snap["whitelist_total"], 17)
        self.assertIn("whitelist", snap)
        self.assertIn("open_positions", snap)
        self.assertIn("kpi", snap)
        self.assertIn("recent_signals", snap)


if __name__ == "__main__":
    unittest.main()
