"""海龟交易策略：20日新高突破 + 成交额过亿 + 动量阳线过滤。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class TurtleTradeStrategy(BaseStrategy):
    """海龟交易策略（A股防诱多改良版）。

    选股条件（向量化，严禁 iterrows）：
    1. 突破新高：今日 close > 前20个交易日 high 的最大值
    2. 流动性：今日 turnover > 100,000,000
    3. 防诱多过滤：今日必须是实体阳线（今日 close > 今日 open），且必须真涨（今日 close > 昨日 close）

    Attributes:
        webhook_key: 路由到原始策略群。
    """

    webhook_key: str = "original_strategies"
    _MIN_BARS: int = 21  # 至少需要 21 根 K 线（20日窗口 + 当日）

    def _run(self) -> list[str]:
        """
        遍历全市场，返回满足海龟突破条件的股票代码列表。
        """
        cfg = self.engine.thresholds
        min_bars = cfg.integer("turtle_trade", "min_bars")
        breakout_days = cfg.integer("turtle_trade", "breakout_days")
        min_turnover = cfg.number("turtle_trade", "min_turnover")
        symbols = self.get_eligible_symbols()
        candidates: list[tuple[str, float, float, float]] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < min_bars:
                    continue

                # 向量化：前20日 high 的滚动最大值（不含当日，shift(1) 后取 rolling(20)）
                df["high_20"] = df["high"].shift(1).rolling(breakout_days).max()
                df["ma20"] = df["close"].rolling(20).mean()
                df["vol20"] = df["volume"].rolling(20).mean()

                last = df.iloc[-1]
                prev = df.iloc[-2]  # 获取昨日数据，用于对比

                if pd.isna(last["high_20"]):
                    continue

                # 核心条件 1：突破前 20 天最高点
                breakout = last["close"] > last["high_20"]
                # 核心条件 2：流动性过亿
                liquid = last["turnover"] > min_turnover

                # 【新增防守条件】拒绝郑州煤电式的高开低走大阴线！
                is_yang = last["close"] > last["open"]   # 实体必须是阳线（红柱）
                is_up = last["close"] > prev["close"]    # 必须是真涨，不能是假阳线
                volume_ratio = last["volume"] / last["vol20"] if last["vol20"] else 0
                volume_confirmed = (
                    cfg.number("turtle_trade", "min_volume_ratio")
                    <= volume_ratio
                    <= cfg.number("turtle_trade", "max_volume_ratio")
                )
                not_extended = (
                    last["close"] <= last["high_20"] * (1 + cfg.number("turtle_trade", "max_breakout_extension"))
                    and last["close"] <= last["ma20"] * (1 + cfg.number("turtle_trade", "max_ma20_deviation"))
                )
                day_range = max(float(last["high"] - last["low"]), 1e-9)
                upper_shadow_ok = (
                    float(last["high"] - last["close"]) / day_range
                    <= cfg.number("turtle_trade", "max_upper_shadow_ratio")
                )

                if breakout and liquid and is_yang and is_up and volume_confirmed and not_extended and upper_shadow_ok:
                    breakout_strength = float(last["close"] / last["high_20"] - 1)
                    close_location = float((last["close"] - last["low"]) / day_range)
                    candidates.append(
                        (symbol, close_location, -breakout_strength, float(last["turnover"]))
                    )

            except Exception as exc:
                logger.warning(f"[{symbol}] TurtleTradeStrategy 计算失败：{exc}")
                continue

        # 使用本地可复现的信号质量排序：收盘位置、不过度追高、成交额。
        candidates.sort(key=lambda item: (item[1], item[2], item[3]), reverse=True)
        selected = [item[0] for item in candidates]

        logger.info(f"TurtleTradeStrategy 选出 {len(selected)} 只股票")
        return selected
