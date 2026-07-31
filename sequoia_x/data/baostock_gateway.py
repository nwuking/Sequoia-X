"""baostock 进程级单连接保护。"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from threading import RLock
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

_BAOSTOCK_SESSION_LOCK = RLock()


def serialized_baostock(func: Callable[P, R]) -> Callable[P, R]:
    """确保 login、查询与 logout 所在的完整调用不会并发执行。"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with _BAOSTOCK_SESSION_LOCK:
            return func(*args, **kwargs)

    return wrapper
