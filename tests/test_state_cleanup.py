"""策略持久状态清理测试。"""

import json
import sqlite3
from pathlib import Path

from sequoia_x.core.config import Settings
from sequoia_x.core.state_cleanup import cleanup_strategy_state
from sequoia_x.data.engine import DataEngine
from sequoia_x.prediction.tracker import PredictionTracker


def test_cleanup_archives_and_resets_all_strategy_state(tmp_path: Path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "sequoia.db"),
        comprehensive_snapshot_path=str(tmp_path / "snapshot.json"),
        strategy_selection_path=str(tmp_path / "selection.json"),
        combined_streak_path=str(tmp_path / "streak.json"),
        intraday_alert_state_path=str(tmp_path / "alerts.json"),
        low_price_rebalance_state_path=str(tmp_path / "rebalance.json"),
        prediction_tracking_db_path=str(tmp_path / "tracking.db"),
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    json_paths = [
        settings.comprehensive_snapshot_path,
        settings.strategy_selection_path,
        settings.combined_streak_path,
        settings.intraday_alert_state_path,
        settings.low_price_rebalance_state_path,
    ]
    for path in json_paths:
        Path(path).write_text(json.dumps({"old": True}), encoding="utf-8")

    PredictionTracker(engine, settings.prediction_tracking_db_path)
    with sqlite3.connect(settings.prediction_tracking_db_path) as conn:
        conn.execute(
            "INSERT INTO prediction_cycles(symbol,start_date,start_close,status) "
            "VALUES ('600519','2026-08-01',100,'active')"
        )

    result = cleanup_strategy_state(engine, settings)

    assert result.archive_dir.exists()
    assert {path.name for path in result.archive_dir.iterdir()} == {
        "snapshot.json", "selection.json", "streak.json", "alerts.json",
        "rebalance.json", "tracking.db",
    }
    with sqlite3.connect(result.archive_dir / "tracking.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_cycles").fetchone()[0] == 1
    snapshot = json.loads(Path(settings.comprehensive_snapshot_path).read_text(encoding="utf-8"))
    selection = json.loads(Path(settings.strategy_selection_path).read_text(encoding="utf-8"))
    streak = json.loads(Path(settings.combined_streak_path).read_text(encoding="utf-8"))
    rebalance = json.loads(Path(settings.low_price_rebalance_state_path).read_text(encoding="utf-8"))
    assert snapshot["valid"] is False
    assert snapshot["assessments"] == []
    assert selection["strategies"] == {}
    assert selection["combined"]["focus"] == []
    assert streak == {"data_date": None, "symbols": [], "counts": {}}
    assert rebalance == {"month": None, "data_date": None, "symbols": []}
    with sqlite3.connect(settings.prediction_tracking_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_cycles").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM prediction_forecasts").fetchone()[0] == 0
