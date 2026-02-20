__all__ = ["Consumer"]

import asyncio
import time

import aiohttp
from unicex import Exchange, KlineDict, MarketType, Timeframe, get_uni_client
from unicex.extra import SignalCounter, TimeoutTracker
from unicex.types import TickerDailyItem

from app.config import config, logger
from app.models import SettingsDTO
from app.utils import TelegramBot, create_text, generate_chart

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
        self._signal_counter = SignalCounter[str]()
        self._uni_client = get_uni_client(self._exchange)(session=aiohttp.ClientSession())
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
        await self._uni_client.close_connection()
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
            signal_count = self._signal_counter.add(symbol)

            text = create_text(
                symbol,
                multiplier,
                self._exchange,
                self._market_type,
                ticker_daily["p"],
                ticker_daily["q"],
                signal_count,
            )

            # Отправляем текстовое сообщение
            message = await self._telegram_bot.send_message(
                bot_token=self._settings.bot_token,  # type: ignore
                chat_id=self._settings.chat_id,  # type: ignore
                text=text,
            )

            if message:
                # Запускаем задачу на генерацию графика и обновление сообщения
                return asyncio.create_task(
                    self._update_message_with_chart(
                        self._settings.bot_token,  # type: ignore
                        self._settings.chat_id,  # type: ignore
                        message.message_id,
                        text,
                        klines,
                        symbol,
                    )
                )
            else:
                logger.error(f"Failed to send message for {symbol}")
        return None

    async def _update_message_with_chart(
        self,
        bot_token: str,
        chat_id: int,
        message_id: int,
        text: str,
        klines: list[KlineDict],
        symbol: str,
    ) -> None:
        """Генерирует график и обновляет сообщение."""
        try:
            # Копируем свечи, чтобы избежать race condition, так как Producer может менять их
            klines_copy = [k.copy() for k in klines]

            if not klines_copy:
                return

            start_price = klines_copy[0]["o"]
            final_price = klines_copy[-1]["c"]
            price_change_pct = (final_price - start_price) / start_price * 100

            # Получаем свечи для постройки графика
            if self._market_type == MarketType.FUTURES:
                klines = await self._uni_client.futures_klines(
                    symbol=symbol, interval=Timeframe.MIN_5, limit=500
                )
            else:
                klines = await self._uni_client.klines(
                    symbol=symbol, interval=Timeframe.MIN_5, limit=500
                )

            # Генерируем график в отдельном потоке
            chart_bio = await asyncio.to_thread(
                generate_chart,
                klines,
                symbol,
                start_price,
                final_price,
                price_change_pct,
            )

            await self._telegram_bot.edit_message_media(
                bot_token=bot_token,
                chat_id=chat_id,
                message_id=message_id,
                photo=chart_bio.getvalue(),
                caption=text,
            )
            logger.info(f"Added chart to message {message_id} for {symbol}")

        except Exception as e:
            logger.error(f"Error generating/sending chart for {symbol}: {e}")

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
        if not klines or daily_volume <= 0:
            return 0.0, 0.0

        interval = self._settings.interval
        if interval <= 0:
            return 0.0, 0.0

        threshold = (time.time() - interval) * 1000

        vol_interval = 0.0
        has_data = False

        for k in klines:
            if k["t"] <= threshold:
                continue

            vol_interval += k["v"]
            has_data = True

        if not has_data:
            return 0.0, 0.0

        vol_per_sec_interval = vol_interval / interval
        vol_per_sec_daily = daily_volume / 86_400

        return vol_interval, vol_per_sec_interval / vol_per_sec_daily
