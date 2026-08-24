"""
Универсальный коннектор для CEX на базе ccxt.pro (WebSocket).

ccxt.pro с версии ccxt 1.95+ входит в бесплатный MIT-пакет ccxt — тот же
unified API, но с push-стримами (watch_tickers / watch_order_book) вместо
polling. Цены/стакан идут по WS, funding тянется REST-ом раз в несколько
минут (меняется редко). Если биржа не поддерживает watch_tickers — честный
фолбэк на REST fetch_tickers, интерфейс ExchangeConnector не меняется.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import ccxt.pro as ccxtpro

from ..base import (
    ExchangeConnector, OrderBookLevel, OrderBookSnapshot, PriceCallback,
)

log = logging.getLogger("connector")

# Канонический id из config.yaml -> id биржи в ccxt.
# ВАЖНО: для деривативов у части бирж отдельный id/тип рынка.
CCXT_ID_MAP = {
    "bybit": "bybit",
    "okx": "okx",
    "gateio": "gate",            # gateio переименован в 'gate' в новых ccxt
    "binance": "binanceusdm",    # USD-M perpetual futures
    "bitget": "bitget",
    "mexc": "mexc",
    "kucoin": "kucoinfutures",   # спотовый 'kucoin' НЕ отдаёт свопы
    "htx": "htx",
    "bingx": "bingx",
    "coinex": "coinex",
}


class CCXTConnector(ExchangeConnector):
    def __init__(self, exchange_id: str, default_taker_fee: float, default_maker_fee: float,
                 api_key: Optional[str] = None, api_secret: Optional[str] = None):
        ccxt_id = CCXT_ID_MAP.get(exchange_id, exchange_id)
        if not hasattr(ccxtpro, ccxt_id):
            raise ValueError(
                f"Биржа '{exchange_id}' (ccxt id '{ccxt_id}') не поддержана в ccxt.pro — "
                f"нужен отдельный адаптер (см. README)."
            )
        self.name = exchange_id
        self._ccxt_id = ccxt_id
        cls = getattr(ccxtpro, ccxt_id)
        params: dict = {
            "enableRateLimit": True,
            "timeout": 30000,  # у gate/mexc огромные списки рынков, 10с мало
            "options": {"defaultType": "swap"},
        }
        if api_key and api_secret:
            # На этапе скринера/эмулятора ключи (если заданы) должны быть READ-ONLY.
            params["apiKey"] = api_key
            params["secret"] = api_secret
        self._client = cls(params)
        self.default_taker_fee = default_taker_fee
        self.default_maker_fee = default_maker_fee
        self._fees: dict[str, tuple[float, float]] = {}
        self._csize: dict[str, float] = {}
        self._supports_watch_tickers = bool(self._client.has.get("watchTickers"))
        self._supports_watch_bidsasks = bool(self._client.has.get("watchBidsAsks"))
        self._closed = False

    async def connect(self) -> None:
        await self._client.load_markets()

    async def close(self) -> None:
        self._closed = True
        try:
            await self._client.close()
        except Exception:
            pass

    async def fetch_symbols(self) -> list[str]:
        markets = self._client.markets or {}
        out = []
        for m in markets.values():
            # active может быть True или None (некоторые биржи не заполняют) —
            # отсекаем только явно НЕактивные (False).
            if (m.get("swap") and m.get("linear") and m.get("quote") == "USDT"
                    and m.get("active") is not False):
                sym = m["symbol"]
                out.append(sym)
                taker = m.get("taker") or self.default_taker_fee
                maker = m.get("maker") or self.default_maker_fee
                self._fees[sym] = (float(taker), float(maker))
                # contractSize: сколько базовой монеты в 1 контракте (для нотионала)
                self._csize[sym] = float(m.get("contractSize") or 1.0)
        return sorted(out)

    def fees_for(self, symbol: str) -> tuple[float, float]:
        return self._fees.get(symbol, (self.default_taker_fee, self.default_maker_fee))

    def _contract_size(self, symbol: str) -> float:
        return self._csize.get(symbol, 1.0)

    # -------------------- WS PRICES --------------------

    async def watch_prices(self, symbols: list[str], on_price: PriceCallback) -> None:
        """Долгоживущий цикл. Сам переподключается. Останавливается по close()."""
        backoff = 1.0
        while not self._closed:
            try:
                if self._supports_watch_bidsasks:
                    await self._loop_bids_asks(symbols, on_price)
                elif self._supports_watch_tickers:
                    await self._loop_tickers(symbols, on_price)
                else:
                    await self._loop_rest(symbols, on_price)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closed:
                    return
                log.warning("[%s] WS обрыв: %s — реконнект через %.1fs", self.name, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _loop_bids_asks(self, symbols, on_price):
        while not self._closed:
            data = await self._client.watch_bids_asks(symbols)
            for sym, t in data.items():
                self._emit(sym, t, on_price)

    async def _loop_tickers(self, symbols, on_price):
        while not self._closed:
            data = await self._client.watch_tickers(symbols)
            for sym, t in data.items():
                self._emit(sym, t, on_price)

    async def _loop_rest(self, symbols, on_price):
        # Фолбэк для бирж без watchTickers — вежливый REST-поллинг.
        while not self._closed:
            try:
                data = await self._client.fetch_tickers(symbols)
                for sym, t in data.items():
                    self._emit(sym, t, on_price)
            except Exception as e:
                log.debug("[%s] REST tickers: %s", self.name, e)
            await asyncio.sleep(1.0)

    def _emit(self, sym: str, t: dict, on_price: PriceCallback):
        bid = t.get("bid")
        ask = t.get("ask")
        if bid is None or ask is None:
            return
        cs = self._contract_size(sym)
        on_price(
            self.name, sym, float(bid), float(ask),
            float(t.get("bidVolume") or 0.0) * cs, float(t.get("askVolume") or 0.0) * cs,
        )

    # -------------------- FUNDING (REST) --------------------

    async def refresh_funding(self, symbols: list[str]) -> dict[str, tuple[float, float, Optional[float]]]:
        out: dict[str, tuple[float, float, Optional[float]]] = {}
        if self._client.has.get("fetchFundingRates"):
            try:
                rates = await self._client.fetch_funding_rates(symbols)
                for sym, fr in rates.items():
                    out[sym] = self._parse_funding(fr)
                if out:
                    return out
            except Exception as e:
                log.debug("[%s] bulk funding: %s", self.name, e)
        # Фолбэк по одному, с ограничением параллелизма
        sem = asyncio.Semaphore(5)

        async def one(sym):
            async with sem:
                try:
                    fr = await self._client.fetch_funding_rate(sym)
                    out[sym] = self._parse_funding(fr)
                except Exception:
                    pass

        await asyncio.gather(*(one(s) for s in symbols))
        return out

    @staticmethod
    def _parse_funding(fr: dict) -> tuple[float, float, Optional[float]]:
        rate = float(fr.get("fundingRate") or 0.0)
        next_ts = fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp")
        interval_h = 8.0
        interval = fr.get("interval")
        if interval:
            s = str(interval).lower().strip()
            try:
                if s.endswith("h"):
                    interval_h = float(s[:-1])
                elif s.endswith("m"):
                    interval_h = float(s[:-1]) / 60.0
                elif s.endswith("d"):
                    interval_h = float(s[:-1]) * 24.0
                else:
                    interval_h = float(s)
            except Exception:
                pass
        return (rate, interval_h, float(next_ts) if next_ts else None)

    # -------------------- ORDER BOOK (REST on demand) --------------------

    async def fetch_orderbook(self, symbol: str, depth: int = 20) -> Optional[OrderBookSnapshot]:
        try:
            ob = await self._client.fetch_order_book(symbol, limit=depth)
        except Exception:
            return None
        # Нормализуем объёмы к базовой монете через contractSize, чтобы
        # price * size = USDT-нотионал одинаково на всех биржах.
        cs = self._contract_size(symbol)
        return OrderBookSnapshot(
            exchange=self.name,
            symbol=symbol,
            ts=time.time(),
            bids=[OrderBookLevel(float(lvl[0]), float(lvl[1]) * cs) for lvl in ob.get("bids", []) if len(lvl) >= 2],
            asks=[OrderBookLevel(float(lvl[0]), float(lvl[1]) * cs) for lvl in ob.get("asks", []) if len(lvl) >= 2],
        )
