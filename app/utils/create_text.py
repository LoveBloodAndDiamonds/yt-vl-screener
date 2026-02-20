__all__ = ["create_text"]


from unicex import Exchange, MarketType
from unicex.extra import generate_ex_link, make_humanreadable


def create_text(
    symbol: str,
    multiplier: float,
    exchange: Exchange,
    market_type: MarketType,
    daily_price: float,
    daily_volume: float,
    signal_count: int,
) -> str:
    """Формирует красивый текст сигнала о резком изменении объема. Готовый текст для отправки пользователю."""
    # Ссылка на биржу для быстрого перехода к инструменту
    ex_link = generate_ex_link(exchange, market_type, symbol)

    direction_emoji = "🚀" if multiplier >= 1 else "🔻"

    signal_emoji = "🔔 " if signal_count == 1 else ""

    # Основной заголовок сигнала
    header = f"{signal_emoji} {direction_emoji} Резкий рост объема: {symbol}"

    # Читаемая часть с цифрами
    body = (
        f"📊 Текущий объем выше среднего в {multiplier:.2f}x\n"
        f"📈 Изменение цены за день: {daily_price:.2f}%\n"
        f"🔈 Объем за день: {make_humanreadable(daily_volume, locale='ru')} $\n"
        f"🛎️ Сигналов за день: {signal_count}"
    )

    # Призыв к действию и ссылка
    footer = f"🔗 {ex_link}"

    return f"{header}\n\n{body}\n\n{footer}"
