"""数据引擎属性测试。"""

import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine, _bs_fetch_batch


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    return engine, settings


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with sqlite3.connect(engine.db_path) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        assert count == 1


def test_worker_stops_when_baostock_login_fails() -> None:
    """登录失败时 worker 不应继续在无效 socket 上批量查询。"""
    fake_bs = SimpleNamespace(
        login=MagicMock(return_value=SimpleNamespace(error_code="1", error_msg="blocked")),
        query_history_k_data_plus=MagicMock(),
    )
    with patch.dict(sys.modules, {"baostock": fake_bs}), patch("time.sleep"):
        success, rows = _bs_fetch_batch(
            [("000001", "sz.000001", "2026-07-28", "2026-07-28")]
        )

    assert success is False
    assert rows == []
    fake_bs.query_history_k_data_plus.assert_not_called()


def test_sync_aborts_when_any_worker_batch_fails() -> None:
    """任一批次失败时应停止策略前的数据同步，避免使用陈旧数据。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        yesterday = str(date.today() - timedelta(days=1))
        with sqlite3.connect(engine.db_path) as conn:
            conn.execute(
                "INSERT INTO stock_daily "
                "(symbol, date, open, high, low, close, volume, turnover) "
                "VALUES (?, ?, 10, 11, 9, 10.5, 1000, 10500)",
                ("000001", yesterday),
            )

        pool = MagicMock()
        pool.__enter__.return_value.map.return_value = [(False, [])]
        with patch("multiprocessing.Pool", return_value=pool):
            with pytest.raises(RuntimeError, match="停止策略执行"):
                engine.sync_today_bulk()


def test_get_latest_date() -> None:
    """应返回本地行情库中所有股票的最大交易日期。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        with sqlite3.connect(engine.db_path) as conn:
            conn.executemany(
                "INSERT INTO stock_daily "
                "(symbol, date, open, high, low, close, volume, turnover) "
                "VALUES (?, ?, 10, 11, 9, 10.5, 1000, 10500)",
                [("000001", "2026-07-25"), ("600000", "2026-07-27")],
            )

        assert engine.get_latest_date() == "2026-07-27"


def test_get_stock_names_from_local_database() -> None:
    """股票中文名应直接从本地基础信息表读取。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        with sqlite3.connect(engine.db_path) as conn:
            conn.executemany(
                "INSERT INTO stock_basic (symbol, name, updated_at) VALUES (?, ?, ?)",
                [
                    ("600519", "贵州茅台", "2026-07-28"),
                    ("000001", "平安银行", "2026-07-28"),
                ],
            )

        assert engine.get_stock_names(["600519", "000001", "999999"]) == {
            "600519": "贵州茅台",
            "000001": "平安银行",
        }
