"""高旗形整理策略：强动量后极度收敛缩量。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class HighTightFlagStrategy(BaseStrategy):
    """高旗形整理策略。

    选股条件（向量化，严禁 iterrows）：
    1. 强动量：过去40天区间最高价 / 区间最低价 > 1.6（涨幅超60%）
    2. 极度收敛：最近10天区间最高价 / 区间最低价 < 1.15（振幅低于15%）
    3. 缩量：今日 volume < 过去20日 volume 均值的 0.6 倍

    Attributes:
        webhook_key: 路由到原始策略群。
    """

    webhook_key: str = "original_strategies"
    _MIN_BARS: int = 40  # 至少需要 40 根 K 线

    def _run(self) -> list[str]:
        """
        遍历全市场，返回满足高旗形整理条件的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        cfg = self.engine.thresholds
        min_bars = cfg.integer("high_tight_flag", "min_bars")
        momentum_days = cfg.integer("high_tight_flag", "momentum_days")
        consolidation_days = cfg.integer("high_tight_flag", "consolidation_days")
        volume_days = cfg.integer("high_tight_flag", "volume_days")
        symbols = self.get_eligible_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < min_bars:
                    continue

                # 向量化计算各窗口指标
                tail40 = df.tail(momentum_days)
                tail10 = df.tail(consolidation_days)

                high40 = tail40["high"].max()
                low40 = tail40["low"].min()
                high10 = tail10["high"].max()
                low10 = tail10["low"].min()

                if low40 == 0 or low10 == 0:
                    continue

                # 条件 1：强动量
                momentum = high40 / low40 > cfg.number("high_tight_flag", "momentum_ratio")
                # 条件 2：极度收敛
                consolidation = high10 / low10 < cfg.number("high_tight_flag", "consolidation_ratio")
                # 条件 3：高位抗跌（近10天最低点不得低于40天最高点的80%）
                high_level = low10 >= high40 * cfg.number("high_tight_flag", "high_level_ratio")
                # 条件 4：缩量（向量化均值）
                vol_ma20 = df["volume"].iloc[-(volume_days + 1):-1].mean()
                # setup 形成后还必须在当日真正突破昨日可见的整理平台。
                prior_flag_high = df["high"].iloc[-(consolidation_days + 1):-1].max()
                trigger = df["close"].iloc[-1] > prior_flag_high
                volume_recovery = df["volume"].iloc[-1] >= vol_ma20 * cfg.number(
                    "high_tight_flag", "trigger_volume_ratio"
                )
                day_range = max(float(df["high"].iloc[-1] - df["low"].iloc[-1]), 1e-9)
                upper_shadow_ok = (
                    float(df["high"].iloc[-1] - df["close"].iloc[-1]) / day_range
                    <= cfg.number("high_tight_flag", "max_upper_shadow_ratio")
                )

                # 整理期整体缩量，触发日允许量能恢复；因此缩量使用触发日前一日。
                setup_shrink = df["volume"].iloc[-2] < vol_ma20 * cfg.number(
                    "high_tight_flag", "shrink_volume_ratio"
                )
                if momentum and consolidation and high_level and setup_shrink and trigger and volume_recovery and upper_shadow_ok:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] HighTightFlagStrategy 计算失败：{exc}")
                continue

        logger.info(f"HighTightFlagStrategy 选出 {len(selected)} 只股票")
        return selected
