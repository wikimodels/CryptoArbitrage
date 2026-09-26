"""
Ultra-fast, lightweight WebSocket streams for Binance Futures & Aster DEX.
Zero polling overhead, auto-reconnect, and sub-millisecond parsing.
"""

import asyncio
import json
import logging
import time
from typing import Callable, List, Optional
import websockets

from .models import OrderBookDepth5

log = logging.getLogger("obi_stream")


class VenueWebSocket:
    """
    Subscribes to depth5@100ms on Binance or Aster DEX.
    Both venues use identical Binance-protocol formats.
    """

    def __init__(
        self,
        name: str,
        base_ws_url: str,
        symbols: List[str],
        callback: Callable[[OrderBookDepth5], None],
    ):
        self.name = name
        self.base_ws_url = base_ws_url.rstrip("/")
        self.symbols = [s.lower() for s in symbols]
        self.callback = callback
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.connected = False
        self.last_msg_ts = 0.0

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False

    async def _run_loop(self):
        # Format combined stream url or individual stream
        # E.g. wss://fstream.binance.com/stream?streams=btcusdt@depth5@100ms/ethusdt@depth5@100ms
        if len(self.symbols) == 1:
            url = f"{self.base_ws_url}/ws/{self.symbols[0]}@depth5@100ms"
        else:
            streams_param = "/".join(f"{s}@depth5@100ms" for s in self.symbols)
            url = f"{self.base_ws_url}/stream?streams={streams_param}"

        while self._running:
            try:
                log.info(f"[{self.name}] Connecting to {url}...")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_queue=100,
                ) as ws:
                    self.connected = True
                    log.info(f"[{self.name}] Connected successfully!")
                    while self._running:
                        msg = await ws.recv()
                        self.last_msg_ts = time.time()
                        self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                log.warning(f"[{self.name}] WS disconnected: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3.0)

    def _handle_message(self, raw_msg: str):
        try:
            payload = json.loads(raw_msg)
            data = payload.get("data", payload)
            
            # Format: 's' = symbol, 'E' = event time, 'b' = bids, 'a' = asks
            sym = data.get("s")
            if not sym:
                return
            
            exchange_ts = int(data.get("E", 0))
            raw_bids = data.get("b", [])
            raw_asks = data.get("a", [])
            
            bids = [(float(p), float(q)) for p, q in raw_bids[:5]]
            asks = [(float(p), float(q)) for p, q in raw_asks[:5]]
            
            snapshot = OrderBookDepth5(
                exchange=self.name,
                symbol=sym.upper(),
                ts=time.time(),
                exchange_ts=exchange_ts,
                bids=bids,
                asks=asks,
            )
            self.callback(snapshot)
        except Exception:
            pass
