import pandas as pd
import sqlite3
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略"""

    webhook_key: str = "original_strategies"
    rps_period: int = 120
    rps_threshold: int = 90

    def _run(self) -> list[str]:
        cfg = self.engine.thresholds
        rps_period = cfg.integer("rps_breakout", "period")
        medium_period = cfg.integer("rps_breakout", "rps_period_medium")
        short_period = cfg.integer("rps_breakout", "rps_period_short")
        rps_threshold = cfg.number("rps_breakout", "rps_threshold")
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                df = pd.read_sql(
                    "SELECT symbol, date, close, high, volume, turnover FROM stock_daily", conn
                )
        except Exception as exc:
            logger.error(f"读取数据库失败: {exc}")
            return []

        if df.empty:
            return []

        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values(['symbol', 'date'])

        groups = df.groupby('symbol')
        for period in (short_period, medium_period, rps_period):
            df[f'ret_{period}'] = groups['close'].pct_change(period, fill_method=None)
        df['ma20'] = groups['close'].transform(lambda item: item.rolling(20).mean())
        df['vol20'] = groups['volume'].transform(lambda item: item.rolling(20).mean())
        df['turnover20'] = groups['turnover'].transform(lambda item: item.rolling(20).mean())

        latest_date = df['date'].max()
        latest_df = df[df['date'] == latest_date].copy()
        latest_df = latest_df.dropna(subset=[f'ret_{rps_period}', f'ret_{medium_period}', f'ret_{short_period}'])

        # 横向排位 (RPS)
        for period in (short_period, medium_period, rps_period):
            latest_df[f'rps_{period}'] = latest_df[f'ret_{period}'].rank(pct=True) * 100
        strong_stocks = latest_df[
            (latest_df[f'rps_{rps_period}'] >= rps_threshold)
            & (latest_df[f'rps_{medium_period}'] >= cfg.number("rps_breakout", "rps_threshold_medium"))
            & (latest_df[f'rps_{short_period}'] >= cfg.number("rps_breakout", "rps_threshold_short"))
        ].copy()

        # 计算滚动最高价
        roll_high = df.groupby('symbol')['high'].transform(
            lambda item: item.shift(1).rolling(
                window=rps_period,
                min_periods=max(
                    1,
                    int(rps_period * cfg.number("rps_breakout", "min_period_ratio")),
                ),
            ).max()
        )
        df['roll_high'] = roll_high

        latest_roll_high = df[df['date'] == latest_date][['symbol', 'roll_high']]
        strong_stocks = strong_stocks.merge(latest_roll_high, on='symbol')

        volume_ratio = strong_stocks['volume'] / strong_stocks['vol20']
        selected = strong_stocks[
            (strong_stocks['close'] > strong_stocks['roll_high'])
            & (strong_stocks['turnover20'] >= cfg.number("rps_breakout", "min_turnover"))
            & volume_ratio.between(
                cfg.number("rps_breakout", "min_volume_ratio"),
                cfg.number("rps_breakout", "max_volume_ratio"),
            )
            & (strong_stocks['close'] <= strong_stocks['ma20'] * (1 + cfg.number("rps_breakout", "max_ma20_deviation")))
        ].sort_values(
            [f'rps_{rps_period}', f'rps_{medium_period}', 'turnover20'], ascending=False
        )

        logger.info(f"RpsBreakoutStrategy 选出 {len(selected)} 只股票")
        return selected['symbol'].tolist()
