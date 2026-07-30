"""盘中模拟交易测试。"""

import tempfile
from pathlib import Path

from sequoia_x.monitor import IntradayAlert
from sequoia_x.simulation import PaperTradingManager


def alert(alert_type: str, price: float, quote_time: str, message: str = "测试信号"):
    return IntradayAlert(
        symbol="000001",
        name="平安银行",
        level="中",
        alert_type=alert_type,
        price=price,
        message=message,
        quote_time=quote_time,
    )


def test_paper_trading_build_add_reduce_and_stop() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = PaperTradingManager(str(Path(tmp_dir) / "paper.db"))
        manager.sync_universe(
            {"000001": {"自选", "策略"}},
            {"000001": "平安银行"},
            {"000001": 10.0},
        )

        built = manager.apply_alerts(
            [alert("盘中突破候选", 10.0, "2026-07-30T10:30:00")]
        )
        added = manager.apply_alerts(
            [alert("尾盘买点确认", 11.0, "2026-07-30T14:50:00")]
        )
        reduced = manager.apply_alerts(
            [alert("放量下跌", 9.0, "2026-07-31T10:30:00")]
        )
        stopped = manager.apply_alerts(
            [alert("硬止损", 8.0, "2026-08-03T10:30:00")]
        )
        account = manager.accounts()[0]

    assert built[0].action == "建仓"
    assert built[0].shares == 3000
    assert added[0].action == "增持"
    assert added[0].shares == 1800
    assert reduced[0].action == "减持"
    assert reduced[0].shares == 2400
    assert stopped[0].action == "清仓"
    assert account.shares == 0
    assert account.cash == 91_000
    assert account.total_pnl == -9_000


def test_paper_trading_uses_independent_capital_and_deduplicates_daily_signal() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = PaperTradingManager(str(Path(tmp_dir) / "paper.db"))
        manager.sync_universe(
            {"000001": {"持仓"}, "000002": {"策略"}},
            {"000001": "平安银行", "000002": "万科A"},
            {"000001": 10.0, "000002": 20.0},
        )
        first = manager.apply_alerts(
            [alert("盘中突破候选", 10.0, "2026-07-30T10:30:00")]
        )
        duplicate = manager.apply_alerts(
            [alert("盘中突破候选", 10.2, "2026-07-30T11:20:00")]
        )
        accounts = {item.symbol: item for item in manager.accounts()}

    assert len(first) == 1
    assert duplicate == []
    assert accounts["000001"].cash == 70_000
    assert accounts["000002"].cash == 100_000
    assert accounts["000002"].sources == "策略"


def test_paper_trading_can_build_from_watchlist_strength_signal() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = PaperTradingManager(str(Path(tmp_dir) / "paper.db"))
        manager.sync_universe(
            {"000001": {"自选"}}, {"000001": "平安银行"}, {"000001": 12.0}
        )
        trades = manager.apply_alerts(
            [alert("盘中走强", 12.0, "2026-07-30T10:30:00")]
        )

    assert trades[0].action == "建仓"
    assert trades[0].shares == 2500
