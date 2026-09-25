"""
Утилита принудительного обновления и ротации 1-минутных свечей для data/raw_1m_30d.
Запуск в 4 потока через cryptoarb.candle_updater.
"""
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")

from cryptoarb.candle_updater import sync_candles

if __name__ == "__main__":
    retention = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    sync_candles(force=True, retention_days=retention)
