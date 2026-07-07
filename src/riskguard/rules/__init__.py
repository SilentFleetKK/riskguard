"""风控规则集合与默认组装。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import RiskRule, RuleContext
from .drawdown import DrawdownCircuitBreaker
from .exposure import GrossExposureLimit
from .position_limit import MaxPositionLimit
from .quarantine import StrategyQuarantine

if TYPE_CHECKING:
    from ..config import RiskConfig


def build_default_rules(config: "RiskConfig") -> list[RiskRule]:
    """按配置组装默认规则栈:文章那三条铁律 + 组合层敞口上限。

    顺序上把熔断放最前(命中即快速拒单),其余规则的结果由引擎统一聚合,
    因此顺序不影响最终裁决,只影响可读性。
    """
    return [
        DrawdownCircuitBreaker(),
        MaxPositionLimit(),
        StrategyQuarantine(),
        GrossExposureLimit(),
    ]


__all__ = [
    "RiskRule",
    "RuleContext",
    "DrawdownCircuitBreaker",
    "MaxPositionLimit",
    "StrategyQuarantine",
    "GrossExposureLimit",
    "build_default_rules",
]
