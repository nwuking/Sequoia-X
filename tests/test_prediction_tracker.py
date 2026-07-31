"""自动多周期预测跟踪测试。"""

import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.prediction.tracker import PredictionTracker


def test_prediction_tracker_starts_refreshes_evaluates_and_resets(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        settings = Settings(
            db_path=str(root / "market.db"),
            prediction_tracking_db_path=str(root / "tracking.db"),
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        dates = pd.bdate_range("2026-07-01", periods=12).strftime("%Y-%m-%d").tolist()
        with sqlite3.connect(engine.db_path) as conn:
            for index, trade_date in enumerate(dates):
                close = 10.0 + index * 0.1
                conn.execute(
                    "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    ("000001", trade_date, close, close, close, close, 1_000_000, 10_000_000),
                )

        def fake_predict(self, symbols, horizon):
            return (
                [
                    SimpleNamespace(
                        symbol=symbol,
                        direction="上涨",
                        up_probability=0.7,
                        expected_return=0.02,
                    )
                    for symbol in symbols
                ],
                {},
            )

        monkeypatch.setattr(
            "sequoia_x.prediction.tracker.EnsemblePredictor.predict",
            fake_predict,
        )
        tracker = PredictionTracker(engine, settings.prediction_tracking_db_path)
        names = {"000001": "平安银行"}

        initial = tracker.run(["000001"], names, dates[0])
        duplicate = tracker.run(["000001"], names, dates[0])
        day1 = tracker.run(["000001"], names, dates[1])
        day10 = tracker.run(["000001"], names, dates[10])
        restarted = tracker.run(["000001"], names, dates[11])

        with sqlite3.connect(settings.prediction_tracking_db_path) as conn:
            statuses = conn.execute(
                "SELECT status,COUNT(*) FROM prediction_cycles GROUP BY status"
            ).fetchall()

    assert initial.started == ("000001",)
    assert len(initial.predictions) == 5
    assert duplicate.started == ()
    assert duplicate.predictions == ()
    assert [item.horizon for item in day1.evaluations] == [1]
    assert day1.evaluations[0].accurate is True
    assert {item.target_horizon for item in day1.predictions} == {3, 5, 7, 10}
    assert "000001" in day10.completed
    assert {item.horizon for item in day10.evaluations} == {3, 5, 7, 10}
    assert restarted.started == ("000001",)
    assert sorted(statuses) == [("active", 1), ("completed", 1)]
