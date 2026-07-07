"""券商适配器集合。

``PaperBroker`` 零依赖、始终可用。``AlpacaBroker`` 依赖可选包 ``alpaca-py``,
未安装或适配器缺失时为 ``None``,不影响其余功能导入。
"""

from __future__ import annotations

from .base import Broker, BrokerOrder
from .paper import PaperBroker

try:  # 可选适配器:需要 `pip install riskguard[alpaca]`
    from .alpaca import AlpacaBroker
except Exception:  # pragma: no cover - 依赖或文件缺失时优雅降级
    AlpacaBroker = None  # type: ignore[assignment]

__all__ = ["Broker", "BrokerOrder", "PaperBroker", "AlpacaBroker"]
