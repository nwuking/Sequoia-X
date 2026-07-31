"""基于价格、成交量和横截面因子的时间序列集成预测模型。"""

import sqlite3
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine

logger = get_logger(__name__)


@dataclass(frozen=True)
class PredictionResult:
    symbol: str
    data_date: str
    horizon: int
    up_probability: float
    direction: str
    expected_return: float


class EnsemblePredictor:
    """非线性梯度提升与线性逻辑回归的等权集成模型。"""

    feature_columns = [
        "ret_1", "ret_5", "ret_10", "ret_20", "ret_60", "ret_120",
        "volatility_10", "volatility_20", "volatility_60",
        "ma_gap_5", "ma_gap_20", "ma_gap_60",
        "rsi_14", "atr_14", "volume_ratio_5_20", "turnover_log",
        "breakout_20", "breakout_60", "drawdown_60", "candle_body", "day_range",
        "rank_ret_20", "rank_ret_60", "rank_volatility_20", "rank_volume_ratio",
        "market_ret_1", "market_ret_5", "market_ret_20", "market_breadth_20",
        "market_volatility_20",
    ]

    def __init__(self, engine: DataEngine, max_train_rows: int = 300_000) -> None:
        self.engine = engine
        self.max_train_rows = max_train_rows

    def _load_data(self) -> pd.DataFrame:
        with sqlite3.connect(self.engine.db_path) as conn:
            df = pd.read_sql(
                "SELECT symbol, date, open, high, low, close, volume, turnover "
                "FROM stock_daily ORDER BY symbol, date",
                conn,
            )
        if df.empty:
            raise ValueError("本地行情数据库为空，请先执行 --backfill")
        df["date"] = pd.to_datetime(df["date"])
        return df

    @classmethod
    def build_features(cls, df: pd.DataFrame, horizon: int) -> pd.DataFrame:
        """仅使用当日及历史数据构造特征，目标为未来 horizon 日涨跌。"""
        if horizon < 1 or horizon > 60:
            raise ValueError("预测周期 horizon 必须在 1 到 60 个交易日之间")

        df = df.sort_values(["symbol", "date"]).copy()
        grouped = df.groupby("symbol", group_keys=False)

        for period in (1, 5, 10, 20, 60, 120):
            df[f"ret_{period}"] = grouped["close"].pct_change(period, fill_method=None)

        daily_return = grouped["close"].pct_change(fill_method=None)
        for period in (10, 20, 60):
            df[f"volatility_{period}"] = (
                daily_return.groupby(df["symbol"]).rolling(period).std().reset_index(level=0, drop=True)
            )

        for period in (5, 20, 60):
            moving_average = (
                grouped["close"].rolling(period).mean().reset_index(level=0, drop=True)
            )
            df[f"ma_gap_{period}"] = df["close"] / moving_average - 1

        delta = grouped["close"].diff()
        gain = delta.clip(lower=0).groupby(df["symbol"]).rolling(14).mean().reset_index(level=0, drop=True)
        loss = (-delta.clip(upper=0)).groupby(df["symbol"]).rolling(14).mean().reset_index(level=0, drop=True)
        relative_strength = gain / loss.replace(0, np.nan)
        df["rsi_14"] = 100 - 100 / (1 + relative_strength)

        previous_close = grouped["close"].shift(1)
        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - previous_close).abs(),
                (df["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr_14"] = (
            true_range.groupby(df["symbol"]).rolling(14).mean().reset_index(level=0, drop=True)
            / df["close"]
        )

        volume_ma5 = grouped["volume"].rolling(5).mean().reset_index(level=0, drop=True)
        volume_ma20 = grouped["volume"].rolling(20).mean().reset_index(level=0, drop=True)
        df["volume_ratio_5_20"] = volume_ma5 / volume_ma20.replace(0, np.nan)
        df["turnover_log"] = np.log1p(df["turnover"].clip(lower=0))

        for period in (20, 60):
            rolling_high = grouped["high"].rolling(period).max().reset_index(level=0, drop=True)
            df[f"breakout_{period}"] = df["close"] / rolling_high - 1
        rolling_high60 = grouped["close"].rolling(60).max().reset_index(level=0, drop=True)
        df["drawdown_60"] = df["close"] / rolling_high60 - 1
        df["candle_body"] = (df["close"] - df["open"]) / df["open"].replace(0, np.nan)
        df["day_range"] = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)

        df["rank_ret_20"] = df.groupby("date")["ret_20"].rank(pct=True)
        df["rank_ret_60"] = df.groupby("date")["ret_60"].rank(pct=True)
        df["rank_volatility_20"] = df.groupby("date")["volatility_20"].rank(pct=True)
        df["rank_volume_ratio"] = df.groupby("date")["volume_ratio_5_20"].rank(pct=True)
        df["market_ret_1"] = df.groupby("date")["ret_1"].transform("median")
        df["market_ret_5"] = df.groupby("date")["ret_5"].transform("median")
        df["market_ret_20"] = df.groupby("date")["ret_20"].transform("median")
        df["market_breadth_20"] = (
            (df["ma_gap_20"] > 0).groupby(df["date"]).transform("mean")
        )
        df["market_volatility_20"] = df.groupby("date")["volatility_20"].transform("median")

        future_close = grouped["close"].shift(-horizon)
        df["forward_return"] = future_close / df["close"] - 1
        df["target"] = np.where(df["forward_return"].notna(), (df["forward_return"] > 0).astype(int), np.nan)
        return df.replace([np.inf, -np.inf], np.nan)

    def _metrics(self, y_true: pd.Series, probabilities: np.ndarray) -> dict[str, float]:
        direction_probability = self.engine.thresholds.number(
            "prediction", "direction_probability"
        )
        predictions = (probabilities >= direction_probability).astype(int)
        metrics = {
            "accuracy": float(accuracy_score(y_true, predictions)),
            "balanced_accuracy": float(balanced_accuracy_score(y_true, predictions)),
            "brier_score": float(brier_score_loss(y_true, probabilities)),
            "baseline_accuracy": float(max(y_true.mean(), 1 - y_true.mean())),
        }
        metrics["roc_auc"] = (
            float(roc_auc_score(y_true, probabilities)) if y_true.nunique() > 1 else 0.5
        )
        confident = (
            probabilities <= self.engine.thresholds.number("prediction", "confidence_low")
        ) | (
            probabilities >= self.engine.thresholds.number("prediction", "confidence_high")
        )
        metrics["high_confidence_coverage"] = float(confident.mean())
        metrics["high_confidence_accuracy"] = (
            float(accuracy_score(y_true[confident], predictions[confident]))
            if confident.any()
            else float("nan")
        )
        return metrics

    @staticmethod
    def _calibration_shift(raw_probabilities: np.ndarray, target_rate: float) -> float:
        """估计仅调整基准概率的 log-odds 截距，保持个股排序单调不变。"""
        clipped = np.clip(raw_probabilities, 1e-6, 1 - 1e-6)
        logits = np.log(clipped / (1 - clipped))
        low, high = -10.0, 10.0
        for _ in range(80):
            middle = (low + high) / 2
            calibrated_mean = (1 / (1 + np.exp(-(logits + middle)))).mean()
            if calibrated_mean < target_rate:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    @staticmethod
    def _apply_calibration(raw_probabilities: np.ndarray, shift: float) -> np.ndarray:
        clipped = np.clip(raw_probabilities, 1e-6, 1 - 1e-6)
        logits = np.log(clipped / (1 - clipped))
        return 1 / (1 + np.exp(-(logits + shift)))

    def predict(
        self,
        symbols: list[str],
        horizon: int = 5,
    ) -> tuple[list[PredictionResult], dict[str, float]]:
        """训练时间外验证模型并预测指定股票未来 horizon 个交易日涨跌概率。"""
        cfg = self.engine.thresholds
        min_horizon = cfg.integer("prediction", "min_horizon")
        max_horizon = cfg.integer("prediction", "max_horizon")
        if horizon < min_horizon or horizon > max_horizon:
            raise ValueError(
                f"预测周期 horizon 必须在 {min_horizon} 到 {max_horizon} 个交易日之间"
            )
        requested = list(dict.fromkeys(symbol.zfill(6) for symbol in symbols))
        if not requested:
            raise ValueError("至少需要指定一个股票代码")

        features = self.build_features(self._load_data(), horizon=horizon)
        usable = features.dropna(subset=self.feature_columns + ["target", "forward_return"]).copy()
        unique_dates = sorted(usable["date"].unique())
        min_days = cfg.integer("prediction", "min_validation_days")
        if len(unique_dates) < min_days:
            raise ValueError(f"有效历史数据不足 {min_days} 个交易日，无法进行可靠的时间外验证")

        split_index = int(len(unique_dates) * cfg.number("prediction", "train_ratio"))
        split_date = unique_dates[split_index]
        train_end_date = unique_dates[max(0, split_index - horizon)]
        train = usable[usable["date"] < train_end_date]
        validation = usable[usable["date"] >= split_date]
        train_dates = sorted(train["date"].unique())
        calibration_index = int(
            len(train_dates) * cfg.number("prediction", "calibration_ratio")
        )
        calibration_date = train_dates[calibration_index]
        model_train_end_date = train_dates[max(0, calibration_index - horizon)]
        model_train = train[train["date"] < model_train_end_date]
        calibration = train[train["date"] >= calibration_date]
        if len(model_train) > self.max_train_rows:
            model_train = model_train.sample(self.max_train_rows, random_state=42)

        x_train = model_train[self.feature_columns]
        y_train = model_train["target"].astype(int)
        x_calibration = calibration[self.feature_columns]
        y_calibration = calibration["target"].astype(int)
        x_validation = validation[self.feature_columns]
        y_validation = validation["target"].astype(int)

        linear_model = make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=500, C=0.5, random_state=42),
        )
        nonlinear_model = HistGradientBoostingClassifier(
            learning_rate=0.06,
            max_iter=180,
            max_leaf_nodes=31,
            min_samples_leaf=80,
            l2_regularization=2.0,
            random_state=42,
        )
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=UserWarning,
                module=r"joblib\.externals\.loky\.backend\.context",
            )
            linear_model.fit(x_train, y_train)
            nonlinear_model.fit(x_train, y_train)

        def raw_probability(values: pd.DataFrame) -> np.ndarray:
            return (
                linear_model.predict_proba(values)[:, 1]
                + nonlinear_model.predict_proba(values)[:, 1]
            ) / 2

        calibration_shift = self._calibration_shift(
            raw_probability(x_calibration),
            float(y_calibration.mean()),
        )
        validation_probability = self._apply_calibration(
            raw_probability(x_validation), calibration_shift
        )
        metrics = self._metrics(y_validation, validation_probability)
        metrics["train_rows"] = float(len(model_train))
        metrics["calibration_rows"] = float(len(calibration))
        metrics["validation_rows"] = float(len(validation))
        metrics["validation_start"] = pd.Timestamp(split_date).strftime("%Y-%m-%d")

        latest = (
            features[features["symbol"].isin(requested)]
            .dropna(subset=self.feature_columns)
            .sort_values("date")
            .groupby("symbol", as_index=False)
            .tail(1)
        )
        missing = sorted(set(requested) - set(latest["symbol"]))
        if missing:
            logger.warning(f"以下股票缺少足够历史数据，无法预测：{', '.join(missing)}")
        if latest.empty:
            return [], metrics

        latest_x = latest[self.feature_columns]
        probabilities = self._apply_calibration(raw_probability(latest_x), calibration_shift)

        validation_with_probability = validation[["forward_return"]].copy()
        validation_with_probability["probability"] = validation_probability
        validation_with_probability["bucket"] = pd.cut(
            validation_with_probability["probability"],
            bins=np.linspace(0, 1, 11),
            labels=False,
            include_lowest=True,
        )
        bucket_returns = validation_with_probability.groupby("bucket")["forward_return"].mean()

        results = []
        for (_, row), probability in zip(latest.iterrows(), probabilities, strict=True):
            bucket = min(int(probability * 10), 9)
            expected_return = float(bucket_returns.get(bucket, validation["forward_return"].mean()))
            results.append(
                PredictionResult(
                    symbol=row["symbol"],
                    data_date=row["date"].strftime("%Y-%m-%d"),
                    horizon=horizon,
                    up_probability=float(probability),
                    direction=(
                        "上涨"
                        if probability >= cfg.number("prediction", "direction_probability")
                        else "下跌"
                    ),
                    expected_return=expected_return,
                )
            )
        return results, metrics
