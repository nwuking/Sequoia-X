"""策略基类模块：定义所有选股策略的统一执行与过滤规则。"""

from abc import ABC, abstractmethod
from collections.abc import Iterable

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine

logger = get_logger(__name__)


class BaseStrategy(ABC):
    """选股策略抽象基类。

    所有具体策略必须继承此类并实现 _run() 方法。公开的 run() 会统一
    过滤名称中包含 ST 的股票，避免各策略重复实现或遗漏风险约束。

    Attributes:
        webhook_key: 策略对应的飞书 webhook 标识，用于路由到不同机器人。
            默认为 'default'，将使用 Settings.feishu_webhook_url。
            子类可覆盖此属性以路由到五类精简飞书群。
    """

    webhook_key: str = "original_strategies"

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        """
        初始化策略。

        Args:
            engine: DataEngine 实例，用于读取行情数据。
            settings: Settings 实例，用于读取配置。
        """
        self.engine = engine
        self.settings = settings

    def run(self) -> list[str]:
        """
        执行选股逻辑，并统一排除 ST、*ST、S*ST 等风险警示股票。

        Returns:
            满足策略条件的股票代码列表，如 ['000001', '600519']。
            无选股结果时返回空列表。
        """
        return self.exclude_st_stocks(self._run())

    @abstractmethod
    def _run(self) -> list[str]:
        """执行具体策略的选股逻辑，由子类实现。"""
        ...

    def exclude_st_stocks(self, symbols: Iterable[str]) -> list[str]:
        """按本地股票简称排除 ST 股票，同时保持原有顺序。

        本地没有名称的代码会保留，避免因基础资料暂未同步而误删普通股票。
        """
        candidates = list(symbols)
        if not candidates:
            return []

        names = self.engine.get_stock_names(candidates)
        filtered = [
            symbol
            for symbol in candidates
            if "ST" not in names.get(symbol, "").upper()
        ]
        removed = len(candidates) - len(filtered)
        if removed:
            logger.info(f"{type(self).__name__} 排除 {removed} 只 ST 股票")
        return filtered

    def get_eligible_symbols(self) -> list[str]:
        """返回已排除 ST 股票的本地选股池。"""
        return self.exclude_st_stocks(self.engine.get_local_symbols())
