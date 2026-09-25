"""
Оркестратор ежедневного конвейера Walk-Forward рекалибровки парного арбитража.
Выполняет полный цикл от скрининга 85+ монет до 4-фолдовой слепой форвардной валидации
и атомарной записи утвержденного реестра active_cross_pairs.json для торгового движка.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from cryptoarb.recalibration.screener import CointegrationScreener
from cryptoarb.recalibration.wfa import WalkForwardEngine, TOTAL_MARGIN, POSITION_SIZE_USDT

log = logging.getLogger("recalibration.pipeline")


def _json_serial(obj: Any) -> Any:
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.floating, float)):
        return float(obj)
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (datetime, Path)):
        return str(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


class RecalibrationPipeline:
    """Конвейер регулярной актуализации пула торгуемых пар."""

    def __init__(
        self,
        data_dir: str | Path = "data/raw_1m_30d/bitget",
        output_dir: str | Path = "output",
        top_screen_candidates: int = 15,
        max_active_pairs: int = 6,
        min_wfe_pct: float = 50.0,
    ):
        self.data_dir = Path(data_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.top_screen_candidates = top_screen_candidates
        self.max_active_pairs = max_active_pairs
        self.min_wfe_pct = min_wfe_pct

        self.screener = CointegrationScreener(data_dir=self.data_dir)
        self.wfa_engine = WalkForwardEngine(data_dir=self.data_dir)

    def run(self, reuse_screening: bool = False) -> dict[str, Any]:
        """Запуск полного цикла рекалибровки."""
        start_time = time.time()
        now_utc = datetime.now(timezone.utc).isoformat()
        log.info("Запуск конвейера суточной рекалибровки [%s]...", now_utc)

        coint_file = self.output_dir / "cointegrated_pairs.json"
        coint_candidates = []

        if reuse_screening and coint_file.exists():
            try:
                with open(coint_file, "r", encoding="utf-8") as f:
                    coint_candidates = json.load(f)
                log.info("Использованы ранее сохраненные кандидаты из %s: %d пар", coint_file, len(coint_candidates))
            except Exception:
                coint_candidates = []

        if not coint_candidates:
            # 1. Загрузка данных ликвидных монет
            series, _ = self.screener.load_liquid_series()
            if not series:
                log.error("Нет данных для скрининга в %s", self.data_dir)
                return {"status": "error", "message": "No data available"}

            # 2. Векторный скрининг коинтеграции
            coint_candidates = self.screener.run_screening(series)
            with open(coint_file, "w", encoding="utf-8") as f:
                json.dump(coint_candidates, f, indent=2, ensure_ascii=False)
            log.info("Кандидатов коинтеграции сохранено в %s: %d", coint_file, len(coint_candidates))

        # 3. Отбор кандидатов на Walk-Forward тестирование
        top_candidates = coint_candidates[:self.top_screen_candidates]
        wfa_results = []

        log.info("Запуск Walk-Forward анализа для топ-%d кандидатов...", len(top_candidates))
        for idx, cand in enumerate(top_candidates, 1):
            ca, cb = cand["coin_a"], cand["coin_b"]
            log.info("[%d/%d] WFA тестирование пары %s / %s...", idx, len(top_candidates), ca, cb)
            res = self.wfa_engine.evaluate_pair(ca, cb)
            if res is not None:
                # Добавляем данные первичного скрининга
                res["corr"] = cand.get("corr", 0.0)
                res["half_life_hours"] = cand.get("half_life_hours", 0.0)
                res["coint_pvalue"] = cand.get("coint_pvalue", 1.0)
                wfa_results.append(res)

        wfa_file = self.output_dir / "wfa_results.json"
        with open(wfa_file, "w", encoding="utf-8") as f:
            json.dump(wfa_results, f, indent=2, ensure_ascii=False, default=_json_serial)

        # 4. Фильтрация прошедших форвардную проверку (OOS PnL > 0 и WFE >= 50%)
        passed = [
            r for r in wfa_results
            if r["tot_oos_pnl_usdt"] > 0 and r["avg_wfe_pct"] >= self.min_wfe_pct
        ]
        # Сортировка по чистому форвардному профиту
        passed.sort(key=lambda x: x["tot_oos_pnl_usdt"], reverse=True)
        selected_pairs = passed[:self.max_active_pairs]

        # 5. Формирование утвержденного реестра для стратегии
        active_registry = {
            "updated_at": now_utc,
            "pipeline_duration_sec": round(time.time() - start_time, 2),
            "position_size_usdt": POSITION_SIZE_USDT,
            "total_margin_usdt": TOTAL_MARGIN,
            "pairs_evaluated": len(top_candidates),
            "pairs_passed_wfa": len(passed),
            "pairs_active_count": len(selected_pairs),
            "pairs": []
        }

        for rank, p in enumerate(selected_pairs, 1):
            label = (
                f"WFA Rank {rank}: {p['coin_a']} / {p['coin_b']} "
                f"(WR {p['avg_oos_win_rate']}%, OOS PnL +${p['tot_oos_pnl_usdt']:.2f}, WFE {p['avg_wfe_pct']:.0f}%)"
            )
            active_registry["pairs"].append({
                "rank": rank,
                "coin_a": p["coin_a"],
                "coin_b": p["coin_b"],
                "sym_a": p["sym_a"],
                "sym_b": p["sym_b"],
                "label": label,
                "beta": p["calibrated_beta"],
                "alpha": p["calibrated_alpha"],
                "entry_z": p["optimal_entry_z"],
                "half_life_hours": p.get("half_life_hours", 0.0),
                "wfe_pct": p["avg_wfe_pct"],
                "oos_pnl_usdt": p["tot_oos_pnl_usdt"],
                "oos_win_rate": p["avg_oos_win_rate"],
                "max_dd_usdt": p["max_oos_dd_usdt"],
                "roi_to_margin_pct": p["roi_to_margin_pct"],
            })

        active_file = self.output_dir / "active_cross_pairs.json"
        temp_file = self.output_dir / "active_cross_pairs.json.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(active_registry, f, indent=2, ensure_ascii=False, default=_json_serial)
        temp_file.replace(active_file)

        log.info("Реестр активных пар обновлен в %s: %d пар", active_file, len(selected_pairs))
        return active_registry


def main():
    parser = argparse.ArgumentParser(description="Автономный конвейер Walk-Forward рекалибровки парного арбитража")
    parser.add_argument("--data-dir", default="data/raw_1m_30d/bitget", help="Путь к 1m паркетам")
    parser.add_argument("--output-dir", default="output", help="Каталог выходных JSON файлов")
    parser.add_argument("--candidates", type=int, default=15, help="Число топ-кандидатов для WFA")
    parser.add_argument("--top", type=int, default=6, help="Максимальное число активных пар в реестре")
    parser.add_argument("--min-wfe", type=float, default=50.0, help="Минимальный порог WFE в процентах")
    parser.add_argument("--reuse-screening", action="store_true", help="Использовать готовый cointegrated_pairs.json")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 80)
    print("DAILY WALK-FORWARD RECALIBRATION PIPELINE")
    print(f"Калибровка: нотионал ${POSITION_SIZE_USDT:.1f} ($2.0 маржи на пару), WFE порог >= {args.min_wfe:.0f}%")
    print("=" * 80)

    pipe = RecalibrationPipeline(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        top_screen_candidates=args.candidates,
        max_active_pairs=args.top,
        min_wfe_pct=args.min_wfe,
    )
    res = pipe.run(reuse_screening=args.reuse_screening)

    print("\n" + "=" * 80)
    print(f"ИТОГИ РЕКАЛИБРОВКИ: УТВЕРЖДЕНО {res['pairs_active_count']} АКТИВНЫХ ПАР")
    print("=" * 80)
    for p in res["pairs"]:
        print(
            f"#{p['rank']} {p['coin_a']:<8} / {p['coin_b']:<8} | "
            f"OOS PnL: +${p['oos_pnl_usdt']:.3f} | "
            f"WR: {p['oos_win_rate']:.1f}% | "
            f"WFE: {p['wfe_pct']:.0f}% | "
            f"Entry Z: {p['entry_z']:.1f} | "
            f"Beta: {p['beta']:.4f} | "
            f"t1/2: {p['half_life_hours']:.1f}h"
        )
    print("=" * 80)


if __name__ == "__main__":
    main()
