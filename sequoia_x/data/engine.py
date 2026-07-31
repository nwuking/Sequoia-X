"""数据引擎模块：负责 SQLite 行情数据与财务因子存储同步。"""

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.core.thresholds import ThresholdConfig
from sequoia_x.data.baostock_gateway import serialized_baostock

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""

_CREATE_STOCK_BASIC_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_basic (
    symbol     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_FINANCIAL_FACTORS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS financial_factors (
    symbol                TEXT NOT NULL,
    report_date           TEXT NOT NULL,
    announcement_date     TEXT,
    eps                   REAL,
    bps                   REAL,
    roe                   REAL,
    pe_dynamic            REAL,
    pb                    REAL,
    revenue               REAL,
    net_profit            REAL,
    revenue_yoy           REAL,
    net_profit_yoy        REAL,
    operating_cashflow_ps REAL,
    gross_margin          REAL,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (symbol, report_date)
);
"""

_CREATE_FINANCIAL_FACTORS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_financial_symbol_report
ON financial_factors (symbol, report_date);
"""

_CREATE_API_USAGE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS api_usage (
    provider     TEXT NOT NULL,
    usage_date   TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (provider, usage_date)
);
"""

class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self.baostock_daily_request_limit: int = settings.baostock_daily_request_limit
        self.thresholds = ThresholdConfig(settings.thresholds_config_path)
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_STOCK_BASIC_TABLE_SQL)
            conn.execute(_CREATE_FINANCIAL_FACTORS_TABLE_SQL)
            conn.execute(_CREATE_FINANCIAL_FACTORS_INDEX_SQL)
            conn.execute(_CREATE_API_USAGE_TABLE_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_api_usage(self, provider: str, usage_date: str | None = None) -> int:
        usage_date = usage_date or date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT request_count FROM api_usage WHERE provider = ? AND usage_date = ?",
                (provider, usage_date),
            ).fetchone()
        return int(row[0]) if row else 0

    def _increment_api_usage(self, provider: str, count: int = 1, usage_date: str | None = None) -> int:
        usage_date = usage_date or date.today().isoformat()
        updated_at = date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO api_usage (provider, usage_date, request_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, usage_date) DO UPDATE SET
                    request_count = request_count + excluded.request_count,
                    updated_at = excluded.updated_at
                """,
                (provider, usage_date, count, updated_at),
            )
            conn.commit()
            row = conn.execute(
                "SELECT request_count FROM api_usage WHERE provider = ? AND usage_date = ?",
                (provider, usage_date),
            ).fetchone()
        return int(row[0]) if row else 0

    def _ensure_baostock_quota(self, upcoming_requests: int = 1) -> None:
        today = date.today().isoformat()
        used = self._get_api_usage("baostock", today)
        projected = used + upcoming_requests
        if projected > self.baostock_daily_request_limit:
            raise RuntimeError(
                "baostock 当日请求配额即将超限："
                f"{today} 已用 {used} 次，请求上限 {self.baostock_daily_request_limit} 次，"
                f"本次预计再发起 {upcoming_requests} 次"
            )

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_latest_date(self) -> str | None:
        """返回本地行情库中的最新交易日期。"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
        return row[0] if row and row[0] else None

    def get_stock_names(self, symbols: list[str]) -> dict[str, str]:
        """从本地基础信息表查询股票中文名。"""
        if not symbols:
            return {}

        mapping: dict[str, str] = {}
        with sqlite3.connect(self.db_path) as conn:
            for start in range(0, len(symbols), 900):
                batch = symbols[start:start + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT symbol, name FROM stock_basic WHERE symbol IN ({placeholders})",
                    batch,
                ).fetchall()
                mapping.update(rows)
        return mapping

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    def get_latest_financial_factors(self, symbols: list[str]) -> pd.DataFrame:
        """返回每只股票最新一期财务因子。"""
        if not symbols:
            return pd.DataFrame()

        placeholders = ",".join("?" for _ in symbols)
        query = f"""
        SELECT f1.*
        FROM financial_factors f1
        WHERE f1.symbol IN ({placeholders})
          AND f1.report_date = (
              SELECT MAX(f2.report_date)
              FROM financial_factors f2
              WHERE f2.symbol = f1.symbol
          )
        """
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql(query, conn, params=symbols)

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    @staticmethod
    def _latest_financial_report_date(as_of: date | None = None) -> str:
        """推断全市场较完整可用的最近一期财报期。"""
        as_of = as_of or date.today()
        year = as_of.year
        month = as_of.month
        if 5 <= month <= 8:
            return f"{year}0331"
        if 9 <= month <= 10:
            return f"{year}0630"
        if 11 <= month <= 12:
            return f"{year}0930"
        return f"{year - 1}0930"

    def sync_financial_factors(self, report_date: str | None = None) -> int:
        """使用 AKShare 从东方财富同步某一期全市场财务因子。"""
        import akshare as ak

        target_date = report_date or self._latest_financial_report_date()
        logger.info(f"开始同步财务因子，报告期：{target_date}")
        raw = ak.stock_yjbb_em(date=target_date)
        if raw.empty:
            logger.warning(f"财务因子同步返回空结果：{target_date}")
            return 0

        rename_map = {
            "股票代码": "symbol",
            "最新公告日期": "announcement_date",
            "公告日期": "announcement_date",
            "每股收益": "eps",
            "每股净资产": "bps",
            "净资产收益率": "roe",
            "营业总收入-营业总收入": "revenue",
            "营业收入-营业收入": "revenue",
            "净利润-净利润": "net_profit",
            "营业总收入-同比增长": "revenue_yoy",
            "营业收入-同比增长": "revenue_yoy",
            "净利润-同比增长": "net_profit_yoy",
            "每股经营现金流量": "operating_cashflow_ps",
            "销售毛利率": "gross_margin",
        }
        selected_columns = [column for column in rename_map if column in raw.columns]
        if "股票代码" not in selected_columns:
            raise RuntimeError("AKShare 返回缺少 '股票代码' 列，无法写入财务因子表")

        df = raw[selected_columns].rename(columns=rename_map).copy()
        df["symbol"] = df["symbol"].astype(str).str.extract(r"(\d{6})")[0]
        df["report_date"] = pd.Timestamp(target_date).strftime("%Y-%m-%d")
        df["announcement_date"] = pd.to_datetime(
            df.get("announcement_date"), errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        df["updated_at"] = date.today().isoformat()

        numeric_columns = [
            "eps",
            "bps",
            "roe",
            "revenue",
            "net_profit",
            "revenue_yoy",
            "net_profit_yoy",
            "operating_cashflow_ps",
            "gross_margin",
        ]
        for column in numeric_columns:
            if column not in df.columns:
                df[column] = pd.NA
            df[column] = pd.to_numeric(df[column], errors="coerce")

        spot = ak.stock_zh_a_spot_em()
        if not spot.empty and {"代码", "市盈率-动态", "市净率"}.issubset(spot.columns):
            valuation = spot[["代码", "市盈率-动态", "市净率"]].copy()
            valuation.columns = ["symbol", "pe_dynamic", "pb"]
            valuation["symbol"] = valuation["symbol"].astype(str).str.extract(r"(\d{6})")[0]
            valuation["pe_dynamic"] = pd.to_numeric(valuation["pe_dynamic"], errors="coerce")
            valuation["pb"] = pd.to_numeric(valuation["pb"], errors="coerce")
            df = df.merge(valuation, on="symbol", how="left")
        else:
            df["pe_dynamic"] = pd.NA
            df["pb"] = pd.NA

        df = df[
            [
                "symbol",
                "report_date",
                "announcement_date",
                "eps",
                "bps",
                "roe",
                "pe_dynamic",
                "pb",
                "revenue",
                "net_profit",
                "revenue_yoy",
                "net_profit_yoy",
                "operating_cashflow_ps",
                "gross_margin",
                "updated_at",
            ]
        ].dropna(subset=["symbol"])

        records = list(df.itertuples(index=False, name=None))
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO financial_factors (
                    symbol, report_date, announcement_date, eps, bps, roe,
                    pe_dynamic, pb, revenue, net_profit, revenue_yoy, net_profit_yoy,
                    operating_cashflow_ps, gross_margin, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, report_date) DO UPDATE SET
                    announcement_date=excluded.announcement_date,
                    eps=excluded.eps,
                    bps=excluded.bps,
                    roe=excluded.roe,
                    pe_dynamic=excluded.pe_dynamic,
                    pb=excluded.pb,
                    revenue=excluded.revenue,
                    net_profit=excluded.net_profit,
                    revenue_yoy=excluded.revenue_yoy,
                    net_profit_yoy=excluded.net_profit_yoy,
                    operating_cashflow_ps=excluded.operating_cashflow_ps,
                    gross_margin=excluded.gross_margin,
                    updated_at=excluded.updated_at
                """,
                records,
            )
            conn.commit()
        logger.info(f"财务因子同步完成，写入 {len(df)} 条记录")
        return len(df)

    # ── 数据同步 ──

    @serialized_baostock
    def sync_today_bulk(self) -> int:
        """通过单连接串行拉取增量数据（后复权），写入 SQLite。"""
        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")

        def _login() -> bool:
            for attempt in range(3):
                try:
                    lg = bs.login()
                    if lg.error_code == "0":
                        return True
                    logger.warning(f"baostock 登录失败: {lg.error_msg}")
                except Exception as exc:
                    logger.warning(f"baostock 登录异常: {exc}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
            return False

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        all_rows = []
        logger.info(f"需要更新 {len(tasks)} 只股票，按 baostock 单连接限制串行拉取...")
        if not _login():
            raise RuntimeError("baostock 增量同步失败：登录异常，已停止策略执行")

        try:
            for symbol, bs_code, start, end in tasks:
                query_ok = False
                for attempt in range(2):
                    try:
                        self._ensure_baostock_quota(1)
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=end,
                            frequency="d",
                            adjustflag="1",
                        )
                        self._increment_api_usage("baostock", 1)
                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)
                        while rs.next():
                            all_rows.append([symbol] + rs.get_row_data())
                        query_ok = True
                        break
                    except Exception as exc:
                        logger.warning(f"[{symbol}] 增量拉取失败: {exc}")
                        try:
                            bs.logout()
                        except Exception:
                            pass
                        time.sleep(2 ** attempt)
                        if attempt == 0 and _login():
                            continue
                        break
                if not query_ok:
                    raise RuntimeError(
                        f"baostock 增量同步失败：{symbol} 拉取异常，已停止策略执行，"
                        "避免使用陈旧或不完整数据"
                    )
        finally:
            try:
                bs.logout()
            except Exception:
                pass

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(all_rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with sqlite3.connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500)
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    @serialized_baostock
    def backfill(self, symbols: list[str], full_history: bool = False) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传
        - full_history=True 时强制从 start_date 全量补齐，并覆盖本地该股票历史
        """
        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次

        def _login():
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0

        try:
            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)
                if not full_history and last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    bs.logout()
                    time.sleep(1)
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = self.start_date if full_history else (last_date or self.start_date)
                if last_date and not full_history:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        self._ensure_baostock_quota(1)
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )
                        self._increment_api_usage("baostock", 1)

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break

                    except Exception as exc:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    continue

                if not rows:
                    skipped += 1
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with sqlite3.connect(self.db_path) as conn:
                        if full_history:
                            conn.execute("DELETE FROM stock_daily WHERE symbol = ?", (symbol,))
                        df.to_sql(
                            "stock_daily", conn, if_exists="append",
                            index=False, method="multi", chunksize=500,
                        )
                        conn.commit()
                except sqlite3.IntegrityError:
                    pass

                success += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表 ──

    @serialized_baostock
    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码及中文名，并保存到本地。"""
        from datetime import date

        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            symbols = []
            stock_basics = []
            while rs.next():
                row = rs.get_row_data()
                code = row[0]           # "sh.600000" or "sz.000001"
                name = row[1]
                status = row[4]         # "1" = 上市
                stock_type = row[5]     # "1" = 股票
                if status == "1" and stock_type == "1":
                    symbol = code.split(".")[1]  # 提取纯数字代码
                    symbols.append(symbol)
                    stock_basics.append((symbol, name, date.today().isoformat()))

            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT INTO stock_basic (symbol, name, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET "
                    "name=excluded.name, updated_at=excluded.updated_at",
                    stock_basics,
                )
                conn.commit()
            logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
            return symbols
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
        finally:
            bs.logout()

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]
