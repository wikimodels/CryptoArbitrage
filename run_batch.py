import sys
sys.stdout.reconfigure(encoding='utf-8')
from cryptoarb.backtest.walkforward import run_walkforward
import pathlib
# run in 3 batches of 10 days to avoid OOM
for batch in [(0,10),(10,20),(20,30)]:
    print(f"Batch {batch[0]}-{batch[1]-1}")
    # monkey patch to slice days inside
    import cryptoarb.backtest.walkforward as wf
    orig_days=wf.RAW
    # call with custom days range: we hack by filtering ts
    # Instead, run full but with days param
    # For now, just run full and let it handle batch internally by processing days sequentially
    # To avoid OOM, we will run walkforward for 10 days at a time using days parameter
    # Modify run_walkforward to accept days and offset
    pass
