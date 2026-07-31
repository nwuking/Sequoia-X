"""策略引擎属性测试。"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy


class _FixedResultStrategy(BaseStrategy):
    """用于验证基类统一过滤规则的固定结果策略。"""

    def _run(self) -> list[str]:
        return ["000001", "000002", "000003", "000004", "000005"]


def test_all_strategies_exclude_st_stocks() -> None:
    """所有策略的公开 run() 都应统一剔除各类 ST 股票。"""
    engine = DataEngine.__new__(DataEngine)
    engine.get_stock_names = lambda symbols: {
        "000001": "平安银行",
        "000002": "ST示例",
        "000003": "*ST风险",
        "000004": "s*st退市",
        # 000005 暂无基础资料，不应误删。
    }
    strategy = _FixedResultStrategy(engine=engine, settings=None)  # type: ignore[arg-type]

    assert strategy.run() == ["000001", "000005"]


# Feature: sequoia-x-v2, Property 9: 策略 run() 返回值类型正确
@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=0, max_size=3, unique=True,
    )
)
@h_settings(max_examples=30, deadline=None)
def test_strategy_run_returns_list_of_str(symbols: list[str]) -> None:
    """属性 9：run() 应返回 list[str]，每个元素为非空字符串。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)

        with patch.object(engine, "get_all_symbols", return_value=symbols):
            with patch.object(engine, "get_ohlcv", return_value=pd.DataFrame()):
                strategy = MaVolumeStrategy(engine=engine, settings=settings)
                result = strategy.run()

    assert isinstance(result, list)
    assert all(isinstance(s, str) and len(s) > 0 for s in result)
