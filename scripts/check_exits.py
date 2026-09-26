import json

# Let's inspect simulate_model_for_coin for Model 5 to see exit reasons
from cryptoarb.research.volume_oi_backtest import load_coin_dataset, simulate_model_for_coin, TARGET_COINS
from collections import Counter

counts = Counter()
pnl_by_reason = {}

for coin in TARGET_COINS:
    df = load_coin_dataset(coin)
    if df is not None:
        trades = simulate_model_for_coin(df, coin, "Model_5_Cross_Exchange_Plus_OI", len(df)//2)
        for t in trades:
            counts[t.exit_reason] += 1
            if t.exit_reason not in pnl_by_reason:
                pnl_by_reason[t.exit_reason] = []
            pnl_by_reason[t.exit_reason].append(t.net_pnl_usd)

print("Exit reason counts for Model 5:")
for reason, cnt in counts.items():
    pnls = pnl_by_reason[reason]
    avg_p = sum(pnls)/len(pnls)
    tot_p = sum(pnls)
    print(f"  {reason:<15}: count={cnt:<4} ({cnt/sum(counts.values())*100:.1f}%), avg_pnl=${avg_p:+.4f}, total_pnl=${tot_p:+.3f}")
