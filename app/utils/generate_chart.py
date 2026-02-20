__all__ = ["generate_chart"]

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from io import BytesIO

import matplotlib
import mplfinance as mpf
import pandas as pd
from matplotlib.ticker import FuncFormatter
from unicex import KlineDict

matplotlib.use("Agg")


def _to_decimal(value: float | str | Decimal) -> Decimal:
    """Преобразует значение в Decimal без типичных float-артефактов."""
    if isinstance(value, Decimal):
        return value

    if isinstance(value, float):
        # 15 значащих цифр обычно достаточно, чтобы убрать хвост вида ...0000000002
        # и сохранить человекочитаемую цену от matplotlib.
        return Decimal(format(value, ".15g"))

    return Decimal(str(value))


def _cleanup_decimal_noise(value: Decimal, significant_digits: int) -> Decimal:
    """Сглаживает хвостовые шумы в дробной части после работы с float.

    Args:
        value: Исходное число в формате Decimal.
        significant_digits: Число значащих цифр, которое мы точно сохраняем.

    Returns:
        Decimal без паразитного хвоста из нулей с последней случайной цифрой.
    """
    if value.is_zero():
        return value

    sign = -1 if value < 0 else 1
    abs_value = abs(value)
    plain = format(abs_value, "f")
    try:
        _, frac_part = plain.split(".", 1)
    except ValueError:
        frac_part = ""
    frac_part = frac_part.rstrip("0")

    # Короткую дробную часть не трогаем.
    if len(frac_part) <= 20:
        return value

    first_non_zero = len(frac_part) - len(frac_part.lstrip("0"))
    keep_decimals = max(first_non_zero + significant_digits + 4, significant_digits + 4)

    with localcontext() as ctx:
        ctx.prec = 80
        quant = Decimal(f"1e-{keep_decimals}")
        cleaned = abs_value.quantize(quant, rounding=ROUND_HALF_UP)

    return cleaned if sign > 0 else -cleaned


def _format_price(value: float | str | Decimal, significant_digits: int = 2) -> str:
    """Форматирует цену в компактный вид для очень маленьких значений.

    Args:
        value: Исходное числовое значение цены.
        significant_digits: Количество значащих цифр после серии нулей.

    Returns:
        Строка цены. Для чисел с большим числом нулей после запятой
        возвращает формат вида ``0.0(N)X``.
    """
    if significant_digits < 1:
        raise ValueError("significant_digits must be >= 1")

    try:
        dec_value = _to_decimal(value)
    except (InvalidOperation, ValueError):
        return str(value)

    if dec_value.is_zero():
        return "0"

    # Убираем артефакты длинной дробной части после конвертации из float:
    # например 0.00240000000000000000001 -> 0.0024.
    dec_value = _cleanup_decimal_noise(dec_value, significant_digits=significant_digits)

    sign = "-" if dec_value < 0 else ""
    dec_value = abs(dec_value)

    plain = format(dec_value, "f")
    try:
        int_part, frac_part = plain.split(".", 1)
    except ValueError:
        int_part, frac_part = plain, ""
    frac_part = frac_part.rstrip("0")

    if not frac_part:
        return f"{sign}{int_part}"

    leading_zeros = len(frac_part) - len(frac_part.lstrip("0"))
    if leading_zeros < 3:
        return f"{sign}{int_part}.{frac_part}"

    with localcontext() as ctx:
        ctx.prec = 60
        decimal_places = leading_zeros + significant_digits
        quant = Decimal(f"1e-{decimal_places}")
        rounded = dec_value.quantize(quant, rounding=ROUND_HALF_UP)

    rounded_plain = format(rounded, "f")
    try:
        _, rounded_frac = rounded_plain.split(".", 1)
    except ValueError:
        rounded_frac = ""
    rounded_frac = rounded_frac.rstrip("0")

    rounded_zeros = len(rounded_frac) - len(rounded_frac.lstrip("0"))
    visible_zeros = max(rounded_zeros - 1, 0)

    significant = rounded_frac[rounded_zeros : rounded_zeros + significant_digits].rstrip("0")
    if not significant:
        significant = "0"

    return f"{sign}0.0({visible_zeros}){significant}"


def generate_chart(
    klines: Sequence[KlineDict],
    symbol: str,
    start_price: float,
    final_price: float,
    price_change_pct: float,
) -> BytesIO:
    """Строит свечной график с лучом от close последней свечи."""
    if not klines:
        raise ValueError("Klines are empty")

    data = {
        "Date": [datetime.fromtimestamp(k["t"] / 1000 + 10_800) for k in klines],
        "Open": [k["o"] for k in klines],
        "High": [k["h"] for k in klines],
        "Low": [k["l"] for k in klines],
        "Close": [k["c"] for k in klines],
        "Volume": [k["v"] for k in klines],
    }
    df = pd.DataFrame(data)
    df.set_index("Date", inplace=True)
    style = mpf.make_mpf_style(
        base_mpf_style="yahoo",
        facecolor="#282D38",
        gridstyle="",
        marketcolors=mpf.make_marketcolors(up="#0C967F", down="#F23645", inherit=True),
        figcolor="#282D38",
        rc={
            "xtick.labelcolor": "white",
            "ytick.labelcolor": "white",
            "xtick.labelsize": 20,
            "ytick.labelsize": 20,
        },
    )

    bio = BytesIO()
    fig, axes = mpf.plot(
        df,
        type="candle",
        figsize=(30, 15),
        volume=True,
        style=style,
        ylabel="",
        ylabel_lower="",
        returnfig=True,
        mav=(20,),
    )

    price_axis = axes[0]
    price_axis.yaxis.set_major_formatter(FuncFormatter(lambda val, _: _format_price(float(val))))
    price_axis.set_title(
        f"{symbol} | {start_price}$ → {final_price}$ | {price_change_pct:.2f}%",
        fontsize=20,
        color="#FFFFFF",
        pad=18,
    )

    fig.savefig(bio, bbox_inches="tight", pad_inches=0.3, dpi=100)
    fig.clf()

    bio.seek(0)
    return bio
