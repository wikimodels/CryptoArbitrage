from cryptoarb.research.volume_oi_backtest import load_coin_dataset, TARGET_COINS, Trade, calculate_metrics, NOTIONAL_USDT, ROUNDTRIP_FEE_USD
import numpy as np

def run_parameter_sweep():
    # Load all coins
    coin_data = {}
    for coin in TARGET_COINS:
        df = load_coin_dataset(coin)
        if df is not None:
            coin_data[coin] = df

    print(f"Loaded {len(coin_data)} coins. Running sensitivity analysis...")
    
    rvol_threshs = [5.0, 8.0, 10.0]
    doi_threshs = [1.0, 2.0]
    
    for rvol_th in rvol_threshs:
        for doi_th in doi_threshs:
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
                
                n = len(df)
                last_trade_exit_idx = -1
                
                for i in range(1440, n - 61):
                    if i <= last_trade_exit_idx + 15:
                        continue
                    if rvol_arr[i] >= rvol_th and cross_arr[i] >= 2 and abs(ret_arr[i]) >= 0.15 and doi5_arr[i] >= doi_th:
                        signal = 1 if ret_arr[i] > 0 else -1
                        entry_idx = i + 1
                        entry_p = o_arr[entry_idx]
                        
                        exit_p = c_arr[entry_idx + 60]
                        peak_favorable = 0.0
                        trailing_active = False
                        trailing_stop_p = 0.0
                        exit_idx = entry_idx + 60
                        
                        for k in range(entry_idx, entry_idx + 60):
                            curr_h = h_arr[k]
                            curr_l = l_arr[k]
                            curr_c = c_arr[k]
                            
                            if signal == 1:
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
                                    peak_favorable = max(peak_favorable, curr_h)
                                    trailing_stop_p = peak_favorable * 0.995
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
                                    peak_favorable = min(peak_favorable, curr_l) if peak_favorable > 0 else curr_l
                                    trailing_stop_p = peak_favorable * 1.005
                                if trailing_active and curr_h >= trailing_stop_p:
                                    exit_p = trailing_stop_p
                                    exit_idx = k
                                    break
                            exit_p = curr_c
                            exit_idx = k
                            
                        gross_pnl_pct = (exit_p - entry_p)/entry_p if signal == 1 else (entry_p - exit_p)/entry_p
                        net_pnl = gross_pnl_pct * NOTIONAL_USDT - ROUNDTRIP_FEE_USD
                        all_trades.append(Trade(
                            coin=coin, model="", entry_ts=int(ts_arr[entry_idx]),
                            entry_price=entry_p, direction=signal, exit_ts=int(ts_arr[exit_idx]),
                            exit_price=exit_p, exit_reason="", gross_pnl_pct=gross_pnl_pct*100,
                            net_pnl_usd=net_pnl, is_sample="IS" if entry_idx < len(df)//2 else "OOS"
                        ))
                        last_trade_exit_idx = exit_idx
                        
            is_trades = [t for t in all_trades if t.is_sample == "IS"]
            oos_trades = [t for t in all_trades if t.is_sample == "OOS"]
            m_tot = calculate_metrics(all_trades)
            m_is = calculate_metrics(is_trades)
            m_oos = calculate_metrics(oos_trades)
            print(f"RVOL>={rvol_th:<4} & dOI>={doi_th}%: Trades={m_tot['total_trades']:<4} | WinRate={m_tot['win_rate']:<5}% (IS:{m_is['win_rate']}%/OOS:{m_oos['win_rate']}%) | NetPnL=${m_tot['net_pnl_usd']:<7} (IS:${m_is['net_pnl_usd']}/OOS:${m_oos['net_pnl_usd']}) | PF={m_tot['profit_factor']}")

if __name__ == "__main__":
    run_parameter_sweep()
