"""多策略组合决策层测试。"""

from sequoia_x.strategy.combiner import StrategyCombiner
from sequoia_x.strategy.comprehensive_trend import TrendAssessment


def _assessment(
    symbol: str,
    score: float,
    entry_signal: str,
    exit_signal: str = "趋势持有/观察",
) -> TrendAssessment:
    return TrendAssessment(
        symbol=symbol,
        score=score,
        regime="主升浪",
        entry_signal=entry_signal,
        exit_signal=exit_signal,
        close=10.0,
        stop_price=9.0,
        position_risk_pct=10.0,
        reasons=(),
    )


def test_combiner_builds_resonance_and_trend_confirmed_focus_pool() -> None:
    selections = {
        "ComprehensiveTrendStrategy": ["000001", "000004"],
        "MaVolumeStrategy": ["000001", "000002", "000004"],
        "RpsBreakoutStrategy": ["000002", "000003", "000004"],
        "PrivatePlacementStrategy": ["000002"],
    }
    assessments = [
        _assessment("000001", 80, "A-平台放量突破"),
        _assessment("000002", 60, "等待确认"),
        _assessment("000004", 78, "A-平台放量突破", "短线过热/分批止盈候选"),
    ]

    result = StrategyCombiner.combine(selections, assessments)

    assert set(result.multi_strategy) == {"000002", "000004"}
    assert result.multi_family == ()
    assert result.trend_confirmed == ("000001",)
    assert result.focus == ("000001",)
    assert "000003" in result.all_candidates
    detail = {item.symbol: item for item in result.details}
    assert detail["000001"].vote_count == 1
    assert detail["000002"].family_count == 1
    assert detail["000002"].families == ("趋势动量",)
    assert detail["000002"].risk_passed is True
    assert detail["000004"].family_count == 1
    assert detail["000004"].risk_passed is False
    assert detail["000004"].trend_confirmed is False


def test_combiner_does_not_use_trend_only_result_as_initial_candidate() -> None:
    result = StrategyCombiner.combine(
        {"ComprehensiveTrendStrategy": ["600519"]},
        [_assessment("600519", 90, "A-平台放量突破")],
    )

    assert result.all_candidates == ()
    assert result.focus == ()


def test_combiner_uses_detailed_factor_ranking_with_decreasing_weight() -> None:
    assessments = [
        _assessment("000001", 55, "等待确认"),
        _assessment("000004", 55, "等待确认"),
        _assessment("000008", 55, "等待确认"),
    ]
    result = StrategyCombiner.combine(
        {},
        assessments,
        factor_candidates=[
            {"symbol": "000001", "factor_rank": 1, "combination_factor_score": 2.0},
            {"symbol": "000004", "factor_rank": 4, "combination_factor_score": 1.0},
            {"symbol": "000008", "factor_rank": 8, "combination_factor_score": 0.2},
        ],
    )

    details = {item.symbol: item for item in result.details}
    assert result.all_candidates == ("000001", "000004", "000008")
    assert details["000001"].factor_contribution == 10.0
    assert details["000004"].factor_contribution == 6.0
    assert details["000008"].factor_contribution == 3.0
    assert details["000004"].factor_score == 1.0


def test_combiner_penalizes_divergence_and_vetoes_limit_down() -> None:
    selections = {
        "RpsBreakoutStrategy": ["000001", "000002"],
        "LimitUpShakeoutStrategy": ["000001"],
        "UptrendLimitDownStrategy": ["000002"],
    }
    assessments = [
        _assessment("000001", 80, "A-平台放量突破"),
        _assessment("000002", 80, "A-平台放量突破"),
    ]

    result = StrategyCombiner.combine(selections, assessments)
    details = {item.symbol: item for item in result.details}

    assert details["000001"].risk_deduction == 15
    assert details["000001"].vetoed is False
    assert details["000001"].combined_score == 40
    assert details["000002"].vetoed is True
    assert details["000002"].risk_passed is False
    assert "000002" not in result.focus
