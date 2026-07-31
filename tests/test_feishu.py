"""飞书通知属性测试。"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.notify.feishu import FeishuNotifier


def make_settings(webhook_url: str = "https://example.com/default") -> Settings:
    return Settings(
        db_path="data/test.db",
        start_date="2024-01-01",
        feishu_webhook_url=webhook_url,
    )


# Feature: sequoia-x-v2, Property 10: 飞书通知包含所有选股结果
@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=1, max_size=10, unique=True,
    )
)
@h_settings(max_examples=50)
def test_notification_contains_all_symbols(symbols: list[str]) -> None:
    """属性 10：send() 发出的请求体应包含所有 symbol。"""
    settings = make_settings()
    notifier = FeishuNotifier(settings)

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.send(symbols=symbols, strategy_name="TestStrategy")

    call_args = mock_post.call_args
    body = json.loads(call_args.kwargs.get("data") or call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["data"])
    card_text = json.dumps(body)
    for symbol in symbols:
        assert symbol in card_text


def test_notification_contains_latest_data_date() -> None:
    """飞书卡片应明确展示策略使用的最新行情日期。"""
    notifier = FeishuNotifier(make_settings())

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.send(
            symbols=["000001"],
            strategy_name="TestStrategy",
            data_date="2026-07-27",
        )

    body = json.loads(mock_post.call_args.kwargs["data"])
    card_text = json.dumps(body, ensure_ascii=False)
    assert "数据日期" in card_text
    assert "2026-07-27" in card_text


def test_notification_uses_local_chinese_stock_name() -> None:
    """飞书卡片应优先展示本地数据库提供的中文名。"""
    notifier = FeishuNotifier(make_settings())

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.send(
            symbols=["600519"],
            strategy_name="TestStrategy",
            stock_names={"600519": "贵州茅台"},
        )

    body = json.loads(mock_post.call_args.kwargs["data"])
    card_text = json.dumps(body, ensure_ascii=False)
    assert "贵州茅台" in card_text
    assert "https://xueqiu.com/S/SH600519" in card_text


def test_prediction_notification_contains_probability_and_metrics() -> None:
    """预测卡片应包含中文名、概率、方向和时间外验证指标。"""
    settings = Settings(
        db_path="data/test.db",
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/default",
        strategy_webhooks={"core_decision": "https://example.com/core-decision"},
    )
    object.__setattr__(
        settings,
        "strategy_webhooks",
        {"core_decision": "https://example.com/core-decision"},
    )
    notifier = FeishuNotifier(settings)
    result = SimpleNamespace(
        symbol="600519",
        data_date="2026-07-27",
        horizon=5,
        direction="上涨",
        up_probability=0.673,
        expected_return=0.028,
    )
    metrics = {
        "roc_auc": 0.609,
        "accuracy": 0.638,
        "baseline_accuracy": 0.635,
        "high_confidence_accuracy": 0.674,
        "brier_score": 0.228,
    }

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"code": 0}),
        )
        notifier.send_prediction(
            [result],
            metrics,
            stock_names={"600519": "贵州茅台"},
        )

    assert mock_post.call_args.args[0] == "https://example.com/core-decision"
    body = json.loads(mock_post.call_args.kwargs["data"])
    card_text = json.dumps(body, ensure_ascii=False)
    assert "贵州茅台" in card_text
    assert "67.3%" in card_text
    assert "AUC" in card_text
    assert "0.609" in card_text


def test_portfolio_report_combines_return_and_advice_in_one_message() -> None:
    """持仓和操作观察应合并为一张飞书卡片。"""
    notifier = FeishuNotifier(make_settings())
    portfolio = pd.DataFrame(
        [
            {
                "symbol": "000783", "name": "长江证券", "shares": 6000,
                "latest_close": 10.0, "data_date": "2026-07-27",
                "return_rate": 0.0353, "market_value": 60000,
                "unrealized_pnl": 2046,
            }
        ]
    )
    advice = [
        SimpleNamespace(
            symbol="000783", name="长江证券", action="继续持有，20日线防守",
            risk="低", reason="均线多头排列", reference_price=10.0,
            next_workday="2026-07-29",
        )
    ]

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"code": 0}),
        )
        notifier.send_portfolio_report(portfolio, advice)

    assert mock_post.call_count == 1
    body = json.loads(mock_post.call_args.kwargs["data"])
    card_text = json.dumps(body, ensure_ascii=False)
    assert "持仓与下一工作日操作观察" in card_text
    assert "3.53%" in card_text
    assert "20日线防守" in card_text


def test_system_alert_contains_failure_and_local_data_date() -> None:
    notifier = FeishuNotifier(make_settings())

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"code": 0}),
        )
        notifier.send_system_alert(
            "baostock 登录或同步失败",
            "登录异常",
            data_date="2026-07-30",
        )

    body = json.loads(mock_post.call_args.kwargs["data"])
    card_text = json.dumps(body, ensure_ascii=False)
    assert "baostock 登录或同步失败" in card_text
    assert "登录异常" in card_text
    assert "2026-07-30" in card_text
    assert "继续使用本地数据" in card_text


def test_intraday_empty_status_and_paper_trades_are_pushed() -> None:
    notifier = FeishuNotifier(make_settings())
    trade = SimpleNamespace(
        symbol="000001", name="平安银行", action="建仓", shares=3000,
        price=10.0, amount=30000.0, reason="盘中突破",
        traded_at="2026-07-30T10:30:00",
    )

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"code": 0}),
        )
        notifier.send_intraday_status(20, 18, 20)
        notifier.send_paper_trades([trade])

    assert mock_post.call_count == 2
    status_text = json.dumps(
        json.loads(mock_post.call_args_list[0].kwargs["data"]), ensure_ascii=False
    )
    trade_text = json.dumps(
        json.loads(mock_post.call_args_list[1].kwargs["data"]), ensure_ascii=False
    )
    assert "暂无新的盘中预警信号" in status_text
    assert "取得实时报价" in status_text
    assert "模拟交易成交" in trade_text
    assert "建仓" in trade_text
    assert "3000股" in trade_text
    assert "不会修改真实持仓" in trade_text


def test_combined_selection_includes_details_and_splits_by_length() -> None:
    notifier = FeishuNotifier(make_settings())
    details = [
        SimpleNamespace(
            symbol=f"00000{index}",
            sources=("MaVolumeStrategy", "PrivatePlacementStrategy"),
            families=("事件驱动", "趋势动量"),
            vote_count=2,
            family_count=2,
            signal_score=30.0,
            factor_rank=index,
            factor_score=1.234,
            factor_contribution=10.0,
            trend_score=70.0,
            trend_signal="A-平台放量突破",
            risk_passed=True,
            trend_confirmed=True,
            combined_score=75.0,
        )
        for index in (1, 2)
    ]

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"code": 0}),
        )
        notifier.send_combined_selection(
            details,
            data_date="2026-07-30",
            stock_names={"000001": "平安银行", "000002": "万科A"},
            consecutive_counts={"000001": 3, "000002": 1},
            max_chars=1,
        )

    assert mock_post.call_count == 2
    all_text = "".join(
        json.dumps(json.loads(call.kwargs["data"]), ensure_ascii=False)
        for call in mock_post.call_args_list
    )
    assert "组合决策重点候选（1/2）" in all_text
    assert "组合评分" in all_text
    assert "策略来源" in all_text
    assert "策略组" in all_text
    assert "低价多因子" in all_text
    assert "风险通过" in all_text
    assert "平安银行" in all_text
    assert "连续入选 3 次" in all_text


def test_prediction_tracking_push_contains_accuracy_and_refresh() -> None:
    notifier = FeishuNotifier(make_settings())
    report = SimpleNamespace(
        data_date="2026-07-30",
        started=("000001",),
        completed=(),
        evaluations=(
            SimpleNamespace(
                symbol="000001", name="平安银行", horizon=1,
                predicted_direction="上涨", actual_return=0.02, accurate=True,
            ),
        ),
        predictions=(
            SimpleNamespace(
                symbol="000001", name="平安银行", target_horizon=3,
                remaining_horizon=2, direction="上涨", probability=0.7,
                expected_return=0.03, refreshed=True,
            ),
        ),
        errors=(),
    )

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"code": 0}),
        )
        notifier.send_prediction_tracking(report)

    card_text = json.dumps(
        json.loads(mock_post.call_args.kwargs["data"]), ensure_ascii=False
    )
    assert "第1个交易日验证" in card_text
    assert "结果：**准确**" in card_text
    assert "刷新预测" in card_text
    assert "距当前剩余：2个交易日" in card_text


def test_prediction_tracking_groups_and_deduplicates_each_stock() -> None:
    """同一股票的多个周期应合并展示，重复的周期结果只发送一次。"""
    notifier = FeishuNotifier(make_settings())
    prediction_3d = SimpleNamespace(
        symbol="000001", name="平安银行", target_horizon=3,
        remaining_horizon=3, direction="上涨", probability=0.7,
        expected_return=0.03, refreshed=False,
    )
    report = SimpleNamespace(
        data_date="2026-07-30",
        started=("000001",),
        completed=(),
        evaluations=(),
        predictions=(
            prediction_3d,
            prediction_3d,
            SimpleNamespace(
                symbol="000001", name="平安银行", target_horizon=5,
                remaining_horizon=5, direction="下跌", probability=0.3,
                expected_return=-0.02, refreshed=False,
            ),
        ),
        errors=(),
    )

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"code": 0}),
        )
        notifier.send_prediction_tracking(report)

    assert mock_post.call_count == 1
    body = json.loads(mock_post.call_args.kwargs["data"])
    content = body["card"]["elements"][2]["text"]["content"]
    assert content.count("### 平安银行 000001") == 1
    assert content.count("周期第3个交易日") == 1
    assert content.count("周期第5个交易日") == 1


# Feature: sequoia-x-v2, Property 11: 飞书通知使用 ConfigManager 中的 Webhook URL
@given(
    webhook_url=st.from_regex(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[a-z0-9\-]{8,36}", fullmatch=True)
)
@h_settings(max_examples=50)
def test_notification_uses_config_url(webhook_url: str) -> None:
    """属性 11：send() 发出的 HTTP 请求目标 URL 应等于 settings.feishu_webhook_url。"""
    settings = make_settings(webhook_url=webhook_url)
    notifier = FeishuNotifier(settings)

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.send(symbols=["000001"], strategy_name="Test", webhook_key="default")

    called_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs.get("url")
    assert called_url == webhook_url


# Feature: sequoia-x-v2, Property 12: HTTP 失败时记录 ERROR 日志
@given(status_code=st.integers(min_value=400, max_value=599))
@h_settings(max_examples=50)
def test_http_failure_logs_error(status_code: int) -> None:
    """属性 12：非 200 响应时，send() 应记录 ERROR 级别日志，不抛出异常。"""
    import logging as _logging
    import sequoia_x.notify.feishu as feishu_module

    settings = make_settings()
    notifier = FeishuNotifier(settings)

    # feishu logger 设置了 propagate=False，需直接在其上挂 handler
    feishu_logger = _logging.getLogger(feishu_module.__name__)
    log_records: list[_logging.LogRecord] = []

    class _ListHandler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            log_records.append(record)

    handler = _ListHandler(_logging.ERROR)
    feishu_logger.addHandler(handler)
    try:
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=status_code, text="error")
            notifier.send(symbols=["000001"], strategy_name="Test")
    finally:
        feishu_logger.removeHandler(handler)

    assert any(r.levelno == _logging.ERROR for r in log_records)
