import json

with open("output/volume_oi_48_results.json") as f:
    res = json.load(f)

for m_name in ["Model_E_Trend_Sniper", "Model_G_Sniper_Maker"]:
    m_data = res[m_name]
    summary = m_data["summary"]["total"]
    print(f"\n=========================================================================")
    print(f"MODEL: {m_name}")
    print(f"Total: Trades={summary['total_trades']}, WinRate={summary['win_rate']}%, NetPnL=${summary['net_pnl_usd']:.2f}, PF={summary['profit_factor']}")
    print("-------------------------------------------------------------------------")
    print(f"{'Coin':<10} | {'Trades':<6} | {'WinRate%':<8} | {'NetPnL($)':<10} | {'PF':<6} | {'IS_WR%':<7} | {'OOS_WR%':<8}")
    print("-------------------------------------------------------------------------")
    
    coin_list = []
    for c, cdata in m_data["coin_breakdown"].items():
        tot = cdata["total"]
        if tot["total_trades"] > 0:
            coin_list.append((c, tot, cdata["is"], cdata["oos"]))
            
    # Sort by Win Rate descending, then trades
    coin_list.sort(key=lambda x: (x[1]["win_rate"], x[1]["total_trades"]), reverse=True)
    
    for c, tot, is_m, oos_m in coin_list:
        print(f"{c:<10} | {tot['total_trades']:<6} | {tot['win_rate']:<8.1f} | {tot['net_pnl_usd']:<10.3f} | {tot['profit_factor']:<6.2f} | {is_m['win_rate']:<7.1f} | {oos_m['win_rate']:<8.1f}")
