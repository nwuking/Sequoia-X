"""按股票独立核算的本地模拟交易账户。"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sequoia_x.core.thresholds import ThresholdConfig
from sequoia_x.monitor import IntradayAlert


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


class PaperTradingManager:
    """每只股票使用独立本金，根据盘中预警自动模拟建仓和调仓。"""

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
                    UNIQUE(symbol, alert_type, traded_at)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol_time
                ON paper_trades(symbol, traded_at);
                """
            )

    def sync_universe(
        self,
        universe_sources: dict[str, set[str]],
        names: dict[str, str],
        prices: dict[str, float],
    ) -> None:
        """为新增标的建立独立账户，并更新来源与最新价格。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            for symbol, sources in universe_sources.items():
                source_text = ",".join(sorted(sources))
                price = float(prices.get(symbol, 0) or 0)
                conn.execute(
                    """
                    INSERT INTO paper_accounts(
                        symbol,name,sources,initial_capital,cash,shares,average_cost,
                        latest_price,updated_at
                    ) VALUES (?,?,?,?,?,0,0,?,?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name, sources=excluded.sources,
                        latest_price=CASE WHEN excluded.latest_price > 0
                                          THEN excluded.latest_price
                                          ELSE paper_accounts.latest_price END,
                        updated_at=excluded.updated_at
                    """,
                    (
                        symbol, names.get(symbol, symbol), source_text,
                        self.initial_capital, self.initial_capital, price, now,
                    ),
                )

    def apply_alerts(self, alerts: list[IntradayAlert]) -> list[PaperTrade]:
        """将新盘中预警转换为模拟交易；同类预警同日只成交一次。"""
        trades: list[PaperTrade] = []
        for alert in alerts:
            trade = self._apply_alert(alert)
            if trade is not None:
                trades.append(trade)
        return trades

    def _apply_alert(self, alert: IntradayAlert) -> PaperTrade | None:
        if alert.price <= 0:
            return None
        trading_date = alert.quote_time[:10]
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_accounts WHERE symbol=?", (alert.symbol,)
            ).fetchone()
            if row is None:
                return None
            duplicate = conn.execute(
                "SELECT 1 FROM paper_trades WHERE symbol=? AND alert_type=? "
                "AND substr(traded_at,1,10)=?",
                (alert.symbol, alert.alert_type, trading_date),
            ).fetchone()
            if duplicate:
                return None

            cash = float(row["cash"])
            current_shares = int(row["shares"])
            average_cost = float(row["average_cost"])
            action = ""
            shares = 0

            if alert.alert_type in {"硬止损", "持仓亏损"}:
                action, shares = "清仓", current_shares
            elif alert.alert_type == "放量下跌":
                action = "减持"
                shares = self._sell_lot(
                    int(
                        current_shares
                        * self.thresholds.number("paper_trading", "sell_ratio")
                    )
                )
                if current_shares > 0 and shares == 0:
                    shares = current_shares
            elif alert.alert_type in {"盘中突破候选", "尾盘买点确认", "盘中走强"}:
                action = "建仓" if current_shares == 0 else "增持"
                ratio = self.thresholds.number(
                    "paper_trading",
                    "initial_buy_ratio" if current_shares == 0 else "add_buy_ratio",
                )
                budget = min(cash, float(row["initial_capital"]) * ratio)
                lot_size = self.thresholds.integer("paper_trading", "lot_size")
                shares = int(budget / alert.price / lot_size) * lot_size

            if shares <= 0:
                conn.execute(
                    "UPDATE paper_accounts SET latest_price=?,updated_at=? WHERE symbol=?",
                    (alert.price, alert.quote_time, alert.symbol),
                )
                return None

            amount = shares * alert.price
            if action in {"建仓", "增持"}:
                total_cost = average_cost * current_shares + amount
                new_shares = current_shares + shares
                cash -= amount
                average_cost = total_cost / new_shares
            else:
                shares = min(shares, current_shares)
                amount = shares * alert.price
                new_shares = current_shares - shares
                cash += amount
                if new_shares == 0:
                    average_cost = 0.0

            conn.execute(
                "UPDATE paper_accounts SET cash=?,shares=?,average_cost=?,latest_price=?,"
                "updated_at=? WHERE symbol=?",
                (cash, new_shares, average_cost, alert.price, alert.quote_time, alert.symbol),
            )
            conn.execute(
                "INSERT INTO paper_trades(symbol,name,action,shares,price,amount,reason,"
                "alert_type,traded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    alert.symbol, alert.name, action, shares, alert.price, amount,
                    alert.message, alert.alert_type, alert.quote_time,
                ),
            )
        return PaperTrade(
            alert.symbol, alert.name, action, shares, alert.price, amount,
            alert.message, alert.quote_time,
        )

    def _sell_lot(self, shares: int) -> int:
        lot_size = self.thresholds.integer("paper_trading", "lot_size")
        return shares // lot_size * lot_size

    def accounts(self) -> list[PaperAccount]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM paper_accounts ORDER BY symbol").fetchall()
        result = []
        for row in rows:
            market_value = int(row["shares"]) * float(row["latest_price"])
            total_assets = float(row["cash"]) + market_value
            pnl = total_assets - float(row["initial_capital"])
            result.append(
                PaperAccount(
                    symbol=row["symbol"], name=row["name"], sources=row["sources"],
                    initial_capital=float(row["initial_capital"]), cash=float(row["cash"]),
                    shares=int(row["shares"]), average_cost=float(row["average_cost"]),
                    latest_price=float(row["latest_price"]), market_value=market_value,
                    total_assets=total_assets, total_pnl=pnl,
                    return_rate=pnl / float(row["initial_capital"]),
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
                row["symbol"], row["name"], row["action"], int(row["shares"]),
                float(row["price"]), float(row["amount"]), row["reason"], row["traded_at"],
            )
            for row in rows
        ]
