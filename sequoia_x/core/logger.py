"""日志模块：同时提供彩色终端输出和按日期归档的文件日志。"""

import logging
from datetime import date
from pathlib import Path
from threading import RLock

from rich.logging import RichHandler

_FORMAT = "%(name)s - %(message)s"
_FILE_FORMAT = "%(asctime)s | %(levelname)s | %(threadName)s | %(name)s | %(message)s"
_FILE_PREFIX = "sequoia-x-"
_FILE_SUFFIX = ".log"
_file_handler: logging.FileHandler | None = None
_known_loggers: set[str] = set()
_lock = RLock()


def _cleanup_old_logs(log_dir: Path, retention: int) -> None:
    """只清理本模块生成的日志文件，并保留日期最新的指定数量。"""
    files = sorted(
        path for path in log_dir.glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}") if path.is_file()
    )
    for path in files[:-retention]:
        try:
            path.unlink()
        except OSError:
            # 日志清理失败不能影响选股主流程。
            continue


def configure_file_logging(log_dir: str, retention: int = 10) -> Path:
    """配置当天日志文件，并将其挂到所有已创建及后续创建的项目 logger。"""
    if retention < 1:
        raise ValueError("日志保留文件数必须大于等于 1")

    global _file_handler
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"{_FILE_PREFIX}{date.today().isoformat()}{_FILE_SUFFIX}"

    with _lock:
        if _file_handler is not None:
            old_handler = _file_handler
            for logger_name in tuple(_known_loggers):
                logging.getLogger(logger_name).removeHandler(old_handler)
            old_handler.close()

        handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        _file_handler = handler
        for logger_name in tuple(_known_loggers):
            logger = logging.getLogger(logger_name)
            if handler not in logger.handlers:
                logger.addHandler(handler)
        _cleanup_old_logs(directory, retention)
    return log_path


def get_logger(name: str) -> logging.Logger:
    """
    工厂函数，返回配置了 RichHandler 的 Logger 实例。

    支持 DEBUG/INFO/WARNING/ERROR 四级日志，由 rich 自动以不同颜色渲染。
    每条日志包含时间戳、模块名和日志级别。
    同名 logger 不重复添加 handler（幂等性）。

    Args:
        name: logger 名称，通常传入 __name__。

    Returns:
        logging.Logger: 配置好的 Logger 实例。
    """
    logger = logging.getLogger(name)

    with _lock:
        _known_loggers.add(name)
        if not any(isinstance(item, RichHandler) for item in logger.handlers):
            handler = RichHandler(
                rich_tracebacks=True,
                show_path=False,
                log_time_format="[%Y-%m-%d %H:%M:%S]",
            )
            handler.setFormatter(logging.Formatter(_FORMAT))
            logger.addHandler(handler)
        if _file_handler is not None and _file_handler not in logger.handlers:
            logger.addHandler(_file_handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

    return logger
