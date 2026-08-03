"""行业归属和量化龙头同步测试。"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def test_sync_stock_industries_marks_market_cap_leaders() -> None:
    boards = pd.DataFrame({"板块名称": ["银行", "食品饮料"]})
    constituents = {
        "银行": pd.DataFrame(
            {
                "代码": ["000001", "600000", "601398"],
                "名称": ["平安银行", "浦发银行", "工商银行"],
                "总市值": [200.0, 300.0, 1000.0],
            }
        ),
        "食品饮料": pd.DataFrame(
            {
                "代码": ["600519", "000858"],
                "名称": ["贵州茅台", "五粮液"],
                "总市值": [2000.0, 800.0],
            }
        ),
    }
    fake_ak = SimpleNamespace(
        stock_board_industry_name_em=lambda: boards,
        stock_board_industry_cons_em=lambda symbol: constituents[symbol],
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        settings = Settings(
            db_path=str(root / "market.db"),
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        output = root / "stock_industry.csv"
        with patch.dict(sys.modules, {"akshare": fake_ak}), patch("time.sleep"):
            result = engine.sync_stock_industries(str(output), leaders_per_industry=1)
        saved = pd.read_csv(output, dtype={"symbol": str})

    leaders = set(result.loc[result["is_industry_leader"], "symbol"])
    assert leaders == {"601398", "600519"}
    assert not saved.empty
    assert {
        "symbol",
        "industry",
        "industry_rank",
        "is_industry_leader",
        "leader_basis",
    }.issubset(saved.columns)
    assert set(saved["leader_basis"]) == {"总市值"}


def test_sync_stock_industries_falls_back_to_turnover() -> None:
    fake_ak = SimpleNamespace(
        stock_board_industry_name_em=lambda: pd.DataFrame({"板块名称": ["软件开发"]}),
        stock_board_industry_cons_em=lambda symbol: pd.DataFrame(
            {
                "代码": ["000001", "000002"],
                "名称": ["甲", "乙"],
                "成交额": [10.0, 20.0],
            }
        ),
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "market.db"),
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        with patch.dict(sys.modules, {"akshare": fake_ak}), patch("time.sleep"):
            result = engine.sync_stock_industries(
                str(Path(tmp_dir) / "industry.csv"), leaders_per_industry=1
            )

    leader = result.loc[result["is_industry_leader"]].iloc[0]
    assert leader["symbol"] == "000002"
    assert leader["leader_basis"] == "成交额"
