"""
Comprehensive 48-Coin Volume Spike & Open Interest Backtesting Engine
Evaluates:
- Baseline Pure Volume Spike
- Cross-Exchange Confirmation
- Open Interest Capital Expansion vs Liquidation Exhaustion
- Trend Filter (15m EMA50)
- Candle Quality Filter (Strong Close >= 0.70)
- Pullback Limit Entry (Maker Execution)

Strict Assumptions:
- Position Size: strictly $10 notional ($1 margin at 10x leverage)
- Friction: 0.22% roundtrip for Taker ($0.022 per trade), 0.04% for Maker ($0.004 per trade)
- Split: In-Sample (first 14 days), Out-of-Sample (second 14 days)
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import polars as pl

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("volume_oi_48")

DATA_DIR = Path("data/raw_1m_30d")
OI_DIR = Path("data/raw_oi_30d/binance")
OUTPUT_FILE = Path("output/volume_oi_48_results.json")

ALL_COINS = [
    "DOGE", "ADA", "AVAX", "APT", "WIF", "ARB", "OP", "TIA", 
    "LDO", "INJ", "FET", "AAVE", "ATOM", "RENDER", "SEI", 
    "ENA", "JUP", "CRV", "DOT", "GALA",
    "UNI", "WLD", "BNB", "BCH", "XLM", "RAY", "DASH", "HBAR", 
    "ETHFI", "VIRTUAL", "COMP", "FIL", "ALGO", "SAND", "MANA", 
    "AXS", "DYDX", "1000BONK", "1INCH", "PYTH", "PENDLE", "GRT", 
    "STX", "CHZ", "ARKM", "SNX", "MAGIC", "EIGEN"
]

NOTIONAL_USDT = 10.0
TAKER_FEE_USD = 0.0022 * NOTIONAL_USDT  # $0.022
MAKER_FEE_USD = 0.0004 * NOTIONAL_USDT  # $0.004

TP_PCT = 0.015
SL_PCT = 0.010
TRAILING_ACT = 0.008
TRAILING_DIST = 0.005
MAX_HOLD_MINUTES = 60
COOLDOWN_MINUTES = 15


@dataclass
class Trade:
    coin: str
    model: str
    entry_ts: int
    entry_price: float
    direction: int
    exit_ts: int
    exit_price: float
    exit_reason: str
    gross_pnl_pct: float
    net_pnl_usd: float
    is_sample: str


def load_coin_dataset(coin: str) -> pl.DataFrame | None:
    bitget_path = DATA_DIR / "bitget" / f"{coin}_USDT_USDT" / "candles.parquet"
    oi_path = OI_DIR / f"{coin}_USDT_USDT" / "oi.parquet"
    
    if not bitget_path.exists() or not oi_path.exists():
        return None
        
    try:
        df = pl.read_parquet(bitget_path).sort("ts").with_columns(pl.col("ts").cast(pl.Int64))
        oi_df = pl.read_parquet(oi_path).sort("ts").with_columns(pl.col("ts").cast(pl.Int64))
    except Exception:
        return None
        
    if len(df) < 2880 or len(oi_df) < 500:
        return None

    # Compute Bitget 24h rolling volume mean (1440 candles)
    df = df.with_columns([
        pl.col("v").rolling_mean(window_size=1440).alias("v_mean_24h"),
        pl.col("c").ewm_mean(span=750).alias("ema_trend_15m"),
        ((pl.col("c") - pl.col("l")) / (pl.col("h") - pl.col("l") + 1e-8)).alias("close_pos")
    ]).with_columns([
        (pl.col("v") / pl.col("v_mean_24h")).fill_nan(1.0).fill_null(1.0).alias("rvol_bitget")
    ])
    
    # Load other exchanges to compute cross-exchange volume spikes
    other_exchanges = ["okx", "mexc", "bingx"]
    for ex in other_exchanges:
        ex_path = DATA_DIR / ex / f"{coin}_USDT_USDT" / "candles.parquet"
        if ex_path.exists():
            try:
                ex_df = pl.read_parquet(ex_path).sort("ts").with_columns(pl.col("ts").cast(pl.Int64))
                ex_df = ex_df.with_columns([
                    pl.col("v").rolling_mean(window_size=1440).alias(f"v_mean_24h_{ex}")
                ]).with_columns([
                    (pl.col("v") / pl.col(f"v_mean_24h_{ex}")).fill_nan(0.0).fill_null(0.0).alias(f"rvol_{ex}")
                ]).select(["ts", f"rvol_{ex}"])
                df = df.join(ex_df, on="ts", how="left").with_columns(pl.col(f"rvol_{ex}").fill_null(0.0))
            except Exception:
                df = df.with_columns(pl.lit(0.0).alias(f"rvol_{ex}"))
        else:
            df = df.with_columns(pl.lit(0.0).alias(f"rvol_{ex}"))
            
    # Calculate cross-exchange count (how many exchanges show RVOL >= 3.0 at this minute)
    df = df.with_columns([
        (
            (pl.col("rvol_bitget") >= 3.0).cast(pl.Int32) +
            (pl.col("rvol_okx") >= 3.0).cast(pl.Int32) +
            (pl.col("rvol_mexc") >= 3.0).cast(pl.Int32) +
            (pl.col("rvol_bingx") >= 3.0).cast(pl.Int32)
        ).alias("cross_count_3x")
    ])
    
    # Join 5m Open Interest from Binance using join_asof
    df = df.join_asof(
        oi_df.select(["ts", "oi_usd", "oi_base"]),
        on="ts",
        strategy="backward"
    ).fill_null(strategy="forward").fill_null(strategy="backward")
    
    df = df.with_columns([
        ((pl.col("oi_usd") - pl.col("oi_usd").shift(5)) / pl.col("oi_usd").shift(5) * 100.0)
        .fill_nan(0.0).fill_null(0.0).alias("delta_oi_5m"),
        ((pl.col("c") - pl.col("o")) / pl.col("o") * 100.0).alias("ret_1m")
    ])
    
    return df


def simulate_model(
    df: pl.DataFrame,
    coin: str,
    model_name: str,
    is_split_idx: int
) -> List[Trade]:
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
    trades: List[Trade] = []
    last_trade_exit_idx = -1
    
    use_pullback = (model_name == "Model_G_Sniper_Maker")
    fee_usd = MAKER_FEE_USD if use_pullback else TAKER_FEE_USD
    
    for i in range(1440, n - MAX_HOLD_MINUTES - 5):
        if i <= last_trade_exit_idx + COOLDOWN_MINUTES:
            continue
            
        rvol = rvol_arr[i]
        body_ret = ret_arr[i]
        doi = doi5_arr[i]
        cross_cnt = cross_arr[i]
        c_price = c_arr[i]
        ema_val = ema_arr[i]
        cpos_val = cpos_arr[i]
        
        signal = 0
        raw_dir = 1 if body_ret > 0 else -1
        
        if model_name == "Model_A_Pure_Volume":
            if rvol >= 5.0 and abs(body_ret) >= 0.15:
                signal = raw_dir
                
        elif model_name == "Model_B_Cross_Exchange":
            if rvol >= 5.0 and cross_cnt >= 2 and abs(body_ret) >= 0.15:
                signal = raw_dir
                
        elif model_name == "Model_C_Volume_Plus_OI":
            if rvol >= 5.0 and cross_cnt >= 2 and abs(body_ret) >= 0.15 and doi >= 1.5:
                signal = raw_dir
                
        elif model_name == "Model_D_Squeeze_Fade":
            if rvol >= 5.0 and abs(body_ret) >= 0.15 and doi <= -1.5:
                signal = -raw_dir  # Fade
                
        elif model_name == "Model_E_Trend_Sniper":
            # Volume >= 5x, cross >= 2, dOI >= 1.5%, Trend alignment, Strong close
            if rvol >= 5.0 and cross_cnt >= 2 and abs(body_ret) >= 0.15 and doi >= 1.5:
                if raw_dir == 1 and c_price >= ema_val and cpos_val >= 0.70:
                    signal = 1
                elif raw_dir == -1 and c_price <= ema_val and cpos_val <= 0.30:
                    signal = -1
                    
        elif model_name == "Model_G_Sniper_Maker":
            # Same as Sniper, but entry via limit pullback 30% of spike candle
            if rvol >= 5.0 and cross_cnt >= 2 and abs(body_ret) >= 0.15 and doi >= 1.5:
                if raw_dir == 1 and c_price >= ema_val and cpos_val >= 0.70:
                    signal = 1
                elif raw_dir == -1 and c_price <= ema_val and cpos_val <= 0.30:
                    signal = -1
                    
        if signal == 0:
            continue
            
        entry_idx = i + 1
        entry_price = o_arr[entry_idx]
        
        # Check limit pullback fill for Model G
        if use_pullback:
            spike_body = abs(c_arr[i] - o_arr[i])
            if signal == 1:
                target_p = c_arr[i] - 0.30 * spike_body
                filled = False
                for fill_k in range(i + 1, min(i + 6, n)):
                    if l_arr[fill_k] <= target_p:
                        entry_idx = fill_k
                        entry_price = target_p
                        filled = True
                        break
                if not filled:
                    continue
            else:
                target_p = c_arr[i] + 0.30 * spike_body
                filled = False
                for fill_k in range(i + 1, min(i + 6, n)):
                    if h_arr[fill_k] >= target_p:
                        entry_idx = fill_k
                        entry_price = target_p
                        filled = True
                        break
                if not filled:
                    continue
                    
        entry_ts = int(ts_arr[entry_idx])
        is_sample = "IS" if entry_idx < is_split_idx else "OOS"
        
        exit_idx = entry_idx
        exit_price = entry_price
        exit_reason = "time_stop"
        peak_favorable = 0.0
        trailing_active = False
        trailing_stop_price = 0.0
        
        for k in range(entry_idx, min(entry_idx + MAX_HOLD_MINUTES, n)):
            curr_h = h_arr[k]
            curr_l = l_arr[k]
            curr_c = c_arr[k]
            
            if signal == 1:
                unreal_h = (curr_h - entry_price) / entry_price
                unreal_l = (curr_l - entry_price) / entry_price
                
                if unreal_h >= TP_PCT:
                    exit_idx = k
                    exit_price = entry_price * (1.0 + TP_PCT)
                    exit_reason = "take_profit"
                    break
                if unreal_l <= -SL_PCT:
                    exit_idx = k
                    exit_price = entry_price * (1.0 - SL_PCT)
                    exit_reason = "stop_loss"
                    break
                if unreal_h >= TRAILING_ACT:
                    trailing_active = True
                    peak_favorable = max(peak_favorable, curr_h)
                    trailing_stop_price = peak_favorable * (1.0 - TRAILING_DIST)
                if trailing_active and curr_l <= trailing_stop_price:
                    exit_idx = k
                    exit_price = trailing_stop_price
                    exit_reason = "trailing_stop"
                    break
            else:
                unreal_fav = (entry_price - curr_l) / entry_price
                unreal_adv = (curr_h - entry_price) / entry_price
                
                if unreal_fav >= TP_PCT:
                    exit_idx = k
                    exit_price = entry_price * (1.0 - TP_PCT)
                    exit_reason = "take_profit"
                    break
                if unreal_adv >= SL_PCT:
                    exit_idx = k
                    exit_price = entry_price * (1.0 + SL_PCT)
                    exit_reason = "stop_loss"
                    break
                if unreal_fav >= TRAILING_ACT:
                    trailing_active = True
                    peak_favorable = min(peak_favorable, curr_l) if peak_favorable > 0 else curr_l
                    trailing_stop_price = peak_favorable * (1.0 + TRAILING_DIST)
                if trailing_active and curr_h >= trailing_stop_price:
                    exit_idx = k
                    exit_price = trailing_stop_price
                    exit_reason = "trailing_stop"
                    break
            exit_idx = k
            exit_price = curr_c
            
        gross_pnl_pct = (exit_price - entry_price) / entry_price if signal == 1 else (entry_price - exit_price) / entry_price
        net_pnl_usd = (gross_pnl_pct * NOTIONAL_USDT) - fee_usd
        
        trades.append(Trade(
            coin=coin, model=model_name, entry_ts=entry_ts, entry_price=entry_price,
            direction=signal, exit_ts=int(ts_arr[exit_idx]), exit_price=exit_price,
            exit_reason=exit_reason, gross_pnl_pct=gross_pnl_pct * 100.0,
            net_pnl_usd=net_pnl_usd, is_sample=is_sample
        ))
        last_trade_exit_idx = exit_idx
        
    return trades


def calculate_metrics(trades: List[Trade]) -> Dict[str, Any]:
    if not trades:
        return {"total_trades": 0, "win_rate": 0.0, "net_pnl_usd": 0.0, "profit_factor": 0.0, "max_drawdown_usd": 0.0}
    pnls = [t.net_pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_trades = len(pnls)
    win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    net_pnl = sum(pnls)
    cum_pnl = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = running_max - cum_pnl
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
    return {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "net_pnl_usd": round(net_pnl, 4),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_usd": round(max_dd, 4),
    }


def main():
    logger.info("Loading datasets for all 48 coins...")
    coin_data: Dict[str, pl.DataFrame] = {}
    for coin in ALL_COINS:
        df = load_coin_dataset(coin)
        if df is not None:
            coin_data[coin] = df
            
    logger.info(f"Loaded {len(coin_data)} coins.")
    
    models = [
        "Model_A_Pure_Volume",
        "Model_B_Cross_Exchange",
        "Model_C_Volume_Plus_OI",
        "Model_D_Squeeze_Fade",
        "Model_E_Trend_Sniper",
        "Model_G_Sniper_Maker",
    ]
    
    results = {}
    for model in models:
        all_trades = []
        coin_breakdown = {}
        for coin, df in coin_data.items():
            is_split_idx = len(df) // 2
            trades = simulate_model(df, coin, model, is_split_idx)
            all_trades.extend(trades)
            coin_breakdown[coin] = {
                "total": calculate_metrics(trades),
                "is": calculate_metrics([t for t in trades if t.is_sample == "IS"]),
                "oos": calculate_metrics([t for t in trades if t.is_sample == "OOS"]),
            }
            
        is_trades = [t for t in all_trades if t.is_sample == "IS"]
        oos_trades = [t for t in all_trades if t.is_sample == "OOS"]
        
        m_tot = calculate_metrics(all_trades)
        m_is = calculate_metrics(is_trades)
        m_oos = calculate_metrics(oos_trades)
        
        wfe = round(m_oos["net_pnl_usd"] / m_is["net_pnl_usd"], 2) if m_is["net_pnl_usd"] > 0 else (99.0 if m_oos["net_pnl_usd"] > 0 else -1.0)
        
        results[model] = {
            "summary": {"total": m_tot, "is": m_is, "oos": m_oos, "wfe": wfe},
            "coin_breakdown": coin_breakdown
        }
        
        logger.info(
            f"[{model:<22}] Trades={m_tot['total_trades']:<5} | "
            f"WinRate={m_tot['win_rate']:<5.1f}% (IS:{m_is['win_rate']:<4.1f}% / OOS:{m_oos['win_rate']:<4.1f}%) | "
            f"NetPnL=${m_tot['net_pnl_usd']:<7.2f} (IS:${m_is['net_pnl_usd']:<6.2f} / OOS:${m_oos['net_pnl_usd']:<6.2f}) | "
            f"PF={m_tot['profit_factor']:<4.2f}"
        )
        
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    logger.info(f"Saved results to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
