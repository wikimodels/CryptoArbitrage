from cryptoarb.research.volume_oi_backtest import load_coin_dataset, TARGET_COINS, Trade, calculate_metrics, NOTIONAL_USDT, ROUNDTRIP_FEE_USD
import polars as pl

print(f"{'Coin':<8} | {'Trades':<6} | {'WinRate%':<8} | {'NetPnL($)':<10} | {'PF':<6} | {'IS_WR%':<7} | {'OOS_WR%':<8}")
print("-" * 65)

for coin in TARGET_COINS:
    df = load_coin_dataset(coin)
    if df is None:
        continue
    df = df.with_columns([
        pl.col("c").ewm_mean(span=750).alias("ema_trend_15m"),
        ((pl.col("c") - pl.col("l")) / (pl.col("h") - pl.col("l") + 1e-8)).alias("close_pos")
    ])

    ts_arr = df["ts"].to_numpy()
    o_arr = df["o"].to_numpy()
    h_arr = df["h"].to_numpy()
    l_arr = df["l"].to_numpy()
    c_arr = df["c"].to_numpy()
    rvol_arr = df["rvol_bitget"].to_numpy()
    cross_arr = df["cross_count_3x"].to_numpy()
    ret_arr = df["ret_1m"].to_numpy()
    doi5_arr = df["delta_oi_5m"].to_numpy()
    ema_arr = df["ema_trend_15m"].to_numpy()
    cpos_arr = df["close_pos"].to_numpy()

    n = len(df)
    last_trade_exit_idx = -1
    trades = []

    for i in range(1440, n - 65):
        if i <= last_trade_exit_idx + 15:
            continue
        if rvol_arr[i] < 5.0 or cross_arr[i] < 2 or abs(ret_arr[i]) < 0.15 or doi5_arr[i] < 1.5:
            continue
        raw_dir = 1 if ret_arr[i] > 0 else -1
        if raw_dir == 1 and (c_arr[i] < ema_arr[i] or cpos_arr[i] < 0.70):
            continue
        if raw_dir == -1 and (c_arr[i] > ema_arr[i] or cpos_arr[i] > 0.30):
            continue

        entry_idx = i + 1
        entry_p = o_arr[entry_idx]
        exit_p = c_arr[min(entry_idx + 60, n - 1)]
        peak_fav = 0.0
        trailing_active = False
        trailing_stop_p = 0.0

        for k in range(entry_idx, min(entry_idx + 60, n)):
            curr_h = h_arr[k]
            curr_l = l_arr[k]
            curr_c = c_arr[k]

            if raw_dir == 1:
                if (curr_h - entry_p)/entry_p >= 0.015:
                    exit_p = entry_p * 1.015
                    break
                if (curr_l - entry_p)/entry_p <= -0.010:
                    exit_p = entry_p * 0.990
                    break
                if (curr_h - entry_p)/entry_p >= 0.008:
                    trailing_active = True
                    peak_fav = max(peak_fav, curr_h)
                    trailing_stop_p = peak_fav * 0.995
                if trailing_active and curr_l <= trailing_stop_p:
                    exit_p = trailing_stop_p
                    break
            else:
                if (entry_p - curr_l)/entry_p >= 0.015:
                    exit_p = entry_p * 0.985
                    break
                if (curr_h - entry_p)/entry_p >= 0.010:
                    exit_p = entry_p * 1.010
                    break
                if (entry_p - curr_l)/entry_p >= 0.008:
                    trailing_active = True
                    peak_fav = min(peak_fav, curr_l) if peak_fav > 0 else curr_l
                    trailing_stop_p = peak_fav * 1.005
                if trailing_active and curr_h >= trailing_stop_p:
                    exit_p = trailing_stop_p
                    break
            exit_p = curr_c

        gross_pnl = (exit_p - entry_p)/entry_p if raw_dir == 1 else (entry_p - exit_p)/entry_p
        net_pnl = gross_pnl * NOTIONAL_USDT - ROUNDTRIP_FEE_USD
        trades.append(Trade(
            coin=coin, model="", entry_ts=0, entry_price=entry_p, direction=raw_dir,
            exit_ts=0, exit_price=exit_p, exit_reason="", gross_pnl_pct=gross_pnl*100,
            net_pnl_usd=net_pnl, is_sample="IS" if entry_idx < len(df)//2 else "OOS"
        ))
        last_trade_exit_idx = k

    if trades:
        m = calculate_metrics(trades)
        is_m = calculate_metrics([t for t in trades if t.is_sample == "IS"])
        oos_m = calculate_metrics([t for t in trades if t.is_sample == "OOS"])
        print(f"{coin:<8} | {m['total_trades']:<6} | {m['win_rate']:<8.1f} | {m['net_pnl_usd']:<10.3f} | {m['profit_factor']:<6.2f} | {is_m['win_rate']:<7.1f} | {oos_m['win_rate']:<8.1f}")
