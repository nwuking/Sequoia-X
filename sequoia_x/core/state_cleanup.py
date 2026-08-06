"""策略选股状态的归档与安全重置。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sequoia_x.core.config import Settings
from sequoia_x.prediction.tracker import PredictionTracker


@dataclass(frozen=True)
class CleanupResult:
    archive_dir: Path
    reset_paths: tuple[Path, ...]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _archive(path: Path, archive_dir: Path) -> None:
    if not path.exists():
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / path.name
    counter = 1
    while target.exists():
        target = archive_dir / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        with sqlite3.connect(path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
    else:
        shutil.copy2(path, target)


def cleanup_strategy_state(engine, settings: Settings) -> CleanupResult:
    """归档并重置所有由选股、组合决策和盘中去重产生的持久状态。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    state_paths = [
        Path(settings.comprehensive_snapshot_path),
        Path(settings.strategy_selection_path),
        Path(settings.combined_streak_path),
        Path(settings.intraday_alert_state_path),
        Path(settings.low_price_rebalance_state_path)
        if settings.low_price_rebalance_state_path
        else Path(engine.db_path).with_name("low_price_rebalance_state.json"),
        Path(settings.prediction_tracking_db_path),
    ]
    archive_root = Path(engine.db_path).parent / "state_archive" / timestamp
    for path in state_paths:
        _archive(path, archive_root)

    reset_at = datetime.now().isoformat(timespec="seconds")
    payloads = {
        state_paths[0]: {
            "generated_at": reset_at,
            "data_date": None,
            "valid": False,
            "reason": "策略状态已手动清理，请重新运行日常策略生成快照",
            "market": {"score": 0.0, "ret20": 0.0, "strong": False, "breadth": 0.0},
            "assessments": [],
        },
        state_paths[1]: {
            "data_date": None,
            "strategies": {},
            "combined": {
                "all_candidates": [],
                "multi_strategy": [],
                "multi_family": [],
                "trend_confirmed": [],
                "focus": [],
                "details": [],
            },
        },
        state_paths[2]: {"data_date": None, "symbols": [], "counts": {}},
        state_paths[3]: {"date": None, "keys": []},
        state_paths[4]: {"month": None, "data_date": None, "symbols": []},
    }
    for path, payload in payloads.items():
        _write_json(path, payload)

    tracking_path = state_paths[5]
    PredictionTracker(engine, str(tracking_path))
    with sqlite3.connect(tracking_path) as conn:
        conn.execute("DELETE FROM prediction_forecasts")
        conn.execute("DELETE FROM prediction_cycles")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('prediction_forecasts','prediction_cycles')"
        )
        conn.commit()
        conn.execute("VACUUM")

    return CleanupResult(archive_dir=archive_root, reset_paths=tuple(state_paths))
