"""日志系统属性测试。"""

import logging
from datetime import date, timedelta

from hypothesis import given, settings as h_settings
from hypothesis import strategies as st


# Feature: sequoia-x-v2, Property 3: get_logger 同名返回同一实例
@given(name=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="._")))
@h_settings(max_examples=100)
def test_get_logger_same_instance(name: str) -> None:
    """属性 3：对任意 name，多次调用 get_logger(name) 应返回同一 Logger 实例。"""
    from sequoia_x.core.logger import get_logger
    logger1 = get_logger(name)
    handler_count = len(logger1.handlers)
    logger2 = get_logger(name)
    assert logger1 is logger2
    # 确保 handler 没有被重复添加
    assert len(logger2.handlers) == handler_count


def test_daily_file_logging_and_retention(tmp_path) -> None:
    """每天共用一个日志文件，并且只保留最新的 10 个应用日志。"""
    from sequoia_x.core.logger import configure_file_logging, get_logger

    for days_ago in range(1, 12):
        log_date = date.today() - timedelta(days=days_ago)
        (tmp_path / f"sequoia-x-{log_date.isoformat()}.log").write_text(
            "old\n", encoding="utf-8"
        )
    unrelated = tmp_path / "daily.log"
    unrelated.write_text("keep\n", encoding="utf-8")

    log_path = configure_file_logging(str(tmp_path), retention=10)
    logger = get_logger("tests.daily_file")
    logger.info("关键路径测试消息")
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()

    assert log_path.name == f"sequoia-x-{date.today().isoformat()}.log"
    assert "关键路径测试消息" in log_path.read_text(encoding="utf-8")
    assert len(list(tmp_path.glob("sequoia-x-*.log"))) == 10
    assert unrelated.exists()
