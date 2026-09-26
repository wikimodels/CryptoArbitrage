import unittest
from cryptoarb.strategies.obi_lead_lag.models import OrderBookDepth5
from cryptoarb.strategies.obi_lead_lag.engine import OBILeadLagEngine


class TestOBILeadLag(unittest.TestCase):
    def test_obi_calculation(self):
        # 15 bids, 5 asks -> (15 - 5) / (15 + 5) = 10 / 20 = 0.50
        b = OrderBookDepth5(
            exchange="binance",
            symbol="BTCUSDT",
            ts=100.0,
            exchange_ts=100000,
            bids=[(100.0, 10.0), (99.0, 5.0)],
            asks=[(101.0, 3.0), (102.0, 2.0)],
        )
        self.assertAlmostEqual(b.obi, 0.50)
        self.assertAlmostEqual(b.mid_price, 100.5)

    def test_signal_and_paper_trade(self):
        engine = OBILeadLagEngine(
            lead_exchange="binance",
            lag_exchange="asterdex",
            symbols=["BTCUSDT"],
            obi_threshold=0.60,
            min_lead_lag_bps=0.5,
        )

        # 1. Update Aster DEX first with lag price
        aster_book = OrderBookDepth5(
            exchange="asterdex",
            symbol="BTCUSDT",
            ts=100.0,
            exchange_ts=100000,
            bids=[(83900.0, 2.0)],
            asks=[(83910.0, 2.0)],
        )
        engine.on_depth_update(aster_book)

        # 2. Update Binance with huge buy OBI (90/10) and higher mid price
        binance_book = OrderBookDepth5(
            exchange="binance",
            symbol="BTCUSDT",
            ts=100.1,
            exchange_ts=100100,
            bids=[(83950.0, 90.0)],
            asks=[(83960.0, 10.0)],
        )
        engine.on_depth_update(binance_book)

        snap = engine.get_snapshot()
        self.assertEqual(snap["stats"]["total_signals"], 1)
        self.assertEqual(len(snap["latest_signals"]), 1)
        sig = snap["latest_signals"][0]
        self.assertEqual(sig["side"], "BUY")
        self.assertEqual(sig["symbol"], "BTCUSDT")
        self.assertEqual(sig["limit_price"], 83900.0)

        # 3. Simulate Aster DEX price moving up and executing the trade
        aster_update = OrderBookDepth5(
            exchange="asterdex",
            symbol="BTCUSDT",
            ts=100.5,
            exchange_ts=100500,
            bids=[(83900.0, 2.0)],
            asks=[(83900.0, 2.0)],  # Ask touched our bid limit -> filled
        )
        engine.on_depth_update(aster_update)
        snap2 = engine.get_snapshot()
        self.assertEqual(snap2["stats"]["total_filled"], 1)


if __name__ == "__main__":
    unittest.main()
