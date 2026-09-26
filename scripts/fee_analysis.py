import json

with open("output/volume_oi_results.json") as f:
    res = json.load(f)

for m in ["Model_1_Pure_Volume_Spike", "Model_3_Volume_Plus_OI_Expansion", "Model_5_Cross_Exchange_Plus_OI"]:
    summary = res[m]["summary"]["total"]
    trades = summary["total_trades"]
    net_pnl = summary["net_pnl_usd"]
    total_fee_taker = trades * 0.022  # 0.22%
    gross_pnl = net_pnl + total_fee_taker
    total_fee_maker = trades * 0.004  # 0.04% maker
    net_pnl_maker = gross_pnl - total_fee_maker
    
    print(f"\n--- {m} ---")
    print(f"Trades: {trades}, Win Rate: {summary['win_rate']}%")
    print(f"Gross PnL (0% fee):  ${gross_pnl:+.3f}")
    print(f"Maker Fee (0.04%):   ${total_fee_maker:.3f} -> Maker Net PnL: ${net_pnl_maker:+.3f}")
    print(f"Taker Fee (0.22%):   ${total_fee_taker:.3f} -> Taker Net PnL: ${net_pnl:+.3f}")
