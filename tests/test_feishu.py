"""飞书通知属性测试。"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
        strategy_webhooks={"prediction": "https://example.com/prediction"},
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

    assert mock_post.call_args.args[0] == "https://example.com/prediction"
    body = json.loads(mock_post.call_args.kwargs["data"])
    card_text = json.dumps(body, ensure_ascii=False)
    assert "贵州茅台" in card_text
    assert "67.3%" in card_text
    assert "AUC" in card_text
    assert "0.609" in card_text


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
