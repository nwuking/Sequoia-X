"""组合重点候选连续推送次数状态。"""

from __future__ import annotations

import json
from pathlib import Path


def update_consecutive_counts(
    path_value: str,
    symbols: list[str] | tuple[str, ...],
    data_date: str,
) -> dict[str, int]:
    """按数据日期更新连续入选次数，同日重复运行不会重复累加。"""
    path = Path(path_value)
    previous: dict = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            previous = {}
    previous_symbols = set(previous.get("symbols", []))
    previous_counts = previous.get("counts", {})
    if previous.get("data_date") == data_date:
        return {symbol: int(previous_counts.get(symbol, 1)) for symbol in symbols}
    counts = {
        symbol: int(previous_counts.get(symbol, 0)) + 1 if symbol in previous_symbols else 1
        for symbol in symbols
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"data_date": data_date, "symbols": list(symbols), "counts": counts},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return counts
