"""带默认值校验的业务阈值配置读取器。"""

from __future__ import annotations

import configparser
from pathlib import Path


class ThresholdConfig:
    """读取项目统一 INI 阈值配置。"""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() and not self.path.exists():
            self.path = Path(__file__).resolve().parents[2] / self.path
        parser = configparser.ConfigParser()
        if not parser.read(self.path, encoding="utf-8"):
            raise FileNotFoundError(f"阈值配置文件不存在：{self.path}")
        self._parser = parser

    def integer(self, section: str, key: str) -> int:
        return self._parser.getint(section, key)

    def number(self, section: str, key: str) -> float:
        return self._parser.getfloat(section, key)
