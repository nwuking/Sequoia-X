"""综合趋势策略测试。"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.strategy.comprehensive_trend import ComprehensiveTrendStrategy


def _breakout_frame() -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=160, freq="B")
    closes = [10 + index * 0.025 for index in range(159)]
    closes.append(max(closes) * 1.06)
    volume = [1_000_000.0] * 159 + [2_000_000.0]
    return pd.DataFrame(
        {
            "date": dates.astype(str),
            "open": [value * 0.99 for value in closes],
            "high": [value * 1.01 for value in closes],
            "low": [value * 0.98 for value in closes],
            "close": closes,
            "volume": volume,
            "turnover": [100_000_000.0] * 160,
        }
    )


def test_assess_detects_strong_breakout_and_sets_stop() -> None:
    frame = _breakout_frame()
    indicators = ComprehensiveTrendStrategy._indicators(frame)
    flags = ComprehensiveTrendStrategy.entry_flags(indicators, True)
    result = ComprehensiveTrendStrategy.assess(
        "000001",
        frame,
        {"score": 20.0, "ret20": 0.01, "strong": True, "breadth": 0.7},
    )

    assert result is not None
    assert bool(flags.iloc[-1]["entry_a"])
    assert result.regime == "主升浪"
    assert result.entry_signal == "A-平台放量突破"
    assert result.score >= 75
    assert 0 < result.stop_price < result.close
    assert result.position_risk_pct > 0


def test_assess_labels_clear_downtrend() -> None:
    dates = pd.date_range("2025-01-01", periods=160, freq="B")
    closes = [30 - index * 0.1 for index in range(160)]
    frame = pd.DataFrame(
        {
            "date": dates.astype(str),
            "open": [value * 1.01 for value in closes],
            "high": [value * 1.02 for value in closes],
            "low": [value * 0.98 for value in closes],
            "close": closes,
            "volume": [1_000_000.0] * 160,
            "turnover": [50_000_000.0] * 160,
        }
    )
    result = ComprehensiveTrendStrategy.assess(
        "000002",
        frame,
        {"score": 0.0, "ret20": -0.03, "strong": False, "breadth": 0.2},
    )

    assert result is not None
    assert result.regime in {"阴跌趋势", "下跌反弹"}
    assert result.entry_signal == "等待确认"
    assert result.score < 45


def test_empty_data_invalidates_old_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps({"data_date": "old", "assessments": [{"symbol": "600519"}]}),
        encoding="utf-8",
    )
    settings = Settings(
        db_path=str(tmp_path / "empty.db"),
        comprehensive_snapshot_path=str(snapshot),
        feishu_webhook_url="https://example.com/hook",
    )
    engine = MagicMock()
    engine.thresholds.integer.side_effect = lambda section, key: 120 if key == "min_history" else 300
    engine.get_local_symbols.return_value = []
    engine.get_stock_names.return_value = {}
    engine.get_latest_date.return_value = None

    result = ComprehensiveTrendStrategy(engine, settings).run()
    payload = json.loads(snapshot.read_text(encoding="utf-8"))

    assert result == []
    assert payload["valid"] is False
    assert payload["data_date"] is None
    assert payload["assessments"] == []
