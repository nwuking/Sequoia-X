"""按交易日推进的事件驱动回测引擎。

信号在收盘后生成，只允许在下一根可用K线开盘成交。所有滚动指标均按股票
分组并只使用当前行及以前的数据，财务因子按公告日期向后匹配。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from sequoia_x.core.market_rules import (
    cannot_buy_at_open,
    cannot_sell_at_open,
    market_board,
    price_limit_ratio,
)
from sequoia_x.strategy.comprehensive_trend import ComprehensiveTrendStrategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 1_000_000.0
    max_positions: int = 10
    max_position_ratio: float = 0.10
    max_exposure_ratio: float = 0.80
    risk_per_trade_ratio: float = 0.008
    min_signal_count: int = 1
    stop_loss_ratio: float = 0.08
    trailing_start_profit: float = 0.08
    trailing_drawdown: float = 0.06
    max_holding_days: int = 15
    slippage_rate: float = 0.001
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    lot_size: int = 100


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    start_date: str
    end_date: str
    metrics: dict[str, float]
    nav: pd.DataFrame
    trades: pd.DataFrame
    attribution: dict[str, list[dict]]


@dataclass(frozen=True)
class WalkForwardResult:
    windows: pd.DataFrame
    out_of_sample_nav: pd.DataFrame
    out_of_sample_trades: pd.DataFrame
    metrics: dict[str, float]
    attribution: dict[str, list[dict]]


class EventDrivenBacktester:
    """将日线信号转换为下一交易日成交事件。"""

    SIGNAL_COLUMNS = {
        "MaVolume": "sig_ma_volume",
        "TurtleBreakout": "sig_turtle",
        "HighTightFlag": "sig_flag",
        "RpsBreakout": "sig_rps",
        "ComprehensiveTrend": "sig_trend",
        "LowPriceFactor": "sig_factor",
    }

    def __init__(
        self,
        db_path: str,
        config: BacktestConfig | None = None,
        symbols: list[str] | None = None,
    ) -> None:
        self.db_path = db_path
        self.config = config or BacktestConfig()
        self.symbols = [str(item).zfill(6) for item in symbols] if symbols else None

    def _load_data(self, end_date: str) -> pd.DataFrame:
        params: list[object] = [end_date]
        symbol_sql = ""
        if self.symbols:
            placeholders = ",".join("?" for _ in self.symbols)
            symbol_sql = f" AND symbol IN ({placeholders})"
            params.extend(self.symbols)
        with sqlite3.connect(self.db_path) as conn:
            daily_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(stock_daily)").fetchall()
            }
            raw_columns = [
                column for column in ("raw_open", "raw_high", "raw_low", "raw_close")
                if column in daily_columns
            ]
            select_columns = "symbol,date,open,high,low,close,volume,turnover"
            if raw_columns:
                select_columns += "," + ",".join(raw_columns)
            frame = pd.read_sql(
                f"SELECT {select_columns} "
                f"FROM stock_daily WHERE date <= ?{symbol_sql} ORDER BY symbol,date",
                conn,
                params=params,
            )
            names = pd.read_sql("SELECT symbol,name FROM stock_basic", conn)
            financials = pd.read_sql(
                "SELECT * FROM financial_factors WHERE announcement_date IS NOT NULL "
                "ORDER BY symbol,announcement_date",
                conn,
            )
            status_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_status_daily'"
            ).fetchone()
            statuses = (
                pd.read_sql("SELECT * FROM stock_status_daily WHERE date <= ?", conn, params=[end_date])
                if status_exists
                else pd.DataFrame()
            )
        if frame.empty:
            raise RuntimeError("回测区间没有日线数据")
        frame["date"] = pd.to_datetime(frame["date"])
        for column in ("open", "high", "low", "close", "volume", "turnover"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in ("open", "high", "low", "close"):
            raw = f"raw_{column}"
            if raw not in frame:
                frame[raw] = frame[column]
            else:
                frame[raw] = pd.to_numeric(frame[raw], errors="coerce").fillna(frame[column])
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        if not statuses.empty:
            statuses["date"] = pd.to_datetime(statuses["date"])
            keep = [
                column for column in ("symbol", "date", "name", "board", "is_st", "is_suspended", "can_buy", "can_sell")
                if column in statuses
            ]
            frame = frame.merge(statuses[keep], on=["symbol", "date"], how="left")
        if not names.empty:
            frame = frame.merge(names, on="symbol", how="left")
            if "name_x" in frame:
                frame["name"] = frame["name_x"].fillna(frame.get("name_y"))
                frame = frame.drop(columns=[column for column in ("name_x", "name_y") if column in frame])
        if "is_st" not in frame:
            frame["is_st"] = frame.get("name", pd.Series("", index=frame.index)).fillna("").str.upper().str.contains("ST")
        else:
            fallback_st = frame.get("name", pd.Series("", index=frame.index)).fillna("").str.upper().str.contains("ST")
            frame["is_st"] = frame["is_st"].fillna(fallback_st).astype(bool)
        frame = frame[~frame["is_st"]]
        frame["board"] = frame.get("board", pd.Series(index=frame.index, dtype=object))
        frame["board"] = frame["board"].fillna(frame["symbol"].map(market_board))
        frame["is_suspended"] = frame.get("is_suspended", False)
        frame["can_buy"] = frame.get("can_buy", True)
        frame["can_sell"] = frame.get("can_sell", True)
        if not financials.empty:
            financials["announcement_date"] = pd.to_datetime(
                financials["announcement_date"], errors="coerce"
            )
            financials = financials.dropna(subset=["announcement_date"])
            keep = ["symbol", "announcement_date", "roe", "net_profit_yoy", "pe_dynamic", "pb"]
            financials = financials[keep].sort_values(["announcement_date", "symbol"])
            ordered = frame.sort_values(["date", "symbol"])
            frame = pd.merge_asof(
                ordered,
                financials,
                left_on="date",
                right_on="announcement_date",
                by="symbol",
                direction="backward",
            ).sort_values(["symbol", "date"])
        return frame.reset_index(drop=True)

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        return (100 - 100 / (1 + rs)).fillna(50).clip(0, 100)

    def prepare_signals(self, end_date: str) -> pd.DataFrame:
        df = self._load_data(end_date)
        groups = df.groupby("symbol", group_keys=False)
        for period in (5, 10, 20, 60, 120, 252):
            df[f"ma{period}"] = groups["close"].transform(
                lambda item, window=period: item.rolling(window).mean()
            )
        df["vol20"] = groups["volume"].transform(lambda item: item.rolling(20).mean())
        df["high20_prev"] = groups["high"].transform(
            lambda item: item.shift(1).rolling(20).max()
        )
        df["high120"] = groups["high"].transform(lambda item: item.rolling(120).max())
        df["high120_prev"] = groups["high"].transform(lambda item: item.shift(1).rolling(120).max())
        df["low40"] = groups["low"].transform(lambda item: item.rolling(40).min())
        df["high40"] = groups["high"].transform(lambda item: item.rolling(40).max())
        df["low10"] = groups["low"].transform(lambda item: item.rolling(10).min())
        df["high10"] = groups["high"].transform(lambda item: item.rolling(10).max())
        df["prev_close"] = groups["close"].shift(1)
        df["prev_raw_close"] = groups["raw_close"].shift(1)
        df["prev2_close"] = groups["close"].shift(2)
        df["prev_open"] = groups["open"].shift(1)
        df["prev_volume"] = groups["volume"].shift(1)
        df["ret120"] = groups["close"].pct_change(120, fill_method=None)
        df["ret60"] = groups["close"].pct_change(60, fill_method=None)
        df["ret20"] = groups["close"].pct_change(20, fill_method=None)
        df["ret252_21"] = groups["close"].transform(
            lambda item: item.shift(21) / item.shift(252) - 1
        )
        df["volatility60"] = groups["close"].transform(
            lambda item: item.pct_change(fill_method=None).rolling(60).std()
        )
        df["rsi"] = groups["close"].transform(self._rsi)
        previous_close = groups["close"].shift(1)
        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - previous_close).abs(),
                (df["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr"] = true_range.groupby(df["symbol"]).transform(
            lambda item: item.ewm(alpha=1 / 14, adjust=False).mean()
        )
        df["rps"] = df.groupby("date")["ret120"].rank(pct=True) * 100
        df["rps60"] = df.groupby("date")["ret60"].rank(pct=True) * 100
        df["rps20"] = df.groupby("date")["ret20"].rank(pct=True) * 100
        day_range = (df["high"] - df["low"]).replace(0, np.nan)
        upper_shadow_ratio = (df["high"] - df["close"]) / day_range
        volume_ratio = df["volume"] / df["vol20"]
        daily_limit_ratio = df["symbol"].map(lambda symbol: price_limit_ratio(symbol) or 0.0)

        df["sig_ma_volume"] = (
            (groups["ma5"].shift(1) <= groups["ma20"].shift(1))
            & (df["ma5"] > df["ma20"])
            & (df["volume"] > df["vol20"] * 1.5)
        )
        df["sig_turtle"] = (
            (df["close"] > df["high20_prev"])
            & (df["turnover"] > 100_000_000)
            & (df["close"] > df["open"])
            & (df["close"] > df["prev_close"])
            & volume_ratio.between(1.2, 3.0)
            & (df["close"] <= df["high20_prev"] * 1.05)
            & (df["close"] <= df["ma20"] * 1.15)
            & (upper_shadow_ratio <= 0.35)
        )
        df["sig_flag"] = (
            (df["high40"] / df["low40"] > 1.6)
            & (df["high10"] / df["low10"] < 1.15)
            & (df["low10"] >= df["high40"] * 0.8)
            & (groups["volume"].shift(1) < groups["vol20"].shift(1) * 0.6)
            & (df["close"] > groups["high10"].shift(1))
            & (df["volume"] >= df["vol20"] * 0.8)
            & (upper_shadow_ratio <= 0.35)
        )
        df["sig_shakeout"] = (
            (df["prev_close"] >= df["prev2_close"] * (1 + daily_limit_ratio - 0.005))
            & (df["close"] < df["open"])
            & (df["volume"] > df["prev_volume"] * 2)
            & (df["low"] >= df["prev_close"])
        )
        df["sig_reversal"] = (
            (groups["ma20"].shift(1) > groups["ma60"].shift(1))
            & (df["close"] <= df["prev_close"] * (1 - daily_limit_ratio + 0.005))
            & (df["volume"] > df["vol20"] * 2)
        )
        df["sig_rps"] = (
            (df["rps"] >= 90)
            & (df["rps60"] >= 80)
            & (df["rps20"] >= 70)
            & (df["close"] > df["high120_prev"])
            & (groups["turnover"].transform(lambda item: item.rolling(20).mean()) >= 80_000_000)
            & volume_ratio.between(1.2, 3.0)
            & (df["close"] <= df["ma20"] * 1.12)
        )
        # 与正式综合趋势策略共用 A/B/C 买点实现，避免回测维护另一套入场条件。
        comprehensive = pd.concat(
            [
                ComprehensiveTrendStrategy._indicators(group)
                for _, group in df.groupby("symbol", sort=False)
            ]
        ).sort_index()
        equal_market_return = df.groupby("date").apply(
            lambda group: group["close"].div(group["prev_close"]).sub(1).mean(),
            include_groups=False,
        )
        market_proxy = (1 + equal_market_return.fillna(0)).cumprod()
        market_ma20 = market_proxy.rolling(20).mean()
        market_ma60 = market_proxy.rolling(60).mean()
        breadth = (df["close"] > df["ma20"]).groupby(df["date"]).mean()
        market_score = (
            (market_proxy > market_ma20).astype(int) * 5
            + (market_ma20 > market_ma20.shift(5)).astype(int) * 5
            + (market_proxy > market_ma60).astype(int) * 5
            + (breadth >= 0.55).astype(int) * 5
        )
        market_strong = df["date"].map((market_score >= 15).to_dict())
        entry_flags = ComprehensiveTrendStrategy.entry_flags(
            comprehensive,
            market_strong.set_axis(comprehensive.index),
        )
        df["sig_trend"] = entry_flags.any(axis=1) & (comprehensive["rsi"] >= 45)
        quality = df.get("roe", pd.Series(np.nan, index=df.index)).fillna(0) >= 0
        profit = df.get("net_profit_yoy", pd.Series(np.nan, index=df.index)).fillna(-100) >= -50
        liquid = groups["turnover"].transform(lambda item: item.rolling(20).mean()) >= 80_000_000
        factor_score = (
            df.groupby("date")["ret252_21"].rank(pct=True).fillna(0)
            + (1 - df.groupby("date")["volatility60"].rank(pct=True).fillna(1))
            + df.groupby("date")["close"].rank(pct=True, ascending=False).fillna(0)
        )
        monthly_rank = factor_score.groupby(df["date"]).rank(ascending=False, method="first")
        previous_month = groups["date"].shift(1).dt.to_period("M")
        is_month_start = df["date"].dt.to_period("M") != previous_month
        df["sig_factor"] = (
            quality
            & profit
            & liquid
            & (df["close"] <= 30)
            & (monthly_rank <= 10)
            & is_month_start
        )

        signal_columns = list(self.SIGNAL_COLUMNS.values())
        df["signal_count"] = df[signal_columns].fillna(False).sum(axis=1)
        df["signal_score"] = (
            df["signal_count"] * 10
            + df["rps"].fillna(0) * 0.2
            + np.where(df["sig_trend"], 20, 0)
        )
        df["next_date"] = groups["date"].shift(-1)
        df["next_open"] = groups["open"].shift(-1)
        df["next_raw_open"] = groups["raw_open"].shift(-1)
        df["next_is_st"] = groups["is_st"].shift(-1)
        df["next_is_suspended"] = groups["is_suspended"].shift(-1)
        df["next_can_buy"] = groups["can_buy"].shift(-1)
        df["next_high"] = groups["high"].shift(-1)
        df["next_low"] = groups["low"].shift(-1)
        return df

    @staticmethod
    def _sources(row: pd.Series) -> tuple[str, ...]:
        return tuple(
            name for name, column in EventDrivenBacktester.SIGNAL_COLUMNS.items() if bool(row[column])
        )

    def run(
        self,
        start_date: str,
        end_date: str,
        prepared_signals: pd.DataFrame | None = None,
    ) -> BacktestResult:
        cfg = self.config
        frame = prepared_signals if prepared_signals is not None else self.prepare_signals(end_date)
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        execution_rows = frame[
            frame["next_date"].between(start, end, inclusive="both")
            & (frame["signal_count"] >= cfg.min_signal_count)
        ].copy()
        signals_by_execution = {
            date: group.sort_values(["signal_score", "turnover"], ascending=False)
            for date, group in execution_rows.groupby("next_date")
        }
        bars = frame[frame["date"].between(start, end, inclusive="both")]
        bars_by_date = {date: group.set_index("symbol") for date, group in bars.groupby("date")}
        dates = sorted(bars_by_date)
        cash = cfg.initial_capital
        positions: dict[str, dict] = {}
        pending_exit: dict[str, str] = {}
        trades: list[dict] = []
        nav_rows: list[dict] = []

        for trading_date in dates:
            day = bars_by_date[trading_date]
            for symbol, reason in list(pending_exit.items()):
                if symbol not in positions or symbol not in day.index:
                    continue
                bar = day.loc[symbol]
                previous_close_value = float(bar.get("prev_raw_close", bar["prev_close"]) or 0)
                open_price = float(bar["raw_open"])
                if bool(bar.get("is_suspended", False)) or not bool(bar.get("can_sell", True)):
                    continue
                if previous_close_value > 0 and cannot_sell_at_open(
                    symbol, previous_close_value, open_price, is_st=bool(bar.get("is_st", False))
                ):
                    continue
                position = positions[symbol]
                gross = position["shares"] * open_price * (1 - cfg.slippage_rate)
                fee = max(cfg.minimum_commission, gross * cfg.commission_rate)
                fee += gross * cfg.stamp_duty_rate
                cash += gross - fee
                pnl = gross - fee - position["cost"]
                trades.append(
                    {
                        "signal_date": position["signal_date"].date().isoformat(),
                        "trade_date": trading_date.date().isoformat(),
                        "symbol": symbol,
                        "action": "卖出",
                        "shares": position["shares"],
                        "price": gross / position["shares"],
                        "fee": fee,
                        "reason": reason,
                        "sources": ",".join(position["sources"]),
                        "holding_days": position["holding_days"],
                        "realized_pnl": pnl,
                    }
                )
                del positions[symbol]
                del pending_exit[symbol]

            candidates = signals_by_execution.get(trading_date)
            if candidates is not None:
                for _, signal in candidates.iterrows():
                    symbol = str(signal["symbol"])
                    if symbol in positions or symbol not in day.index:
                        continue
                    if len(positions) >= cfg.max_positions:
                        break
                    open_price = float(signal.get("next_raw_open", signal["next_open"]))
                    previous_close_value = float(signal.get("raw_close", signal["close"]))
                    if bool(signal.get("next_is_suspended", False)) or not bool(signal.get("next_can_buy", True)):
                        continue
                    if open_price <= 0 or cannot_buy_at_open(
                        symbol, previous_close_value, open_price, is_st=bool(signal.get("next_is_st", False))
                    ):
                        continue
                    execution_price = open_price * (1 + cfg.slippage_rate)
                    atr = float(signal["atr"] or 0)
                    stop_price = min(execution_price * (1 - cfg.stop_loss_ratio), execution_price - 2 * atr)
                    per_share_risk = max(0.01, execution_price - stop_price)
                    market_value = sum(
                        item["shares"] * float(day.loc[sym, "open"])
                        for sym, item in positions.items()
                        if sym in day.index
                    )
                    budget = min(
                        cash,
                        cfg.initial_capital * cfg.max_position_ratio,
                        max(0.0, cfg.initial_capital * cfg.max_exposure_ratio - market_value),
                        cfg.initial_capital * cfg.risk_per_trade_ratio
                        / per_share_risk
                        * execution_price,
                    )
                    shares = math.floor(budget / execution_price / cfg.lot_size) * cfg.lot_size
                    if shares <= 0:
                        continue
                    gross = shares * execution_price
                    fee = max(cfg.minimum_commission, gross * cfg.commission_rate)
                    if gross + fee > cash:
                        continue
                    cash -= gross + fee
                    sources = self._sources(signal)
                    positions[symbol] = {
                        "shares": shares,
                        "cost": gross + fee,
                        "average_cost": (gross + fee) / shares,
                        "stop_price": stop_price,
                        "highest": execution_price,
                        "holding_days": 0,
                        "sources": sources,
                        "signal_date": signal["date"],
                    }
                    trades.append(
                        {
                            "signal_date": signal["date"].date().isoformat(),
                            "trade_date": trading_date.date().isoformat(),
                            "symbol": symbol,
                            "action": "买入",
                            "shares": shares,
                            "price": execution_price,
                            "fee": fee,
                            "reason": "信号入场",
                            "sources": ",".join(sources),
                            "holding_days": 0,
                            "realized_pnl": 0.0,
                        }
                    )

            market_value = 0.0
            for symbol, position in positions.items():
                if symbol not in day.index:
                    continue
                bar = day.loc[symbol]
                close = float(bar["close"])
                position["highest"] = max(position["highest"], float(bar["high"]))
                position["holding_days"] += 1
                market_value += position["shares"] * close
                profit = close / position["average_cost"] - 1
                drawdown = close / position["highest"] - 1
                if float(bar["low"]) <= position["stop_price"]:
                    pending_exit[symbol] = "初始止损"
                elif (
                    position["highest"] / position["average_cost"] - 1
                    >= cfg.trailing_start_profit
                    and drawdown <= -cfg.trailing_drawdown
                ):
                    pending_exit[symbol] = "移动止盈"
                elif close < float(bar["ma60"] or 0):
                    pending_exit[symbol] = "跌破MA60"
                elif position["holding_days"] >= cfg.max_holding_days and profit < 0.02:
                    pending_exit[symbol] = "时间止损"

            total_assets = cash + market_value
            benchmark_return = float(day["close"].div(day["prev_close"]).sub(1).mean())
            nav_rows.append(
                {
                    "date": trading_date.date().isoformat(),
                    "cash": cash,
                    "market_value": market_value,
                    "total_assets": total_assets,
                    "position_count": len(positions),
                    "benchmark_return": benchmark_return,
                }
            )

        nav = pd.DataFrame(nav_rows)
        trades_frame = pd.DataFrame(trades)
        nav = self._decorate_nav(nav, cfg.initial_capital)
        metrics = self._metrics(nav, trades_frame)
        return BacktestResult(
            cfg,
            start_date,
            end_date,
            metrics,
            nav,
            trades_frame,
            self._attribution(trades_frame),
        )

    @staticmethod
    def _decorate_nav(nav: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
        if nav.empty:
            return nav
        nav = nav.copy()
        nav["daily_return"] = nav["total_assets"].pct_change().fillna(0)
        nav["cumulative_return"] = nav["total_assets"] / initial_capital - 1
        nav["benchmark_cumulative"] = (1 + nav["benchmark_return"].fillna(0)).cumprod() - 1
        nav["drawdown"] = nav["total_assets"] / nav["total_assets"].cummax() - 1
        return nav

    @staticmethod
    def _metrics(nav: pd.DataFrame, trades: pd.DataFrame) -> dict[str, float]:
        if nav.empty:
            return {}
        returns = nav["daily_return"]
        periods = max(1, len(nav))
        total_return = float(nav["cumulative_return"].iloc[-1])
        annual_return = (1 + total_return) ** (252 / periods) - 1 if total_return > -1 else -1.0
        volatility = float(returns.std(ddof=0) * np.sqrt(252))
        downside = float(returns.clip(upper=0).std(ddof=0) * np.sqrt(252))
        exits = trades[trades["action"] == "卖出"] if not trades.empty else pd.DataFrame()
        wins = exits["realized_pnl"] > 0 if not exits.empty else pd.Series(dtype=bool)
        gross_profit = float(exits.loc[exits["realized_pnl"] > 0, "realized_pnl"].sum()) if not exits.empty else 0.0
        gross_loss = abs(float(exits.loc[exits["realized_pnl"] < 0, "realized_pnl"].sum())) if not exits.empty else 0.0
        total_fees = float(trades["fee"].sum()) if not trades.empty else 0.0
        traded_amount = (
            float((trades["shares"] * trades["price"]).sum()) if not trades.empty else 0.0
        )
        return {
            "total_return": total_return,
            "annual_return": float(annual_return),
            "annual_volatility": volatility,
            "sharpe": float(annual_return / volatility) if volatility > 0 else 0.0,
            "sortino": float(annual_return / downside) if downside > 0 else 0.0,
            "max_drawdown": float(nav["drawdown"].min()),
            "calmar": float(annual_return / abs(nav["drawdown"].min()))
            if nav["drawdown"].min() < 0
            else 0.0,
            "benchmark_return": float(nav["benchmark_cumulative"].iloc[-1]),
            "closed_trades": float(len(exits)),
            "win_rate": float(wins.mean()) if len(wins) else 0.0,
            "net_profit": float(exits["realized_pnl"].sum()) if not exits.empty else 0.0,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else 0.0,
            "average_trade_pnl": float(exits["realized_pnl"].mean()) if not exits.empty else 0.0,
            "average_holding_days": float(exits["holding_days"].mean()) if not exits.empty else 0.0,
            "total_fees": total_fees,
            "turnover_ratio": traded_amount / float(nav["total_assets"].mean()) if len(nav) else 0.0,
            "average_exposure": float((nav["market_value"] / nav["total_assets"].replace(0, np.nan)).mean()),
        }

    @staticmethod
    def _attribution(trades: pd.DataFrame) -> dict[str, list[dict]]:
        if trades.empty:
            return {"strategy": [], "exit_reason": []}
        exits = trades[trades["action"] == "卖出"].copy()
        strategy_rows: list[dict] = []
        if not exits.empty:
            expanded = exits.assign(strategy=exits["sources"].str.split(",")).explode("strategy")
            for strategy, group in expanded.groupby("strategy"):
                strategy_rows.append(
                    {
                        "strategy": strategy,
                        "trades": int(len(group)),
                        "win_rate": float((group["realized_pnl"] > 0).mean()),
                        "realized_pnl": float(group["realized_pnl"].sum()),
                        "average_holding_days": float(group["holding_days"].mean()),
                    }
                )
        reason_rows = [
            {
                "reason": reason,
                "trades": int(len(group)),
                "win_rate": float((group["realized_pnl"] > 0).mean()),
                "realized_pnl": float(group["realized_pnl"].sum()),
            }
            for reason, group in exits.groupby("reason")
        ]
        return {"strategy": strategy_rows, "exit_reason": reason_rows}


class WalkForwardValidator:
    """在训练窗口选择参数，只把随后测试窗口计入样本外结果。"""

    def __init__(self, backtester: EventDrivenBacktester) -> None:
        self.backtester = backtester

    def run(
        self,
        start_date: str,
        end_date: str,
        train_days: int = 252,
        test_days: int = 63,
    ) -> WalkForwardResult:
        signals = self.backtester.prepare_signals(end_date)
        dates = sorted(
            item
            for item in signals["date"].drop_duplicates()
            if pd.Timestamp(start_date) <= item <= pd.Timestamp(end_date)
        )
        windows: list[dict] = []
        nav_parts: list[pd.DataFrame] = []
        trade_parts: list[pd.DataFrame] = []
        candidates = [
            (1, 0.06),
            (1, 0.08),
            (2, 0.06),
            (2, 0.08),
        ]
        cursor = train_days
        while cursor + test_days <= len(dates):
            train_start, train_end = dates[cursor - train_days], dates[cursor - 1]
            test_start, test_end = dates[cursor], dates[cursor + test_days - 1]
            scored: list[tuple[float, BacktestConfig]] = []
            for min_signals, stop_loss in candidates:
                config = replace(
                    self.backtester.config,
                    min_signal_count=min_signals,
                    stop_loss_ratio=stop_loss,
                )
                engine = EventDrivenBacktester(
                    self.backtester.db_path, config, self.backtester.symbols
                )
                result = engine.run(
                    train_start.date().isoformat(),
                    train_end.date().isoformat(),
                    prepared_signals=signals,
                )
                score = result.metrics.get("calmar", 0) + result.metrics.get("sharpe", 0) * 0.25
                scored.append((score, config))
            _, best = max(scored, key=lambda item: item[0])
            test_engine = EventDrivenBacktester(
                self.backtester.db_path, best, self.backtester.symbols
            )
            test = test_engine.run(
                test_start.date().isoformat(),
                test_end.date().isoformat(),
                prepared_signals=signals,
            )
            windows.append(
                {
                    "train_start": train_start.date().isoformat(),
                    "train_end": train_end.date().isoformat(),
                    "test_start": test_start.date().isoformat(),
                    "test_end": test_end.date().isoformat(),
                    "min_signal_count": best.min_signal_count,
                    "stop_loss_ratio": best.stop_loss_ratio,
                    **test.metrics,
                }
            )
            nav_parts.append(test.nav.assign(window=len(windows)))
            trade_parts.append(test.trades.assign(window=len(windows)))
            cursor += test_days
        if not windows:
            raise RuntimeError("历史长度不足以生成滚动样本外窗口")
        nav = pd.concat(nav_parts, ignore_index=True)
        trades = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()
        chained = []
        base = self.backtester.config.initial_capital
        for _, group in nav.groupby("window", sort=True):
            group = group.copy()
            group["total_assets"] = base * (1 + group["daily_return"]).cumprod()
            base = float(group["total_assets"].iloc[-1])
            chained.append(group)
        nav = EventDrivenBacktester._decorate_nav(
            pd.concat(chained, ignore_index=True), self.backtester.config.initial_capital
        )
        metrics = EventDrivenBacktester._metrics(nav, trades)
        return WalkForwardResult(
            pd.DataFrame(windows),
            nav,
            trades,
            metrics,
            EventDrivenBacktester._attribution(trades),
        )


def write_backtest_report(
    result: BacktestResult | WalkForwardResult,
    output_dir: str,
    prefix: str = "backtest",
) -> dict[str, str]:
    """写出机器可读JSON和便于分析的CSV报表。"""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    nav = result.nav if isinstance(result, BacktestResult) else result.out_of_sample_nav
    trades = result.trades if isinstance(result, BacktestResult) else result.out_of_sample_trades
    nav_path = target / f"{prefix}_nav.csv"
    trades_path = target / f"{prefix}_trades.csv"
    attribution_path = target / f"{prefix}_attribution.json"
    summary_path = target / f"{prefix}_summary.json"
    report_path = target / f"{prefix}_report.md"
    nav.to_csv(nav_path, index=False)
    trades.to_csv(trades_path, index=False)
    attribution_path.write_text(
        json.dumps(result.attribution, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {"metrics": result.metrics}
    if isinstance(result, BacktestResult):
        summary.update(
            {
                "start_date": result.start_date,
                "end_date": result.end_date,
                "config": asdict(result.config),
            }
        )
    else:
        windows_path = target / f"{prefix}_windows.csv"
        result.windows.to_csv(windows_path, index=False)
        summary["windows"] = str(windows_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    metric_lines = ["| 指标 | 数值 |", "|---|---:|"]
    metric_lines.extend(f"| {key} | {value:.6f} |" for key, value in result.metrics.items())
    strategy_lines = [
        "| 策略 | 交易数 | 胜率 | 已实现盈亏 | 平均持有日 |",
        "|---|---:|---:|---:|---:|",
    ]
    strategy_lines.extend(
        "| {strategy} | {trades} | {win_rate:.2%} | {realized_pnl:.2f} | "
        "{average_holding_days:.2f} |".format(**item)
        for item in result.attribution.get("strategy", [])
    )
    reason_lines = ["| 退出原因 | 交易数 | 胜率 | 已实现盈亏 |", "|---|---:|---:|---:|"]
    reason_lines.extend(
        "| {reason} | {trades} | {win_rate:.2%} | {realized_pnl:.2f} |".format(**item)
        for item in result.attribution.get("exit_reason", [])
    )
    report_path.write_text(
        "\n".join(
            [
                f"# {prefix} 报告",
                "",
                "## 组合指标",
                "",
                *metric_lines,
                "",
                "## 策略共同归因",
                "",
                "多策略共振交易会同时计入每个来源策略，归因盈亏不可直接横向相加。",
                "",
                *strategy_lines,
                "",
                "## 退出原因归因",
                "",
                *reason_lines,
            ]
        ),
        encoding="utf-8",
    )
    return {
        "summary": str(summary_path),
        "nav": str(nav_path),
        "trades": str(trades_path),
        "attribution": str(attribution_path),
        "report": str(report_path),
    }
