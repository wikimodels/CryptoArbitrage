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
        self.max_ws_symbols = 100        # подписок на один вызов watch (kucoin <=100)
        self.ws_max_total = 380          # лимит подписок на СЕССИЮ (kucoin <=400);
                                         # больше символов -> вся биржа на REST
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
        """Долгоживущий цикл сбора цен. Авто-выбор транспорта:
        watch_tickers (если проходит проба) иначе bulk REST fetch_tickers.
        Если символов больше лимита подписок сессии — топ по объёму идёт
        на WS, хвост поллится одним bulk-REST раз в 10с (1 запрос)."""
        if not symbols:
            return
        use_ws = self._supports_watch_tickers and await self._probe_ws(symbols[:10])

        if use_ws and len(symbols) > self.ws_max_total:
            # Приоритет по ликвидности: 24h quoteVolume с одного bulk-снимка
            try:
                tick = await self._client.fetch_tickers(symbols)
                vol = {s: float(t.get("quoteVolume") or 0.0)
                       for s, t in tick.items() if isinstance(t, dict)}
                ranked = sorted(symbols, key=lambda s: vol.get(s, 0.0), reverse=True)
            except Exception as e:
                log.info("[%s] volume snapshot failed (%s) — алфавитный порядок", self.name, str(e)[:50])
                ranked = list(symbols)
            ws_syms = ranked[:self.ws_max_total]
            tail = ranked[self.ws_max_total:]
            log.info("[%s] WS: топ-%d по объёму + REST-хвост %d (10s)",
                     self.name, len(ws_syms), len(tail))
            await asyncio.gather(
                self._ws_all(ws_syms, on_price),
                self._tail_rest(tail, on_price),
            )
            return

        if not use_ws:
            log.info("[%s] транспорт цен: REST fetch_tickers (%d символов)", self.name, len(symbols))
            await self._rest_batch(symbols, on_price)
            return

        log.info("[%s] транспорт цен: watch_tickers (%d символов)", self.name, len(symbols))
        await self._ws_all(symbols, on_price)

    async def _ws_all(self, symbols: list[str], on_price: PriceCallback):
        chunk = max(1, self.max_ws_symbols)
        batches = [symbols[i:i + chunk] for i in range(0, len(symbols), chunk)]
        await asyncio.gather(*(self._ws_batch(b, on_price) for b in batches))

    async def _tail_rest(self, tail: list[str], on_price: PriceCallback):
        """Хвост вне лимита подписок: один bulk fetch_tickers раз в 10с."""
        while not self._closed:
            try:
                data = await self._client.fetch_tickers(tail)
                for sym, t in data.items():
                    self._emit(sym, t, on_price)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("[%s] tail REST: %s", self.name, str(e)[:50])
            await asyncio.sleep(10.0)

    async def _probe_ws(self, sample: list[str]) -> bool:
        try:
            await asyncio.wait_for(self._client.watch_tickers(sample), timeout=8)
            return True
        except Exception as e:
            log.info("[%s] watch_tickers недоступен (%s) -> REST", self.name, str(e)[:60])
            return False

    async def _ws_batch(self, batch: list[str], on_price: PriceCallback):
        backoff = 1.0
        fails = 0
        while not self._closed:
            try:
                data = await asyncio.wait_for(
                    self._client.watch_tickers(batch), timeout=30)
                for sym, t in data.items():
                    self._emit(sym, t, on_price)
                backoff = 1.0
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closed:
                    return
                fails += 1
                if fails >= 3:
                    log.warning("[%s] watch_tickers падает (%s) -> REST для чанка", self.name, str(e)[:50])
                    await self._rest_batch(batch, on_price)
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _rest_batch(self, batch: list[str], on_price: PriceCallback):
        fails = 0
        while not self._closed:
            try:
                data = await self._client.fetch_tickers(batch)
                fails = 0
                for sym, t in data.items():
                    self._emit(sym, t, on_price)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fails += 1
                if fails in (1, 5, 15):  # видимые варнинги, не спам
                    log.warning("[%s] REST tickers fail x%d: %s", self.name, fails, str(e)[:60])
            await asyncio.sleep(1.0)

    def _emit(self, sym: str, t, on_price: PriceCallback):
        if not isinstance(t, dict):
            return  # некоторые биржи (coinex) шлют иной payload
        bid = t.get("bid")
        ask = t.get("ask")
        if (bid is None or ask is None):
            last = t.get("last")
            if last:
                # binance/coinex bulk-tickers отдают только last — используем
                # как bid≈ask (экран); реальный стакан проверяется при входе
                bid = ask = float(last)
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
