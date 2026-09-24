import sys
sys.stdout.reconfigure(encoding='utf-8')
from cryptoarb.backtest.walkforward import run_walkforward
run_walkforward(top40_file="output/top40_4ex.txt", days=5)
