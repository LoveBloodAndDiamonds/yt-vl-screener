__all__ = ["Consumer"]

import asyncio
import time

from unicex import Exchange, KlineDict, MarketType
from unicex.extra import TimeoutTracker
from unicex.types import TickerDailyItem

from app.config import config, logger
from app.models import SettingsDTO
from app.utils import TelegramBot, create_text

from .producer import Producer


class Consumer:
    """Обработчик данных для скринера."""

    _PARSE_INTERVAL: int = 1
    """Интервал проверки данных."""

    def __init__(
        self,
        producer: Producer,
        settings: SettingsDTO,
        exchange: Exchange = config.exchange,
        market_type: MarketType = config.market_type,
    ) -> None:
        self._producer = producer
        self._settings = settings
        self._exchange = exchange
        self._market_type = market_type
        self._telegram_bot = TelegramBot()
        self._timeout_tracker = TimeoutTracker[str]()
        self._running = True

    def update_settings(self, settings: SettingsDTO) -> None:
        """Обновляет настройки скринера."""
        self._settings = settings

    async def start(self) -> None:
        """Запускает обработку данных."""
        logger.info("Starting consumer...")
        while self._running:
            try:
                if not self._settings.is_ready:
                    continue
                await self._process()
            except Exception as e:
                logger.error(f"Error processing data: {e}")
            finally:
                await asyncio.sleep(self._PARSE_INTERVAL)

    async def stop(self) -> None:
        """Останавливает обработку данных."""
        logger.info("Stopping consumer...")
        self._running = False
        await self._telegram_bot.close()

    async def _process(self) -> None:
        """Обрабатывает данные."""
        all_klines = await self._producer.fetch_collected_data()
        all_ticker_daily = await self._producer.fetch_ticker_daily()

        tasks = []
        for symbol, klines in all_klines.items():
            if self._timeout_tracker.is_blocked(symbol):
                continue

            ticker_daily = all_ticker_daily.get(symbol)
            if not ticker_daily:
                logger.warning(f"Ticker daily data not found for symbol {symbol}")
                continue

            task = await self._process_symbol(symbol, klines, ticker_daily)
            if task:
                self._timeout_tracker.block(symbol, self._settings.timeout)
                tasks.append(task)

        if tasks:
            await asyncio.gather(*tasks)
            logger.info(f"Sended {len(tasks)} signals!")

    async def _process_symbol(
        self,
        symbol: str,
        klines: list[KlineDict],
        ticker_daily: TickerDailyItem,
    ) -> asyncio.Task | None:
        """Обрабатывает данные по тикеру."""
        _, multiplier = self._calculate_volume_multiplier(klines, ticker_daily["q"])

        if multiplier > self._settings.min_multiplier:
            logger.success(f"{symbol}: {multiplier}x, {ticker_daily}")
            return asyncio.create_task(
                self._telegram_bot.send_message(
                    bot_token=self._settings.bot_token,  # type: ignore
                    chat_id=self._settings.chat_id,  # type: ignore
                    text=create_text(
                        symbol,
                        multiplier,
                        self._exchange,
                        self._market_type,
                        ticker_daily["p"],
                        ticker_daily["q"],
                    ),
                )
            )

    def _calculate_volume_multiplier(
        self,
        klines: list[KlineDict],
        daily_volume: float,
    ) -> tuple[float, float]:
        """Вычисляет множитель объема относительно среднего объема за тот же интервал за сутки по формуле:
        Объем_за_интервал / Интервал / Объем за сутуки / 86_400(Секунд в сутках)

        Returns:
            float: Объем за интервал, float: Множитель объема
        """
        if not klines:
            return 0, 0

        threshold = (time.time() - self._settings.interval) * 1000
        valid_klines = [k for k in klines if k["t"] > threshold]

        if not valid_klines:
            return 0, 0

        vol_interval = sum(k["v"] for k in valid_klines)
        try:
            vol_per_sec_interval = vol_interval / self._settings.interval
            vol_per_sec_daily = daily_volume / 86400
            multiplier = vol_per_sec_interval / vol_per_sec_daily
        except ZeroDivisionError:
            multiplier = 0

        return vol_interval, multiplier
