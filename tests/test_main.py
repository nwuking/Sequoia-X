"""主程序入口属性测试。"""

import sys
from threading import Barrier
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

# 预先导入 main 模块，避免在 @given 循环中重复导入
import main as main_module


def test_strategies_run_concurrently_and_results_keep_declared_order() -> None:
    barrier = Barrier(2)

    class FirstStrategy:
        def run(self) -> list[str]:
            barrier.wait(timeout=2)
            return ["000001"]

    class SecondStrategy:
        def run(self) -> list[str]:
            barrier.wait(timeout=2)
            return ["000002"]

    result = main_module._run_strategies(
        [FirstStrategy(), SecondStrategy()],  # type: ignore[list-item]
        max_workers=2,
        logger=MagicMock(),
    )

    assert list(result) == ["FirstStrategy", "SecondStrategy"]
    assert result == {
        "FirstStrategy": ["000001"],
        "SecondStrategy": ["000002"],
    }


def test_strategy_failure_does_not_cancel_other_futures() -> None:
    class FailedStrategy:
        def run(self) -> list[str]:
            raise RuntimeError("failed")

    class HealthyStrategy:
        def run(self) -> list[str]:
            return ["600519"]

    result = main_module._run_strategies(
        [FailedStrategy(), HealthyStrategy()],  # type: ignore[list-item]
        max_workers=2,
        logger=MagicMock(),
    )

    assert result == {"FailedStrategy": [], "HealthyStrategy": ["600519"]}


def test_sync_failure_alerts_and_continues_by_default() -> None:
    """默认模式同步失败应推送告警并降级使用本地数据。"""
    engine = MagicMock()
    engine.sync_today_bulk.side_effect = RuntimeError("sync failed")
    engine.get_latest_date.return_value = "2026-07-30"
    notifier = MagicMock()
    logger = MagicMock()

    result = main_module._sync_latest(
        engine,
        force=False,
        logger=logger,
        notifier=notifier,
    )

    assert result is False
    logger.warning.assert_called_once()
    notifier.send_system_alert.assert_called_once_with(
        title="baostock 登录或同步失败",
        message="sync failed",
        data_date="2026-07-30",
    )


def test_force_continues_after_sync_failure() -> None:
    """保留 --force 兼容性，同步失败仍记录告警并继续。"""
    engine = MagicMock()
    engine.sync_today_bulk.side_effect = RuntimeError("sync failed")
    logger = MagicMock()

    result = main_module._sync_latest(engine, force=True, logger=logger)

    assert result is False
    logger.warning.assert_called_once()
    assert "使用本地数据" in logger.warning.call_args.args[0]


# Feature: sequoia-x-v2, Property 13: 主程序异常以非零退出码终止
@given(error_msg=st.text(min_size=1, max_size=100))
@h_settings(max_examples=30, deadline=None)
def test_main_exits_nonzero_on_exception(error_msg: str) -> None:
    """属性 13：main() 中任意未捕获异常应导致 sys.exit(1)。"""
    # patch main 模块中直接引用的 get_settings
    with patch.object(main_module, "get_settings", side_effect=RuntimeError(error_msg)):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
        assert exc_info.value.code != 0
