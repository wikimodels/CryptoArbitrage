"""
Test Volume Spike & OI Anomaly on BTC, ETH, SOL
1. Fetch 30-day 1m candles for BTC/USDT:USDT, ETH/USDT:USDT, SOL/USDT:USDT from Binance
2. Fetch 30-day 5m OI from Binance
3. Test Model Sniper on them
"""

import time
from datetime import datetime, timezone
from pathlib import Path
import ccxt
import polars as pl
from cryptoarb.research.volume_oi_backtest_48 import calculate_metrics, Trade, NOTIONAL_USDT, MAKER_FEE_USD, TAKER_FEE_USD

ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "swap"}})
now_ms = int(time.time() * 1000)
days = 30
start_ms = now_ms - days * 24 * 3600 * 1000

majors = ["BTC", "ETH", "SOL"]
data_dir = Path("data/raw_majors_30d")
data_dir.mkdir(parents=True, exist_ok=True)

for coin in majors:
    sym = f"{coin}/USDT:USDT"
    c_path = data_dir / f"{coin}_candles.parquet"
    oi_path = data_dir / f"{coin}_oi.parquet"
    
    # 1. Candles
    if not c_path.exists():
        print(f"Downloading 30d 1m candles for {sym}...")
        curr = start_ms
        c_recs = []
        while curr < now_ms:
            try:
                batch = ex.fetch_ohlcv(sym, timeframe="1m", since=curr, limit=1000)
                if not batch:
                    break
                for b in batch:
                    c_recs.append({"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]), "l": float(b[3]), "c": float(b[4]), "v": float(b[5])})
                curr = batch[-1][0] + 60 * 1000
                time.sleep(0.05)
            except Exception as e:
                print(f"Error {sym} candles: {e}")
                time.sleep(1.0)
                break
        df_c = pl.DataFrame(c_recs).unique(subset=["ts"]).sort("ts")
        df_c.write_parquet(c_path)
        print(f"Saved {len(df_c)} candles for {coin}")
        
    # 2. OI
    if not oi_path.exists():
        print(f"Downloading 30d 5m OI for {sym}...")
        curr = start_ms
        oi_recs = []
        while curr < now_ms:
            try:
                batch = ex.fetch_open_interest_history(sym, timeframe="5m", since=curr, limit=500)
                if not batch:
                    break
                for r in batch:
                    oi_recs.append({
                        "ts": int(r["timestamp"]),
                        "oi_usd": float(r.get("openInterestValue") or r.get("info", {}).get("sumOpenInterestValue", 0.0))
                    })
                curr = batch[-1]["timestamp"] + 5 * 60 * 1000
                time.sleep(0.05)
            except Exception as e:
                print(f"Error {sym} OI: {e}")
                time.sleep(1.0)
                break
        df_oi = pl.DataFrame(oi_recs).unique(subset=["ts"]).sort("ts")
        df_oi.write_parquet(oi_path)
        print(f"Saved {len(df_oi)} OI records for {coin}")

# Now analyze and run backtest
print("\n--- Running Empirical Backtest on BTC, ETH, SOL ---")
for coin in majors:
    df_c = pl.read_parquet(data_dir / f"{coin}_candles.parquet").sort("ts")
    df_oi = pl.read_parquet(data_dir / f"{coin}_oi.parquet").sort("ts")
    
    df = df_c.with_columns([
        pl.col("v").rolling_mean(1440).alias("v_mean_24h"),
        pl.col("c").ewm_mean(span=750).alias("ema_trend_15m"),
        ((pl.col("c") - pl.col("l")) / (pl.col("h") - pl.col("l") + 1e-8)).alias("close_pos")
    ]).with_columns([
        (pl.col("v") / pl.col("v_mean_24h")).fill_nan(1.0).alias("rvol")
    ])
    
    df = df.join_asof(df_oi.select(["ts", "oi_usd"]), on="ts", strategy="backward").fill_null(strategy="forward")
    df = df.with_columns([
        ((pl.col("oi_usd") - pl.col("oi_usd").shift(5)) / pl.col("oi_usd").shift(5) * 100.0).alias("delta_oi_5m"),
        ((pl.col("c") - pl.col("o")) / pl.col("o") * 100.0).alias("ret_1m")
    ])
    
    # Check what delta_oi quantile looks like for BTC vs Alts
    doi_p95 = df["delta_oi_5m"].quantile(0.95)
    doi_p99 = df["delta_oi_5m"].quantile(0.99)
    print(f"[{coin}] 5m Delta OI: 95th Pct = +{doi_p95:.2f}%, 99th Pct = +{doi_p99:.2f}%")
    
    # Test with coin-appropriate threshold (for BTC/ETH, dOI is smaller than for alts)
    doi_th = 0.5 if coin in ["BTC", "ETH"] else 1.0
    
    ts_arr = df["ts"].to_numpy()
    o_arr = df["o"].to_numpy()
    h_arr = df["h"].to_numpy()
    l_arr = df["l"].to_numpy()
    c_arr = df["c"].to_numpy()
    rvol_arr = df["rvol"].to_numpy()
    ret_arr = df["ret_1m"].to_numpy()
    doi_arr = df["delta_oi_5m"].to_numpy()
    ema_arr = df["ema_trend_15m"].to_numpy()
    cpos_arr = df["close_pos"].to_numpy()
    
    trades = []
    last_exit = -1
    n = len(df)
    
    # For majors: TP 1.0%, SL 0.7%, Trailing at +0.5% (dist 0.3%)
    tp_pct = 0.010
    sl_pct = 0.007
    trail_act = 0.005
    trail_dist = 0.003
    
    for i in range(1440, n - 65):
        if i <= last_exit + 15:
            continue
        if rvol_arr[i] < 4.0 or abs(ret_arr[i]) < 0.10 or doi_arr[i] < doi_th:
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
                fav = (curr_h - entry_p)/entry_p
                adv = (curr_l - entry_p)/entry_p
                if fav >= tp_pct:
                    exit_p = entry_p * (1.0 + tp_pct)
                    break
                if adv <= -sl_pct:
                    exit_p = entry_p * (1.0 - sl_pct)
                    break
                if fav >= trail_act:
                    trailing_active = True
                    peak_fav = max(peak_fav, curr_h)
                    trailing_stop_p = peak_fav * (1.0 - trail_dist)
                if trailing_active and curr_l <= trailing_stop_p:
                    exit_p = trailing_stop_p
                    break
            else:
                fav = (entry_p - curr_l)/entry_p
                adv = (curr_h - entry_p)/entry_p
                if fav >= tp_pct:
                    exit_p = entry_p * (1.0 - tp_pct)
                    break
                if adv >= sl_pct:
                    exit_p = entry_p * (1.0 + sl_pct)
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
        net_pnl = gross_pnl * NOTIONAL_USDT - MAKER_FEE_USD
        trades.append(Trade(coin=coin, model="majors", entry_ts=int(ts_arr[entry_idx]), entry_price=entry_p,
                            direction=raw_dir, exit_ts=int(ts_arr[k]), exit_price=exit_p, exit_reason="",
                            gross_pnl_pct=gross_pnl*100, net_pnl_usd=net_pnl, is_sample="IS" if entry_idx < n//2 else "OOS"))
        last_exit = k
        
    m = calculate_metrics(trades)
    is_m = calculate_metrics([t for t in trades if t.is_sample == "IS"])
    oos_m = calculate_metrics([t for t in trades if t.is_sample == "OOS"])
    print(f"[{coin:<4}] Trades={m['total_trades']:<3} | WinRate={m['win_rate']:.1f}% (IS:{is_m['win_rate']:.1f}% / OOS:{oos_m['win_rate']:.1f}%) | NetPnL=${m['net_pnl_usd']:+.3f} | PF={m['profit_factor']:.2f}")
