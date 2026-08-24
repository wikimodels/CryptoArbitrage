"""
Точка входа CryptoArbitrage.

Поднимает в одном event loop:
  - Engine: WS-стримы цен (ccxt.pro) по всем биржам, REST funding,
    сканер спредов, эмулятор сделок.
  - Dashboard: FastAPI + WebSocket (тёмная тема), если включён в конфиге.

Запуск:
    poetry run cryptoarb                 # (script из pyproject)
    poetry run python -m cryptoarb.main  # эквивалент
    poetry run python -m cryptoarb.main --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def _force_utf8_stdio():
    """Windows-консоли часто cp1252 — кириллические print падают.
    Принудительно UTF-8 с заменой непечатаемых, независимо от окружения."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from .alerts import ConsoleAlertChannel
from .config import load_config
from .emulator import Emulator
from .engine import Engine
from .logger_setup import Loggers
from .storage import Storage


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # ccxt довольно болтлив на INFO — приглушаем
    logging.getLogger("ccxt").setLevel(logging.WARNING)


async def main_async(config_path: str):
    _setup_logging()
    cfg = load_config(config_path)

    loggers = Loggers(cfg["logging"]["dir"], cfg["logging"].get("retention_days", 90))
    storage = Storage(cfg["storage"]["sqlite_path"], cfg["storage"].get("retention_days", 30))
    alerts = ConsoleAlertChannel()
    emulator = Emulator(
        loggers, storage, alerts,
        position_size_usdt=cfg["emulator"].get(
            "fixed_position_size_usdt",
            cfg["emulator"].get("virtual_position_size_usdt", 1000)),
        orphan_timeout_sec=cfg["emulator"]["orphan_leg_timeout_sec"],
        cooldown_sec=cfg["emulator"]["cooldown_sec"],
        max_holding_hours=cfg["emulator"]["max_holding_hours"],
        loss_cooldown_sec=cfg["emulator"].get("loss_cooldown_sec", 900),
        max_open_per_pair=cfg["emulator"].get("max_open_per_pair", 1),
        max_entry_slippage_pct=cfg["emulator"].get("max_entry_slippage_pct", 0.15),
        position_size_mode=cfg["emulator"].get("position_size_mode", "dynamic"),
        dynamic_size_pct_of_book=cfg["emulator"].get("dynamic_size_pct_of_book", 10),
        dynamic_size_top_levels=cfg["emulator"].get("dynamic_size_top_levels", 3),
        min_position_size_usdt=cfg["emulator"].get("min_position_size_usdt", 50),
        max_position_size_usdt=cfg["emulator"].get("max_position_size_usdt", 1000),
    )

    engine = Engine(cfg, loggers, storage, alerts, emulator)
    await engine.start()

    server_task = None
    if cfg["dashboard"]["enabled"]:
        import uvicorn
        from .dashboard import create_app
        app = create_app(engine)
        uv_cfg = uvicorn.Config(
            app, host=cfg["dashboard"]["host"], port=cfg["dashboard"]["port"],
            log_level="warning", loop="asyncio",
        )
        server = uvicorn.Server(uv_cfg)
        server_task = asyncio.create_task(server.serve(), name="dashboard")
        print(f"[SYSTEM] Дашборд: http://{cfg['dashboard']['host']}:{cfg['dashboard']['port']}")

    try:
        if server_task:
            await server_task
        else:
            while True:
                await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await engine.stop()
        loggers.close_all()
        storage.close()


def run():
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="CryptoArbitrage screener + dashboard")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.config))
    except KeyboardInterrupt:
        print("\n[SYSTEM] Остановка.")


if __name__ == "__main__":
    run()
