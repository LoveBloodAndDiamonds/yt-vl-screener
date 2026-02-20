__all__ = ["Producer"]

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from unicex import (
    Exchange,
    IUniClient,
    KlineDict,
    MarketType,
    TickerDailyDict,
    TradeDict,
    Websocket,
    get_uni_client,
    get_uni_websocket_manager,
)

from app.config import config, logger


class Producer:
    """Продюсер данных для скринера."""

    _MAX_HISTORY_LEN = 60 * 15
    """Максимальная длина истории в секундах для всех парсеров."""

    WS_CHUNK_SIZE = {
        Exchange.BINGX: 30,
    }
    """Количество тикеров в одном вебсокет соединении."""

    DEFAULT_WS_CHUNK_SIZE = 20
    """Стандартное количество тикеров в одном вебсокет соединении"""

    TIMEFRAME = 3
    """Таймфрейм для аггрегации свечей из сделок в секундах"""

    TICKERS_CHECK_INTERVAL = 600
    """Интервал проверки новых тикеров в секундах"""

    TICKER_DAILY_UPDATE_INTERVAL = 5
    """Интервал обновления суточной статистики тикеров в секундах"""

    def __init__(
        self,
        exchange: Exchange = config.exchange,
        market_type: MarketType = config.market_type,
    ) -> None:
        """Инициализирует парсера данных.

        Args:
            exchange (Exchange): На какой бирже парсить данные.
            market_type (MarketType): Тип рынка с которого парсить данные.
        """
        self._exchange = exchange
        self._market_type = market_type
        self._is_running = True

        self._websockets: list[Websocket] = []
        self._klines_lock = asyncio.Lock()
        self._ticker_daily_lock = asyncio.Lock()
        self._klines: dict[str, list[KlineDict]] = defaultdict(list)
        self._tickers: set[str] = set()
        self._ticker_daily: TickerDailyDict = {}
        self._tickers_monitor_task: asyncio.Task | None = None
        self._ticker_daily_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Запускает парсер данных."""
        logger.info(f"{self.repr} started")
        self._is_running = True
        try:
            async with self._client_context() as client:
                tickers_batched = await self._fetch_tickers_list_batched(client)
            self._tickers = {symbol for batch in tickers_batched for symbol in batch}
            self._websockets = self._create_websockets(tickers_batched)
            tasks = await self._start_websockets()
            self._tickers_monitor_task = asyncio.create_task(self._monitor_new_tickers())
            self._ticker_daily_task = asyncio.create_task(self._update_ticker_daily())
            await asyncio.gather(*tasks, self._tickers_monitor_task)
        except TimeoutError:
            logger.error(f"{self.repr} timeout error occurred")
        except Exception as e:
            logger.exception(f"{self.repr} error: {e}")

    async def stop(self) -> None:
        """Останавливает парсер данных."""
        logger.info(f"{self.repr} stopped")
        gather_results = await asyncio.gather(
            *[ws.stop() for ws in self._websockets], return_exceptions=True
        )
        for result in gather_results:
            if isinstance(result, Exception):
                logger.error(f"{self.repr} error while stopping websocket: {result}")
        if self._tickers_monitor_task:
            self._tickers_monitor_task.cancel()
            try:
                await self._tickers_monitor_task
            except asyncio.CancelledError:
                pass
        self._is_running = False

    async def _update_ticker_daily(self) -> None:
        """В цикле обновляет суточную статистику тикеров."""
        while self._is_running:
            try:
                async with self._client_context() as client:
                    match self._market_type:
                        case MarketType.SPOT:
                            self._ticker_daily = await client.ticker_24hr()
                        case MarketType.FUTURES:
                            self._ticker_daily = await client.futures_ticker_24hr()
                        case _:
                            raise ValueError(f"Unsupported market type: {self._market_type}")
            except Exception as e:
                logger.error(f"{self.repr} error while updating ticker daily: {e}")
            await asyncio.sleep(self.TICKER_DAILY_UPDATE_INTERVAL)

    async def fetch_collected_data(self) -> dict[str, list[KlineDict]]:
        """Возвращает накопленные данные. Возвращает ссылку на объект в котором хранятся данные."""
        async with self._klines_lock:
            return self._klines

    async def fetch_ticker_daily(self) -> TickerDailyDict:
        """Возвращает накопленные данные. Возвращает ссылку на объект в котором хранятся данные."""
        async with self._ticker_daily_lock:
            return self._ticker_daily

    async def _fetch_tickers_list_batched(self, client: IUniClient) -> list[list[str]]:
        """Возвращает список тикеров в батчах."""
        chunk_size = self.WS_CHUNK_SIZE.get(self._exchange, self.DEFAULT_WS_CHUNK_SIZE)
        match self._market_type:
            case MarketType.SPOT:
                return await client.tickers_batched(batch_size=chunk_size)
            case MarketType.FUTURES:
                return await client.futures_tickers_batched(batch_size=chunk_size)
            case _:
                raise ValueError(f"Unsupported market type: {self._market_type}")

    async def _monitor_new_tickers(self) -> None:
        """Периодически проверяет новые тикеры и запускает для них отдельные вебсокеты."""
        while self._is_running:
            await asyncio.sleep(self.TICKERS_CHECK_INTERVAL)
            try:
                async with self._client_context() as client:
                    tickers_batched = await self._fetch_tickers_list_batched(client)

                # Проверяем только новые тикеры, чтобы не перегружать парсер.
                current_symbols = {symbol for batch in tickers_batched for symbol in batch}
                new_symbols = current_symbols - self._tickers
                if not new_symbols:
                    continue
                logger.info(f"{self.repr} new symbols detected: {new_symbols}")
                self._tickers.update(new_symbols)

                websockets = self._create_websockets(
                    [list(new_symbols)]
                )  # Удобнее все таки переиспользовать функцию, чем писать новую
                for websocket in websockets:
                    self._websockets.append(websocket)
                    asyncio.create_task(websocket.start())

            except Exception as e:
                logger.exception(f"{self.repr} error while checking new tickers: {e}")

    def _create_websockets(self, tickers_batched: list[list[str]]) -> list[Websocket]:
        """Генерирует вебсокеты исходя из списка тикеров разбитых на чанки."""
        manager = get_uni_websocket_manager(self._exchange)(logger=logger)
        match self._market_type:
            case MarketType.SPOT:
                factory = manager.aggtrades
            case MarketType.FUTURES:
                factory = manager.futures_aggtrades
            case _:
                raise ValueError(f"Unsupported market type: {self._market_type}")
        return [
            factory(
                callback=self._aggtrades_callback,
                symbols=batch,
            )
            for batch in tickers_batched
        ]

    async def _start_websockets(self) -> list[asyncio.Task]:
        """Запускает вебсокеты."""
        tasks = []
        for websocket in self._websockets:
            if not websocket.running:
                tasks.append(websocket.start())
                logger.debug(f"{self.repr} {websocket} started")
                await asyncio.sleep(0.5)
        return tasks

    async def _aggtrades_callback(self, aggtrade: TradeDict) -> None:
        """Обработчик агрегированных сделок."""
        async with self._klines_lock:
            self._process_trade(aggtrade)

    def _process_trade(self, aggtrade: TradeDict) -> None:
        """Агрегирует сделки в свечи и очищает историю."""
        symbol = aggtrade["s"]
        trade_time = int(float(aggtrade["t"]))
        timeframe_ms = self.TIMEFRAME * 1000
        aligned_open_time = (trade_time // timeframe_ms) * timeframe_ms
        trade_price = float(aggtrade["p"])
        trade_volume = float(aggtrade["v"])

        klines = self._klines[symbol]
        if not klines:
            klines.append(
                self._create_new_kline(symbol, aligned_open_time, trade_price, trade_volume)
            )
            return

        kline = klines[-1]
        expected_close_time = kline["t"] + timeframe_ms
        if trade_time >= expected_close_time:
            kline["T"], kline["x"] = expected_close_time, True
            klines.append(
                self._create_new_kline(symbol, aligned_open_time, trade_price, trade_volume)
            )
        else:
            kline["h"] = max(kline["h"], trade_price)
            kline["l"] = min(kline["l"], trade_price)
            kline["c"] = trade_price
            kline["v"] += trade_volume
            kline["q"] += trade_volume * trade_price

        min_open_time = aligned_open_time - self._MAX_HISTORY_LEN * 1000
        self._klines[symbol] = [k for k in klines if k["t"] >= min_open_time]

    def _create_new_kline(
        self, symbol: str, open_time: int, price: float, volume: float
    ) -> KlineDict:
        """Создаёт новую свечу из сделки."""
        return KlineDict(
            s=symbol,
            t=open_time,
            o=price,
            h=price,
            l=price,
            c=price,
            v=volume,
            q=volume * price,
            T=None,
            x=False,
        )

    # Утилиты

    async def _safe_sleep(self, seconds: int) -> None:
        """Безопасное ожидание, которое может прерваться в процессе."""
        for _ in range(seconds):
            if not self._is_running:
                return
            await asyncio.sleep(1)

    @asynccontextmanager
    async def _client_context(self, **kwargs: Any) -> AsyncIterator[IUniClient]:
        """Создаёт клиента unicex и гарантированно закрывает соединение по завершении контекста.

        Args:
            **kwargs (Any): Любые параметры, которые нужно передать в UniClient.create (например, logger).

        Yields:
            AsyncIterator[Any]: Инициализированный и готовый к работе клиент.
        """
        client = await get_uni_client(self._exchange).create(**kwargs)
        async with client:
            yield client

    @property
    def repr(self) -> str:
        """Возвращает строковое представление парсера."""
        return f"{self.__class__.__name__} ({self._exchange.capitalize()})"
