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
    priority_score: float = 0.0
    candidate_tier: str = "B"
    stop_price: float = 0.0
    atr: float = 0.0
    strategy_family: str = "未知"
    industry: str = "未知"
    market_exposure_limit: float = 0.8


class IntradayMonitor:
    """读取前一日综合趋势快照，监控持仓、自选和高分候选。"""

    def __init__(
        self,
        engine: DataEngine,
        settings: Settings,
        quote_fetcher: Callable[[str], IntradayQuote | None] | None = None,
        paper_positions: dict[str, tuple[int, float] | dict] | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.quote_fetcher = quote_fetcher or self.fetch_quote
        self.paper_positions = paper_positions or {}
        self.latest_universe_sources: dict[str, set[str]] = {}
        self.latest_names: dict[str, str] = {}
        self.latest_prices: dict[str, float] = {}
        self.latest_market_exposure_limit = 0.0

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
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"综合趋势快照损坏，需重新运行日常策略：{exc}") from exc
        if payload.get("valid") is False:
            raise RuntimeError(payload.get("reason") or "综合趋势快照已失效")
        return payload

    def _load_portfolio(self) -> pd.DataFrame:
        path = Path(self.settings.portfolio_csv_path)
        if not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(path, dtype={"symbol": str})
        except (pd.errors.EmptyDataError, pd.errors.ParserError, OSError):
            logger.warning("组合文件为空或损坏，本次按空组合处理")
            return pd.DataFrame()

    def _load_strategy_sources(
        self,
    ) -> tuple[set[str], set[str], dict[str, float], dict[str, str]]:
        path = Path(self.settings.strategy_selection_path)
        if not path.exists():
            return set(), set(), {}, {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            strategy_symbols = {
                str(symbol).zfill(6)
                for symbols in payload.get("strategies", {}).values()
                for symbol in symbols
            }
            combined_symbols = {
                str(symbol).zfill(6)
                for symbol in payload.get("combined", {}).get("focus", [])
            }
            combined_scores = {
                str(item.get("symbol", "")).zfill(6): float(item.get("combined_score", 0) or 0)
                for item in payload.get("combined", {}).get("details", [])
                if item.get("symbol")
            }
            family_map = {
                str(item.get("symbol", "")).zfill(6): ",".join(item.get("families", []))
                for item in payload.get("combined", {}).get("details", [])
                if item.get("symbol")
            }
            return strategy_symbols, combined_symbols, combined_scores, family_map
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            logger.warning("策略选股池读取失败，本次仅监控综合趋势快照与组合")
            return set(), set(), {}, {}

    def _load_industries(self) -> dict[str, str]:
        path = Path(self.settings.stock_industry_csv_path)
        if not path.exists():
            return {}
        try:
            frame = pd.read_csv(path, dtype={"symbol": str})
            if not {"symbol", "industry"}.issubset(frame.columns):
                return {}
            return {
                str(symbol).zfill(6): str(industry)
                for symbol, industry in zip(frame["symbol"], frame["industry"], strict=True)
                if pd.notna(industry) and str(industry).strip()
            }
        except (OSError, pd.errors.ParserError):
            logger.warning("股票行业映射读取失败，本次跳过行业集中度控制")
            return {}

    @staticmethod
    def _paper_position_values(position: tuple[int, float] | dict) -> tuple[int, float]:
        if isinstance(position, dict):
            return int(position.get("shares", 0)), float(position.get("average_cost", 0))
        return int(position[0]), float(position[1])

    def _market_exposure_limit(self, snapshot: dict) -> float:
        market = snapshot.get("market", {})
        cfg = self.engine.thresholds
        if bool(market.get("strong", False)):
            return cfg.number("paper_trading", "strong_market_exposure_ratio")
        if float(market.get("score", 0) or 0) > 0 or float(market.get("breadth", 0) or 0) >= 0.45:
            return cfg.number("paper_trading", "neutral_market_exposure_ratio")
        return cfg.number("paper_trading", "weak_market_exposure_ratio")

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
        cfg = self.engine.thresholds
        snapshot = self._load_snapshot()
        assessments = {
            item["symbol"]: item
            for item in snapshot.get("assessments", [])
            if float(item.get("score", 0)) >= cfg.number("intraday_monitor", "snapshot_score")
            or item.get("entry_signal") != "等待确认"
        }
        portfolio = self._load_portfolio()
        strategy_symbols, focus_symbols, combined_scores, family_map = (
            self._load_strategy_sources()
        )
        industries = self._load_industries()
        self.latest_market_exposure_limit = self._market_exposure_limit(snapshot)
        holdings, costs = self._portfolio_maps(portfolio)
        watchlist = set()
        if not portfolio.empty:
            flags = portfolio.get("is_watchlist", False).astype(str).str.lower().isin({"true", "1"})
            watchlist = set(portfolio.loc[flags, "symbol"].astype(str).str.zfill(6))
        paper_holdings = {
            symbol
            for symbol, position in self.paper_positions.items()
            if self._paper_position_values(position)[0] > 0
        }
        universe_symbols = (
            set(assessments)
            | strategy_symbols
            | focus_symbols
            | holdings
            | watchlist
            | paper_holdings
        )
        universe = sorted(universe_symbols)
        names = self.engine.get_stock_names(universe)
        self.latest_universe_sources = {
            symbol: (
                ({"趋势"} if symbol in assessments else set())
                | ({"重点"} if symbol in focus_symbols else set())
                | ({"策略"} if symbol in strategy_symbols else set())
                | ({"持仓"} if symbol in holdings else set())
                | ({"模拟持仓"} if symbol in paper_holdings else set())
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
            has_exit_risk = str(assessment.get("exit_signal", "")).endswith("候选")
            priority_score = max(score, combined_scores.get(symbol, 0.0))
            candidate_tier = (
                "A" if symbol in focus_symbols else ("B" if symbol in assessments else "C")
            )
            history = self.engine.get_ohlcv(symbol)
            average_volume = (
                float(pd.to_numeric(history["volume"], errors="coerce").tail(20).mean())
                if len(history) >= 20
                else 0.0
            )
            if len(history) >= 15:
                previous_close = pd.to_numeric(history["close"], errors="coerce").shift(1)
                true_range = pd.concat(
                    [
                        history["high"] - history["low"],
                        (history["high"] - previous_close).abs(),
                        (history["low"] - previous_close).abs(),
                    ],
                    axis=1,
                ).max(axis=1)
                atr = float(true_range.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
            else:
                atr = 0.0
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
                        priority_score=priority_score,
                        candidate_tier=candidate_tier,
                        stop_price=stop_price,
                        atr=atr,
                        strategy_family=family_map.get(symbol, "未知"),
                        industry=industries.get(symbol, "未知"),
                        market_exposure_limit=self.latest_market_exposure_limit,
                    )
                )

            has_position = symbol in holdings or symbol in paper_holdings
            if symbol in holdings and stop_price > 0 and quote.price <= stop_price:
                add("高", "硬止损", f"实时价跌破日线计划止损价 {stop_price:.3f}")
            paper_position = self.paper_positions.get(symbol, (0, 0.0))
            paper_shares, paper_cost = self._paper_position_values(paper_position)
            stored_stop = (
                float(paper_position.get("initial_stop_price", 0) or 0)
                if isinstance(paper_position, dict)
                else 0.0
            )
            effective_stop = stop_price if stop_price > 0 else stored_stop
            if paper_shares > 0 and effective_stop > 0 and quote.price <= effective_stop:
                add("高", "硬止损", f"实时价跌破模拟持仓止损价 {effective_stop:.3f}")
            cost = paper_cost if symbol in paper_holdings and paper_cost > 0 else costs.get(symbol)
            if has_position and cost and quote.price / cost - 1 <= cfg.number(
                "intraday_monitor", "holding_stop_loss"
            ):
                add("高", "持仓亏损", f"相对持仓成本亏损已达 {quote.price / cost - 1:.1%}")
            if paper_shares > 0 and isinstance(paper_position, dict):
                highest = max(float(paper_position.get("highest_price", 0) or 0), quote.price)
                profit = quote.price / paper_cost - 1 if paper_cost > 0 else 0.0
                drawdown = quote.price / highest - 1 if highest > 0 else 0.0
                if (
                    highest / paper_cost - 1 >= cfg.number("paper_trading", "trailing_start_profit")
                    and drawdown <= -cfg.number("paper_trading", "trailing_drawdown")
                ):
                    add(
                        "高",
                        "移动止盈",
                        f"持仓最高价回撤 {drawdown:.1%}，当前收益 {profit:+.1%}",
                    )
                if len(history) >= 60:
                    close = pd.to_numeric(history["close"], errors="coerce")
                    ma20 = float(close.tail(20).mean())
                    ma60 = float(close.tail(60).mean())
                    if quote.price <= ma60:
                        add("高", "趋势清仓", f"实时价跌破MA60 {ma60:.3f}")
                    elif quote.price <= ma20:
                        add("中", "趋势减仓", f"实时价跌破MA20 {ma20:.3f}")
                entry_date = str(paper_position.get("entry_date", ""))
                if entry_date and not history.empty:
                    holding_days = int((history["date"].astype(str) >= entry_date).sum())
                    if (
                        holding_days >= cfg.integer("paper_trading", "max_holding_days")
                        and profit < cfg.number("paper_trading", "time_stop_min_return")
                    ):
                        add(
                            "中",
                            "时间止损",
                            f"持有 {holding_days} 个交易日，收益仅 {profit:+.1%}",
                        )
            if (
                projected_ratio >= cfg.number("intraday_monitor", "sell_volume_ratio")
                and quote.change_pct <= cfg.number("intraday_monitor", "sell_change_pct")
                and below_vwap
            ):
                add(
                    "高" if symbol in holdings else "中",
                    "放量下跌",
                    f"预计全天量比 {projected_ratio:.2f}，涨跌幅 {quote.change_pct:+.1%}，低于VWAP",
                )
            if (
                score >= cfg.number("intraday_monitor", "breakout_score")
                and not has_exit_risk
                and quote.price
                >= quote.high * cfg.number("intraday_monitor", "breakout_price_to_high")
                and projected_ratio
                >= cfg.number("intraday_monitor", "breakout_volume_ratio")
            ):
                add("中", "盘中突破候选", f"日线评分 {score:.1f}，预计全天量比 {projected_ratio:.2f}")
            if (
                symbol not in assessments
                and symbol in (strategy_symbols | focus_symbols | watchlist)
                and quote.change_pct >= cfg.number("intraday_monitor", "strong_change_pct")
                and quote.price >= quote.vwap
                and projected_ratio >= cfg.number("intraday_monitor", "strong_volume_ratio")
            ):
                add(
                    "中",
                    "盘中走强",
                    f"自选/策略标的涨幅 {quote.change_pct:+.1%}，预计全天量比 {projected_ratio:.2f}",
                )
            is_tail = (
                quote_dt.hour == cfg.integer("intraday_monitor", "tail_hour")
                and quote_dt.minute >= cfg.integer("intraday_monitor", "tail_minute")
            )
            if (
                is_tail
                and not has_exit_risk
                and entry != "等待确认"
                and quote.price >= quote.vwap
            ):
                add("中", "尾盘买点确认", f"前一日信号 {entry}，实时价仍在VWAP上方")

        trading_date = max(quote_dates) if quote_dates else datetime.now().date().isoformat()
        sent = self._load_sent_keys(trading_date)
        fresh = [item for item in alerts if f"{item.symbol}:{item.alert_type}" not in sent]
        sent.update(f"{item.symbol}:{item.alert_type}" for item in fresh)
        self._save_sent_keys(trading_date, sent)
        logger.info(f"盘中监控 {len(universe)} 只股票，产生 {len(fresh)} 条新预警")
        return fresh
