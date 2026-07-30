"""盘中监控测试。"""

import json
import tempfile
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.monitor import IntradayMonitor, IntradayQuote


def test_intraday_monitor_reads_snapshot_and_deduplicates_alerts() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        settings = Settings(
            db_path=str(root / "test.db"),
            portfolio_csv_path=str(root / "portfolio.csv"),
            comprehensive_snapshot_path=str(root / "snapshot.json"),
            intraday_alert_state_path=str(root / "alerts.json"),
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        dates = pd.bdate_range("2026-01-01", periods=30)
        frame = pd.DataFrame(
            {
                "symbol": "000001",
                "date": dates.astype(str),
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 1_000_000.0,
                "turnover": 10_000_000.0,
            }
        )
        import sqlite3

        with sqlite3.connect(engine.db_path) as conn:
            frame.to_sql("incoming", conn, index=False, if_exists="replace")
            conn.execute(
                "INSERT INTO stock_basic(symbol,name,updated_at) VALUES (?,?,?)",
                ("000001", "平安银行", "2026-07-30"),
            )
            conn.execute(
                "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) "
                "SELECT symbol,date,open,high,low,close,volume,turnover FROM incoming"
            )
        pd.DataFrame(
            [{"symbol": "000001", "name": "平安银行", "is_watchlist": True, "shares": 1000,
              "cost_price": 10.0}]
        ).to_csv(settings.portfolio_csv_path, index=False)
        Path(settings.comprehensive_snapshot_path).write_text(
            json.dumps(
                {
                    "data_date": "2026-07-29",
                    "assessments": [
                        {
                            "symbol": "000001", "score": 80, "entry_signal": "A-平台放量突破",
                            "stop_price": 9.5,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        quote = IntradayQuote(
            symbol="000001", price=9.4, previous_close=10.0, open=10.0, high=10.1,
            low=9.3, volume=900_000, amount=8_550_000, quote_time="2026-07-30T10:30:00",
        )
        monitor = IntradayMonitor(engine, settings, quote_fetcher=lambda _: quote)

        first = monitor.run()
        second = monitor.run()

    assert {item.alert_type for item in first} >= {"硬止损", "放量下跌"}
    assert second == []


def test_elapsed_volume_ratio_covers_lunch_and_close() -> None:
    from datetime import datetime

    assert IntradayMonitor._elapsed_volume_ratio(datetime(2026, 7, 30, 11, 30)) == 0.5
    assert IntradayMonitor._elapsed_volume_ratio(datetime(2026, 7, 30, 12, 0)) == 0.5
    assert IntradayMonitor._elapsed_volume_ratio(datetime(2026, 7, 30, 15, 0)) == 1.0
