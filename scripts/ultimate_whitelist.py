from cryptoarb.research.volume_oi_backtest_48 import load_coin_dataset, simulate_model, calculate_metrics, Trade, NOTIONAL_USDT, MAKER_FEE_USD
import polars as pl
from pathlib import Path

whitelist = [
    "BTC", "WLD", "INJ", "OP", "COMP", "LDO", "DYDX", "ENA", 
    "UNI", "BCH", "GRT", "MAGIC", "BNB", "ADA", "1000BONK", "FET", "AAVE"
]

all_trades = []

# 1. Alts from 48-coin data
for coin in whitelist:
    if coin == "BTC":
        continue
    df = load_coin_dataset(coin)
    if df is not None:
        trades = simulate_model(df, coin, "Model_G_Sniper_Maker", len(df)//2)
        all_trades.extend(trades)

# 2. BTC from majors data
btc_c = pl.read_parquet("data/raw_majors_30d/BTC_candles.parquet").sort("ts")
btc_oi = pl.read_parquet("data/raw_majors_30d/BTC_oi.parquet").sort("ts")
btc_df = btc_c.with_columns([
    pl.col("v").rolling_mean(1440).alias("v_mean_24h"),
    pl.col("c").ewm_mean(span=750).alias("ema_trend_15m"),
    ((pl.col("c") - pl.col("l")) / (pl.col("h") - pl.col("l") + 1e-8)).alias("close_pos")
]).with_columns([
    (pl.col("v") / pl.col("v_mean_24h")).fill_nan(1.0).alias("rvol")
]).join_asof(btc_oi.select(["ts", "oi_usd"]), on="ts", strategy="backward").fill_null(strategy="forward")

btc_df = btc_df.with_columns([
    ((pl.col("oi_usd") - pl.col("oi_usd").shift(5)) / pl.col("oi_usd").shift(5) * 100.0).alias("delta_oi_5m"),
    ((pl.col("c") - pl.col("o")) / pl.col("o") * 100.0).alias("ret_1m")
])

ts_arr = btc_df["ts"].to_numpy()
o_arr = btc_df["o"].to_numpy()
h_arr = btc_df["h"].to_numpy()
l_arr = btc_df["l"].to_numpy()
c_arr = btc_df["c"].to_numpy()
rvol_arr = btc_df["rvol"].to_numpy()
ret_arr = btc_df["ret_1m"].to_numpy()
doi_arr = btc_df["delta_oi_5m"].to_numpy()
ema_arr = btc_df["ema_trend_15m"].to_numpy()
cpos_arr = btc_df["close_pos"].to_numpy()

n_btc = len(btc_df)
last_exit = -1
for i in range(1440, n_btc - 65):
    if i <= last_exit + 15:
        continue
    if rvol_arr[i] < 4.0 or abs(ret_arr[i]) < 0.10 or doi_arr[i] < 0.35:
        continue
    raw_dir = 1 if ret_arr[i] > 0 else -1
    if raw_dir == 1 and (c_arr[i] < ema_arr[i] or cpos_arr[i] < 0.70):
        continue
    if raw_dir == -1 and (c_arr[i] > ema_arr[i] or cpos_arr[i] > 0.30):
        continue
        
    spike_body = abs(c_arr[i] - o_arr[i])
    target_p = c_arr[i] - 0.30 * spike_body if raw_dir == 1 else c_arr[i] + 0.30 * spike_body
    filled = False
    entry_idx = i + 1
    for fill_k in range(i + 1, min(i + 6, n_btc)):
        if raw_dir == 1 and l_arr[fill_k] <= target_p:
            entry_idx = fill_k
            filled = True
            break
        elif raw_dir == -1 and h_arr[fill_k] >= target_p:
            entry_idx = fill_k
            filled = True
            break
    if not filled:
        continue
        
    entry_p = target_p
    exit_p = c_arr[min(entry_idx + 60, n_btc - 1)]
    peak_fav = 0.0
    trailing_active = False
    trailing_stop_p = 0.0
    
    for k in range(entry_idx, min(entry_idx + 60, n_btc)):
        curr_h = h_arr[k]
        curr_l = l_arr[k]
        curr_c = c_arr[k]
        if raw_dir == 1:
            fav = (curr_h - entry_p)/entry_p
            adv = (curr_l - entry_p)/entry_p
            if fav >= 0.010:
                exit_p = entry_p * 1.010
                break
            if adv <= -0.007:
                exit_p = entry_p * 0.993
                break
            if fav >= 0.005:
                trailing_active = True
                peak_fav = max(peak_fav, curr_h)
                trailing_stop_p = peak_fav * 0.997
            if trailing_active and curr_l <= trailing_stop_p:
                exit_p = trailing_stop_p
                break
        else:
            fav = (entry_p - curr_l)/entry_p
            adv = (curr_h - entry_p)/entry_p
            if fav >= 0.010:
                exit_p = entry_p * 0.990
                break
            if adv >= 0.007:
                exit_p = entry_p * 1.007
                break
            if fav >= 0.005:
                trailing_active = True
                peak_fav = min(peak_fav, curr_l) if peak_fav > 0 else curr_l
                trailing_stop_p = peak_fav * 1.003
            if trailing_active and curr_h >= trailing_stop_p:
                exit_p = trailing_stop_p
                break
        exit_p = curr_c
        
    gross_pnl = (exit_p - entry_p)/entry_p if raw_dir == 1 else (entry_p - exit_p)/entry_p
    net_pnl = gross_pnl * NOTIONAL_USDT - MAKER_FEE_USD
    all_trades.append(Trade(coin="BTC", model="Model_G_Sniper_Maker", entry_ts=int(ts_arr[entry_idx]),
                            entry_price=entry_p, direction=raw_dir, exit_ts=int(ts_arr[k]), exit_price=exit_p,
                            exit_reason="", gross_pnl_pct=gross_pnl*100, net_pnl_usd=net_pnl,
                            is_sample="IS" if entry_idx < n_btc//2 else "OOS"))
    last_exit = k

m_tot = calculate_metrics(all_trades)
is_trades = [t for t in all_trades if t.is_sample == "IS"]
oos_trades = [t for t in all_trades if t.is_sample == "OOS"]
m_is = calculate_metrics(is_trades)
m_oos = calculate_metrics(oos_trades)
wfe = round(m_oos["net_pnl_usd"] / m_is["net_pnl_usd"], 2) if m_is["net_pnl_usd"] > 0 else 99.0

tot_wins = sum(1 for t in all_trades if t.net_pnl_usd > 0)
tot_losses = sum(1 for t in all_trades if t.net_pnl_usd <= 0)
is_wins = sum(1 for t in is_trades if t.net_pnl_usd > 0)
is_losses = sum(1 for t in is_trades if t.net_pnl_usd <= 0)
oos_wins = sum(1 for t in oos_trades if t.net_pnl_usd > 0)
oos_losses = sum(1 for t in oos_trades if t.net_pnl_usd <= 0)

print(f"\n=========================================================================")
print(f"ULTIMATE VERIFIED PORTFOLIO ({len(whitelist)} ASSETS INCLUDING BTC)")
print(f"TOTAL TRADES:        {m_tot['total_trades']}")
print(f"WINNING TRADES:      {tot_wins}")
print(f"LOSING TRADES:       {tot_losses}")
print(f"OVERALL WIN RATE:    {m_tot['win_rate']}%")
print(f"IN-SAMPLE WIN RATE:  {m_is['win_rate']}%  ({is_wins} wins / {is_losses} losses)")
print(f"OUT-OF-SAMPLE WR:    {m_oos['win_rate']}%  ({oos_wins} wins / {oos_losses} losses)")
print(f"NET PNL ($10 pos):   ${m_tot['net_pnl_usd']:+.3f}")
print(f"IN-SAMPLE NET PNL:   ${m_is['net_pnl_usd']:+.3f}")
print(f"OUT-OF-SAMPLE PNL:   ${m_oos['net_pnl_usd']:+.3f}")
print(f"PROFIT FACTOR:       {m_tot['profit_factor']:.2f}  (IS: {m_is['profit_factor']:.2f}, OOS: {m_oos['profit_factor']:.2f})")
print(f"MAX DRAWDOWN:        ${m_tot['max_drawdown_usd']:.3f} ({m_tot['max_drawdown_usd']/10*100:.1f}% of position)")
print(f"WALK-FORWARD EFF:    {wfe}")
print(f"=========================================================================")

print("\n--- PER-COIN STATS (Sorted by Net PnL) ---")
coin_map = {}
for t in all_trades:
    if t.coin not in coin_map:
        coin_map[t.coin] = []
    coin_map[t.coin].append(t)

stats_list = []
for c, ctrades in coin_map.items():
    c_m = calculate_metrics(ctrades)
    c_wins = sum(1 for t in ctrades if t.net_pnl_usd > 0)
    c_loss = sum(1 for t in ctrades if t.net_pnl_usd <= 0)
    stats_list.append((c, c_m, c_wins, c_loss))
stats_list.sort(key=lambda x: x[1]["net_pnl_usd"], reverse=True)

print(f"{'Coin':<10} | {'Trades':<6} | {'Wins':<4} | {'Losses':<6} | {'WinRate%':<8} | {'NetPnL($)':<10} | {'PF':<6}")
print("-" * 65)
for c, cm, c_wins, c_loss in stats_list:
    print(f"{c:<10} | {cm['total_trades']:<6} | {c_wins:<4} | {c_loss:<6} | {cm['win_rate']:<8.1f} | {cm['net_pnl_usd']:<10.3f} | {cm['profit_factor']:<6.2f}")
