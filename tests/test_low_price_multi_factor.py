"""低价多因子策略测试。"""

import tempfile
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.low_price_multi_factor import LowPriceMultiFactorStrategy


def test_low_price_strategy_disables_standalone_push() -> None:
    """低价多因子结果已并入组合报告，不应再发送独立消息。"""
    assert LowPriceMultiFactorStrategy.standalone_push_enabled is False


def _build_ohlcv(close_values: list[float], turnover_values: list[float]) -> pd.DataFrame:
    base = pd.date_range("2024-01-01", periods=len(close_values), freq="B")
    return pd.DataFrame(
        {
            "date": base.astype(str),
            "open": close_values,
            "high": [value * 1.02 for value in close_values],
            "low": [value * 0.98 for value in close_values],
            "close": close_values,
            "volume": [1_000_000.0] * len(close_values),
            "turnover": turnover_values,
        }
    )


def test_low_price_multi_factor_returns_at_most_three_symbols(monkeypatch) -> None:
    """策略最多返回 3 只股票，且保留低价约束。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        strategy = LowPriceMultiFactorStrategy(engine=engine, settings=settings)

        symbols = ["000001", "000002", "000003", "000004", "000005"]
        close_map = {
            "000001": [10 + i * 0.05 for i in range(300)],
            "000002": [12 + i * 0.04 for i in range(300)],
            "000003": [8 + i * 0.03 for i in range(300)],
            "000004": [15 + i * 0.02 for i in range(300)],
            "000005": [31 + i * 0.01 for i in range(300)],
        }
        turnover_map = {
            "000001": [120_000_000.0] * 300,
            "000002": [100_000_000.0] * 300,
            "000003": [90_000_000.0] * 300,
            "000004": [85_000_000.0] * 300,
            "000005": [150_000_000.0] * 300,
        }

        monkeypatch.setattr(engine, "get_local_symbols", lambda: symbols)
        monkeypatch.setattr(
            engine,
            "get_ohlcv",
            lambda symbol: _build_ohlcv(close_map[symbol], turnover_map[symbol]),
        )
        monkeypatch.setattr(engine, "get_latest_financial_factors", lambda _: pd.DataFrame())

        result = strategy.run()
        combination_ranking = strategy.last_combination_ranking

    assert len(result) <= 3
    assert "000005" not in result
    assert set(result).issubset(set(symbols))
    assert not combination_ranking.empty
    assert list(combination_ranking.columns) == [
        "symbol",
        "factor_rank",
        "combination_factor_score",
    ]
    assert combination_ranking["factor_rank"].tolist() == list(
        range(1, len(combination_ranking) + 1)
    )


def test_low_price_multi_factor_uses_financial_scores_when_available(monkeypatch) -> None:
    """财务因子存在时，策略应能利用估值和质量信息参与排序。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)
        strategy = LowPriceMultiFactorStrategy(engine=engine, settings=settings)

        symbols = ["000001", "000002", "000003"]
        close_map = {
            "000001": [10 + i * 0.04 for i in range(300)],
            "000002": [10 + i * 0.04 for i in range(300)],
            "000003": [10 + i * 0.04 for i in range(300)],
        }
        turnover_map = {symbol: [100_000_000.0] * 300 for symbol in symbols}

        monkeypatch.setattr(engine, "get_local_symbols", lambda: symbols)
        monkeypatch.setattr(
            engine,
            "get_ohlcv",
            lambda symbol: _build_ohlcv(close_map[symbol], turnover_map[symbol]),
        )
        monkeypatch.setattr(
            engine,
            "get_latest_financial_factors",
            lambda _: pd.DataFrame(
                {
                    "symbol": ["000001", "000002", "000003"],
                    "roe": [20.0, 10.0, 5.0],
                    "revenue_yoy": [30.0, 10.0, 0.0],
                    "net_profit_yoy": [25.0, 8.0, -5.0],
                    "gross_margin": [40.0, 25.0, 20.0],
                    "operating_cashflow_ps": [1.5, 0.9, 0.4],
                    "eps": [1.0, 1.0, 1.0],
                    "pe_dynamic": [8.0, 12.0, 20.0],
                    "pb": [1.0, 1.5, 2.5],
                }
            ),
        )

        result = strategy.run()

    assert result[0] == "000001"
