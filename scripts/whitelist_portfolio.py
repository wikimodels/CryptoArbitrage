import json

with open("output/volume_oi_48_results.json") as f:
    res = json.load(f)

# Whitelist of high-performing liquid coins
whitelist = {
    "WLD", "INJ", "OP", "COMP", "LDO", "DYDX", "ENA", "UNI", 
    "BCH", "GRT", "MAGIC", "BNB", "ADA", "1000BONK", "FET", "AAVE"
}

from cryptoarb.research.volume_oi_backtest_48 import load_coin_dataset, simulate_model, calculate_metrics

all_trades = []
for coin in whitelist:
    df = load_coin_dataset(coin)
    if df is not None:
        trades = simulate_model(df, coin, "Model_G_Sniper_Maker", len(df)//2)
        all_trades.extend(trades)

m_tot = calculate_metrics(all_trades)
m_is = calculate_metrics([t for t in all_trades if t.is_sample == "IS"])
m_oos = calculate_metrics([t for t in all_trades if t.is_sample == "OOS"])
wfe = round(m_oos["net_pnl_usd"] / m_is["net_pnl_usd"], 2) if m_is["net_pnl_usd"] > 0 else 99.0

print("\n=======================================================")
print("WHITELIST HIGH-TURNOVER PORTFOLIO (16 ASSETS, Model G Maker)")
print(f"TOTAL: Trades={m_tot['total_trades']}, WinRate={m_tot['win_rate']}%, NetPnL=${m_tot['net_pnl_usd']:+.2f}, PF={m_tot['profit_factor']}, MaxDD=${m_tot['max_drawdown_usd']:.2f}")
print(f"IS:    Trades={m_is['total_trades']}, WinRate={m_is['win_rate']}%, NetPnL=${m_is['net_pnl_usd']:+.2f}, PF={m_is['profit_factor']}")
print(f"OOS:   Trades={m_oos['total_trades']}, WinRate={m_oos['win_rate']}%, NetPnL=${m_oos['net_pnl_usd']:+.2f}, PF={m_oos['profit_factor']}")
print(f"WFE:   {wfe}")
print("=======================================================")
