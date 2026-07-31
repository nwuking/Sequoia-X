"""组合候选连续入选次数测试。"""

import tempfile
from pathlib import Path

from sequoia_x.strategy.selection_state import update_consecutive_counts


def test_consecutive_counts_do_not_repeat_on_same_date_and_reset_after_gap() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = str(Path(tmp_dir) / "streak.json")
        first = update_consecutive_counts(path, ["000001", "000002"], "2026-07-30")
        repeated = update_consecutive_counts(path, ["000001", "000002"], "2026-07-30")
        second = update_consecutive_counts(path, ["000001", "000003"], "2026-07-31")
        third = update_consecutive_counts(path, ["000002"], "2026-08-03")

    assert first == {"000001": 1, "000002": 1}
    assert repeated == first
    assert second == {"000001": 2, "000003": 1}
    assert third == {"000002": 1}
