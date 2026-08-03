"""低价多因子月度轮动策略。"""

from __future__ import annotations

import math
import json
from pathlib import Path

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

    webhook_key: str = "portfolio_management"
    # 前10名已经包含在自选/持仓组合报告中，不再单独发送策略卡片。
    standalone_push_enabled: bool = False
    _MIN_BARS: int = 252
    _MAX_HOLDINGS: int = 3
    _MAX_CLOSE: float = 30.0
    _MIN_TURNOVER_MA20: float = 80_000_000.0

    def __init__(self, engine, settings) -> None:
        super().__init__(engine, settings)
        self.last_combination_ranking = pd.DataFrame()

    @staticmethod
    def _safe_zscore(series: pd.Series, ascending: bool = True) -> pd.Series:
        """对横截面数据做稳健标准化，避免标准差为 0 的异常。"""
        cleaned = pd.to_numeric(series, errors="coerce").replace([math.inf, -math.inf], pd.NA)
        if cleaned.notna().sum() >= 10:
            lower, upper = cleaned.quantile([0.01, 0.99])
            cleaned = cleaned.clip(lower=lower, upper=upper)
        mean = cleaned.mean()
        std = cleaned.std(ddof=0)
        if pd.isna(std) or std == 0:
            result = pd.Series(0.0, index=series.index, dtype="float64")
        else:
            result = ((cleaned - mean) / std).fillna(0.0)
        return result if ascending else -result

    def _score_symbol(self, symbol: str) -> dict[str, float] | None:
        cfg = self.engine.thresholds
        min_bars = cfg.integer("low_price_multi_factor", "min_bars")
        df = self.engine.get_ohlcv(symbol)
        if len(df) < min_bars:
            return None

        df = df.copy()
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")
        df = df.dropna(subset=["close", "turnover"])
        if len(df) < min_bars:
            return None

        skip_days = cfg.integer("low_price_multi_factor", "momentum_skip_days")
        lookback_days = cfg.integer("low_price_multi_factor", "momentum_lookback_days")
        volatility_days = cfg.integer("low_price_multi_factor", "volatility_days")
        trend_days = cfg.integer("low_price_multi_factor", "trend_ma_days")
        turnover_days = cfg.integer("low_price_multi_factor", "turnover_days")
        df["ret_21"] = df["close"].pct_change(skip_days, fill_method=None)
        df["ret_252"] = df["close"].pct_change(lookback_days, fill_method=None)
        df["mom_12_1"] = (
            df["close"].shift(skip_days) / df["close"].shift(lookback_days)
        ) - 1
        df["daily_ret"] = df["close"].pct_change(fill_method=None)
        df["volatility_60"] = df["daily_ret"].rolling(volatility_days).std()
        df["ma120"] = df["close"].rolling(trend_days).mean()
        df["turnover_ma20"] = df["turnover"].rolling(turnover_days).mean()

        last = df.iloc[-1]
        if (
            pd.isna(last["mom_12_1"])
            or pd.isna(last["volatility_60"])
            or pd.isna(last["ma120"])
            or pd.isna(last["turnover_ma20"])
        ):
            return None

        if last["close"] > cfg.number("low_price_multi_factor", "max_close"):
            return None
        if last["turnover_ma20"] < cfg.number("low_price_multi_factor", "min_turnover_ma20"):
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

    def _run(self) -> list[str]:
        """按月锁定目标组合，月内重复运行不因短期排名变化而换仓。"""
        cfg = self.engine.thresholds
        ranked = self.rank_candidates(limit=cfg.integer("low_price_multi_factor", "ranking_limit"))
        selected = ranked.head(cfg.integer("low_price_multi_factor", "max_holdings"))
        latest_date = self.engine.get_latest_date()
        if latest_date is None:
            symbols = selected["symbol"].tolist() if not selected.empty else []
            return symbols
        month = str(latest_date)[:7]
        configured = getattr(self.settings, "low_price_rebalance_state_path", None)
        state_path = Path(configured) if configured else Path(self.engine.db_path).with_name(
            "low_price_rebalance_state.json"
        )
        previous: dict = {}
        if state_path.exists():
            try:
                previous = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
        if previous.get("month") == month:
            symbols = [str(item) for item in previous.get("symbols", [])]
            logger.info(f"LowPriceMultiFactorStrategy 沿用 {month} 月度组合 {len(symbols)} 只")
            return symbols
        symbols = selected["symbol"].tolist() if not selected.empty else []
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"month": month, "data_date": latest_date, "symbols": symbols}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(state_path)
        logger.info(f"LowPriceMultiFactorStrategy 生成 {month} 月度组合 {len(symbols)} 只")
        return symbols

    def rank_candidates(self, limit: int = 10) -> pd.DataFrame:
        """返回按分数降序排列的候选股票明细。"""
        rows: list[dict[str, float]] = []
        for symbol in self.get_eligible_symbols():
            try:
                scored = self._score_symbol(symbol)
                if scored is not None:
                    rows.append(scored)
            except Exception as exc:
                logger.warning(f"[{symbol}] LowPriceMultiFactorStrategy 计算失败：{exc}")

        if not rows:
            logger.info("LowPriceMultiFactorStrategy 无候选股票")
            self.last_combination_ranking = pd.DataFrame()
            return pd.DataFrame()

        frame = pd.DataFrame(rows)
        cfg = self.engine.thresholds
        industry_path = getattr(self.settings, "stock_industry_csv_path", "")
        if industry_path and pd.io.common.file_exists(industry_path):
            industries = pd.read_csv(industry_path, dtype={"symbol": str})
            if {"symbol", "industry"}.issubset(industries.columns):
                industries["symbol"] = industries["symbol"].str.zfill(6)
                frame = frame.merge(industries[["symbol", "industry"]], on="symbol", how="left")
        if "industry" not in frame:
            frame["industry"] = "未知"

        def factor_score(column: str, ascending: bool = True) -> pd.Series:
            # 样本足够时先做行业内标准化，减少行业天然差异带来的偏置。
            sizes = frame.groupby("industry")[column].transform("count")
            within = frame.groupby("industry", group_keys=False)[column].transform(
                lambda item: self._safe_zscore(item, ascending=ascending)
            )
            market = self._safe_zscore(frame[column], ascending=ascending)
            return within.where(sizes >= 5, market)

        frame["score"] = (
            cfg.number("low_price_multi_factor", "weight_momentum") * factor_score("momentum", ascending=True)
            + cfg.number("low_price_multi_factor", "weight_low_volatility") * factor_score("volatility", ascending=False)
            + cfg.number("low_price_multi_factor", "weight_trend") * factor_score("trend_strength", ascending=True)
            + cfg.number("low_price_multi_factor", "weight_liquidity") * factor_score("turnover_ma20", ascending=True)
        )
        # 组合层专用分数弱化动量和趋势，优先提供低波动与流动性证据，
        # 避免和 ComprehensiveTrendStrategy 对同一趋势信号重复加权。
        frame["combination_factor_score"] = (
            cfg.number("low_price_multi_factor", "combo_weight_low_volatility") * self._safe_zscore(frame["volatility"], ascending=False)
            + cfg.number("low_price_multi_factor", "combo_weight_liquidity") * self._safe_zscore(frame["turnover_ma20"], ascending=True)
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
            eps = pd.to_numeric(frame["eps"], errors="coerce")
            cashflow_quality = frame["operating_cashflow_ps"] / eps.where(eps.abs() >= 0.01)
            # 亏损企业的负PE、净资产为负的PB不参与“越低越好”排序。
            frame["pe_dynamic"] = pd.to_numeric(frame["pe_dynamic"], errors="coerce").where(
                lambda item: item > 0
            )
            frame["pb"] = pd.to_numeric(frame["pb"], errors="coerce").where(
                lambda item: item > 0
            )
            frame["score"] = (
                frame["score"]
                + cfg.number("low_price_multi_factor", "weight_roe") * self._safe_zscore(frame["roe"], ascending=True)
                + cfg.number("low_price_multi_factor", "weight_revenue_yoy") * self._safe_zscore(frame["revenue_yoy"], ascending=True)
                + cfg.number("low_price_multi_factor", "weight_profit_yoy") * self._safe_zscore(frame["net_profit_yoy"], ascending=True)
                + cfg.number("low_price_multi_factor", "weight_cashflow") * self._safe_zscore(cashflow_quality, ascending=True)
                + cfg.number("low_price_multi_factor", "weight_pe") * self._safe_zscore(frame["pe_dynamic"], ascending=False)
                + cfg.number("low_price_multi_factor", "weight_pb") * self._safe_zscore(frame["pb"], ascending=False)
            )
            frame["combination_factor_score"] = (
                frame["combination_factor_score"]
                + cfg.number("low_price_multi_factor", "combo_weight_roe") * self._safe_zscore(frame["roe"], ascending=True)
                + cfg.number("low_price_multi_factor", "combo_weight_revenue_yoy") * self._safe_zscore(frame["revenue_yoy"], ascending=True)
                + cfg.number("low_price_multi_factor", "combo_weight_profit_yoy") * self._safe_zscore(frame["net_profit_yoy"], ascending=True)
                + cfg.number("low_price_multi_factor", "combo_weight_cashflow") * self._safe_zscore(cashflow_quality, ascending=True)
                + cfg.number("low_price_multi_factor", "combo_weight_pe") * self._safe_zscore(frame["pe_dynamic"], ascending=False)
                + cfg.number("low_price_multi_factor", "combo_weight_pb") * self._safe_zscore(frame["pb"], ascending=False)
            )
            if frame["gross_margin"].notna().any():
                frame["score"] = frame["score"] + cfg.number(
                    "low_price_multi_factor", "weight_gross_margin"
                ) * self._safe_zscore(
                    frame["gross_margin"], ascending=True
                )
                frame["combination_factor_score"] = (
                    frame["combination_factor_score"]
                    + cfg.number("low_price_multi_factor", "combo_weight_gross_margin")
                    * self._safe_zscore(frame["gross_margin"], ascending=True)
                )
        combination_ranking = (
            frame.sort_values(
                ["combination_factor_score", "volatility", "turnover_ma20"],
                ascending=[False, True, False],
            )
            .head(cfg.integer("low_price_multi_factor", "ranking_limit"))
            .reset_index(drop=True)
        )
        combination_ranking["factor_rank"] = combination_ranking.index + 1
        self.last_combination_ranking = combination_ranking[
            ["symbol", "factor_rank", "combination_factor_score"]
        ].copy()
        ranked = (
            frame.sort_values(["score", "momentum", "turnover_ma20"], ascending=[False, False, False])
            .head(limit)
            .reset_index(drop=True)
        )
        ranked["rank"] = ranked.index + 1
        return ranked
