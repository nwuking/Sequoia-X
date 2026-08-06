"""A股综合趋势策略：状态识别、量化评分、买卖点与风险控制。"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.core.thresholds import ThresholdConfig
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


@dataclass(frozen=True)
class TrendAssessment:
    """单只股票在最新交易日的完整策略判断。"""

    symbol: str
    score: float
    regime: str
    entry_signal: str
    exit_signal: str
    close: float
    stop_price: float
    position_risk_pct: float
    reasons: tuple[str, ...]


class ComprehensiveTrendStrategy(BaseStrategy):
    """整合趋势、量价、动能、相对强度和风险的日线波段策略。

    “吸筹、洗盘、撤离”均是量价行为的概率标签，不代表能够识别真实账户身份。
    run() 只返回出现明确买点且综合分不低于 65 分的股票；完整判断保存在
    last_assessments，供终端、通知和后续组合管理使用。
    """

    webhook_key = "core_decision"
    min_history = 130
    max_candidates = 30

    def __init__(self, engine, settings) -> None:
        super().__init__(engine, settings)
        self.last_assessments: list[TrendAssessment] = []

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        result = 100 - 100 / (1 + rs)
        return result.fillna(100).clip(0, 100)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        previous_close = df["close"].shift(1)
        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - previous_close).abs(),
                (df["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return true_range.ewm(alpha=1 / period, adjust=False).mean()

    @classmethod
    def _indicators(cls, source: pd.DataFrame) -> pd.DataFrame:
        df = source.sort_values("date").copy()
        for column in ("open", "high", "low", "close", "volume", "turnover"):
            if column in df:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        close = df["close"]
        volume = df["volume"]
        for period in (5, 10, 20, 60, 120):
            df[f"ma{period}"] = close.rolling(period).mean()
        df["vol5"] = volume.rolling(5).mean()
        df["vol20"] = volume.rolling(20).mean()
        df["ema12"] = close.ewm(span=12, adjust=False).mean()
        df["ema26"] = close.ewm(span=26, adjust=False).mean()
        df["dif"] = df["ema12"] - df["ema26"]
        df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = (df["dif"] - df["dea"]) * 2
        df["rsi"] = cls._rsi(close)
        df["atr"] = cls._atr(df)
        df["boll_mid"] = close.rolling(20).mean()
        boll_std = close.rolling(20).std()
        df["boll_width"] = 4 * boll_std / df["boll_mid"].replace(0, np.nan)
        direction = np.sign(close.diff()).fillna(0)
        df["obv"] = (direction * volume.fillna(0)).cumsum()
        df["high20_prev"] = df["high"].shift(1).rolling(20).max()
        df["high60_prev"] = df["high"].shift(1).rolling(60).max()
        df["high120_prev"] = df["high"].shift(1).rolling(120).max()
        df["low20_prev"] = df["low"].shift(1).rolling(20).min()
        df["ret20"] = close.pct_change(20, fill_method=None)
        df["ret60"] = close.pct_change(60, fill_method=None)
        df["obv60_prev"] = df["obv"].shift(1).rolling(60).max()
        return df

    @staticmethod
    def entry_flags(
        df: pd.DataFrame,
        market_strong: bool | pd.Series,
        thresholds: ThresholdConfig | None = None,
    ) -> pd.DataFrame:
        """返回 A/B/C 三类买点布尔序列，供实时评估和回测共同调用。"""
        cfg = thresholds or ThresholdConfig("config/thresholds.ini")
        body_range = (df["high"] - df["low"]).clip(lower=1e-9)
        if isinstance(market_strong, pd.Series):
            strong = market_strong.reindex(df.index).fillna(False).astype(bool)
        else:
            strong = pd.Series(bool(market_strong), index=df.index)
        breakout = (
            (df["close"] >= df["high60_prev"])
            & (
                df["volume"]
                >= df["vol20"]
                * cfg.number("comprehensive_trend", "breakout_volume_ratio")
            )
            & (
                (df["high"] - df["close"]) / body_range
                <= cfg.number("comprehensive_trend", "breakout_upper_shadow_ratio")
            )
            & strong
        )
        pullback = (
            (df["close"] > df["ma60"])
            & (
                df["low"]
                <= df[["ma10", "ma20"]].max(axis=1)
                * cfg.number("comprehensive_trend", "pullback_price_buffer")
            )
            & (df["close"] >= df["ma20"])
            & (
                df["volume"]
                <= df["vol20"]
                * cfg.number("comprehensive_trend", "pullback_volume_ratio")
            )
            & (df["close"] > df["close"].shift(1))
        )
        recovery = (
            (df["close"] > df["ma60"])
            & (df["close"].shift(1) <= df["ma10"].shift(1))
            & (df["close"] > df["ma10"])
            & (df["dif"] > df["dif"].shift(1))
            & (df["volume"] >= df["vol5"])
            & (df["close"] >= df["high"].shift(1).rolling(5).max())
        )
        return pd.DataFrame(
            {"entry_a": breakout, "entry_b": pullback, "entry_c": recovery},
            index=df.index,
        ).fillna(False)

    @staticmethod
    def _market_context(
        frames: dict[str, pd.DataFrame],
        thresholds: ThresholdConfig | None = None,
    ) -> dict[str, float | bool]:
        cfg = thresholds or ThresholdConfig("config/thresholds.ini")
        returns = []
        latest_above20: list[bool] = []
        for frame in frames.values():
            if len(frame) < 61:
                continue
            series = frame.set_index(pd.to_datetime(frame["date"]))["close"].pct_change()
            returns.append(series.rename(len(returns)))
            ma20 = frame["close"].rolling(20).mean().iloc[-1]
            latest_above20.append(bool(frame["close"].iloc[-1] > ma20))
        if not returns:
            return {"score": 0.0, "ret20": 0.0, "strong": False, "breadth": 0.0}
        market_returns = pd.concat(returns, axis=1).mean(axis=1, skipna=True).dropna()
        proxy = (1 + market_returns).cumprod()
        ma20 = proxy.rolling(20).mean()
        ma60 = proxy.rolling(60).mean()
        breadth = float(np.mean(latest_above20)) if latest_above20 else 0.0
        score = 0.0
        if len(proxy) >= 60:
            score += 5 if proxy.iloc[-1] > ma20.iloc[-1] else 0
            score += 5 if ma20.iloc[-1] > ma20.iloc[-6] else 0
            score += 5 if proxy.iloc[-1] > ma60.iloc[-1] else 0
            score += 5 if breadth >= cfg.number(
                "comprehensive_trend", "market_breadth_threshold"
            ) else 0
        ret20 = float(proxy.pct_change(20).iloc[-1]) if len(proxy) > 20 else 0.0
        return {
            "score": score,
            "ret20": ret20,
            "strong": score >= cfg.number("comprehensive_trend", "market_strong_score"),
            "breadth": breadth,
        }

    @staticmethod
    def _classify(
        df: pd.DataFrame,
        thresholds: ThresholdConfig | None = None,
    ) -> str:
        cfg = thresholds or ThresholdConfig("config/thresholds.ini")
        last, prev = df.iloc[-1], df.iloc[-2]
        recent = df.iloc[-20:]
        up_volume = recent.loc[recent["close"].diff() > 0, "volume"].mean()
        down_volume = recent.loc[recent["close"].diff() < 0, "volume"].mean()
        up_volume = 0.0 if pd.isna(up_volume) else float(up_volume)
        down_volume = 0.0 if pd.isna(down_volume) else float(down_volume)
        drawdown60 = float(last["close"] / df["high"].iloc[-60:].max() - 1)
        range40 = float(df["high"].iloc[-40:].max() / df["low"].iloc[-40:].min() - 1)
        shrinking_band = last["boll_width"] < df["boll_width"].iloc[-20:-5].median()
        obv_holds = last["obv"] >= df["obv"].iloc[-20:].min()

        if (
            last["close"] > last["ma20"] > last["ma60"]
            and last["ma20"] > df["ma20"].iloc[-6]
            and last["ma60"] > df["ma60"].iloc[-6]
            and last["dif"] > last["dea"]
            and last["rsi"] >= cfg.number("comprehensive_trend", "uptrend_rsi")
        ):
            return "主升浪" if last["close"] >= last["high60_prev"] else "上升趋势"
        if (
            last["close"] < last["ma20"] < last["ma60"]
            and last["ma20"] < df["ma20"].iloc[-6]
            and last["ma60"] < df["ma60"].iloc[-6]
        ):
            if last["rsi"] > prev["rsi"] and last["dif"] > prev["dif"]:
                return "下跌反弹"
            return "阴跌趋势"
        if (
            drawdown60 < cfg.number("comprehensive_trend", "accumulation_drawdown")
            and range40 <= cfg.number("comprehensive_trend", "accumulation_range")
            and abs(last["ma20"] / df["ma20"].iloc[-6] - 1)
            <= cfg.number("comprehensive_trend", "accumulation_ma_slope")
            and shrinking_band
            and up_volume
            >= down_volume * cfg.number("comprehensive_trend", "accumulation_volume_balance")
            and obv_holds
        ):
            return "底部吸筹候选"
        if (
            last["close"] > last["ma60"]
            and last["ma60"] > df["ma60"].iloc[-6]
            and last["volume"] < last["vol20"]
            and last["rsi"] >= cfg.number("comprehensive_trend", "wash_rsi_floor")
            and obv_holds
        ):
            return "洗盘候选"
        high_zone = last["close"] >= df["high"].iloc[-60:].max() * cfg.number(
            "comprehensive_trend", "distribution_high_zone"
        )
        distribution = (
            high_zone
            and down_volume > up_volume
            and last["close"] < last["ma20"]
            and last["obv"]
            < df["obv"].iloc[-20:].max()
            * cfg.number("comprehensive_trend", "distribution_obv_ratio")
        )
        if distribution:
            return "主力撤离候选"
        return "震荡整理"

    @classmethod
    def assess(
        cls,
        symbol: str,
        source: pd.DataFrame,
        market: dict[str, float | bool],
        name: str = "",
        financial: pd.Series | None = None,
        thresholds: ThresholdConfig | None = None,
    ) -> TrendAssessment | None:
        cfg = thresholds or ThresholdConfig("config/thresholds.ini")
        if len(source) < cfg.integer("comprehensive_trend", "min_history"):
            return None
        df = cls._indicators(source)
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last[["ma120", "vol20", "atr", "high120_prev"]]).any():
            return None

        raw = float(market["score"])
        reasons: list[str] = []
        trend_checks = [
            (last["close"] > last["ma20"], "站上MA20"),
            (last["close"] > last["ma60"], "站上MA60"),
            (last["ma20"] > last["ma60"], "MA20高于MA60"),
            (last["ma20"] > df["ma20"].iloc[-6], "MA20向上"),
            (last["ma60"] > df["ma60"].iloc[-6], "MA60向上"),
            (
                last["high"] > df["high"].iloc[-20:-1].max()
                and last["low"] > df["low"].iloc[-20:-1].min(),
                "高低点抬升",
            ),
        ]
        for passed, reason in trend_checks:
            if passed:
                raw += 5
                reasons.append(reason)

        positive_days = df["close"].diff() > 0
        recent10 = df.iloc[-10:]
        recent_up = recent10.loc[positive_days.iloc[-10:], "volume"].mean()
        recent_down = recent10.loc[~positive_days.iloc[-10:], "volume"].mean()
        volume_checks = [
            (last["close"] >= last["high60_prev"], "突破60日高点"),
            (last["volume"] >= last["vol20"] * 1.5, "突破放量"),
            (last["obv"] >= last["obv60_prev"], "OBV创新高"),
            (pd.notna(recent_up) and pd.notna(recent_down) and recent_up > recent_down, "上涨量占优"),
            (last["volume"] < last["vol20"] and last["close"] >= last["ma20"] * 0.98, "缩量回踩"),
        ]
        for passed, reason in volume_checks:
            if passed:
                raw += 5
                reasons.append(reason)

        relative20 = float(last["ret20"] - float(market["ret20"]))
        momentum_checks = [
            (last["dif"] > last["dea"] and last["dif"] > 0, "MACD零轴上多头"),
            (55 <= last["rsi"] <= 75, "RSI强势未过热"),
            (relative20 > 0, "20日跑赢市场"),
        ]
        for passed, reason in momentum_checks:
            if passed:
                raw += 5
                reasons.append(reason)

        # 原规则正向项目合计90分，先归一到100，再执行风险扣分。
        score = raw / 90 * 100
        risk_deduction = 0.0
        deviation = float(last["close"] / last["ma20"] - 1)
        upper_shadow = float(last["high"] - max(last["open"], last["close"]))
        body_range = max(float(last["high"] - last["low"]), 1e-9)
        high_volume_shadow = (
            last["volume"]
            >= last["vol20"] * cfg.number("comprehensive_trend", "high_volume_ratio")
            and upper_shadow / body_range
            >= cfg.number("comprehensive_trend", "upper_shadow_ratio")
        )
        if deviation > cfg.number("comprehensive_trend", "deviation_risk"):
            risk_deduction += 5
            reasons.append("风险：偏离MA20超过15%")
        if high_volume_shadow:
            risk_deduction += 10
            reasons.append("风险：高量长上影")
        if last["close"] < last["low20_prev"]:
            risk_deduction += 10
            reasons.append("风险：跌破20日平台")
        if "ST" in name.upper():
            risk_deduction += 30
            reasons.append("风险：ST股票")
        if financial is not None:
            profit_yoy = pd.to_numeric(financial.get("net_profit_yoy"), errors="coerce")
            roe = pd.to_numeric(financial.get("roe"), errors="coerce")
            if (
                pd.notna(profit_yoy)
                and profit_yoy
                <= cfg.number("comprehensive_trend", "financial_profit_yoy_floor")
            ) or (
                pd.notna(roe)
                and roe < cfg.number("comprehensive_trend", "financial_roe_floor")
            ):
                risk_deduction += 10
                reasons.append("风险：财务质量显著走弱")
        score = round(max(0.0, min(100.0, score - risk_deduction)), 1)

        entry_flags = cls.entry_flags(df, bool(market["strong"]), thresholds=cfg).iloc[-1]
        breakout = bool(entry_flags["entry_a"])
        pullback = bool(entry_flags["entry_b"])
        recovery = bool(entry_flags["entry_c"])
        if breakout and score >= cfg.number("comprehensive_trend", "entry_score_a"):
            entry_signal = "A-平台放量突破"
        elif pullback and score >= cfg.number("comprehensive_trend", "entry_score_b"):
            entry_signal = "B-突破后缩量回踩"
        elif recovery and score >= cfg.number("comprehensive_trend", "entry_score_c"):
            entry_signal = "C-上升趋势恢复"
        else:
            entry_signal = "等待确认"

        if (
            last["close"] < last["ma60"]
            and last["volume"]
            > last["vol20"] * cfg.number("comprehensive_trend", "exit_volume_ratio")
        ):
            exit_signal = "放量跌破MA60/清仓候选"
        elif last["close"] < last["ma20"] and prev["close"] < prev["ma20"]:
            exit_signal = "连续跌破MA20/减仓候选"
        elif deviation > cfg.number("comprehensive_trend", "deviation_risk") or (
            last["rsi"] >= cfg.number("comprehensive_trend", "overheat_rsi")
            and prev["rsi"] > last["rsi"]
        ):
            exit_signal = "短线过热/分批止盈候选"
        else:
            exit_signal = "趋势持有/观察"

        structural_stop = min(
            float(last["ma20"] * cfg.number("comprehensive_trend", "stop_ma_ratio")),
            float(df["low"].iloc[-10:].min()),
        )
        atr_stop = float(
            last["close"]
            - cfg.number("comprehensive_trend", "atr_stop_multiple") * last["atr"]
        )
        stop_price = round(max(0.01, max(structural_stop, atr_stop)), 3)
        risk_pct = round(max(0.0, float(last["close"] - stop_price) / float(last["close"]) * 100), 2)
        return TrendAssessment(
            symbol=symbol,
            score=score,
            regime=cls._classify(df, thresholds=cfg),
            entry_signal=entry_signal,
            exit_signal=exit_signal,
            close=round(float(last["close"]), 3),
            stop_price=stop_price,
            position_risk_pct=risk_pct,
            reasons=tuple(reasons),
        )

    def _run(self) -> list[str]:
        cfg = self.engine.thresholds
        min_history = cfg.integer("comprehensive_trend", "min_history")
        symbols = self.get_eligible_symbols()
        frames: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) >= min_history:
                    frames[symbol] = df.tail(
                        cfg.integer("comprehensive_trend", "history_limit")
                    ).copy()
            except Exception as exc:
                logger.warning(f"[{symbol}] 综合趋势行情读取失败：{exc}")
        if not frames:
            self.last_assessments = []
            self._save_snapshot(
                [],
                {"score": 0.0, "ret20": 0.0, "strong": False, "breadth": 0.0},
                valid=False,
                reason="本地没有足够的行情数据，综合趋势快照已失效",
            )
            return []

        market = self._market_context(frames, thresholds=cfg)
        names = self.engine.get_stock_names(list(frames))
        financial_df = self.engine.get_latest_financial_factors(list(frames))
        financial_map = (
            financial_df.set_index("symbol").to_dict("index") if not financial_df.empty else {}
        )
        assessments: list[TrendAssessment] = []
        for symbol, frame in frames.items():
            try:
                financial = pd.Series(financial_map[symbol]) if symbol in financial_map else None
                assessment = self.assess(
                    symbol,
                    frame,
                    market,
                    names.get(symbol, ""),
                    financial,
                    thresholds=cfg,
                )
                if assessment is not None:
                    assessments.append(assessment)
            except Exception as exc:
                logger.warning(f"[{symbol}] 综合趋势计算失败：{exc}")
        assessments.sort(key=lambda item: item.score, reverse=True)
        self.last_assessments = assessments
        self._save_snapshot(assessments, market)
        selected = [
            item.symbol
            for item in assessments
            if item.entry_signal != "等待确认"
            and item.score >= cfg.number("comprehensive_trend", "selection_score")
        ][: cfg.integer("comprehensive_trend", "max_candidates")]
        logger.info(
            f"ComprehensiveTrendStrategy 市场宽度={float(market['breadth']):.1%}，"
            f"评估 {len(assessments)} 只，选出 {len(selected)} 只"
        )
        return selected

    def _save_snapshot(
        self,
        assessments: list[TrendAssessment],
        market: dict[str, float | bool],
        valid: bool = True,
        reason: str | None = None,
    ) -> None:
        """保存收盘决策层结果，供盘中监控读取；不会写入或修改日K表。"""
        path = Path(self.settings.comprehensive_snapshot_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "data_date": self.engine.get_latest_date(),
            "valid": valid,
            "reason": reason,
            "market": market,
            "assessments": [asdict(item) for item in assessments],
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        logger.info(f"综合趋势快照已保存：{path}")
