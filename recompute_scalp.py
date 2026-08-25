"""Пересчёт скальп-статистики из журнала сигналов (signals).
Восстанавливает спайки/сходимости по временным рядам спредов —
данные, собранные до фикса трекера, не пропадают."""
import sqlite3, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPIKE_MIN = 0.3      # % — порог спайка (как в конфиге)
CONV_FRAC = 0.5      # сжатие до 50% = сошёлся
CONV_WIN = 120       # секунд на сходимость

conn = sqlite3.connect("data/scanner.db")
rows = conn.execute("""SELECT ts, symbol, exch_long, exch_short, raw_spread_pct
    FROM signals WHERE raw_spread_pct > 0 ORDER BY ts""").fetchall()
print("сигналов в журнале:", len(rows))

# (symbol, pair) -> {"spike_ts":, "spike":, "spikes": n, "converged": n,
#                    "capture_sum":, "width_sum":, "width_n":, "expired": n}
state = {}
for ts, sym, el, es, raw in rows:
    key = (sym, el, es)
    st = state.setdefault(key, {"spike_ts": None, "spike": None, "spikes": 0,
                                "converged": 0, "capture_sum": 0.0,
                                "width_sum": 0.0, "width_n": 0})
    # ширина книг — среднее по сигналам
    if False:
        st["width_sum"] += width
        st["width_n"] += 1

    open_spike = st["spike"] is not None
    if open_spike:
        dt = ts - st["spike_ts"]
        if raw <= st["spike"] * CONV_FRAC:
            if dt <= CONV_WIN:
                st["converged"] += 1
                st["capture_sum"] += (st["spike"] - raw)
            # иначе — спайк истёк, списан
            st["spike_ts"], st["spike"] = None, None
        elif dt > CONV_WIN:
            # окно истекло без сжатия: если спред всё ещё высок — это новый спайк
            if raw >= SPIKE_MIN:
                st["spikes"] += 1
                st["spike_ts"], st["spike"] = ts, raw
            else:
                st["spike_ts"], st["spike"] = None, None
        # иначе: ждём (спред между спайком и порогом сходимости)
    elif raw >= SPIKE_MIN:
        st["spikes"] += 1
        st["spike_ts"], st["spike"] = ts, raw

# собираем результат
out = {}
print("\n%-22s %7s %9s %8s %9s %8s" % ("symbol", "spikes", "converged", "rate%", "capture", "width"))
for (sym, el, es), st in state.items():
    if st["spikes"] == 0:
        continue
    rate = st["converged"] / st["spikes"] * 100
    cap = st["capture_sum"] / max(st["converged"], 1)
    w = st["width_sum"] / st["width_n"] if st["width_n"] else 0.0
    print("%-22s %7d %9d %7.1f%% %8.3f %8.3f" % (sym[:22], st["spikes"], st["converged"], rate, cap, w))
    # ключ как в движке: только symbol (статистика ведётся по символу)
    prev = out.get(sym)
    if prev:
        prev["spikes"] += st["spikes"]; prev["converged"] += st["converged"]
        prev["capture_sum"] += st["capture_sum"]; prev["width_sum"] += st["width_sum"]
        prev["width_n"] += st["width_n"]
    else:
        out[sym] = {"spikes": st["spikes"], "converged": st["converged"],
                    "capture_sum": st["capture_sum"], "width_sum": st["width_sum"],
                    "width_n": st["width_n"], "updated_ts": time.time()}

# записываем в scalp_stats (перезаписываем сломанные агрегаты)
now = time.time()
for sym, st in out.items():
    conn.execute("""INSERT OR REPLACE INTO scalp_stats
        (symbol, spikes, converged, capture_sum, width_sum, width_n, updated_ts)
        VALUES (?,?,?,?,?,?,?)""",
        (sym, st["spikes"], st["converged"], st["capture_sum"],
         st["width_sum"], st["width_n"], now))
conn.commit()
print("\nобновлено монет в scalp_stats:", len(out))
conn.close()
