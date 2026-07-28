"""集成涨跌预测模型测试。"""

import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.prediction import EnsemblePredictor


def make_engine(tmp_dir: str) -> DataEngine:
    return DataEngine(
        Settings(
            db_path=str(Path(tmp_dir) / "prediction.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
    )


def synthetic_market(symbol_count: int = 12, days: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2025-01-01", periods=days)
    frames = []
    for index in range(symbol_count):
        returns = rng.normal(0.0004 + index * 0.00005, 0.018, size=days)
        close = 20 * np.cumprod(1 + returns)
        open_price = close * (1 + rng.normal(0, 0.004, size=days))
        high = np.maximum(open_price, close) * (1 + rng.uniform(0, 0.015, size=days))
        low = np.minimum(open_price, close) * (1 - rng.uniform(0, 0.015, size=days))
        volume = rng.integers(1_000_000, 8_000_000, size=days)
        frames.append(
            pd.DataFrame(
                {
                    "symbol": f"{index + 1:06d}",
                    "date": dates.strftime("%Y-%m-%d"),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "turnover": volume * close,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def test_prediction_for_specified_symbols() -> None:
    """模型应为指定代码输出概率，并报告时间外验证指标。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = make_engine(tmp_dir)
        data = synthetic_market()
        with sqlite3.connect(engine.db_path) as conn:
            data.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")

        predictor = EnsemblePredictor(engine, max_train_rows=10_000)
        results, metrics = predictor.predict(["000001", "000002"], horizon=5)

    assert {result.symbol for result in results} == {"000001", "000002"}
    assert all(0 <= result.up_probability <= 1 for result in results)
    assert 0 <= metrics["roc_auc"] <= 1
    assert metrics["validation_rows"] > 0
