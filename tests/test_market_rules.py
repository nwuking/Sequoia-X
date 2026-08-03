"""A 股涨跌停和板块规则测试。"""

from sequoia_x.core.market_rules import (
    cannot_buy_at_open,
    cannot_sell_at_open,
    daily_price_limits,
    market_board,
)


def test_board_specific_price_limits() -> None:
    assert market_board("600000") == "主板"
    assert market_board("300001") == "创业板"
    assert market_board("688001") == "科创板"
    assert market_board("830001") == "北交所"
    assert daily_price_limits("600000", 10).upper == 11.0
    assert daily_price_limits("300001", 10).upper == 12.0
    assert daily_price_limits("688001", 10).lower == 8.0
    assert daily_price_limits("830001", 10).upper == 13.0


def test_st_and_listing_period_rules() -> None:
    assert daily_price_limits("600000", 10, is_st=True).upper == 10.5
    assert daily_price_limits("600000", 10, trading_days_since_listing=3).upper is None
    assert daily_price_limits("830001", 10, trading_days_since_listing=1).upper is None


def test_open_limit_blocks_only_corresponding_side() -> None:
    assert cannot_buy_at_open("300001", 10, 12)
    assert not cannot_buy_at_open("300001", 10, 11)
    assert cannot_sell_at_open("688001", 10, 8)
    assert not cannot_sell_at_open("688001", 10, 9)
