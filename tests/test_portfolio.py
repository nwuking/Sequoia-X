"""自选、持仓与操作建议测试。"""

import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.portfolio import PortfolioAdvisor, PortfolioManager, PositionInput
from sequoia_x.strategy.low_price_multi_factor import LowPriceMultiFactorStrategy


def build_settings(tmp_dir: str) -> Settings:
    return Settings(
        db_path=str(Path(tmp_dir) / "portfolio.db"),
        portfolio_csv_path=str(Path(tmp_dir) / "portfolio.csv"),
        feishu_webhook_url="https://example.com/hook",
    )


def build_engine(tmp_dir: str) -> DataEngine:
    engine = DataEngine(build_settings(tmp_dir))
    dates = pd.bdate_range("2026-01-01", periods=80)
    rows = []
    for symbol, start in (
        ("000783", 9.0),
        ("000425", 8.0),
        ("000001", 10.0),
        ("000002", 9.0),
        ("000003", 7.5),
    ):
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
            """
            INSERT INTO financial_factors (
                symbol, report_date, announcement_date, eps, bps, roe, pe_dynamic, pb,
                revenue, net_profit, revenue_yoy, net_profit_yoy,
                operating_cashflow_ps, gross_margin, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                ("000001", "2026-03-31", "2026-04-20", 1.0, 10.0, 20.0, 8.0, 1.0, 100, 20, 30, 25, 1.2, 40, "2026-04-20"),
                ("000002", "2026-03-31", "2026-04-20", 1.0, 9.0, 15.0, 10.0, 1.2, 95, 18, 20, 15, 1.0, 35, "2026-04-20"),
                ("000003", "2026-03-31", "2026-04-20", 1.0, 8.0, 12.0, 12.0, 1.4, 90, 15, 10, 8, 0.9, 30, "2026-04-20"),
            ],
        )
        conn.executemany(
            "INSERT INTO stock_basic(symbol,name,updated_at) VALUES (?,?,?)",
            [
                ("000783", "长江证券", "2026-01-01"),
                ("000425", "徐工机械", "2026-01-01"),
                ("000001", "平安银行", "2026-01-01"),
                ("000002", "万科A", "2026-01-01"),
                ("000003", "国华网安", "2026-01-01"),
            ],
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
        strategy = LowPriceMultiFactorStrategy(engine=engine, settings=build_settings(tmp_dir))
        report = PortfolioAdvisor(engine, strategy=strategy).advise(portfolio)

        holding = portfolio[portfolio["symbol"] == "000783"].iloc[0]
        assert changed is True
        assert holding["shares"] == 6000
        assert holding["return_rate"] == holding["latest_close"] / 9.659 - 1
        assert Path(csv_path).exists()
        assert {item.symbol for item in report.advice} == {"000783", "000425"}
        assert len(report.candidates) <= 10


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


def test_portfolio_replacements_mark_top_three_candidates() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = build_engine(tmp_dir)
        manager = PortfolioManager(
            engine,
            str(Path(tmp_dir) / "portfolio.csv"),
            quote_fetcher=lambda symbol: {
                "000783": (10.0, "2026-04-22"),
                "000425": (9.5, "2026-04-22"),
            }.get(symbol),
        )
        manager.upsert_positions(
            [
                PositionInput("000783", 6000, 10.5, 10.5),
                PositionInput("000425", 3000, 9.8, 9.8),
            ]
        )
        portfolio, _ = manager.refresh()
        strategy = LowPriceMultiFactorStrategy(engine=engine, settings=build_settings(tmp_dir))
        strategy.rank_candidates = lambda limit=10: pd.DataFrame(
            {
                "symbol": ["000001", "000002", "000003"],
                "close": [12.0, 11.0, 10.0],
                "score": [3.0, 2.5, 2.1],
                "rank": [1, 2, 3],
            }
        )
        original_fetch_real_quote = PortfolioManager._fetch_real_quote
        PortfolioManager._fetch_real_quote = staticmethod(
            lambda symbol: {
                "000001": (21.5, "2026-04-22"),
                "000002": (18.8, "2026-04-22"),
                "000003": (16.6, "2026-04-22"),
            }.get(symbol)
        )

        try:
            report = PortfolioAdvisor(engine, strategy=strategy).advise(portfolio)
        finally:
            PortfolioManager._fetch_real_quote = original_fetch_real_quote

    assert len(report.candidates) == 3
    assert all(item.is_focus for item in report.candidates)
    assert len(report.replacements) == 2
    assert report.replacements[0].buy_symbol == "000001"
    assert report.candidates[0].close == 21.5


def test_zero_byte_portfolio_file_is_treated_as_empty(tmp_path) -> None:
    settings = build_settings(str(tmp_path))
    engine = DataEngine(settings)
    Path(settings.portfolio_csv_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.portfolio_csv_path).write_bytes(b"")

    frame = PortfolioManager(engine, settings.portfolio_csv_path).load()

    assert frame.empty
    assert list(frame.columns) == PortfolioManager.columns
