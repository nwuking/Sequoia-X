"""财务因子同步测试。"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def test_latest_financial_report_date() -> None:
    """报告期推断应匹配 A 股常见完整披露节奏。"""
    assert DataEngine._latest_financial_report_date(as_of=pd.Timestamp("2026-07-29").date()) == "20260331"
    assert DataEngine._latest_financial_report_date(as_of=pd.Timestamp("2026-09-15").date()) == "20260630"
    assert DataEngine._latest_financial_report_date(as_of=pd.Timestamp("2026-12-01").date()) == "20260930"
    assert DataEngine._latest_financial_report_date(as_of=pd.Timestamp("2026-02-01").date()) == "20250930"


def test_sync_financial_factors_persists_rows(monkeypatch) -> None:
    """同步结果应写入 financial_factors 表。"""
    fake_df = pd.DataFrame(
        {
            "股票代码": ["000001", "600000"],
            "最新公告日期": ["2026-04-28", "2026-04-29"],
            "每股收益": [1.2, 0.8],
            "每股净资产": [10.5, 8.6],
            "净资产收益率": [12.0, 9.5],
            "营业总收入-营业总收入": [1000, 900],
            "净利润-净利润": [120, 88],
            "营业总收入-同比增长": [15.0, 10.0],
            "净利润-同比增长": [18.0, 11.0],
            "每股经营现金流量": [0.6, 0.4],
            "销售毛利率": [35.0, 28.0],
        }
    )
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            stock_yjbb_em=lambda date: fake_df.copy(),
            stock_zh_a_spot_em=lambda: pd.DataFrame(
                {
                    "代码": ["000001", "600000"],
                    "市盈率-动态": [8.5, 6.2],
                    "市净率": [0.9, 0.7],
                }
            ),
        ),
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        written = engine.sync_financial_factors(report_date="20260331")
        latest = engine.get_latest_financial_factors(["000001", "600000"])

    assert written == 2
    assert sorted(latest["symbol"].tolist()) == ["000001", "600000"]
    assert set(["roe", "revenue_yoy", "net_profit_yoy", "pe_dynamic", "pb"]).issubset(latest.columns)
