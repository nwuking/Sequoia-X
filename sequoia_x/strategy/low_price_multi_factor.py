"""低价多因子月度轮动策略。"""

from __future__ import annotations

import math

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class LowPriceMultiFactorStrategy(BaseStrategy):
    """低价多因子月度轮动策略。

    基于当前项目已有的日线行情数据，构建一个偏实用的价格型多因子模型：
    1. 价格约束：最新收盘价不高于 30 元
    2. 流动性约束：近 20 日日均成交额不低于 8000 万
    3. 动量因子：12-1 动量，避免短期反转噪音
    4. 低波动因子：近 60 日波动率越低越好
    5. 趋势强度：收盘价相对 120 日均线越强越好
    6. 流动性加分：近 20 日日均成交额越高越好

    输出固定限制为前 3 名，适合月度调仓时作为候选持仓池。
    """

    webhook_key: str = "low_price_multi_factor"
    _MIN_BARS: int = 252
    _MAX_HOLDINGS: int = 3
    _MAX_CLOSE: float = 30.0
    _MIN_TURNOVER_MA20: float = 80_000_000.0

    @staticmethod
    def _safe_zscore(series: pd.Series, ascending: bool = True) -> pd.Series:
        """对横截面数据做稳健标准化，避免标准差为 0 的异常。"""
        cleaned = pd.to_numeric(series, errors="coerce").replace([math.inf, -math.inf], pd.NA)
        mean = cleaned.mean()
        std = cleaned.std(ddof=0)
        if pd.isna(std) or std == 0:
            result = pd.Series(0.0, index=series.index, dtype="float64")
        else:
            result = ((cleaned - mean) / std).fillna(0.0)
        return result if ascending else -result

    def _score_symbol(self, symbol: str) -> dict[str, float] | None:
        df = self.engine.get_ohlcv(symbol)
        if len(df) < self._MIN_BARS:
            return None

        df = df.copy()
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")
        df = df.dropna(subset=["close", "turnover"])
        if len(df) < self._MIN_BARS:
            return None

        df["ret_21"] = df["close"].pct_change(21, fill_method=None)
        df["ret_252"] = df["close"].pct_change(252, fill_method=None)
        df["mom_12_1"] = (df["close"].shift(21) / df["close"].shift(252)) - 1
        df["daily_ret"] = df["close"].pct_change(fill_method=None)
        df["volatility_60"] = df["daily_ret"].rolling(60).std()
        df["ma120"] = df["close"].rolling(120).mean()
        df["turnover_ma20"] = df["turnover"].rolling(20).mean()

        last = df.iloc[-1]
        if (
            pd.isna(last["mom_12_1"])
            or pd.isna(last["volatility_60"])
            or pd.isna(last["ma120"])
            or pd.isna(last["turnover_ma20"])
        ):
            return None

        if last["close"] > self._MAX_CLOSE:
            return None
        if last["turnover_ma20"] < self._MIN_TURNOVER_MA20:
            return None
        if last["close"] <= 0 or last["ma120"] <= 0:
            return None

        trend_strength = last["close"] / last["ma120"] - 1
        return {
            "symbol": symbol,
            "close": float(last["close"]),
            "momentum": float(last["mom_12_1"]),
            "volatility": float(last["volatility_60"]),
            "trend_strength": float(trend_strength),
            "turnover_ma20": float(last["turnover_ma20"]),
        }

    def run(self) -> list[str]:
        """返回当前横截面评分最高的前 3 只低价股。"""
        ranked = self.rank_candidates(limit=self._MAX_HOLDINGS)
        logger.info(f"LowPriceMultiFactorStrategy 选出 {len(ranked)} 只股票")
        return ranked["symbol"].tolist() if not ranked.empty else []

    def rank_candidates(self, limit: int = 10) -> pd.DataFrame:
        """返回按分数降序排列的候选股票明细。"""
        rows: list[dict[str, float]] = []
        for symbol in self.engine.get_local_symbols():
            try:
                scored = self._score_symbol(symbol)
                if scored is not None:
                    rows.append(scored)
            except Exception as exc:
                logger.warning(f"[{symbol}] LowPriceMultiFactorStrategy 计算失败：{exc}")

        if not rows:
            logger.info("LowPriceMultiFactorStrategy 无候选股票")
            return pd.DataFrame()

        frame = pd.DataFrame(rows)
        frame["score"] = (
            0.35 * self._safe_zscore(frame["momentum"], ascending=True)
            + 0.20 * self._safe_zscore(frame["volatility"], ascending=False)
            + 0.15 * self._safe_zscore(frame["trend_strength"], ascending=True)
            + 0.10 * self._safe_zscore(frame["turnover_ma20"], ascending=True)
        )

        financials = self.engine.get_latest_financial_factors(frame["symbol"].tolist())
        if not financials.empty:
            frame = frame.merge(
                financials[
                    [
                        "symbol",
                        "roe",
                        "revenue_yoy",
                        "net_profit_yoy",
                        "gross_margin",
                        "operating_cashflow_ps",
                        "eps",
                        "pe_dynamic",
                        "pb",
                    ]
                ],
                on="symbol",
                how="left",
            )
            cashflow_quality = frame["operating_cashflow_ps"] / frame["eps"].replace(0, pd.NA)
            frame["score"] = (
                frame["score"]
                + 0.10 * self._safe_zscore(frame["roe"], ascending=True)
                + 0.05 * self._safe_zscore(frame["revenue_yoy"], ascending=True)
                + 0.05 * self._safe_zscore(frame["net_profit_yoy"], ascending=True)
                + 0.05 * self._safe_zscore(cashflow_quality, ascending=True)
                + 0.05 * self._safe_zscore(frame["pe_dynamic"], ascending=False)
                + 0.05 * self._safe_zscore(frame["pb"], ascending=False)
            )
            if frame["gross_margin"].notna().any():
                frame["score"] = frame["score"] + 0.05 * self._safe_zscore(
                    frame["gross_margin"], ascending=True
                )
        ranked = (
            frame.sort_values(["score", "momentum", "turnover_ma20"], ascending=[False, False, False])
            .head(limit)
            .reset_index(drop=True)
        )
        ranked["rank"] = ranked.index + 1
        return ranked
