"""
Скрипт автоматического отбора вселенной монет для арбитража.
Отбирает топ-N монет, доступных на всех целевых биржах,
в заданном коридоре ликвидности (по умолчанию 100k - 10M USDT/сутки).
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import argparse
import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("universe")

from cryptoarb.backtest.universe import fetch_perp_symbols, fetch_volumes


def select_universe(
    exchanges: list[str],
    top_n: int = 250,
    min_volume: float = 100_000.0,
    max_volume: float = 50_000_000.0,
    min_common_exchanges: int = 3,
    exclude_symbols: list[str] | None = None,
    output_file: Path | str = "output/top40_4ex.txt",
) -> list[tuple[str, float]]:
    log.info("Загрузка активных контрактов с бирж: %s...", exchanges)
    symbols_by_ex: dict[str, set[str]] = {}
    with ThreadPoolExecutor(max_workers=len(exchanges)) as pool:
        futures = {pool.submit(fetch_perp_symbols, ex): ex for ex in exchanges}
        for fut in futures:
            ex = futures[fut]
            try:
                syms, _ = fut.result()
                symbols_by_ex[ex] = set(syms)
                log.info("[%s] Доступно %d USDT-фьючерсов", ex, len(syms))
            except Exception as e:
                log.error("[%s] Ошибка получения рынков: %s", ex, e)
                symbols_by_ex[ex] = set()

    # Монеты, которые есть как минимум на min_common_exchanges биржах
    from collections import Counter
    symbol_counter = Counter()
    for ex in exchanges:
        for s in symbols_by_ex[ex]:
            symbol_counter[s] += 1

    common_symbols = sorted(s for s, count in symbol_counter.items() if count >= min_common_exchanges)
    log.info("Монет на >= %d из %d бирж: %d", min_common_exchanges, len(exchanges), len(common_symbols))

    log.info("Загрузка 24ч объемов торгов параллельно...")
    vols_by_ex: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=len(exchanges)) as pool:
        futures = {pool.submit(fetch_volumes, ex): ex for ex in exchanges}
        for fut in futures:
            ex = futures[fut]
            try:
                vols_by_ex[ex] = fut.result()
                log.info("[%s] Загружено %d объемов", ex, len(vols_by_ex[ex]))
            except Exception as e:
                log.error("[%s] Ошибка получения объемов: %s", ex, e)
                vols_by_ex[ex] = {}

    exclude_set = {x.strip().upper() for x in (exclude_symbols or []) if x.strip()}

    candidates: list[tuple[str, float, list[float]]] = []
    for s in common_symbols:
        s_upper = s.upper()
        base = s.split("/")[0].upper()
        if s_upper in exclude_set or base in exclude_set or f"{base}/USDT:USDT" in exclude_set:
            continue
        vs = [vols_by_ex[ex].get(s, 0.0) for ex in exchanges if vols_by_ex[ex].get(s, 0.0) > 0]
        if len(vs) < min_common_exchanges:
            continue
        med = float(statistics.median(vs))
        if min_volume <= med <= max_volume:
            candidates.append((s, med, vs))

    candidates.sort(key=lambda x: x[1], reverse=True)
    selected = candidates[:top_n]

    log.info("Отобрано кандидатов в коридоре $%d - $%d (мин. %d бирж): %d (берем топ-%d)",
             int(min_volume), int(max_volume), min_common_exchanges, len(candidates), len(selected))

    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(s for s, _, _ in selected) + "\n")

    log.info("Список из %d монет успешно сохранен в: %s", len(selected), out_path)
    return [(s, med) for s, med, _ in selected]


def main():
    parser = argparse.ArgumentParser(description="Отбор топа монет для арбитража")
    parser.add_argument("--top", type=int, default=250, help="Количество монет (по умолчанию 250)")
    parser.add_argument("--min-exchanges", type=int, default=3, help="Минимум бирж для монеты (по умолчанию 3)")
    parser.add_argument("--min-vol", type=float, default=100_000, help="Мин. суточный объем USDT (по умолчанию 100000)")
    parser.add_argument("--max-vol", type=float, default=50_000_000, help="Макс. суточный объем USDT (по умолчанию 50000000)")
    parser.add_argument("--exclude", type=str, default="BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT", help="Символы или базы для исключения через запятую")
    parser.add_argument("--exchanges", type=str, default="okx,bitget,mexc,bingx", help="Список бирж через запятую")
    parser.add_argument("--output", type=str, default="output/top40_4ex.txt", help="Путь к файлу со списком")

    args = parser.parse_args()
    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]
    exclude_list = [e.strip() for e in args.exclude.split(",") if e.strip()]

    selected = select_universe(
        exchanges=exchanges,
        top_n=args.top,
        min_volume=args.min_vol,
        max_volume=args.max_vol,
        min_common_exchanges=args.min_exchanges,
        exclude_symbols=exclude_list,
        output_file=args.output,
    )

    print("\n" + "=" * 65)
    print(f"ТОП-{len(selected)} ОТОБРАННЫХ МОНЕТ (Медианный суточный объем)")
    print("=" * 65)
    for idx, (sym, med) in enumerate(selected, 1):
        print(f"{idx:3d}. {sym:<25} : ${med:,.0f} USDT/сутки")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
