import sys
sys.stdout.reconfigure(encoding='utf-8')
from cryptoarb.backtest.walkforward import run_walkforward
# test with 5 symbols, 5 days, 3 days train
# monkey patch top40 to 5 symbols
import pathlib
open("output/top40_4ex_small.txt","w").write("\n".join(open("output/top40_4ex.txt").read().splitlines()[:5]))
print("running 5 symbols 5 days train 3")
run_walkforward(top40_file="output/top40_4ex_small.txt", days=5, train_days=3)
print("done small")
