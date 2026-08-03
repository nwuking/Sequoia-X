"""多策略组合决策层：共振统计、趋势确认与重点候选排序。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sequoia_x.core.thresholds import ThresholdConfig
from sequoia_x.strategy.comprehensive_trend import TrendAssessment


@dataclass(frozen=True)
class CombinedCandidate:
    """一只股票的组合决策明细。"""

    symbol: str
    sources: tuple[str, ...]
    families: tuple[str, ...]
    vote_count: int
    family_count: int
    signal_score: float
    factor_rank: int | None
    factor_score: float | None
    factor_contribution: float
    trend_score: float
    regime: str
    trend_signal: str
    reasons: tuple[str, ...]
    risk_labels: tuple[str, ...]
    risk_deduction: float
    vetoed: bool
    risk_passed: bool
    trend_confirmed: bool
    combined_score: float


@dataclass(frozen=True)
class CombinedSelection:
    """组合层产生的候选池及可解释明细。"""

    all_candidates: tuple[str, ...]
    multi_strategy: tuple[str, ...]
    multi_family: tuple[str, ...]
    trend_confirmed: tuple[str, ...]
    focus: tuple[str, ...]
    details: tuple[CombinedCandidate, ...]

    def to_dict(self) -> dict:
        """转换为可直接写入 JSON 的结构。"""
        return {
            "all_candidates": list(self.all_candidates),
            "multi_strategy": list(self.multi_strategy),
            "multi_family": list(self.multi_family),
            "trend_confirmed": list(self.trend_confirmed),
            "focus": list(self.focus),
            "details": [asdict(item) for item in self.details],
        }


class StrategyCombiner:
    """在独立策略运行结束后生成共振与趋势确认候选池。"""

    trend_strategy_name = "ComprehensiveTrendStrategy"
    strategy_families = {
        "MaVolumeStrategy": "趋势动量",
        "TurtleTradeStrategy": "趋势动量",
        "RpsBreakoutStrategy": "趋势动量",
        "HighTightFlagStrategy": "形态整理",
        "LimitUpShakeoutStrategy": "形态整理",
        "UptrendLimitDownStrategy": "反转机会",
        "PrivatePlacementStrategy": "事件驱动",
        "LowPriceMultiFactorStrategy": "横截面因子",
    }
    family_weights = {
        "趋势动量": 15.0,
        "形态整理": 10.0,
        "反转机会": 5.0,
        "事件驱动": 5.0,
        "横截面因子": 10.0,
    }
    # 历史审计为负或仅提供事件信息的策略只保留为标签，不参与正向买入投票。
    risk_or_context_only = {
        "LimitUpShakeoutStrategy",
        "UptrendLimitDownStrategy",
        "PrivatePlacementStrategy",
    }

    @classmethod
    def combine(
        cls,
        selections: dict[str, list[str]],
        assessments: list[TrendAssessment],
        factor_candidates: list[dict] | None = None,
        thresholds: ThresholdConfig | None = None,
    ) -> CombinedSelection:
        """组合策略结果。

        普通策略提供初始候选和投票；综合趋势只负责二次确认与风险评分，
        避免把目标相反的形态策略强制串成单一路径。
        """
        cfg = thresholds or ThresholdConfig("config/thresholds.ini")
        family_weights = {
            "趋势动量": cfg.number("strategy_combiner", "weight_trend_momentum"),
            "形态整理": cfg.number("strategy_combiner", "weight_pattern"),
            "反转机会": cfg.number("strategy_combiner", "weight_reversal"),
            "事件驱动": cfg.number("strategy_combiner", "weight_event"),
            "横截面因子": cfg.number("strategy_combiner", "weight_cross_section"),
        }
        sources_by_symbol: dict[str, set[str]] = {}
        risk_labels_by_symbol: dict[str, set[str]] = {}
        for strategy_name, symbols in selections.items():
            if strategy_name == cls.trend_strategy_name:
                continue
            if strategy_name in cls.risk_or_context_only:
                for symbol in symbols:
                    risk_labels_by_symbol.setdefault(symbol, set()).add(strategy_name)
                continue
            for symbol in symbols:
                sources_by_symbol.setdefault(symbol, set()).add(strategy_name)

        factor_map: dict[str, dict] = {}
        for item in factor_candidates or []:
            symbol = str(item.get("symbol", ""))
            if not symbol:
                continue
            factor_map[symbol] = item
            sources_by_symbol.setdefault(symbol, set()).add("LowPriceMultiFactorStrategy")

        assessment_map = {item.symbol: item for item in assessments}
        details: list[CombinedCandidate] = []
        for symbol, sources in sources_by_symbol.items():
            assessment = assessment_map.get(symbol)
            factor = factor_map.get(symbol)
            factor_rank = int(factor["factor_rank"]) if factor is not None else None
            factor_score = (
                float(factor["combination_factor_score"])
                if factor is not None
                else None
            )
            if factor_rank == 1:
                factor_contribution = cfg.number("strategy_combiner", "factor_rank_1")
            elif factor_rank == 2:
                factor_contribution = cfg.number("strategy_combiner", "factor_rank_2")
            elif factor_rank == 3:
                factor_contribution = cfg.number("strategy_combiner", "factor_rank_3")
            elif factor_rank is not None and factor_rank <= 5:
                factor_contribution = cfg.number("strategy_combiner", "factor_rank_4_5")
            elif factor_rank is not None and factor_rank <= 10:
                factor_contribution = cfg.number("strategy_combiner", "factor_rank_6_10")
            else:
                factor_contribution = 0.0
            families = {
                cls.strategy_families.get(source, source)
                for source in sources
            }
            family_count = len(families)
            signal_score = sum(
                factor_contribution
                if family == "横截面因子" and factor_rank is not None
                else family_weights.get(family, 5.0)
                for family in families
            )
            min_family_count = cfg.integer("strategy_combiner", "cross_family_min_count")
            if family_count >= min_family_count:
                signal_score += cfg.number("strategy_combiner", "cross_family_bonus")
            trend_score = assessment.score if assessment is not None else 0.0
            regime = assessment.regime if assessment is not None else "未评估"
            trend_signal = assessment.entry_signal if assessment is not None else "等待确认"
            reasons = assessment.reasons if assessment is not None else ()
            risk_labels = risk_labels_by_symbol.get(symbol, set())
            risk_deduction = 0.0
            if "LimitUpShakeoutStrategy" in risk_labels:
                risk_deduction += cfg.number(
                    "strategy_combiner", "limit_up_shakeout_penalty"
                )
            vetoed = bool(
                "UptrendLimitDownStrategy" in risk_labels
                and cfg.boolean("strategy_combiner", "uptrend_limit_down_veto")
            )
            has_exit_risk = bool(
                assessment is not None and assessment.exit_signal.endswith("候选")
            )
            if assessment is not None and "清仓候选" in assessment.exit_signal:
                vetoed = True
            risk_passed = bool(
                assessment is not None
                and assessment.score >= cfg.number("strategy_combiner", "risk_pass_score")
                and not has_exit_risk
                and not vetoed
            )
            trend_confirmed = bool(
                assessment is not None
                and risk_passed
                and assessment.score >= cfg.number("strategy_combiner", "trend_confirm_score")
                and assessment.entry_signal != "等待确认"
            )
            vote_count = len(sources)
            combined_score = round(
                max(
                    0.0,
                    min(
                    cfg.number("strategy_combiner", "max_combined_score"),
                    trend_score * cfg.number("strategy_combiner", "trend_score_weight")
                    + signal_score
                    - risk_deduction,
                    ),
                ),
                1,
            )
            details.append(
                CombinedCandidate(
                    symbol=symbol,
                    sources=tuple(sorted(sources)),
                    families=tuple(sorted(families)),
                    vote_count=vote_count,
                    family_count=family_count,
                    signal_score=signal_score,
                    factor_rank=factor_rank,
                    factor_score=factor_score,
                    factor_contribution=factor_contribution,
                    trend_score=trend_score,
                    regime=regime,
                    trend_signal=trend_signal,
                    reasons=tuple(reasons),
                    risk_labels=tuple(sorted(risk_labels)),
                    risk_deduction=risk_deduction,
                    vetoed=vetoed,
                    risk_passed=risk_passed,
                    trend_confirmed=trend_confirmed,
                    combined_score=combined_score,
                )
            )

        details.sort(
            key=lambda item: (
                item.trend_confirmed,
                item.family_count,
                item.combined_score,
                item.trend_score,
                item.vote_count,
            ),
            reverse=True,
        )
        all_candidates = tuple(item.symbol for item in details)
        multi_strategy = tuple(item.symbol for item in details if item.vote_count >= 2)
        min_family_count = cfg.integer("strategy_combiner", "cross_family_min_count")
        multi_family = tuple(
            item.symbol for item in details if item.family_count >= min_family_count
        )
        trend_confirmed = tuple(item.symbol for item in details if item.trend_confirmed)
        focus = tuple(
            item.symbol
            for item in details
            if item.trend_confirmed
            or (item.family_count >= min_family_count and item.risk_passed)
        )
        return CombinedSelection(
            all_candidates=all_candidates,
            multi_strategy=multi_strategy,
            multi_family=multi_family,
            trend_confirmed=trend_confirmed,
            focus=focus,
            details=tuple(details),
        )
