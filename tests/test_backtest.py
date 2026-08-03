"""事件驱动回测、滚动样本外验证与归因测试。"""

import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

from sequoia_x.backtest import (
    BacktestConfig,
    EventDrivenBacktester,
    WalkForwardValidator,
    write_backtest_report,
)
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def build_market(root: Path, periods: int = 360) -> str:
    settings = Settings(
        db_path=str(root / "market.db"),
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    dates = pd.bdate_range("2024-01-02", periods=periods)
    records = []
    for symbol, offset in (("000001", 0.0), ("000002", 2.0)):
        for index, trading_date in enumerate(dates):
            trend = 10 + offset + index * 0.035
            cycle = (index % 30) * 0.01
            close = trend + cycle
            if index > periods - 20:
                close -= (index - (periods - 20)) * 0.25
            records.append(
                (
                    symbol,
                    trading_date.date().isoformat(),
                    close * 0.998,
                    close * 1.01,
                    close * 0.99,
                    close,
                    2_000_000 + index * 1000,
                    150_000_000,
                )
            )
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) "
            "VALUES(?,?,?,?,?,?,?,?)",
            records,
        )
        conn.executemany(
            "INSERT INTO stock_basic(symbol,name,updated_at) VALUES(?,?,?)",
            [("000001", "测试一", "2024-01-01"), ("000002", "测试二", "2024-01-01")],
        )
    return engine.db_path


def test_backtest_executes_after_signal_and_writes_attribution() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        db_path = build_market(root)
        tester = EventDrivenBacktester(
            db_path,
            BacktestConfig(min_signal_count=1, max_holding_days=10),
        )
        result = tester.run("2024-07-01", "2025-05-01")
        paths = write_backtest_report(result, str(root / "report"))

        buys = result.trades[result.trades["action"] == "买入"]
        reports_exist = all(Path(path).exists() for path in paths.values())

    assert not result.nav.empty
    assert not buys.empty
    assert (pd.to_datetime(buys["trade_date"]) > pd.to_datetime(buys["signal_date"])).all()
    assert "max_drawdown" in result.metrics
    assert set(paths) == {"summary", "nav", "trades", "attribution", "report"}
    assert reports_exist


def test_walk_forward_only_combines_test_windows() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = build_market(Path(tmp_dir), periods=390)
        tester = EventDrivenBacktester(db_path, BacktestConfig(max_holding_days=10))
        result = WalkForwardValidator(tester).run(
            "2024-01-02",
            "2025-06-30",
            train_days=126,
            test_days=42,
        )

    assert len(result.windows) >= 1
    assert not result.out_of_sample_nav.empty
    assert set(result.windows["min_signal_count"]).issubset({1, 2})
    assert "total_return" in result.metrics
