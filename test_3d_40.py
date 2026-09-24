import sys
sys.stdout.reconfigure(encoding='utf-8')
from cryptoarb.backtest.walkforward import run_walkforward
print("start 40 symbols 5 days train 3")
run_walkforward(top40_file="output/top40_4ex.txt", days=5, train_days=3)
print("done")
