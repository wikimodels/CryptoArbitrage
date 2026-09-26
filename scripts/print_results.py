import json

with open("output/volume_oi_results.json") as f:
    res = json.load(f)

for model_name, model_data in res.items():
    s = model_data["summary"]["total"]
    s_is = model_data["summary"]["is"]
    s_oos = model_data["summary"]["oos"]
    wfe = model_data["summary"]["wfe"]
    print(f"\n=====================================================================")
    print(f"MODEL: {model_name}")
    print(f"TOTAL: Trades={s['total_trades']}, WinRate={s['win_rate']}%, NetPnL=${s['net_pnl_usd']:.2f}, PF={s['profit_factor']}")
    print(f"IS:    Trades={s_is['total_trades']}, WinRate={s_is['win_rate']}%, NetPnL=${s_is['net_pnl_usd']:.2f}, PF={s_is['profit_factor']}")
    print(f"OOS:   Trades={s_oos['total_trades']}, WinRate={s_oos['win_rate']}%, NetPnL=${s_oos['net_pnl_usd']:.2f}, PF={s_oos['profit_factor']}")
    print(f"WFE:   {wfe}")
    print("---------------------------------------------------------------------")
    print(f"{'Coin':<8} | {'Trades':<6} | {'WinRate%':<8} | {'NetPnL($)':<10} | {'PF':<6} | {'IS_PnL':<8} | {'OOS_PnL':<8}")
    print("---------------------------------------------------------------------")
    for coin, cdata in model_data["coin_breakdown"].items():
        ctot = cdata["total"]
        cis = cdata["is"]
        coos = cdata["oos"]
        if ctot["total_trades"] > 0:
            print(f"{coin:<8} | {ctot['total_trades']:<6} | {ctot['win_rate']:<8.1f} | {ctot['net_pnl_usd']:<10.3f} | {ctot['profit_factor']:<6.2f} | {cis['net_pnl_usd']:<8.3f} | {coos['net_pnl_usd']:<8.3f}")
