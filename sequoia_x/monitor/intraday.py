"""盘中实时监控：独立于日K决策层，只做预警和尾盘确认。"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine

logger = get_logger(__name__)


@dataclass(frozen=True)
class IntradayQuote:
    symbol: str
    price: float
    previous_close: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    quote_time: str

    @property
    def change_pct(self) -> float:
        return self.price / self.previous_close - 1 if self.previous_close > 0 else 0.0

    @property
    def vwap(self) -> float:
        return self.amount / self.volume if self.volume > 0 else self.price


@dataclass(frozen=True)
class IntradayAlert:
    symbol: str
    name: str
    level: str
    alert_type: str
    price: float
    message: str
    quote_time: str


class IntradayMonitor:
    """读取前一日综合趋势快照，监控持仓、自选和高分候选。"""

    def __init__(
        self,
        engine: DataEngine,
        settings: Settings,
        quote_fetcher: Callable[[str], IntradayQuote | None] | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.quote_fetcher = quote_fetcher or self.fetch_quote
        self.latest_universe_sources: dict[str, set[str]] = {}
        self.latest_names: dict[str, str] = {}
        self.latest_prices: dict[str, float] = {}

    @staticmethod
    def fetch_quote(symbol: str) -> IntradayQuote | None:
        """从腾讯行情读取实时量价；所有结果只在内存中使用。"""
        exchange = "sh" if symbol.startswith(("6", "9")) else "sz"
        try:
            response = requests.get(
                f"https://qt.gtimg.cn/q={exchange}{symbol}",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=10,
            )
            response.raise_for_status()
            fields = response.content.decode("gbk").split('="', 1)[1].split("~")
            # 腾讯成交量字段为手、成交额字段为万元，统一换算为股和元。
            quote = IntradayQuote(
                symbol=symbol,
                price=float(fields[3]),
                previous_close=float(fields[4]),
                open=float(fields[5]),
                volume=float(fields[36]) * 100,
                amount=float(fields[37]) * 10_000,
                high=float(fields[33]),
                low=float(fields[34]),
                quote_time=datetime.strptime(fields[30], "%Y%m%d%H%M%S").isoformat(),
            )
            return quote if quote.price > 0 and quote.previous_close > 0 else None
        except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
            logger.warning(f"[{symbol}] 盘中详细行情获取失败：{exc}")
            return None

    def _load_snapshot(self) -> dict:
        path = Path(self.settings.comprehensive_snapshot_path)
        if not path.exists():
            raise RuntimeError("缺少综合趋势快照，请先在收盘后运行一次日常策略")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_portfolio(self) -> pd.DataFrame:
        path = Path(self.settings.portfolio_csv_path)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_csv(path, dtype={"symbol": str})

    def _load_strategy_symbols(self) -> set[str]:
        path = Path(self.settings.strategy_selection_path)
        if not path.exists():
            return set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {
                str(symbol).zfill(6)
                for symbols in payload.get("strategies", {}).values()
                for symbol in symbols
            }
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            logger.warning("策略选股池读取失败，本次仅监控综合趋势快照与组合")
            return set()

    @staticmethod
    def _elapsed_volume_ratio(quote_time: datetime) -> float:
        """按A股交易时间估计截至当前应完成的全天成交量比例。"""
        minute = quote_time.hour * 60 + quote_time.minute
        if minute <= 9 * 60 + 30:
            return 0.03
        if minute <= 11 * 60 + 30:
            return max(0.03, min(0.50, 0.03 + (minute - 570) / 120 * 0.47))
        if minute < 13 * 60:
            return 0.50
        if minute <= 15 * 60:
            return min(1.0, 0.50 + (minute - 780) / 120 * 0.50)
        return 1.0

    def _average_volume20(self, symbol: str) -> float:
        df = self.engine.get_ohlcv(symbol)
        if len(df) < 20:
            return 0.0
        return float(pd.to_numeric(df["volume"], errors="coerce").tail(20).mean())

    @staticmethod
    def _portfolio_maps(portfolio: pd.DataFrame) -> tuple[set[str], dict[str, float]]:
        if portfolio.empty:
            return set(), {}
        portfolio = portfolio.copy()
        portfolio["symbol"] = portfolio["symbol"].astype(str).str.zfill(6)
        shares = pd.to_numeric(portfolio.get("shares", 0), errors="coerce").fillna(0)
        holdings = set(portfolio.loc[shares > 0, "symbol"])
        costs = pd.to_numeric(portfolio.get("cost_price"), errors="coerce")
        cost_map = {
            str(symbol): float(cost)
            for symbol, cost in zip(portfolio["symbol"], costs, strict=True)
            if pd.notna(cost) and float(cost) > 0
        }
        return holdings, cost_map

    def _load_sent_keys(self, trading_date: str) -> set[str]:
        path = Path(self.settings.intraday_alert_state_path)
        if not path.exists():
            return set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return set(payload.get("keys", [])) if payload.get("date") == trading_date else set()
        except (json.JSONDecodeError, OSError):
            return set()

    def _save_sent_keys(self, trading_date: str, keys: set[str]) -> None:
        path = Path(self.settings.intraday_alert_state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"date": trading_date, "keys": sorted(keys)}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def run(self) -> list[IntradayAlert]:
        snapshot = self._load_snapshot()
        assessments = {
            item["symbol"]: item
            for item in snapshot.get("assessments", [])
            if float(item.get("score", 0)) >= 65 or item.get("entry_signal") != "等待确认"
        }
        portfolio = self._load_portfolio()
        strategy_symbols = self._load_strategy_symbols()
        holdings, costs = self._portfolio_maps(portfolio)
        watchlist = set()
        if not portfolio.empty:
            flags = portfolio.get("is_watchlist", False).astype(str).str.lower().isin({"true", "1"})
            watchlist = set(portfolio.loc[flags, "symbol"].astype(str).str.zfill(6))
        universe = sorted(set(assessments) | strategy_symbols | holdings | watchlist)
        names = self.engine.get_stock_names(universe)
        self.latest_universe_sources = {
            symbol: (
                ({"策略"} if symbol in assessments or symbol in strategy_symbols else set())
                | ({"持仓"} if symbol in holdings else set())
                | ({"自选"} if symbol in watchlist else set())
            )
            for symbol in universe
        }
        self.latest_names = names
        self.latest_prices = {}
        alerts: list[IntradayAlert] = []
        quote_dates: set[str] = set()

        for symbol in universe:
            quote = self.quote_fetcher(symbol)
            if quote is None:
                continue
            self.latest_prices[symbol] = quote.price
            quote_dt = datetime.fromisoformat(quote.quote_time)
            quote_dates.add(quote_dt.date().isoformat())
            assessment = assessments.get(symbol, {})
            stop_price = float(assessment.get("stop_price", 0) or 0)
            score = float(assessment.get("score", 0) or 0)
            entry = str(assessment.get("entry_signal", "等待确认"))
            average_volume = self._average_volume20(symbol)
            elapsed = self._elapsed_volume_ratio(quote_dt)
            projected_ratio = quote.volume / elapsed / average_volume if average_volume > 0 else 0.0
            below_vwap = quote.price < quote.vwap

            def add(level: str, alert_type: str, message: str) -> None:
                alerts.append(
                    IntradayAlert(
                        symbol=symbol,
                        name=names.get(symbol, symbol),
                        level=level,
                        alert_type=alert_type,
                        price=quote.price,
                        message=message,
                        quote_time=quote.quote_time,
                    )
                )

            if symbol in holdings and stop_price > 0 and quote.price <= stop_price:
                add("高", "硬止损", f"实时价跌破日线计划止损价 {stop_price:.3f}")
            cost = costs.get(symbol)
            if symbol in holdings and cost and quote.price / cost - 1 <= -0.08:
                add("高", "持仓亏损", f"相对持仓成本亏损已达 {quote.price / cost - 1:.1%}")
            if projected_ratio >= 1.8 and quote.change_pct <= -0.03 and below_vwap:
                add(
                    "高" if symbol in holdings else "中",
                    "放量下跌",
                    f"预计全天量比 {projected_ratio:.2f}，涨跌幅 {quote.change_pct:+.1%}，低于VWAP",
                )
            if score >= 65 and quote.price >= quote.high * 0.995 and projected_ratio >= 1.5:
                add("中", "盘中突破候选", f"日线评分 {score:.1f}，预计全天量比 {projected_ratio:.2f}")
            if (
                symbol not in assessments
                and symbol in (strategy_symbols | watchlist)
                and quote.change_pct >= 0.02
                and quote.price >= quote.vwap
                and projected_ratio >= 1.3
            ):
                add(
                    "中",
                    "盘中走强",
                    f"自选/策略标的涨幅 {quote.change_pct:+.1%}，预计全天量比 {projected_ratio:.2f}",
                )
            is_tail = quote_dt.hour == 14 and quote_dt.minute >= 45
            if is_tail and entry != "等待确认" and quote.price >= quote.vwap:
                add("中", "尾盘买点确认", f"前一日信号 {entry}，实时价仍在VWAP上方")

        trading_date = max(quote_dates) if quote_dates else datetime.now().date().isoformat()
        sent = self._load_sent_keys(trading_date)
        fresh = [item for item in alerts if f"{item.symbol}:{item.alert_type}" not in sent]
        sent.update(f"{item.symbol}:{item.alert_type}" for item in fresh)
        self._save_sent_keys(trading_date, sent)
        logger.info(f"盘中监控 {len(universe)} 只股票，产生 {len(fresh)} 条新预警")
        return fresh
