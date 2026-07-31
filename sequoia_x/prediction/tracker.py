"""重点组合、持仓和自选股的多周期预测跟踪。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sequoia_x.core.logger import get_logger
from sequoia_x.prediction.ensemble import EnsemblePredictor

logger = get_logger(__name__)


@dataclass(frozen=True)
class PredictionEvaluation:
    symbol: str
    name: str
    horizon: int
    predicted_direction: str
    actual_return: float
    accurate: bool


@dataclass(frozen=True)
class TrackedPrediction:
    symbol: str
    name: str
    target_horizon: int
    remaining_horizon: int
    direction: str
    probability: float
    expected_return: float
    refreshed: bool


@dataclass(frozen=True)
class PredictionTrackingReport:
    data_date: str
    started: tuple[str, ...]
    completed: tuple[str, ...]
    evaluations: tuple[PredictionEvaluation, ...]
    predictions: tuple[TrackedPrediction, ...]
    errors: tuple[str, ...]


class PredictionTracker:
    """维护固定10交易日周期，并在检查点刷新剩余预测。"""

    def __init__(self, engine, db_path: str) -> None:
        self.engine = engine
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.horizons = tuple(sorted(engine.thresholds.integers("prediction", "tracking_horizons")))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS prediction_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    start_close REAL NOT NULL,
                    status TEXT NOT NULL,
                    last_checkpoint INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT,
                    UNIQUE(symbol, start_date)
                );
                CREATE INDEX IF NOT EXISTS idx_prediction_cycles_active
                ON prediction_cycles(symbol, status);
                CREATE TABLE IF NOT EXISTS prediction_forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    target_horizon INTEGER NOT NULL,
                    issued_date TEXT NOT NULL,
                    issued_close REAL NOT NULL,
                    remaining_horizon INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    probability REAL NOT NULL,
                    expected_return REAL NOT NULL,
                    evaluated_at TEXT,
                    actual_return REAL,
                    accurate INTEGER,
                    FOREIGN KEY(cycle_id) REFERENCES prediction_cycles(id)
                );
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(prediction_forecasts)")
            }
            if "issued_close" not in columns:
                conn.execute(
                    "ALTER TABLE prediction_forecasts ADD COLUMN issued_close REAL NOT NULL DEFAULT 0"
                )

    def _close(self, symbol: str, data_date: str) -> float | None:
        with sqlite3.connect(self.engine.db_path) as conn:
            row = conn.execute(
                "SELECT close FROM stock_daily WHERE symbol=? AND date<=? "
                "ORDER BY date DESC LIMIT 1",
                (symbol, data_date),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def _elapsed_days(self, start_date: str, data_date: str) -> int:
        with sqlite3.connect(self.engine.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT date) FROM stock_daily WHERE date>? AND date<=?",
                (start_date, data_date),
            ).fetchone()
        return int(row[0]) if row else 0

    def _target_quote(self, symbol: str, start_date: str, horizon: int) -> tuple[str, float] | None:
        with sqlite3.connect(self.engine.db_path) as conn:
            row = conn.execute(
                "SELECT date,close FROM stock_daily WHERE symbol=? AND date>? "
                "ORDER BY date LIMIT 1 OFFSET ?",
                (symbol, start_date, horizon - 1),
            ).fetchone()
        return (str(row[0]), float(row[1])) if row and row[1] is not None else None

    def _predict_and_store(
        self,
        requests: list[tuple[int, str, int, int]],
        data_date: str,
        names: dict[str, str],
        refreshed: bool,
    ) -> tuple[list[TrackedPrediction], list[str]]:
        predictions: list[TrackedPrediction] = []
        errors: list[str] = []
        by_remaining: dict[int, list[tuple[int, str, int, int]]] = {}
        for request in requests:
            by_remaining.setdefault(request[3], []).append(request)
        predictor = EnsemblePredictor(self.engine)
        for remaining, group in by_remaining.items():
            symbols = [item[1] for item in group]
            try:
                results, _ = predictor.predict(symbols, horizon=remaining)
                result_map = {item.symbol: item for item in results}
                with self._connect() as conn:
                    for cycle_id, symbol, target_horizon, _ in group:
                        result = result_map.get(symbol)
                        if result is None:
                            errors.append(f"{symbol} 缺少{remaining}日预测结果")
                            continue
                        issued_close = self._close(symbol, data_date)
                        if issued_close is None:
                            errors.append(f"{symbol} 缺少预测签发日收盘价")
                            continue
                        conn.execute(
                            "INSERT INTO prediction_forecasts("
                            "cycle_id,symbol,target_horizon,issued_date,issued_close,remaining_horizon,"
                            "direction,probability,expected_return) VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                cycle_id, symbol, target_horizon, data_date, issued_close, remaining,
                                result.direction, result.up_probability, result.expected_return,
                            ),
                        )
                        predictions.append(
                            TrackedPrediction(
                                symbol=symbol,
                                name=names.get(symbol, symbol),
                                target_horizon=target_horizon,
                                remaining_horizon=remaining,
                                direction=result.direction,
                                probability=result.up_probability,
                                expected_return=result.expected_return,
                                refreshed=refreshed,
                            )
                        )
            except Exception as exc:
                errors.append(f"剩余{remaining}日预测失败：{exc}")
        return predictions, errors

    def run(
        self,
        symbols: list[str],
        names: dict[str, str],
        data_date: str,
    ) -> PredictionTrackingReport:
        universe = list(dict.fromkeys(str(symbol).zfill(6) for symbol in symbols))
        logger.info(f"预测跟踪开始：数据日期 {data_date}，候选 {len(universe)} 只")
        started: list[str] = []
        completed: list[str] = []
        evaluations: list[PredictionEvaluation] = []
        all_predictions: list[TrackedPrediction] = []
        errors: list[str] = []
        refresh_requests: list[tuple[int, str, int, int]] = []
        completed_now: set[str] = set()

        with self._connect() as conn:
            active = conn.execute(
                "SELECT * FROM prediction_cycles WHERE status='active' ORDER BY id"
            ).fetchall()
            for cycle in active:
                elapsed = self._elapsed_days(cycle["start_date"], data_date)
                due = [h for h in self.horizons if cycle["last_checkpoint"] < h <= elapsed]
                if not due:
                    continue
                for horizon in due:
                    target_quote = self._target_quote(cycle["symbol"], cycle["start_date"], horizon)
                    if target_quote is None:
                        errors.append(f"{cycle['symbol']} 缺少第{horizon}日收盘价")
                        continue
                    target_date, target_close = target_quote
                    forecast = conn.execute(
                        "SELECT * FROM prediction_forecasts WHERE cycle_id=? AND target_horizon=? "
                        "ORDER BY issued_date DESC,id DESC LIMIT 1",
                        (cycle["id"], horizon),
                    ).fetchone()
                    if forecast is None:
                        errors.append(f"{cycle['symbol']} 缺少第{horizon}日预测")
                        continue
                    baseline_close = float(forecast["issued_close"] or cycle["start_close"])
                    actual_return = target_close / baseline_close - 1
                    actual_up = actual_return > 0
                    accurate = (forecast["direction"] == "上涨") == actual_up
                    conn.execute(
                        "UPDATE prediction_forecasts SET evaluated_at=?,actual_return=?,accurate=? "
                        "WHERE id=?",
                        (target_date, actual_return, int(accurate), forecast["id"]),
                    )
                    evaluations.append(
                        PredictionEvaluation(
                            symbol=cycle["symbol"],
                            name=names.get(cycle["symbol"], cycle["symbol"]),
                            horizon=horizon,
                            predicted_direction=forecast["direction"],
                            actual_return=actual_return,
                            accurate=accurate,
                        )
                    )
                    logger.info(
                        f"预测评估：{cycle['symbol']} 第{horizon}日，"
                        f"方向={forecast['direction']}，实际收益={actual_return:+.2%}，"
                        f"结果={'准确' if accurate else '不准确'}"
                    )
                checkpoint = max(due)
                conn.execute(
                    "UPDATE prediction_cycles SET last_checkpoint=? WHERE id=?",
                    (checkpoint, cycle["id"]),
                )
                if checkpoint >= self.horizons[-1]:
                    conn.execute(
                        "UPDATE prediction_cycles SET status='completed',completed_at=? WHERE id=?",
                        (datetime.now().isoformat(timespec="seconds"), cycle["id"]),
                    )
                    completed.append(cycle["symbol"])
                    completed_now.add(cycle["symbol"])
                    logger.info(f"预测周期完成并重置：{cycle['symbol']} 已到第{checkpoint}个交易日")
                else:
                    for target in self.horizons:
                        if target > checkpoint:
                            refresh_requests.append(
                                (
                                    cycle["id"], cycle["symbol"], target,
                                    target - checkpoint,
                                )
                            )

            active_symbols = {
                row[0]
                for row in conn.execute(
                    "SELECT symbol FROM prediction_cycles WHERE status='active'"
                ).fetchall()
            }
            new_requests: list[tuple[int, str, int, int]] = []
            for symbol in universe:
                if symbol in active_symbols or symbol in completed_now:
                    continue
                close = self._close(symbol, data_date)
                if close is None:
                    errors.append(f"{symbol} 缺少起始收盘价")
                    continue
                cursor = conn.execute(
                    "INSERT INTO prediction_cycles(symbol,start_date,start_close,status) "
                    "VALUES (?,?,?,'active')",
                    (symbol, data_date, close),
                )
                cycle_id = int(cursor.lastrowid)
                started.append(symbol)
                logger.info(f"创建预测周期：{symbol}，起始日 {data_date}，收盘价 {close:.3f}")
                for horizon in self.horizons:
                    new_requests.append((cycle_id, symbol, horizon, horizon))

        refreshed_predictions, refresh_errors = self._predict_and_store(
            refresh_requests, data_date, names, refreshed=True
        )
        initial_predictions, initial_errors = self._predict_and_store(
            new_requests, data_date, names, refreshed=False
        )
        all_predictions.extend(refreshed_predictions)
        all_predictions.extend(initial_predictions)
        errors.extend(refresh_errors)
        errors.extend(initial_errors)
        valid_started: list[str] = []
        with self._connect() as conn:
            for symbol in started:
                cycle = conn.execute(
                    "SELECT id FROM prediction_cycles WHERE symbol=? AND start_date=?",
                    (symbol, data_date),
                ).fetchone()
                if cycle is None:
                    continue
                forecast_count = conn.execute(
                    "SELECT COUNT(*) FROM prediction_forecasts WHERE cycle_id=?",
                    (cycle["id"],),
                ).fetchone()[0]
                if int(forecast_count) < len(self.horizons):
                    conn.execute(
                        "DELETE FROM prediction_forecasts WHERE cycle_id=?", (cycle["id"],)
                    )
                    conn.execute("DELETE FROM prediction_cycles WHERE id=?", (cycle["id"],))
                    all_predictions = [
                        item for item in all_predictions if item.symbol != symbol or item.refreshed
                    ]
                    errors.append(f"{symbol} 初始预测不完整，已回滚并等待下次重试")
                    logger.warning(f"{symbol} 初始预测不完整，已回滚并等待下次重试")
                else:
                    valid_started.append(symbol)
        report = PredictionTrackingReport(
            data_date=data_date,
            started=tuple(valid_started),
            completed=tuple(completed),
            evaluations=tuple(evaluations),
            predictions=tuple(all_predictions),
            errors=tuple(errors),
        )
        logger.info(
            f"预测跟踪结束：新周期 {len(report.started)}，完成 {len(report.completed)}，"
            f"评估 {len(report.evaluations)}，预测 {len(report.predictions)}，"
            f"异常 {len(report.errors)}"
        )
        return report
