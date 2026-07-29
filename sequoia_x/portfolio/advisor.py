"""基于持仓状态和多因子候选生成下一交易日建议。"""

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.low_price_multi_factor import LowPriceMultiFactorStrategy


@dataclass(frozen=True)
class PortfolioAdvice:
    symbol: str
    name: str
    is_holding: bool
    action: str
    risk: str
    reason: str
    reference_price: float
    next_workday: str


@dataclass(frozen=True)
class PortfolioCandidate:
    symbol: str
    name: str
    score: float
    close: float
    rank: int
    is_focus: bool


@dataclass(frozen=True)
class PortfolioReplacement:
    sell_symbol: str
    sell_name: str
    buy_symbol: str
    buy_name: str
    buy_rank: int
    buy_score: float
    current_return: float
    next_workday: str


@dataclass(frozen=True)
class PortfolioAdviceReport:
    advice: list[PortfolioAdvice]
    candidates: list[PortfolioCandidate]
    replacements: list[PortfolioReplacement]


class PortfolioAdvisor:
    """透明规则策略；建议用于风险管理，不代表确定性买卖指令。"""

    def __init__(self, engine: DataEngine, strategy: LowPriceMultiFactorStrategy | None = None) -> None:
        self.engine = engine
        self.strategy = strategy

    @staticmethod
    def _next_workday() -> str:
        candidate = date.today() + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate.isoformat()

    def _build_candidates(self) -> list[PortfolioCandidate]:
        if self.strategy is None:
            return []
        ranked = self.strategy.rank_candidates(limit=10)
        if ranked.empty:
            return []
        from sequoia_x.portfolio.manager import PortfolioManager

        names = self.engine.get_stock_names(ranked["symbol"].tolist())
        candidates: list[PortfolioCandidate] = []
        for _, row in ranked.iterrows():
            realtime_quote = PortfolioManager._fetch_real_quote(str(row["symbol"]))
            display_close = (
                float(realtime_quote[0])
                if realtime_quote is not None
                else float(row["close"])
            )
            candidates.append(
                PortfolioCandidate(
                    symbol=str(row["symbol"]),
                    name=names.get(str(row["symbol"]), str(row["symbol"])),
                    score=float(row["score"]),
                    close=display_close,
                    rank=int(row["rank"]),
                    is_focus=int(row["rank"]) <= 3,
                )
            )
        return candidates

    @staticmethod
    def _holding_return(row: pd.Series) -> float:
        shares = float(pd.to_numeric(pd.Series([row["shares"]]), errors="coerce").fillna(0).iloc[0])
        close = pd.to_numeric(pd.Series([row["latest_close"]]), errors="coerce").iloc[0]
        cost = pd.to_numeric(pd.Series([row["cost_price"]]), errors="coerce").iloc[0]
        if shares <= 0 or pd.isna(close) or pd.isna(cost) or float(cost) <= 0:
            return 0.0
        return float(close) / float(cost) - 1

    def _build_replacements(
        self,
        portfolio: pd.DataFrame,
        candidates: list[PortfolioCandidate],
        next_workday: str,
    ) -> list[PortfolioReplacement]:
        holdings = portfolio[pd.to_numeric(portfolio["shares"], errors="coerce").fillna(0) > 0].copy()
        if holdings.empty or not candidates:
            return []

        holdings["holding_return_value"] = holdings.apply(self._holding_return, axis=1)
        ranked_holdings = holdings.sort_values(
            ["total_return_rate", "holding_return_value"],
            ascending=[True, True],
            na_position="first",
        )
        candidate_pool = [candidate for candidate in candidates if candidate.rank <= 3]
        replacements: list[PortfolioReplacement] = []
        used_buys: set[str] = set()
        for (_, row), candidate in zip(ranked_holdings.iterrows(), candidate_pool, strict=False):
            if str(row["symbol"]) == candidate.symbol:
                continue
            if candidate.symbol in used_buys:
                continue
            replacements.append(
                PortfolioReplacement(
                    sell_symbol=str(row["symbol"]),
                    sell_name=str(row["name"]),
                    buy_symbol=candidate.symbol,
                    buy_name=candidate.name,
                    buy_rank=candidate.rank,
                    buy_score=candidate.score,
                    current_return=self._holding_return(row),
                    next_workday=next_workday,
                )
            )
            used_buys.add(candidate.symbol)
        return replacements

    def advise(self, portfolio: pd.DataFrame) -> PortfolioAdviceReport:
        advice: list[PortfolioAdvice] = []
        next_workday = self._next_workday()
        for _, row in portfolio.iterrows():
            symbol = row["symbol"]
            df = self.engine.get_ohlcv(symbol)
            if len(df) < 60:
                continue
            close = df["close"]
            adjusted_latest = float(close.iloc[-1])
            real_latest = pd.to_numeric(pd.Series([row["latest_close"]]), errors="coerce").iloc[0]
            if pd.isna(real_latest):
                continue
            real_latest = float(real_latest)
            ma5 = float(close.rolling(5).mean().iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])
            ma60 = float(close.rolling(60).mean().iloc[-1])
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
            loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
            rsi = 100.0 if loss == 0 else float(100 - 100 / (1 + gain / loss))
            volume_ratio = float(df["volume"].iloc[-1] / df["volume"].rolling(20).mean().iloc[-1])
            ret5 = float(close.pct_change(5, fill_method=None).iloc[-1])
            shares = float(pd.to_numeric(pd.Series([row["shares"]]), errors="coerce").fillna(0).iloc[0])
            cost = pd.to_numeric(pd.Series([row["cost_price"]]), errors="coerce").iloc[0]
            holding_return = real_latest / float(cost) - 1 if shares > 0 and pd.notna(cost) and cost > 0 else 0

            if shares > 0:
                if holding_return <= -0.08 and adjusted_latest < ma20 and ma5 < ma20:
                    action, risk = "减仓/执行止损纪律", "高"
                    reason = f"持仓亏损{holding_return:.1%}，价格低于20日线且短期趋势向下"
                elif adjusted_latest < ma60 and ma20 < ma60:
                    action, risk = "逢反弹减仓", "高"
                    reason = "收盘价与20日线均低于60日线，中期趋势偏弱"
                elif holding_return >= 0.15 and rsi >= 72:
                    action, risk = "分批止盈", "中"
                    reason = f"持仓收益{holding_return:.1%}且RSI={rsi:.1f}，短线偏热"
                elif adjusted_latest > ma20 and ma5 > ma20 and ma20 > ma60:
                    action, risk = "继续持有，20日线防守", "低"
                    reason = f"均线多头排列，5日涨幅{ret5:.1%}，RSI={rsi:.1f}"
                else:
                    action, risk = "持有观察，不加仓", "中"
                    reason = f"趋势信号混合；RSI={rsi:.1f}，量比={volume_ratio:.2f}"
            else:
                if ma5 > ma20 > ma60 and 45 <= rsi <= 70 and volume_ratio >= 1:
                    action, risk = "关注回踩确认，避免追高", "中"
                    reason = f"均线多头、RSI={rsi:.1f}、量比={volume_ratio:.2f}"
                elif rsi < 30:
                    action, risk = "超跌观察，等待止跌", "高"
                    reason = f"RSI={rsi:.1f}，尚需价格企稳确认"
                else:
                    action, risk = "观望", "中"
                    reason = f"尚无高质量入场共振；RSI={rsi:.1f}，量比={volume_ratio:.2f}"

            advice.append(
                PortfolioAdvice(
                    symbol=symbol,
                    name=str(row["name"]),
                    is_holding=shares > 0,
                    action=action,
                    risk=risk,
                    reason=reason,
                    reference_price=real_latest,
                    next_workday=next_workday,
                )
            )
        candidates = self._build_candidates()
        replacements = self._build_replacements(portfolio, candidates, next_workday)
        return PortfolioAdviceReport(advice=advice, candidates=candidates, replacements=replacements)
