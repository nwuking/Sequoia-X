"""本地 CSV 自选股和持仓管理。"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine

logger = get_logger(__name__)


@dataclass(frozen=True)
class PositionInput:
    symbol: str
    shares: int
    cost_price: float
    buy_avg_price: float


class PortfolioManager:
    """维护自选与持仓 CSV，并根据本地行情刷新派生字段。"""

    columns = [
        "symbol", "name", "is_watchlist", "shares", "cost_price", "buy_avg_price",
        "latest_close", "data_date", "return_rate", "market_value", "unrealized_pnl",
        "quote_source", "updated_at",
    ]

    def __init__(
        self,
        engine: DataEngine,
        csv_path: str,
        quote_fetcher: Callable[[str], tuple[float, str] | None] | None = None,
    ) -> None:
        self.engine = engine
        self.csv_path = Path(csv_path)
        self.quote_fetcher = quote_fetcher or self._fetch_real_quote

    @staticmethod
    def _fetch_real_quote(symbol: str) -> tuple[float, str] | None:
        """获取不复权实时价格；腾讯为主，东方财富为备用。"""
        exchange = "sh" if symbol.startswith(("6", "9")) else "sz"
        try:
            response = requests.get(
                f"https://qt.gtimg.cn/q={exchange}{symbol}",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=10,
            )
            response.raise_for_status()
            fields = response.content.decode("gbk").split('="', 1)[1].split("~")
            price = float(fields[3])
            quote_date = datetime.strptime(fields[30][:8], "%Y%m%d").date().isoformat()
            if price > 0:
                return price, quote_date
        except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
            logger.warning(f"[{symbol}] 腾讯真实报价失败，尝试备用源：{exc}")

        market = "1" if symbol.startswith(("6", "9")) else "0"
        try:
            response = requests.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={"secid": f"{market}.{symbol}", "fields": "f43,f57,f58,f86"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
            raw_price = data.get("f43")
            timestamp = data.get("f86")
            if not isinstance(raw_price, (int, float)) or not isinstance(timestamp, int):
                return None
            quote_date = datetime.fromtimestamp(timestamp).date().isoformat()
            return float(raw_price) / 100, quote_date
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning(f"[{symbol}] 获取不复权真实报价失败：{exc}")
            return None

    def load(self) -> pd.DataFrame:
        if not self.csv_path.exists():
            df = pd.DataFrame(columns=self.columns)
        else:
            df = pd.read_csv(self.csv_path, dtype={"symbol": str})
        for column in self.columns:
            if column not in df.columns:
                df[column] = pd.NA
        for column in ("symbol", "name", "data_date", "quote_source", "updated_at"):
            df[column] = df[column].astype("string")
        df["symbol"] = df["symbol"].str.zfill(6)
        return df[self.columns]

    def save(self, df: pd.DataFrame) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        df[self.columns].sort_values(["is_watchlist", "symbol"], ascending=[False, True]).to_csv(
            self.csv_path,
            index=False,
            encoding="utf-8-sig",
        )

    def resolve_symbol(self, value: str) -> str:
        value = value.strip()
        if value.isdigit():
            return value.zfill(6)
        with_names = self.engine.get_stock_names(self.engine.get_local_symbols())
        reverse = {name: symbol for symbol, name in with_names.items()}
        if value not in reverse:
            raise ValueError(f"无法识别股票名称或代码：{value}")
        return reverse[value]

    def set_watchlist(self, values: list[str]) -> pd.DataFrame:
        symbols = [self.resolve_symbol(value) for value in values]
        df = self.load().set_index("symbol", drop=False)
        if not df.empty:
            df["is_watchlist"] = False
        names = self.engine.get_stock_names(symbols)
        for symbol in symbols:
            if symbol not in df.index:
                df.loc[symbol, :] = pd.NA
                df.loc[symbol, "symbol"] = symbol
                df.loc[symbol, "shares"] = 0
            df.loc[symbol, "name"] = names.get(symbol, symbol)
            df.loc[symbol, "is_watchlist"] = True
        # 持仓始终保留在组合中，即使未显式列入自选。
        if not df.empty:
            df.loc[pd.to_numeric(df["shares"], errors="coerce").fillna(0) > 0, "is_watchlist"] = True
        result = df.reset_index(drop=True)
        self.save(result)
        return result

    def upsert_positions(self, positions: list[PositionInput]) -> pd.DataFrame:
        df = self.load().set_index("symbol", drop=False)
        symbols = [self.resolve_symbol(position.symbol) for position in positions]
        names = self.engine.get_stock_names(symbols)
        for position, symbol in zip(positions, symbols, strict=True):
            if position.shares < 0 or position.cost_price < 0 or position.buy_avg_price < 0:
                raise ValueError("持仓股数、成本和买入均价不能为负数")
            if symbol not in df.index:
                df.loc[symbol, :] = pd.NA
                df.loc[symbol, "symbol"] = symbol
            df.loc[symbol, "name"] = names.get(symbol, symbol)
            df.loc[symbol, "is_watchlist"] = True
            df.loc[symbol, "shares"] = position.shares
            df.loc[symbol, "cost_price"] = position.cost_price
            df.loc[symbol, "buy_avg_price"] = position.buy_avg_price
        result = df.reset_index(drop=True)
        self.save(result)
        return result

    def remove_positions(self, values: list[str]) -> pd.DataFrame:
        df = self.load().set_index("symbol", drop=False)
        for value in values:
            symbol = self.resolve_symbol(value)
            if symbol in df.index:
                df.loc[symbol, ["shares", "cost_price", "buy_avg_price"]] = [0, pd.NA, pd.NA]
        result = df.reset_index(drop=True)
        self.save(result)
        return result

    def refresh(self) -> tuple[pd.DataFrame, bool]:
        """若本地最新行情发生变化，则刷新收益率、市值和盈亏字段。"""
        df = self.load()
        if df.empty:
            return df, False

        changed = False
        names = self.engine.get_stock_names(df["symbol"].tolist())
        now = datetime.now().isoformat(timespec="seconds")
        for index, row in df.iterrows():
            symbol = row["symbol"]
            quote = self.quote_fetcher(symbol)
            if quote is None:
                if str(row["quote_source"]) != "realtime_unadjusted":
                    df.loc[index, [
                        "latest_close", "data_date", "return_rate", "market_value",
                        "unrealized_pnl", "quote_source",
                    ]] = [pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA]
                continue
            latest_close, data_date = quote
            old_close = pd.to_numeric(pd.Series([row["latest_close"]]), errors="coerce").iloc[0]
            if pd.isna(old_close) or old_close != latest_close or str(row["data_date"]) != data_date:
                changed = True
            shares = float(pd.to_numeric(pd.Series([row["shares"]]), errors="coerce").fillna(0).iloc[0])
            cost = pd.to_numeric(pd.Series([row["cost_price"]]), errors="coerce").iloc[0]
            existing_name = row["name"] if pd.notna(row["name"]) else symbol
            df.loc[index, "name"] = names.get(symbol, existing_name)
            df.loc[index, "latest_close"] = latest_close
            df.loc[index, "data_date"] = data_date
            df.loc[index, "quote_source"] = "realtime_unadjusted"
            df.loc[index, "market_value"] = latest_close * shares
            if shares > 0 and pd.notna(cost) and float(cost) > 0:
                df.loc[index, "return_rate"] = latest_close / float(cost) - 1
                df.loc[index, "unrealized_pnl"] = (latest_close - float(cost)) * shares
            else:
                df.loc[index, "return_rate"] = pd.NA
                df.loc[index, "unrealized_pnl"] = 0.0
            df.loc[index, "updated_at"] = now

        self.save(df)
        logger.info("组合行情已更新" if changed else "组合行情已是最新，无需更新")
        return df, changed

    @staticmethod
    def parse_position(value: str) -> PositionInput:
        """解析 CODE:SHARES:COST[:BUY_AVG] 格式。"""
        parts = [part.strip() for part in value.replace(",", ":").split(":")]
        if len(parts) not in (3, 4):
            raise ValueError("持仓格式应为 股票:股数:成本[:买入均价]")
        cost = float(parts[2])
        return PositionInput(
            symbol=parts[0],
            shares=int(parts[1]),
            cost_price=cost,
            buy_avg_price=float(parts[3]) if len(parts) == 4 else cost,
        )
