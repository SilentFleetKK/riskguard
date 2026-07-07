"""实时监控守护进程。

``RiskMonitor`` 周期性观测账户权益、评估熔断,并可在触发时自动踩刹车
(撤单 + 平仓)。适配器文件暂缺时为 ``None``。
"""

from __future__ import annotations

try:
    from .daemon import RiskMonitor
except Exception:  # pragma: no cover
    RiskMonitor = None  # type: ignore[assignment]

__all__ = ["RiskMonitor"]
