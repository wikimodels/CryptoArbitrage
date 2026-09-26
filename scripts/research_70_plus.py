"""
Research Script: Testing Filters to Push Win Rate to 70%+
1. Trend Filter: 15m / 1h EMA50 alignment
2. Candle Quality Filter: Strong close (Close >= 75% of candle range, minimal upper/lower wick)
3. Pullback Limit Entry: Entry at 38.2% or 50% retest of spike candle body
4. Coin Selection: Filtering out high-noise / low-liquidity coins
"""

from cryptoarb.research.volume_oi_backtest import load_coin_dataset, TARGET_COINS, Trade, calculate_metrics, NOTIONAL_USDT, ROUNDTRIP_FEE_USD
import polars as pl
import numpy as np

def run_tests():
    coin_data = {}
    for coin in TARGET_COINS:
        df = load_coin_dataset(coin)
        if df is not None:
            # Add EMA50 on 15m (which is 15 * 50 = 750 1m candles)
            df = df.with_columns([
                pl.col("c").ewm_mean(span=750).alias("ema_trend_15m"),
                # Candle range and close position
                (pl.col("h") - pl.col("l")).alias("candle_range"),
                ((pl.col("c") - pl.col("l")) / (pl.col("h") - pl.col("l") + 1e-8)).alias("close_pos")
            ])
            coin_data[coin] = df

    print(f"Loaded {len(coin_data)} coins. Testing 70%+ win rate filters...")

    # Let's test combinations:
    # Baseline: RVOL >= 5, cross >= 2, dOI >= 1.5%
    # Filter A: Trend Alignment (Long only if C > EMA_trend, Short only if C < EMA_trend)
    # Filter B: Strong Close (close_pos >= 0.70 for Long, close_pos <= 0.30 for Short)
    # Filter C: Pullback Entry (Limit order at 38.2% retracement into spike candle)
    # Filter D: Coin Tiering (Top 10 liquid coins vs all 20)

    configs = [
        {"name": "Baseline (RVOL>=5, cross>=2, dOI>=1.5%)", "trend": False, "strong_close": False, "pullback": 0.0, "top_coins": False},
        {"name": "Trend Filter Only (with 15m EMA50)", "trend": True, "strong_close": False, "pullback": 0.0, "top_coins": False},
        {"name": "Strong Close Filter (close_pos >= 0.75)", "trend": False, "strong_close": True, "pullback": 0.0, "top_coins": False},
        {"name": "Trend + Strong Close", "trend": True, "strong_close": True, "pullback": 0.0, "top_coins": False},
        {"name": "Trend + Strong Close + Pullback 38.2%", "trend": True, "strong_close": True, "pullback": 0.382, "top_coins": False},
        {"name": "Trend + Strong Close + Top Coins Only", "trend": True, "strong_close": True, "pullback": 0.0, "top_coins": True},
        {"name": "Full Combo: Trend + Strong Close + Pullback 38.2% + Top Coins", "trend": True, "strong_close": True, "pullback": 0.382, "top_coins": True},
    ]

    top_10_coins = {"DOGE", "AVAX", "APT", "ARB", "OP", "LDO", "INJ", "AAVE", "SEI", "JUP"}

    for cfg in configs:
        all_trades = []
        for coin, df in coin_data.items():
            if cfg["top_coins"] and coin not in top_10_coins:
                continue

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

                # Trend filter
                if cfg["trend"]:
                    if raw_dir == 1 and c_arr[i] < ema_arr[i]:
                        continue
                    if raw_dir == -1 and c_arr[i] > ema_arr[i]:
                        continue

                # Strong close filter
                if cfg["strong_close"]:
                    if raw_dir == 1 and cpos_arr[i] < 0.70:
                        continue
                    if raw_dir == -1 and cpos_arr[i] > 0.30:
                        continue

                # Entry execution
                spike_open = o_arr[i]
                spike_close = c_arr[i]
                spike_body = abs(spike_close - spike_open)

                entry_idx = i + 1
                if cfg["pullback"] > 0:
                    # Place limit order at pullback level of the spike candle
                    if raw_dir == 1:
                        target_entry = spike_close - cfg["pullback"] * spike_body
                        # Check if next 5 candles dip to target_entry
                        filled = False
                        for fill_k in range(i + 1, min(i + 6, n)):
                            if l_arr[fill_k] <= target_entry:
                                entry_idx = fill_k
                                entry_p = target_entry
                                filled = True
                                break
                        if not filled:
                            continue
                    else:
                        target_entry = spike_close + cfg["pullback"] * spike_body
                        filled = False
                        for fill_k in range(i + 1, min(i + 6, n)):
                            if h_arr[fill_k] >= target_entry:
                                entry_idx = fill_k
                                entry_p = target_entry
                                filled = True
                                break
                        if not filled:
                            continue
                else:
                    entry_p = o_arr[entry_idx]

                # Trade simulation (TP 1.5%, SL 1.0%, Trailing at +0.8% with 0.5% pullback)
                exit_p = c_arr[min(entry_idx + 60, n - 1)]
                peak_fav = 0.0
                trailing_active = False
                trailing_stop_p = 0.0
                exit_idx = min(entry_idx + 60, n - 1)

                for k in range(entry_idx, min(entry_idx + 60, n)):
                    curr_h = h_arr[k]
                    curr_l = l_arr[k]
                    curr_c = c_arr[k]

                    if raw_dir == 1:
                        if (curr_h - entry_p)/entry_p >= 0.015:
                            exit_p = entry_p * 1.015
                            exit_idx = k
                            break
                        if (curr_l - entry_p)/entry_p <= -0.010:
                            exit_p = entry_p * 0.990
                            exit_idx = k
                            break
                        if (curr_h - entry_p)/entry_p >= 0.008:
                            trailing_active = True
                            peak_fav = max(peak_fav, curr_h)
                            trailing_stop_p = peak_fav * 0.995
                        if trailing_active and curr_l <= trailing_stop_p:
                            exit_p = trailing_stop_p
                            exit_idx = k
                            break
                    else:
                        if (entry_p - curr_l)/entry_p >= 0.015:
                            exit_p = entry_p * 0.985
                            exit_idx = k
                            break
                        if (curr_h - entry_p)/entry_p >= 0.010:
                            exit_p = entry_p * 1.010
                            exit_idx = k
                            break
                        if (entry_p - curr_l)/entry_p >= 0.008:
                            trailing_active = True
                            peak_fav = min(peak_fav, curr_l) if peak_fav > 0 else curr_l
                            trailing_stop_p = peak_fav * 1.005
                        if trailing_active and curr_h >= trailing_stop_p:
                            exit_p = trailing_stop_p
                            exit_idx = k
                            break
                    exit_p = curr_c
                    exit_idx = k

                gross_pnl_pct = (exit_p - entry_p)/entry_p if raw_dir == 1 else (entry_p - exit_p)/entry_p
                # If pullback entry was used, it's a Maker order (0.04% roundtrip fee instead of 0.22% taker)
                fee = 0.0004 * NOTIONAL_USDT if cfg["pullback"] > 0 else ROUNDTRIP_FEE_USD
                net_pnl = gross_pnl_pct * NOTIONAL_USDT - fee

                all_trades.append(Trade(
                    coin=coin, model=cfg["name"], entry_ts=int(ts_arr[entry_idx]),
                    entry_price=entry_p, direction=raw_dir, exit_ts=int(ts_arr[exit_idx]),
                    exit_price=exit_p, exit_reason="", gross_pnl_pct=gross_pnl_pct*100,
                    net_pnl_usd=net_pnl, is_sample="IS" if entry_idx < len(df)//2 else "OOS"
                ))
                last_trade_exit_idx = exit_idx

        is_trades = [t for t in all_trades if t.is_sample == "IS"]
        oos_trades = [t for t in all_trades if t.is_sample == "OOS"]
        m_tot = calculate_metrics(all_trades)
        m_is = calculate_metrics(is_trades)
        m_oos = calculate_metrics(oos_trades)
        print(f"\n>>> CONFIG: {cfg['name']}")
        print(f"    TOTAL: Trades={m_tot['total_trades']}, WinRate={m_tot['win_rate']}%, NetPnL=${m_tot['net_pnl_usd']:.2f}, PF={m_tot['profit_factor']}")
        print(f"    IS:    Trades={m_is['total_trades']}, WinRate={m_is['win_rate']}%, NetPnL=${m_is['net_pnl_usd']:.2f}")
        print(f"    OOS:   Trades={m_oos['total_trades']}, WinRate={m_oos['win_rate']}%, NetPnL=${m_oos['net_pnl_usd']:.2f}")

if __name__ == "__main__":
    run_tests()
