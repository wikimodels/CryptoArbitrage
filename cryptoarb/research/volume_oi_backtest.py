"""
Volume Spike Anomaly & Open Interest Backtesting Engine
Evaluates 1-minute Volume Spikes, Cross-Exchange Volume Confirmation,
and 5-minute Open Interest (OI) Influx vs Liquidation Exhaustion.

Strict Assumptions:
- Position Size: strictly $10 notional ($1 margin at 10x leverage)
- Friction: 0.22% roundtrip (taker fee + slippage = $0.022 per trade)
- In-Sample (IS): First 14 days
- Out-of-Sample (OOS): Second 14 days
- Metrics: Trades, Win Rate, Net PnL ($), Profit Factor, Max Drawdown ($), Walk-Forward Efficiency (WFE)
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
logger = logging.getLogger("volume_oi_backtest")

DATA_DIR = Path("data/raw_1m_30d")
OI_DIR = Path("data/raw_oi_30d/binance")
OUTPUT_FILE = Path("output/volume_oi_results.json")

TARGET_COINS = [
    "DOGE", "ADA", "AVAX", "APT", "WIF", "ARB", "OP", "TIA", 
    "LDO", "INJ", "FET", "AAVE", "ATOM", "RENDER", "SEI", 
    "ENA", "JUP", "CRV", "DOT", "GALA"
]

NOTIONAL_USDT = 10.0  # $10 notional position
ROUNDTRIP_FEE_PCT = 0.0022  # 0.22% roundtrip taker fee + slippage ($0.022 / trade)
ROUNDTRIP_FEE_USD = NOTIONAL_USDT * ROUNDTRIP_FEE_PCT

TP_PCT = 0.015       # 1.5% Take Profit
SL_PCT = 0.010       # 1.0% Stop Loss
TRAILING_ACT = 0.008 # Activate trailing stop at +0.8%
TRAILING_DIST = 0.005 # Retracement distance 0.5%
MAX_HOLD_MINUTES = 60
COOLDOWN_MINUTES = 15


@dataclass
class Trade:
    coin: str
    model: str
    entry_ts: int
    entry_price: float
    direction: int  # +1 = Long, -1 = Short
    exit_ts: int
    exit_price: float
    exit_reason: str
    gross_pnl_pct: float
    net_pnl_usd: float
    is_sample: str  # "IS" or "OOS"


def load_coin_dataset(coin: str) -> pl.DataFrame | None:
    bitget_path = DATA_DIR / "bitget" / f"{coin}_USDT_USDT" / "candles.parquet"
    oi_path = OI_DIR / f"{coin}_USDT_USDT" / "oi.parquet"
    
    if not bitget_path.exists() or not oi_path.exists():
        return None
        
    df = pl.read_parquet(bitget_path).sort("ts").with_columns(pl.col("ts").cast(pl.Int64))
    oi_df = pl.read_parquet(oi_path).sort("ts").with_columns(pl.col("ts").cast(pl.Int64))
    
    if len(df) < 2880 or len(oi_df) < 500:
        return None

    # Compute Bitget 24h rolling volume mean (1440 candles)
    df = df.with_columns([
        pl.col("v").rolling_mean(window_size=1440).alias("v_mean_24h")
    ]).with_columns([
        (pl.col("v") / pl.col("v_mean_24h")).fill_nan(1.0).fill_null(1.0).alias("rvol_bitget")
    ])
    
    # Load other exchanges to compute cross-exchange volume spikes
    other_exchanges = ["okx", "mexc", "bingx"]
    for ex in other_exchanges:
        ex_path = DATA_DIR / ex / f"{coin}_USDT_USDT" / "candles.parquet"
        if ex_path.exists():
            ex_df = pl.read_parquet(ex_path).sort("ts").with_columns(pl.col("ts").cast(pl.Int64))
            ex_df = ex_df.with_columns([
                pl.col("v").rolling_mean(window_size=1440).alias(f"v_mean_24h_{ex}")
            ]).with_columns([
                (pl.col("v") / pl.col(f"v_mean_24h_{ex}")).fill_nan(0.0).fill_null(0.0).alias(f"rvol_{ex}")
            ]).select(["ts", f"rvol_{ex}"])
            df = df.join(ex_df, on="ts", how="left").with_columns(pl.col(f"rvol_{ex}").fill_null(0.0))
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
    
    # Compute 5m and 15m OI percentage change
    # Since candles are 1m, shift(5) corresponds to 5 minutes ago
    df = df.with_columns([
        ((pl.col("oi_usd") - pl.col("oi_usd").shift(5)) / pl.col("oi_usd").shift(5) * 100.0)
        .fill_nan(0.0).fill_null(0.0).alias("delta_oi_5m"),
        ((pl.col("oi_usd") - pl.col("oi_usd").shift(15)) / pl.col("oi_usd").shift(15) * 100.0)
        .fill_nan(0.0).fill_null(0.0).alias("delta_oi_15m")
    ])
    
    # Compute candle body return
    df = df.with_columns([
        ((pl.col("c") - pl.col("o")) / pl.col("o") * 100.0).alias("ret_1m")
    ])
    
    return df


def simulate_model_for_coin(
    df: pl.DataFrame,
    coin: str,
    model_name: str,
    is_split_idx: int
) -> List[Trade]:
    """
    Simulates trading for a specific model on one coin dataset.
    """
    ts_arr = df["ts"].to_numpy()
    o_arr = df["o"].to_numpy()
    h_arr = df["h"].to_numpy()
    l_arr = df["l"].to_numpy()
    c_arr = df["c"].to_numpy()
    rvol_arr = df["rvol_bitget"].to_numpy()
    cross_arr = df["cross_count_3x"].to_numpy()
    ret_arr = df["ret_1m"].to_numpy()
    doi5_arr = df["delta_oi_5m"].to_numpy()
    
    n = len(df)
    trades: List[Trade] = []
    last_trade_exit_idx = -1
    
    # Warmup 1440 candles (1 day for 24h rolling volume)
    for i in range(1440, n - MAX_HOLD_MINUTES - 1):
        if i <= last_trade_exit_idx + COOLDOWN_MINUTES:
            continue
            
        rvol = rvol_arr[i]
        body_ret = ret_arr[i]
        doi = doi5_arr[i]
        cross_cnt = cross_arr[i]
        
        # Check model trigger
        signal = 0  # +1 = Long, -1 = Short
        
        if model_name == "Model_1_Pure_Volume_Spike":
            # Baseline: RVOL >= 5x, enter momentum direction
            if rvol >= 5.0 and abs(body_ret) >= 0.15:
                signal = 1 if body_ret > 0 else -1
                
        elif model_name == "Model_2_Cross_Exchange_Spike":
            # Multi-exchange confirmation: RVOL >= 5x on Bitget AND >= 2 exchanges show RVOL >= 3x
            if rvol >= 5.0 and cross_cnt >= 2 and abs(body_ret) >= 0.15:
                signal = 1 if body_ret > 0 else -1
                
        elif model_name == "Model_3_Volume_Plus_OI_Expansion":
            # Volume Spike + Positive Open Interest Influx (Capital Expansion)
            # Long when green spike & OI jumps; Short when red dump & OI jumps
            if rvol >= 5.0 and abs(body_ret) >= 0.15 and doi >= 1.0:
                signal = 1 if body_ret > 0 else -1
                
        elif model_name == "Model_4_Liquidation_Squeeze_Fade":
            # Liquidation cascade / squeeze exhaustion: Volume spike with collapsing OI (Delta OI <= -1.0%)
            # FADE the move: Counter-trend entry!
            if rvol >= 5.0 and abs(body_ret) >= 0.15 and doi <= -1.0:
                signal = -1 if body_ret > 0 else 1  # Fade green pump with short, fade red dump with long
                
        elif model_name == "Model_5_Cross_Exchange_Plus_OI":
            # Full combination: RVOL >= 5x, Cross-exchange confirmed (>=2), and Positive OI Influx (>=1.0%)
            if rvol >= 5.0 and cross_cnt >= 2 and abs(body_ret) >= 0.15 and doi >= 1.0:
                signal = 1 if body_ret > 0 else -1
                
        if signal == 0:
            continue
            
        # Entry at next candle open
        entry_idx = i + 1
        entry_price = o_arr[entry_idx]
        entry_ts = int(ts_arr[entry_idx])
        is_sample = "IS" if entry_idx < is_split_idx else "OOS"
        
        # Simulate position tracking minute by minute
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
            
            if signal == 1:  # LONG
                unrealized_high = (curr_h - entry_price) / entry_price
                unrealized_low = (curr_l - entry_price) / entry_price
                
                # Check Take Profit
                if unrealized_high >= TP_PCT:
                    exit_idx = k
                    exit_price = entry_price * (1.0 + TP_PCT)
                    exit_reason = "take_profit"
                    break
                    
                # Check Stop Loss
                if unrealized_low <= -SL_PCT:
                    exit_idx = k
                    exit_price = entry_price * (1.0 - SL_PCT)
                    exit_reason = "stop_loss"
                    break
                    
                # Trailing stop update
                if unrealized_high >= TRAILING_ACT:
                    trailing_active = True
                    peak_favorable = max(peak_favorable, curr_h)
                    trailing_stop_price = peak_favorable * (1.0 - TRAILING_DIST)
                    
                if trailing_active and curr_l <= trailing_stop_price:
                    exit_idx = k
                    exit_price = trailing_stop_price
                    exit_reason = "trailing_stop"
                    break
                    
            else:  # SHORT
                unrealized_favorable = (entry_price - curr_l) / entry_price
                unrealized_adverse = (curr_h - entry_price) / entry_price
                
                # Check Take Profit
                if unrealized_favorable >= TP_PCT:
                    exit_idx = k
                    exit_price = entry_price * (1.0 - TP_PCT)
                    exit_reason = "take_profit"
                    break
                    
                # Check Stop Loss
                if unrealized_adverse >= SL_PCT:
                    exit_idx = k
                    exit_price = entry_price * (1.0 + SL_PCT)
                    exit_reason = "stop_loss"
                    break
                    
                # Trailing stop update
                if unrealized_favorable >= TRAILING_ACT:
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
            
        # Compute PnL
        if signal == 1:
            gross_pnl_pct = (exit_price - entry_price) / entry_price
        else:
            gross_pnl_pct = (entry_price - exit_price) / entry_price
            
        net_pnl_usd = (gross_pnl_pct * NOTIONAL_USDT) - ROUNDTRIP_FEE_USD
        
        trades.append(Trade(
            coin=coin,
            model=model_name,
            entry_ts=entry_ts,
            entry_price=entry_price,
            direction=signal,
            exit_ts=int(ts_arr[exit_idx]),
            exit_price=exit_price,
            exit_reason=exit_reason,
            gross_pnl_pct=gross_pnl_pct * 100.0,
            net_pnl_usd=net_pnl_usd,
            is_sample=is_sample
        ))
        
        last_trade_exit_idx = exit_idx
        
    return trades


def calculate_metrics(trades: List[Trade]) -> Dict[str, Any]:
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "net_pnl_usd": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_usd": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_pnl_usd": 0.0,
            "win_trades": 0,
            "loss_trades": 0,
        }
        
    pnls = [t.net_pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    
    total_trades = len(pnls)
    win_trades = len(wins)
    loss_trades = len(losses)
    win_rate = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0
    
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    net_pnl = sum(pnls)
    avg_pnl = net_pnl / total_trades if total_trades > 0 else 0.0
    
    # Calculate Drawdown curve
    cum_pnl = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = running_max - cum_pnl
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
    max_dd_pct = (max_dd / NOTIONAL_USDT) * 100.0  # drawdown relative to position size
    
    return {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "net_pnl_usd": round(net_pnl, 4),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_usd": round(max_dd, 4),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "avg_pnl_usd": round(avg_pnl, 4),
        "win_trades": win_trades,
        "loss_trades": loss_trades,
    }


def main():
    logger.info("Starting Volume Spike & Open Interest Backtest...")
    
    models = [
        "Model_1_Pure_Volume_Spike",
        "Model_2_Cross_Exchange_Spike",
        "Model_3_Volume_Plus_OI_Expansion",
        "Model_4_Liquidation_Squeeze_Fade",
        "Model_5_Cross_Exchange_Plus_OI",
    ]
    
    # Load all coins and align data
    coin_data: Dict[str, pl.DataFrame] = {}
    for coin in TARGET_COINS:
        df = load_coin_dataset(coin)
        if df is not None:
            coin_data[coin] = df
            logger.info(f"Loaded {coin}: {len(df)} 1m candles aligned with 5m OI.")
            
    logger.info(f"Total available coins for backtesting: {len(coin_data)}")
    if not coin_data:
        logger.error("No coin data available! Please check data directories.")
        return
        
    all_results = {}
    
    for model in models:
        all_trades: List[Trade] = []
        coin_breakdown = {}
        
        for coin, df in coin_data.items():
            # In-Sample is first 50% of candles (~14 days), Out-of-Sample is second 50%
            is_split_idx = len(df) // 2
            trades = simulate_model_for_coin(df, coin, model, is_split_idx)
            all_trades.extend(trades)
            
            is_trades = [t for t in trades if t.is_sample == "IS"]
            oos_trades = [t for t in trades if t.is_sample == "OOS"]
            coin_breakdown[coin] = {
                "total": calculate_metrics(trades),
                "is": calculate_metrics(is_trades),
                "oos": calculate_metrics(oos_trades),
            }
            
        is_all = [t for t in all_trades if t.is_sample == "IS"]
        oos_all = [t for t in all_trades if t.is_sample == "OOS"]
        
        is_metrics = calculate_metrics(is_all)
        oos_metrics = calculate_metrics(oos_all)
        total_metrics = calculate_metrics(all_trades)
        
        # Walk-Forward Efficiency: PnL(OOS) / PnL(IS) annualized or proportional
        # If IS > 0: WFE = OOS_pnl / IS_pnl
        if is_metrics["net_pnl_usd"] > 0:
            wfe = round(oos_metrics["net_pnl_usd"] / is_metrics["net_pnl_usd"], 2)
        elif is_metrics["net_pnl_usd"] < 0 and oos_metrics["net_pnl_usd"] > 0:
            wfe = 99.0  # Turnaround from negative to positive
        elif is_metrics["net_pnl_usd"] < 0 and oos_metrics["net_pnl_usd"] < 0:
            wfe = -1.0  # Consistently negative
        else:
            wfe = 0.0
            
        all_results[model] = {
            "summary": {
                "total": total_metrics,
                "is": is_metrics,
                "oos": oos_metrics,
                "wfe": wfe,
            },
            "coin_breakdown": coin_breakdown,
        }
        
        logger.info(
            f"[{model}] Total Trades: {total_metrics['total_trades']}, "
            f"Win Rate: {total_metrics['win_rate']}%, "
            f"Net PnL: ${total_metrics['net_pnl_usd']:.2f}, "
            f"PF: {total_metrics['profit_factor']:.2f}, "
            f"IS PnL: ${is_metrics['net_pnl_usd']:.2f}, "
            f"OOS PnL: ${oos_metrics['net_pnl_usd']:.2f}, "
            f"WFE: {wfe}"
        )
        
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)
        
    logger.info(f"Results successfully saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
