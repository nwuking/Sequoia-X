"""RPS多周期真实突破测试。"""

import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy


def test_rps_uses_previous_120_day_high() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "market.db"),
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        dates = pd.bdate_range("2025-01-01", periods=130)
        rows = []
        for number in range(10):
            symbol = f"0000{number:02d}"
            for index, trading_date in enumerate(dates):
                if number == 0:
                    close = 10 + index * 0.02
                    if index == len(dates) - 1:
                        close += 0.12
                else:
                    close = 10 + number * 0.1
                volume = 2_000_000 if number == 0 and index == len(dates) - 1 else 1_000_000
                rows.append(
                    (
                        symbol,
                        trading_date.date().isoformat(),
                        close * 0.995,
                        close,
                        close * 0.99,
                        close,
                        volume,
                        100_000_000,
                    )
                )
        with sqlite3.connect(engine.db_path) as conn:
            conn.executemany(
                "INSERT INTO stock_daily "
                "(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )

        selected = RpsBreakoutStrategy(engine, settings).run()

    assert "000000" in selected
