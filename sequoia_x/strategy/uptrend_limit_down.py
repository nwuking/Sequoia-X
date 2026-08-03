"""上升趋势跌停策略：趋势中放量跌停，捕捉错杀机会。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.core.market_rules import daily_price_limits
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class UptrendLimitDownStrategy(BaseStrategy):
    """上升趋势跌停策略。

    选股条件（向量化，严禁 iterrows）：
    1. 处于上升趋势：昨日20日均线 > 昨日60日均线
    2. 放量跌停：今日 close <= 昨日 close * 0.905
                且今日 volume > 20日均量的 2.0 倍

    Attributes:
        webhook_key: 路由到原始策略群。
    """

    webhook_key: str = "original_strategies"
    _MIN_BARS: int = 60  # 至少需要 60 根 K 线（60日均线）

    def _run(self) -> list[str]:
        """
        遍历全市场，返回满足上升趋势跌停条件的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        cfg = self.engine.thresholds
        short_ma = cfg.integer("uptrend_limit_down", "short_ma")
        long_ma = cfg.integer("uptrend_limit_down", "long_ma")
        symbols = self.get_eligible_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < cfg.integer("uptrend_limit_down", "min_bars"):
                    continue

                # 向量化计算均线
                df["ma20"] = df["close"].rolling(short_ma).mean()
                df["ma60"] = df["close"].rolling(long_ma).mean()
                df["vol_ma20"] = df["volume"].rolling(short_ma).mean()

                prev = df.iloc[-2]  # 昨日
                today = df.iloc[-1]  # 今日

                if pd.isna(prev["ma20"]) or pd.isna(prev["ma60"]) or pd.isna(today["vol_ma20"]):
                    continue

                # 条件 1：上升趋势（昨日均线多头排列）
                uptrend = prev["ma20"] > prev["ma60"]
                # 条件 2：放量跌停
                previous_close = float(prev.get("raw_close", prev["close"]))
                today_close = float(today.get("raw_close", today["close"]))
                limits = daily_price_limits(symbol, previous_close)
                limit_down = limits.lower is not None and today_close <= limits.lower
                volume_surge = today["volume"] > today["vol_ma20"] * cfg.number(
                    "uptrend_limit_down", "volume_ratio"
                )

                if uptrend and limit_down and volume_surge:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] UptrendLimitDownStrategy 计算失败：{exc}")
                continue

        logger.info(f"UptrendLimitDownStrategy 选出 {len(selected)} 只股票")
        return selected
