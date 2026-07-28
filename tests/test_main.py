"""主程序入口属性测试。"""

import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

# 预先导入 main 模块，避免在 @given 循环中重复导入
import main as main_module


def test_sync_failure_exits_by_default() -> None:
    """默认模式下增量同步失败应继续向上抛出。"""
    engine = MagicMock()
    engine.sync_today_bulk.side_effect = RuntimeError("sync failed")

    with pytest.raises(RuntimeError, match="sync failed"):
        main_module._sync_latest(engine, force=False, logger=MagicMock())


def test_force_continues_after_sync_failure() -> None:
    """--force 模式下增量同步失败应记录警告并继续。"""
    engine = MagicMock()
    engine.sync_today_bulk.side_effect = RuntimeError("sync failed")
    logger = MagicMock()

    main_module._sync_latest(engine, force=True, logger=logger)

    logger.warning.assert_called_once()
    assert "--force" in logger.warning.call_args.args[0]


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
