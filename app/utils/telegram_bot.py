__all__ = ["TelegramBot"]

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message

from app.config import logger


class TelegramBot:
    """Telegram Bot class based on aiogram"""

    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._token: str | None = None

    async def _get_bot(self, token: str) -> Bot:
        if self._bot and self._token == token:
            return self._bot

        if self._bot:
            await self._bot.session.close()

        self._bot = Bot(
            token=token,
            default=DefaultBotProperties(
                parse_mode="HTML",
                link_preview_is_disabled=True,
            ),
        )
        self._token = token
        return self._bot

    async def send_message(self, bot_token: str, chat_id: int, text: str) -> Message | None:
        try:
            bot = await self._get_bot(bot_token)
            return await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return None

    async def send_photo(
        self,
        bot_token: str,
        chat_id: int,
        photo: bytes | str,
        caption: str | None = None,
    ) -> Message | None:
        try:
            bot = await self._get_bot(bot_token)
            if isinstance(photo, str):
                return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
            else:
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=BufferedInputFile(photo, filename="chart.jpg"),
                    caption=caption,
                )
        except Exception as e:
            logger.error(f"Error sending photo: {e}")
            return None

    async def edit_message_media(
        self,
        bot_token: str,
        chat_id: int,
        message_id: int,
        photo: bytes,
        caption: str | None = None,
    ) -> Message | None:
        try:
            bot = await self._get_bot(bot_token)
            media = InputMediaPhoto(
                media=BufferedInputFile(photo, filename="chart.jpg"),
                caption=caption,
                parse_mode="HTML",
            )
            # edit_message_media returns Message or True (bool) depending on type of edit,
            # but for media replacement it usually returns Message.
            result = await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=media,
            )
            if isinstance(result, Message):
                return result
            return None
        except Exception as e:
            logger.error(f"Error editing message media: {e}")
            return None

    async def close(self) -> None:
        if self._bot:
            await self._bot.session.close()
