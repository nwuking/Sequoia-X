"""盘中模拟交易测试。"""

import tempfile
import sqlite3
from pathlib import Path

from sequoia_x.monitor import IntradayAlert
from sequoia_x.simulation import PaperTradingManager


def alert(
    alert_type: str,
    price: float,
    quote_time: str,
    message: str = "测试信号",
    symbol: str = "000001",
    candidate_tier: str = "B",
    priority_score: float = 70,
    stop_price: float = 0,
    atr: float = 0,
    strategy_family: str = "趋势动量",
    industry: str = "银行",
    market_exposure_limit: float = 0.8,
):
    return IntradayAlert(
        symbol=symbol,
        name="平安银行",
        level="中",
        alert_type=alert_type,
        price=price,
        message=message,
        quote_time=quote_time,
        candidate_tier=candidate_tier,
        priority_score=priority_score,
        stop_price=stop_price,
        atr=atr,
        strategy_family=strategy_family,
        industry=industry,
        market_exposure_limit=market_exposure_limit,
    )


def test_paper_trading_build_add_reduce_and_stop() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = PaperTradingManager(str(Path(tmp_dir) / "paper.db"))
        manager.sync_universe(
            {"000001": {"自选", "策略"}},
            {"000001": "平安银行"},
            {"000001": 10.0},
        )

        built = manager.apply_alerts(
            [alert("盘中突破候选", 10.0, "2026-07-30T10:30:00")]
        )
        duplicate_buy = manager.apply_alerts(
            [alert("尾盘买点确认", 11.0, "2026-07-30T14:50:00")]
        )
        added = manager.apply_alerts(
            [alert("尾盘买点确认", 11.0, "2026-07-31T14:50:00")]
        )
        reduced = manager.apply_alerts(
            [alert("放量下跌", 9.0, "2026-08-03T10:30:00")]
        )
        stopped = manager.apply_alerts(
            [alert("硬止损", 8.0, "2026-08-04T10:30:00")]
        )
        account = manager.accounts()[0]
        portfolio = manager.portfolio()

    assert built[0].action == "建仓"
    assert built[0].shares == 400
    assert duplicate_buy == []
    assert added[0].action == "增持"
    assert added[0].shares == 200
    assert reduced[0].action == "减持"
    assert reduced[0].shares == 300
    assert stopped[0].action == "清仓"
    assert account.shares == 0
    assert portfolio.cash < 100_000
    assert portfolio.position_count == 0


def test_paper_trading_uses_shared_capital_and_deduplicates_daily_buy() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = PaperTradingManager(str(Path(tmp_dir) / "paper.db"))
        manager.sync_universe(
            {"000001": {"持仓"}, "000002": {"策略"}},
            {"000001": "平安银行", "000002": "万科A"},
            {"000001": 10.0, "000002": 20.0},
        )
        first = manager.apply_alerts(
            [alert("盘中突破候选", 10.0, "2026-07-30T10:30:00")]
        )
        duplicate = manager.apply_alerts(
            [alert("盘中突破候选", 10.2, "2026-07-30T11:20:00")]
        )
        accounts = {item.symbol: item for item in manager.accounts()}

    assert len(first) == 1
    assert duplicate == []
    assert accounts["000001"].cash == accounts["000002"].cash
    assert accounts["000001"].cash < 100_000
    assert accounts["000002"].sources == "策略"


def test_paper_trading_does_not_build_from_tier_c_watchlist_signal() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = PaperTradingManager(str(Path(tmp_dir) / "paper.db"))
        manager.sync_universe(
            {"000001": {"自选"}}, {"000001": "平安银行"}, {"000001": 12.0}
        )
        trades = manager.apply_alerts(
            [alert("盘中走强", 12.0, "2026-07-30T10:30:00", candidate_tier="C")]
        )

    assert trades == []


def test_paper_trading_prioritizes_higher_score_when_position_limit_is_one() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        config = Path(tmp_dir) / "thresholds.ini"
        content = Path("config/thresholds.ini").read_text()
        config.write_text(content.replace("max_positions = 10", "max_positions = 1"))
        from sequoia_x.core.thresholds import ThresholdConfig

        manager = PaperTradingManager(
            str(Path(tmp_dir) / "paper.db"), thresholds=ThresholdConfig(str(config))
        )
        manager.sync_universe(
            {"000001": {"重点"}, "000002": {"重点"}},
            {"000001": "平安银行", "000002": "万科A"},
            {"000001": 10.0, "000002": 10.0},
        )
        trades = manager.apply_alerts(
            [
                alert("盘中突破候选", 10.0, "2026-07-30T10:30:00", priority_score=70),
                alert(
                    "盘中突破候选", 10.0, "2026-07-30T10:30:00",
                    symbol="000002", priority_score=90,
                ),
            ]
        )

    assert [item.symbol for item in trades] == ["000002"]


def test_paper_trading_migrates_legacy_independent_accounts() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "paper.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE paper_accounts (
                    symbol TEXT PRIMARY KEY, name TEXT NOT NULL, sources TEXT NOT NULL,
                    initial_capital REAL NOT NULL, cash REAL NOT NULL,
                    shares INTEGER NOT NULL, average_cost REAL NOT NULL,
                    latest_price REAL NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                    name TEXT NOT NULL, action TEXT NOT NULL, shares INTEGER NOT NULL,
                    price REAL NOT NULL, amount REAL NOT NULL, reason TEXT NOT NULL,
                    alert_type TEXT NOT NULL, traded_at TEXT NOT NULL,
                    UNIQUE(symbol, alert_type, traded_at)
                );
                """
            )
            conn.execute(
                "INSERT INTO paper_accounts VALUES(?,?,?,?,?,?,?,?,?)",
                ("000001", "平安银行", "策略", 100_000, 95_000, 500, 10, 10, "2026-07-30"),
            )

        manager = PaperTradingManager(str(db_path), initial_capital=100_000)
        portfolio = manager.portfolio()

    assert portfolio.cash == 95_000
    assert portfolio.market_value == 5_000
    assert portfolio.total_assets == 100_000


def test_paper_trading_t1_only_blocks_same_day_lot() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = PaperTradingManager(str(Path(tmp_dir) / "paper.db"))
        manager.sync_universe(
            {"000001": {"重点"}}, {"000001": "平安银行"}, {"000001": 10.0}
        )
        first = manager.apply_alerts(
            [alert("盘中突破候选", 10.0, "2026-07-30T10:30:00")]
        )[0]
        added = manager.apply_alerts(
            [alert("尾盘买点确认", 10.0, "2026-07-31T10:30:00")]
        )[0]
        stopped = manager.apply_alerts(
            [alert("硬止损", 9.0, "2026-07-31T14:30:00")]
        )[0]
        account = manager.accounts()[0]

    assert stopped.shares == first.shares
    assert account.shares == added.shares


def test_paper_trading_stores_risk_plan_and_nav_attribution() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = PaperTradingManager(str(Path(tmp_dir) / "paper.db"))
        manager.sync_universe(
            {"000001": {"重点"}}, {"000001": "平安银行"}, {"000001": 10.0}
        )
        manager.apply_alerts(
            [
                alert(
                    "盘中突破候选",
                    10.0,
                    "2026-07-30T10:30:00",
                    stop_price=9.4,
                    atr=0.3,
                )
            ]
        )
        manager.snapshot_nav("2026-07-30")
        state = manager.position_states()["000001"]
        nav = manager.nav_history()[0]

    assert abs(state["initial_stop_price"] - 9.41) < 1e-9
    assert state["strategy_family"] == "趋势动量"
    assert state["industry"] == "银行"
    assert nav["position_count"] == 1
    assert "drawdown" in nav
