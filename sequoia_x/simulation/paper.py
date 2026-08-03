"""使用统一资金池、按股票记录持仓的本地模拟交易组合。"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sequoia_x.core.logger import get_logger
from sequoia_x.core.thresholds import ThresholdConfig
from sequoia_x.monitor import IntradayAlert

logger = get_logger(__name__)


@dataclass(frozen=True)
class PaperTrade:
    symbol: str
    name: str
    action: str
    shares: int
    price: float
    amount: float
    reason: str
    traded_at: str
    fee: float = 0.0
    alert_type: str = ""
    candidate_tier: str = ""
    priority_score: float = 0.0
    strategy_family: str = "未知"
    industry: str = "未知"
    realized_pnl: float = 0.0
    shares_after: int = 0
    average_cost_after: float = 0.0
    stop_price: float = 0.0


@dataclass(frozen=True)
class PaperAccount:
    symbol: str
    name: str
    sources: str
    initial_capital: float
    cash: float
    shares: int
    average_cost: float
    latest_price: float
    market_value: float
    total_assets: float
    total_pnl: float
    return_rate: float
    entry_date: str = ""
    initial_stop_price: float = 0.0
    highest_price: float = 0.0
    candidate_tier: str = ""
    strategy_family: str = "未知"
    industry: str = "未知"


@dataclass(frozen=True)
class PaperPortfolio:
    initial_capital: float
    cash: float
    market_value: float
    total_assets: float
    total_pnl: float
    return_rate: float
    position_count: int
    exposure_rate: float


class PaperTradingManager:
    """共享一个组合资金池，按候选等级和信号优先级执行模拟交易。"""

    BUY_ALERTS = {"盘中突破候选", "尾盘买点确认", "盘中走强"}
    EXIT_ALERTS = {
        "硬止损",
        "持仓亏损",
        "放量下跌",
        "移动止盈",
        "趋势减仓",
        "趋势清仓",
        "时间止损",
    }

    def __init__(
        self,
        db_path: str,
        initial_capital: float = 100_000.0,
        thresholds: ThresholdConfig | None = None,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("模拟初始本金必须大于 0")
        self.db_path = Path(db_path)
        self.initial_capital = float(initial_capital)
        self.thresholds = thresholds or ThresholdConfig("config/thresholds.ini")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sources TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    cash REAL NOT NULL,
                    shares INTEGER NOT NULL DEFAULT 0,
                    average_cost REAL NOT NULL DEFAULT 0,
                    latest_price REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    last_buy_date TEXT NOT NULL DEFAULT '',
                    entry_date TEXT NOT NULL DEFAULT '',
                    initial_stop_price REAL NOT NULL DEFAULT 0,
                    highest_price REAL NOT NULL DEFAULT 0,
                    atr REAL NOT NULL DEFAULT 0,
                    candidate_tier TEXT NOT NULL DEFAULT '',
                    strategy_family TEXT NOT NULL DEFAULT '未知',
                    industry TEXT NOT NULL DEFAULT '未知',
                    buy_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS paper_portfolio (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    initial_capital REAL NOT NULL,
                    cash REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    price REAL NOT NULL,
                    amount REAL NOT NULL,
                    reason TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    traded_at TEXT NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    candidate_tier TEXT NOT NULL DEFAULT '',
                    priority_score REAL NOT NULL DEFAULT 0,
                    strategy_family TEXT NOT NULL DEFAULT '未知',
                    industry TEXT NOT NULL DEFAULT '未知',
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    UNIQUE(symbol, alert_type, traded_at)
                );
                CREATE TABLE IF NOT EXISTS paper_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    buy_date TEXT NOT NULL,
                    shares_remaining INTEGER NOT NULL,
                    unit_cost REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_lots_symbol_date
                ON paper_lots(symbol,buy_date,id);
                CREATE TABLE IF NOT EXISTS paper_nav_history (
                    trading_date TEXT PRIMARY KEY,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    total_assets REAL NOT NULL,
                    daily_return REAL NOT NULL,
                    cumulative_return REAL NOT NULL,
                    drawdown REAL NOT NULL DEFAULT 0,
                    exposure_rate REAL NOT NULL,
                    position_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol_time
                ON paper_trades(symbol, traded_at);
                """
            )
            account_columns = self._columns(conn, "paper_accounts")
            account_defaults = {
                "last_buy_date": "TEXT NOT NULL DEFAULT ''",
                "entry_date": "TEXT NOT NULL DEFAULT ''",
                "initial_stop_price": "REAL NOT NULL DEFAULT 0",
                "highest_price": "REAL NOT NULL DEFAULT 0",
                "atr": "REAL NOT NULL DEFAULT 0",
                "candidate_tier": "TEXT NOT NULL DEFAULT ''",
                "strategy_family": "TEXT NOT NULL DEFAULT '未知'",
                "industry": "TEXT NOT NULL DEFAULT '未知'",
                "buy_reason": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in account_defaults.items():
                if column not in account_columns:
                    conn.execute(f"ALTER TABLE paper_accounts ADD COLUMN {column} {definition}")
            trade_columns = self._columns(conn, "paper_trades")
            trade_defaults = {
                "fee": "REAL NOT NULL DEFAULT 0",
                "candidate_tier": "TEXT NOT NULL DEFAULT ''",
                "priority_score": "REAL NOT NULL DEFAULT 0",
                "strategy_family": "TEXT NOT NULL DEFAULT '未知'",
                "industry": "TEXT NOT NULL DEFAULT '未知'",
                "realized_pnl": "REAL NOT NULL DEFAULT 0",
            }
            for column, definition in trade_defaults.items():
                if column not in trade_columns:
                    conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {column} {definition}")
            nav_columns = self._columns(conn, "paper_nav_history")
            if "drawdown" not in nav_columns:
                conn.execute(
                    "ALTER TABLE paper_nav_history ADD COLUMN drawdown REAL NOT NULL DEFAULT 0"
                )
            existing = conn.execute("SELECT 1 FROM paper_portfolio WHERE id=1").fetchone()
            if existing is None:
                invested = conn.execute(
                    "SELECT COALESCE(SUM(shares * average_cost),0) FROM paper_accounts"
                ).fetchone()[0]
                cash = max(0.0, self.initial_capital - float(invested or 0))
                conn.execute(
                    "INSERT INTO paper_portfolio(id,initial_capital,cash,updated_at) "
                    "VALUES(1,?,?,?)",
                    (self.initial_capital, cash, datetime.now().isoformat(timespec="seconds")),
                )
            lot_count = int(conn.execute("SELECT COUNT(*) FROM paper_lots").fetchone()[0])
            if lot_count == 0:
                legacy_positions = conn.execute(
                    "SELECT symbol,shares,average_cost,last_buy_date,updated_at "
                    "FROM paper_accounts WHERE shares > 0"
                ).fetchall()
                for position in legacy_positions:
                    buy_date = str(position["last_buy_date"] or position["updated_at"] or "1970-01-01")[:10]
                    conn.execute(
                        "INSERT INTO paper_lots(symbol,buy_date,shares_remaining,unit_cost) "
                        "VALUES(?,?,?,?)",
                        (
                            position["symbol"],
                            buy_date,
                            int(position["shares"]),
                            float(position["average_cost"]),
                        ),
                    )

    def sync_universe(
        self,
        universe_sources: dict[str, set[str]],
        names: dict[str, str],
        prices: dict[str, float],
    ) -> None:
        """同步候选和持仓标的；新增标的不再获得独立本金。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            portfolio = conn.execute("SELECT * FROM paper_portfolio WHERE id=1").fetchone()
            for symbol, sources in universe_sources.items():
                source_text = ",".join(sorted(sources))
                price = float(prices.get(symbol, 0) or 0)
                conn.execute(
                    """
                    INSERT INTO paper_accounts(
                        symbol,name,sources,initial_capital,cash,shares,average_cost,
                        latest_price,updated_at,last_buy_date
                    ) VALUES (?,?,?,?,?,0,0,?,?, '')
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name, sources=excluded.sources,
                        latest_price=CASE WHEN excluded.latest_price > 0
                                          THEN excluded.latest_price
                                          ELSE paper_accounts.latest_price END,
                        highest_price=CASE WHEN excluded.latest_price > paper_accounts.highest_price
                                           THEN excluded.latest_price
                                           ELSE paper_accounts.highest_price END,
                        updated_at=excluded.updated_at
                    """,
                    (
                        symbol,
                        names.get(symbol, symbol),
                        source_text,
                        float(portfolio["initial_capital"]),
                        float(portfolio["cash"]),
                        price,
                        now,
                    ),
                )
        logger.info(f"模拟组合标的同步完成：{len(universe_sources)} 只")

    def apply_alerts(self, alerts: list[IntradayAlert]) -> list[PaperTrade]:
        """先处理退出，再按评分处理买入；每只股票每天最多买入一次。"""
        exiting_symbols = {
            item.symbol for item in alerts if item.alert_type in self.EXIT_ALERTS
        }
        alerts = [
            item
            for item in alerts
            if not (item.symbol in exiting_symbols and item.alert_type in self.BUY_ALERTS)
        ]
        ordered = sorted(
            alerts,
            key=lambda item: (
                item.alert_type not in self.EXIT_ALERTS,
                -item.priority_score,
                item.symbol,
            ),
        )
        trades = [trade for item in ordered if (trade := self._apply_alert(item)) is not None]
        logger.info(
            f"模拟交易处理完成：有效预警 {len(alerts)} 条，成交 {len(trades)} 笔"
        )
        return trades

    def _number(self, key: str) -> float:
        return self.thresholds.number("paper_trading", key)

    def _integer(self, key: str) -> int:
        return self.thresholds.integer("paper_trading", key)

    def _apply_alert(self, alert: IntradayAlert) -> PaperTrade | None:
        if alert.price <= 0:
            return None
        trading_date = alert.quote_time[:10]
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_accounts WHERE symbol=?", (alert.symbol,)
            ).fetchone()
            portfolio = conn.execute("SELECT * FROM paper_portfolio WHERE id=1").fetchone()
            if row is None or portfolio is None:
                return None

            if alert.alert_type in self.BUY_ALERTS:
                duplicate = conn.execute(
                    "SELECT 1 FROM paper_trades WHERE symbol=? AND action IN ('建仓','增持') "
                    "AND substr(traded_at,1,10)=?",
                    (alert.symbol, trading_date),
                ).fetchone()
            else:
                duplicate = conn.execute(
                    "SELECT 1 FROM paper_trades WHERE symbol=? AND alert_type=? "
                    "AND substr(traded_at,1,10)=?",
                    (alert.symbol, alert.alert_type, trading_date),
                ).fetchone()
            if duplicate:
                return None

            cash = float(portfolio["cash"])
            initial_capital = float(portfolio["initial_capital"])
            current_shares = int(row["shares"])
            average_cost = float(row["average_cost"])
            action = ""
            shares = 0
            is_buy = alert.alert_type in self.BUY_ALERTS

            if is_buy:
                allow_tier_c = self.thresholds.boolean("paper_trading", "allow_tier_c_buy")
                if alert.candidate_tier == "C" and not allow_tier_c:
                    return None
                positions = conn.execute(
                    "SELECT COUNT(*) FROM paper_accounts WHERE shares > 0"
                ).fetchone()[0]
                if current_shares == 0 and positions >= self._integer("max_positions"):
                    return None
                if current_shares > 0:
                    add_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM paper_trades WHERE symbol=? AND action='增持' "
                            "AND traded_at >= ?",
                            (alert.symbol, str(row["entry_date"])),
                        ).fetchone()[0]
                    )
                    if add_count >= self._integer("max_add_count"):
                        return None
                new_positions_today = conn.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE action='建仓' "
                    "AND substr(traded_at,1,10)=?",
                    (trading_date,),
                ).fetchone()[0]
                if (
                    current_shares == 0
                    and new_positions_today >= self._integer("max_daily_new_positions")
                ):
                    return None
                market_value = conn.execute(
                    "SELECT COALESCE(SUM(shares * latest_price),0) FROM paper_accounts"
                ).fetchone()[0]
                current_value = current_shares * alert.price
                ratio = self._number(
                    "initial_buy_ratio" if current_shares == 0 else "add_buy_ratio"
                )
                desired = initial_capital * ratio
                position_room = max(
                    0.0, initial_capital * self._number("max_position_ratio") - current_value
                )
                exposure_limit = min(
                    self._number("max_exposure_ratio"), alert.market_exposure_limit
                )
                exposure_room = max(0.0, initial_capital * exposure_limit - float(market_value))
                family_room = desired
                if alert.strategy_family != "未知":
                    family_value = conn.execute(
                        "SELECT COALESCE(SUM(shares * latest_price),0) FROM paper_accounts "
                        "WHERE strategy_family=?",
                        (alert.strategy_family,),
                    ).fetchone()[0]
                    family_room = max(
                        0.0,
                        initial_capital * self._number("max_strategy_family_ratio")
                        - float(family_value),
                    )
                industry_room = desired
                if alert.industry != "未知":
                    industry_value = conn.execute(
                        "SELECT COALESCE(SUM(shares * latest_price),0) FROM paper_accounts "
                        "WHERE industry=?",
                        (alert.industry,),
                    ).fetchone()[0]
                    industry_room = max(
                        0.0,
                        initial_capital * self._number("max_industry_ratio")
                        - float(industry_value),
                    )
                execution_price = alert.price * (1 + self._number("slippage_rate"))
                atr_stop = execution_price - 2 * alert.atr if alert.atr > 0 else 0.0
                initial_stop = max(alert.stop_price, atr_stop)
                if initial_stop <= 0 or initial_stop >= execution_price:
                    initial_stop = execution_price * 0.92
                per_share_risk = max(0.01, execution_price - initial_stop)
                portfolio_heat = conn.execute(
                    "SELECT COALESCE(SUM(shares * MAX(latest_price-initial_stop_price,0)),0) "
                    "FROM paper_accounts WHERE shares > 0 AND initial_stop_price > 0"
                ).fetchone()[0]
                heat_room = max(
                    0.0,
                    initial_capital * self._number("max_portfolio_heat_ratio")
                    - float(portfolio_heat),
                )
                risk_budget = min(
                    initial_capital * self._number("risk_per_trade_ratio"), heat_room
                )
                risk_room = risk_budget / per_share_risk * execution_price
                budget = min(
                    cash,
                    desired,
                    position_room,
                    exposure_room,
                    family_room,
                    industry_room,
                    risk_room,
                )
                lot_size = self._integer("lot_size")
                shares = int(budget / execution_price / lot_size) * lot_size
                action = "建仓" if current_shares == 0 else "增持"
            elif alert.alert_type in {
                "硬止损",
                "持仓亏损",
                "移动止盈",
                "趋势清仓",
                "时间止损",
            }:
                action, shares = "清仓", current_shares
                execution_price = alert.price * (1 - self._number("slippage_rate"))
            elif alert.alert_type in {"放量下跌", "趋势减仓"}:
                action = "减持"
                sell_ratio = self._number(
                    "ma20_reduce_ratio"
                    if alert.alert_type == "趋势减仓"
                    else "sell_ratio"
                )
                shares = self._sell_lot(int(current_shares * sell_ratio))
                if current_shares > 0 and shares == 0:
                    shares = current_shares
                execution_price = alert.price * (1 - self._number("slippage_rate"))
            else:
                return None

            if not is_buy:
                available_shares = int(
                    conn.execute(
                        "SELECT COALESCE(SUM(shares_remaining),0) FROM paper_lots "
                        "WHERE symbol=? AND buy_date < ?",
                        (alert.symbol, trading_date),
                    ).fetchone()[0]
                )
                shares = min(shares, available_shares)
            if shares <= 0:
                conn.execute(
                    "UPDATE paper_accounts SET latest_price=?,updated_at=? WHERE symbol=?",
                    (alert.price, alert.quote_time, alert.symbol),
                )
                return None

            shares = min(shares, current_shares) if not is_buy else shares
            gross_amount = shares * execution_price
            commission = max(
                self._number("minimum_commission"),
                gross_amount * self._number("commission_rate"),
            )
            stamp_duty = 0.0 if is_buy else gross_amount * self._number("stamp_duty_rate")
            fee = commission + stamp_duty
            if is_buy:
                while shares > 0 and gross_amount + fee > cash:
                    shares -= self._integer("lot_size")
                    gross_amount = shares * execution_price
                    commission = max(
                        self._number("minimum_commission"),
                        gross_amount * self._number("commission_rate"),
                    )
                    fee = commission
                if shares <= 0:
                    return None
                total_cost = average_cost * current_shares + gross_amount + fee
                new_shares = current_shares + shares
                cash -= gross_amount + fee
                average_cost = total_cost / new_shares
                last_buy_date = trading_date
                entry_date = str(row["entry_date"]) or trading_date
                existing_stop = float(row["initial_stop_price"] or 0)
                stored_stop = initial_stop if existing_stop <= 0 else max(existing_stop, initial_stop)
                conn.execute(
                    "INSERT INTO paper_lots(symbol,buy_date,shares_remaining,unit_cost) "
                    "VALUES(?,?,?,?)",
                    (alert.symbol, trading_date, shares, (gross_amount + fee) / shares),
                )
                realized_pnl = 0.0
            else:
                new_shares = current_shares - shares
                cash += gross_amount - fee
                remaining = shares
                realized_pnl = -fee
                lots = conn.execute(
                    "SELECT * FROM paper_lots WHERE symbol=? AND buy_date < ? "
                    "AND shares_remaining > 0 ORDER BY buy_date,id",
                    (alert.symbol, trading_date),
                ).fetchall()
                for lot in lots:
                    consumed = min(remaining, int(lot["shares_remaining"]))
                    realized_pnl += consumed * (execution_price - float(lot["unit_cost"]))
                    conn.execute(
                        "UPDATE paper_lots SET shares_remaining=shares_remaining-? WHERE id=?",
                        (consumed, int(lot["id"])),
                    )
                    remaining -= consumed
                    if remaining == 0:
                        break
                if new_shares == 0:
                    average_cost = 0.0
                last_buy_date = str(row["last_buy_date"])
                entry_date = str(row["entry_date"]) if new_shares > 0 else ""
                stored_stop = float(row["initial_stop_price"]) if new_shares > 0 else 0.0

            conn.execute(
                "UPDATE paper_accounts SET shares=?,average_cost=?,latest_price=?,updated_at=?,"
                "last_buy_date=?,entry_date=?,initial_stop_price=?,highest_price=?,atr=?,"
                "candidate_tier=?,strategy_family=?,industry=?,buy_reason=? WHERE symbol=?",
                (
                    new_shares,
                    average_cost,
                    alert.price,
                    alert.quote_time,
                    last_buy_date,
                    entry_date,
                    stored_stop,
                    max(float(row["highest_price"] or 0), alert.price) if new_shares > 0 else 0.0,
                    alert.atr if is_buy else float(row["atr"] or 0),
                    alert.candidate_tier if is_buy else str(row["candidate_tier"]),
                    alert.strategy_family if is_buy else str(row["strategy_family"]),
                    alert.industry if is_buy else str(row["industry"]),
                    alert.message if is_buy else str(row["buy_reason"]),
                    alert.symbol,
                ),
            )
            conn.execute(
                "UPDATE paper_portfolio SET cash=?,updated_at=? WHERE id=1",
                (cash, alert.quote_time),
            )
            conn.execute(
                "UPDATE paper_accounts SET cash=?,initial_capital=?",
                (cash, initial_capital),
            )
            conn.execute(
                "INSERT INTO paper_trades(symbol,name,action,shares,price,amount,reason,"
                "alert_type,traded_at,fee,candidate_tier,priority_score,strategy_family,"
                "industry,realized_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    alert.symbol,
                    alert.name,
                    action,
                    shares,
                    execution_price,
                    gross_amount,
                    alert.message,
                    alert.alert_type,
                    alert.quote_time,
                    fee,
                    alert.candidate_tier,
                    alert.priority_score,
                    alert.strategy_family,
                    alert.industry,
                    realized_pnl,
                ),
            )

            self._record_nav(conn, trading_date, alert.quote_time)

        trade = PaperTrade(
            alert.symbol,
            alert.name,
            action,
            shares,
            execution_price,
            gross_amount,
            f"{alert.message}；费用 {fee:.2f} 元",
            alert.quote_time,
            fee=fee,
            alert_type=alert.alert_type,
            candidate_tier=alert.candidate_tier,
            priority_score=alert.priority_score,
            strategy_family=alert.strategy_family,
            industry=alert.industry,
            realized_pnl=realized_pnl,
            shares_after=new_shares,
            average_cost_after=average_cost,
            stop_price=stored_stop,
        )
        logger.info(
            f"模拟成交：{trade.symbol} {trade.action} {trade.shares} 股，"
            f"价格 {trade.price:.3f}，金额 {trade.amount:.2f}"
        )
        return trade

    def _sell_lot(self, shares: int) -> int:
        lot_size = self._integer("lot_size")
        return shares // lot_size * lot_size

    def _record_nav(
        self, conn: sqlite3.Connection, trading_date: str, updated_at: str
    ) -> None:
        portfolio = conn.execute("SELECT * FROM paper_portfolio WHERE id=1").fetchone()
        market_value = float(
            conn.execute(
                "SELECT COALESCE(SUM(shares * latest_price),0) FROM paper_accounts"
            ).fetchone()[0]
        )
        cash = float(portfolio["cash"])
        initial = float(portfolio["initial_capital"])
        total_assets = cash + market_value
        previous = conn.execute(
            "SELECT total_assets FROM paper_nav_history WHERE trading_date < ? "
            "ORDER BY trading_date DESC LIMIT 1",
            (trading_date,),
        ).fetchone()
        daily_return = total_assets / float(previous[0]) - 1 if previous else 0.0
        historical_peak = float(
            conn.execute(
                "SELECT COALESCE(MAX(total_assets),?) FROM paper_nav_history "
                "WHERE trading_date < ?",
                (total_assets, trading_date),
            ).fetchone()[0]
        )
        peak = max(historical_peak, total_assets)
        drawdown = total_assets / peak - 1 if peak > 0 else 0.0
        position_count = int(
            conn.execute("SELECT COUNT(*) FROM paper_accounts WHERE shares > 0").fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO paper_nav_history(
                trading_date,cash,market_value,total_assets,daily_return,
                cumulative_return,drawdown,exposure_rate,position_count,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trading_date) DO UPDATE SET
                cash=excluded.cash,market_value=excluded.market_value,
                total_assets=excluded.total_assets,daily_return=excluded.daily_return,
                cumulative_return=excluded.cumulative_return,
                drawdown=excluded.drawdown,
                exposure_rate=excluded.exposure_rate,position_count=excluded.position_count,
                updated_at=excluded.updated_at
            """,
            (
                trading_date,
                cash,
                market_value,
                total_assets,
                daily_return,
                total_assets / initial - 1,
                drawdown,
                market_value / initial,
                position_count,
                updated_at,
            ),
        )

    def snapshot_nav(self, trading_date: str | None = None) -> None:
        """保存当日组合净值；无成交的交易日也会留下快照。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            self._record_nav(conn, trading_date or now[:10], now)

    def position_states(self) -> dict[str, dict]:
        """返回盘中风险监控所需的持仓计划。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_accounts WHERE shares > 0 ORDER BY symbol"
            ).fetchall()
        return {
            str(row["symbol"]): {
                "shares": int(row["shares"]),
                "average_cost": float(row["average_cost"]),
                "entry_date": str(row["entry_date"]),
                "initial_stop_price": float(row["initial_stop_price"]),
                "highest_price": float(row["highest_price"]),
                "atr": float(row["atr"]),
                "candidate_tier": str(row["candidate_tier"]),
                "strategy_family": str(row["strategy_family"]),
                "industry": str(row["industry"]),
            }
            for row in rows
        }

    def nav_history(self, limit: int = 250) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_nav_history ORDER BY trading_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def portfolio(self) -> PaperPortfolio:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM paper_portfolio WHERE id=1").fetchone()
            positions = conn.execute(
                "SELECT shares,latest_price FROM paper_accounts WHERE shares > 0"
            ).fetchall()
        market_value = sum(int(item["shares"]) * float(item["latest_price"]) for item in positions)
        initial = float(row["initial_capital"])
        cash = float(row["cash"])
        total_assets = cash + market_value
        pnl = total_assets - initial
        return PaperPortfolio(
            initial,
            cash,
            market_value,
            total_assets,
            pnl,
            pnl / initial,
            len(positions),
            market_value / initial,
        )

    def accounts(self) -> list[PaperAccount]:
        portfolio = self.portfolio()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM paper_accounts ORDER BY symbol").fetchall()
        result = []
        for row in rows:
            market_value = int(row["shares"]) * float(row["latest_price"])
            position_cost = int(row["shares"]) * float(row["average_cost"])
            pnl = market_value - position_cost
            result.append(
                PaperAccount(
                    symbol=row["symbol"],
                    name=row["name"],
                    sources=row["sources"],
                    initial_capital=portfolio.initial_capital,
                    cash=portfolio.cash,
                    shares=int(row["shares"]),
                    average_cost=float(row["average_cost"]),
                    latest_price=float(row["latest_price"]),
                    market_value=market_value,
                    total_assets=market_value,
                    total_pnl=pnl,
                    return_rate=pnl / position_cost if position_cost > 0 else 0.0,
                    entry_date=str(row["entry_date"]),
                    initial_stop_price=float(row["initial_stop_price"]),
                    highest_price=float(row["highest_price"]),
                    candidate_tier=str(row["candidate_tier"]),
                    strategy_family=str(row["strategy_family"]),
                    industry=str(row["industry"]),
                )
            )
        return result

    def trades(self, limit: int = 100) -> list[PaperTrade]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades ORDER BY traded_at DESC,id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            PaperTrade(
                row["symbol"],
                row["name"],
                row["action"],
                int(row["shares"]),
                float(row["price"]),
                float(row["amount"]),
                row["reason"],
                row["traded_at"],
                fee=float(row["fee"]),
                alert_type=str(row["alert_type"]),
                candidate_tier=str(row["candidate_tier"]),
                priority_score=float(row["priority_score"]),
                strategy_family=str(row["strategy_family"]),
                industry=str(row["industry"]),
                realized_pnl=float(row["realized_pnl"]),
            )
            for row in rows
        ]
