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
from sequoia_x.data.engine import DataEngine


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


def test_sync_today_bulk_stops_when_baostock_login_fails() -> None:
    """登录失败时增量同步不应继续在无效连接上查询。"""
    fake_bs = SimpleNamespace(
        login=MagicMock(return_value=SimpleNamespace(error_code="1", error_msg="blocked")),
        query_history_k_data_plus=MagicMock(),
        logout=MagicMock(),
    )
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
        with patch.dict(sys.modules, {"baostock": fake_bs}), patch("time.sleep"):
            with pytest.raises(RuntimeError, match="登录异常"):
                engine.sync_today_bulk()

    fake_bs.query_history_k_data_plus.assert_not_called()


def test_sync_aborts_when_any_symbol_query_fails() -> None:
    """任一股票拉取失败时应停止策略前的数据同步。"""
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

        fake_bs = SimpleNamespace(
            login=MagicMock(return_value=SimpleNamespace(error_code="0", error_msg="")),
            logout=MagicMock(),
            query_history_k_data_plus=MagicMock(side_effect=RuntimeError("blocked")),
        )
        with patch.dict(sys.modules, {"baostock": fake_bs}), patch("time.sleep"):
            with pytest.raises(RuntimeError, match="000001 拉取异常"):
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


def test_init_db_repairs_incomplete_stock_daily_schema() -> None:
    """旧版本或中断创建的残缺行情表应在首次拉取前自动修复。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE stock_daily (symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL)"
            )
            conn.executemany(
                "INSERT INTO stock_daily (symbol, date, close) VALUES (?, ?, ?)",
                [("000001", "2026-08-03", 10.0), ("000001", "2026-08-03", 10.1)],
            )

        settings = Settings(
            db_path=db_path,
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
        DataEngine(settings)

        with sqlite3.connect(db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(stock_daily)")}
            duplicate_count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol = ? AND date = ?",
                ("000001", "2026-08-03"),
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO stock_daily (symbol, date, close) VALUES (?, ?, ?)",
                    ("000001", "2026-08-03", 11.0),
                )

        assert {
            "symbol", "date", "open", "high", "low", "close", "raw_open",
            "raw_high", "raw_low", "raw_close", "volume", "turnover",
        }.issubset(columns)
        assert duplicate_count == 1


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


def test_write_daily_status_converts_pandas_na_to_sql_null() -> None:
    """尚未计算的涨跌停字段应以 SQL NULL 写入，而不是把 pandas.NA 交给 sqlite。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        frame = pd.DataFrame(
            [{
                "symbol": "600519",
                "date": "2026-08-04",
                "tradestatus": "1",
                "isST": "0",
            }]
        )

        engine._write_daily_status(frame)

        with sqlite3.connect(engine.db_path) as conn:
            row = conn.execute(
                "SELECT limit_ratio, limit_up, limit_down "
                "FROM stock_status_daily WHERE symbol = ? AND date = ?",
                ("600519", "2026-08-04"),
            ).fetchone()

        assert row == (None, None, None)


def test_baostock_daily_quota_blocks_new_requests() -> None:
    """当日本地计数达到阈值时，应阻止新的 baostock 请求。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            baostock_daily_request_limit=2,
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        today = date.today().isoformat()
        engine._increment_api_usage("baostock", 2, today)

        with pytest.raises(RuntimeError, match="请求配额即将超限"):
            engine._ensure_baostock_quota(1)


def test_baostock_daily_quota_increments_after_request() -> None:
    """请求成功发起后，应更新本地 baostock 计数。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        today = date.today().isoformat()

        assert engine._get_api_usage("baostock", today) == 0
        assert engine._increment_api_usage("baostock", 1, today) == 1
        assert engine._increment_api_usage("baostock", 2, today) == 3


def test_backfill_full_history_overwrites_existing_symbol_rows() -> None:
    """full_history 模式应从 start_date 重拉，并覆盖该股票旧历史。"""
    fake_rows = [
        ["2024-01-02", "10", "11", "9", "10.5", "1000", "10500"],
        ["2024-01-03", "10.5", "11.5", "10", "11", "1200", "13200"],
    ]

    class FakeResult:
        error_code = "0"
        error_msg = ""
        fields = ["date", "open", "high", "low", "close", "volume", "amount"]

        def __init__(self, rows):
            self._rows = rows
            self._idx = -1

        def next(self):
            self._idx += 1
            return self._idx < len(self._rows)

        def get_row_data(self):
            return self._rows[self._idx]

    fake_bs = SimpleNamespace(
        login=MagicMock(return_value=SimpleNamespace(error_code="0", error_msg="")),
        logout=MagicMock(),
        query_history_k_data_plus=MagicMock(return_value=FakeResult(fake_rows)),
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        with sqlite3.connect(engine.db_path) as conn:
            conn.execute(
                "INSERT INTO stock_daily "
                "(symbol, date, open, high, low, close, volume, turnover) "
                "VALUES (?, ?, 20, 21, 19, 20.5, 100, 2050)",
                ("000001", "2026-07-01"),
            )

        with patch.dict(sys.modules, {"baostock": fake_bs}), patch("time.sleep"):
            engine.backfill(["000001"], full_history=True)

        with sqlite3.connect(engine.db_path) as conn:
            rows = conn.execute(
                "SELECT date, close FROM stock_daily WHERE symbol = ? ORDER BY date",
                ("000001",),
            ).fetchall()

    assert rows == [("2024-01-02", 10.5), ("2024-01-03", 11.0)]
