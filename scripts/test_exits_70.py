from cryptoarb.research.volume_oi_backtest import load_coin_dataset, TARGET_COINS, Trade, calculate_metrics, NOTIONAL_USDT
import polars as pl
import numpy as np

coin_data = {}
for coin in ["DOGE", "AVAX", "APT", "ARB", "OP", "LDO", "INJ", "AAVE", "SEI", "JUP"]:
    df = load_coin_dataset(coin)
    if df is not None:
        df = df.with_columns([
            pl.col("c").ewm_mean(span=750).alias("ema_trend_15m"),
            ((pl.col("c") - pl.col("l")) / (pl.col("h") - pl.col("l") + 1e-8)).alias("close_pos")
        ])
        coin_data[coin] = df

# Test different exit configurations:
# 1: TP 1.2%, SL 1.2%, Trailing at +0.6% (0.3% pullback), Breakeven at +0.5%
# 2: TP 1.0%, SL 1.0%, Trailing at +0.5% (0.25% pullback)
# 3: TP 1.5%, SL 1.5%, Trailing at +0.8% (0.4% pullback)

for tp, sl, trail_act, trail_dist, be_act in [
    (0.015, 0.010, 0.008, 0.005, 0.000),  # original
    (0.012, 0.012, 0.006, 0.003, 0.005),  # faster profit capture + BE
    (0.010, 0.010, 0.005, 0.0025, 0.004), # scalper exit
    (0.015, 0.015, 0.008, 0.004, 0.006),  # wider SL to avoid wickouts
]:
    all_trades = []
    for coin, df in coin_data.items():
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

        for i in range(1440, n - 65):
            if i <= last_trade_exit_idx + 15:
                continue

            if rvol_arr[i] < 5.0 or cross_arr[i] < 2 or abs(ret_arr[i]) < 0.15 or doi5_arr[i] < 1.5:
                continue

            raw_dir = 1 if ret_arr[i] > 0 else -1

            # Trend & strong close filters
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
            stop_price = entry_p * (1.0 - sl) if raw_dir == 1 else entry_p * (1.0 + sl)

            for k in range(entry_idx, min(entry_idx + 60, n)):
                curr_h = h_arr[k]
                curr_l = l_arr[k]
                curr_c = c_arr[k]

                if raw_dir == 1:
                    fav = (curr_h - entry_p) / entry_p
                    adv = (curr_l - entry_p) / entry_p
                    
                    # Breakeven stop move
                    if be_act > 0 and fav >= be_act:
                        stop_price = max(stop_price, entry_p * 1.001)

                    if fav >= tp:
                        exit_p = entry_p * (1.0 + tp)
                        break
                    if curr_l <= stop_price:
                        exit_p = stop_price
                        break
                    if fav >= trail_act:
                        trailing_active = True
                        peak_fav = max(peak_fav, curr_h)
                        trailing_stop_p = peak_fav * (1.0 - trail_dist)
                    if trailing_active and curr_l <= trailing_stop_p:
                        exit_p = trailing_stop_p
                        break
                else:
                    fav = (entry_p - curr_l) / entry_p
                    adv = (curr_h - entry_p) / entry_p
                    
                    if be_act > 0 and fav >= be_act:
                        stop_price = min(stop_price, entry_p * 0.999)

                    if fav >= tp:
                        exit_p = entry_p * (1.0 - tp)
                        break
                    if curr_h >= stop_price:
                        exit_p = stop_price
                        break
                    if fav >= trail_act:
                        trailing_active = True
                        peak_fav = min(peak_fav, curr_l) if peak_fav > 0 else curr_l
                        trailing_stop_p = peak_fav * (1.0 + trail_dist)
                    if trailing_active and curr_h >= trailing_stop_p:
                        exit_p = trailing_stop_p
                        break
                exit_p = curr_c

            gross_pnl = (exit_p - entry_p)/entry_p if raw_dir == 1 else (entry_p - exit_p)/entry_p
            net_pnl = gross_pnl * NOTIONAL_USDT - (0.0022 * NOTIONAL_USDT)
            all_trades.append(Trade(
                coin="", model="", entry_ts=0, entry_price=entry_p, direction=raw_dir,
                exit_ts=0, exit_price=exit_p, exit_reason="", gross_pnl_pct=gross_pnl*100,
                net_pnl_usd=net_pnl, is_sample="IS" if entry_idx < len(df)//2 else "OOS"
            ))
            last_trade_exit_idx = k

    m_tot = calculate_metrics(all_trades)
    is_t = [t for t in all_trades if t.is_sample == "IS"]
    oos_t = [t for t in all_trades if t.is_sample == "OOS"]
    m_is = calculate_metrics(is_t)
    m_oos = calculate_metrics(oos_t)
    print(f"TP={tp*100:.1f}%, SL={sl*100:.1f}%, TrailAct={trail_act*100:.1f}%, BE={be_act*100:.1f}% -> Total WR={m_tot['win_rate']}% | OOS WR={m_oos['win_rate']}% | OOS PnL=${m_oos['net_pnl_usd']:+.2f} | PF={m_tot['profit_factor']}")
