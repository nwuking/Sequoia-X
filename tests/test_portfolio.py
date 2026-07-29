"""自选、持仓与操作建议测试。"""

import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.portfolio import PortfolioAdvisor, PortfolioManager, PositionInput


def build_engine(tmp_dir: str) -> DataEngine:
    engine = DataEngine(
        Settings(
            db_path=str(Path(tmp_dir) / "portfolio.db"),
            portfolio_csv_path=str(Path(tmp_dir) / "portfolio.csv"),
            feishu_webhook_url="https://example.com/hook",
        )
    )
    dates = pd.bdate_range("2026-01-01", periods=80)
    rows = []
    for symbol, start in (("000783", 9.0), ("000425", 8.0)):
        close = np.linspace(start, start + 1.5, len(dates))
        for day, price in zip(dates, close, strict=True):
            rows.append(
                (
                    symbol, day.strftime("%Y-%m-%d"), price * 0.99, price * 1.01,
                    price * 0.98, price, 2_000_000, price * 2_000_000,
                )
            )
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO stock_basic(symbol,name,updated_at) VALUES (?,?,?)",
            [("000783", "长江证券", "2026-01-01"), ("000425", "徐工机械", "2026-01-01")],
        )
    return engine


def test_portfolio_refresh_and_advice() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = build_engine(tmp_dir)
        csv_path = str(Path(tmp_dir) / "portfolio.csv")
        quotes = {
            "000783": (10.0, "2026-04-22"),
            "000425": (9.5, "2026-04-22"),
        }
        manager = PortfolioManager(engine, csv_path, quote_fetcher=quotes.get)
        manager.set_watchlist(["长江证券", "徐工机械"])
        manager.upsert_positions(
            [PositionInput("000783", 6000, 9.659, 9.522)]
        )

        portfolio, changed = manager.refresh()
        advice = PortfolioAdvisor(engine).advise(portfolio)

        holding = portfolio[portfolio["symbol"] == "000783"].iloc[0]
        assert changed is True
        assert holding["shares"] == 6000
        assert holding["return_rate"] == holding["latest_close"] / 9.659 - 1
        assert Path(csv_path).exists()
        assert {item.symbol for item in advice} == {"000783", "000425"}


def test_sell_position_tracks_total_and_history_then_clears_holding() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = build_engine(tmp_dir)
        manager = PortfolioManager(
            engine,
            str(Path(tmp_dir) / "portfolio.csv"),
            quote_fetcher=lambda _symbol: (12.0, "2026-04-22"),
        )
        manager.upsert_positions([PositionInput("000783", 1000, 10.0, 10.0)])
        manager.sell_positions([manager.parse_sale("长江证券:400:11")])
        portfolio, _ = manager.refresh()
        partial = portfolio.iloc[0]
        assert partial["shares"] == 600
        assert partial["realized_pnl"] == 400
        assert partial["total_pnl"] == 1600
        assert partial["total_return_rate"] == 0.16

        manager.sell_positions([manager.parse_sale("000783:600:9")])
        cleared = manager.load().iloc[0]
        assert cleared["shares"] == 0
        assert bool(cleared["is_watchlist"]) is True
        assert pd.isna(cleared["cost_price"])
        assert cleared["realized_pnl"] == -200
        assert cleared["historical_return_rate"] == -0.02
        assert cleared["total_pnl"] == -200
        assert cleared["total_return_rate"] == -0.02


def test_sell_position_rejects_more_than_holding() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = build_engine(tmp_dir)
        manager = PortfolioManager(engine, str(Path(tmp_dir) / "portfolio.csv"))
        manager.upsert_positions([PositionInput("000783", 100, 10.0, 10.0)])
        with np.testing.assert_raises_regex(ValueError, "超过持仓"):
            manager.sell_positions([manager.parse_sale("000783:101:11")])
