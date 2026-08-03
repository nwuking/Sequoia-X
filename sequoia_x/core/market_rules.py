"""A 股交易制度与价格限制的统一判断。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


def round_price(value: float) -> float:
    """按 A 股 0.01 元最小报价单位四舍五入。"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def market_board(symbol: str) -> str:
    code = str(symbol).zfill(6)
    if code.startswith(("8", "4", "9")):
        return "北交所"
    if code.startswith("688"):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    return "主板"


def price_limit_ratio(
    symbol: str,
    *,
    is_st: bool = False,
    trading_days_since_listing: int | None = None,
) -> float | None:
    """返回当日涨跌停比例；无限价期返回 ``None``。

    新股上市初期规则只在已提供上市交易日序号时启用。主板前 5 个交易日、
    注册制创业板/科创板前 5 个交易日和北交所上市首日按无限价处理。
    """
    if is_st:
        return 0.05
    board = market_board(symbol)
    if trading_days_since_listing is not None:
        if board in {"主板", "创业板", "科创板"} and trading_days_since_listing <= 5:
            return None
        if board == "北交所" and trading_days_since_listing <= 1:
            return None
    return {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}[board]


@dataclass(frozen=True)
class DailyPriceLimits:
    ratio: float | None
    upper: float | None
    lower: float | None


def daily_price_limits(
    symbol: str,
    previous_close: float,
    *,
    is_st: bool = False,
    trading_days_since_listing: int | None = None,
) -> DailyPriceLimits:
    ratio = price_limit_ratio(
        symbol,
        is_st=is_st,
        trading_days_since_listing=trading_days_since_listing,
    )
    if ratio is None or previous_close <= 0:
        return DailyPriceLimits(ratio, None, None)
    return DailyPriceLimits(
        ratio,
        round_price(previous_close * (1 + ratio)),
        round_price(previous_close * (1 - ratio)),
    )


def cannot_buy_at_open(
    symbol: str,
    previous_close: float,
    open_price: float,
    *,
    is_st: bool = False,
    trading_days_since_listing: int | None = None,
) -> bool:
    limits = daily_price_limits(
        symbol,
        previous_close,
        is_st=is_st,
        trading_days_since_listing=trading_days_since_listing,
    )
    return limits.upper is not None and open_price >= limits.upper - 1e-9


def cannot_sell_at_open(
    symbol: str,
    previous_close: float,
    open_price: float,
    *,
    is_st: bool = False,
    trading_days_since_listing: int | None = None,
) -> bool:
    limits = daily_price_limits(
        symbol,
        previous_close,
        is_st=is_st,
        trading_days_since_listing=trading_days_since_listing,
    )
    return limits.lower is not None and open_price <= limits.lower + 1e-9
