"""baostock 单连接保护测试。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sequoia_x.data.baostock_gateway import serialized_baostock


def test_baostock_calls_are_serialized_for_the_whole_session() -> None:
    first_started = Event()
    release_first = Event()
    second_started = Event()

    @serialized_baostock
    def first_call() -> None:
        first_started.set()
        assert release_first.wait(timeout=2)

    @serialized_baostock
    def second_call() -> None:
        second_started.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_call)
        assert first_started.wait(timeout=2)
        second = executor.submit(second_call)
        assert not second_started.is_set()
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert second_started.is_set()
