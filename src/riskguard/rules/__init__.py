"""风控规则集合与默认组装。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import RiskRule, RuleContext
from .daily_loss import DailyLossLimit
from .drawdown import DrawdownCircuitBreaker
from .exposure import GrossExposureLimit, NetExposureLimit
from .position_limit import MaxPositionLimit
from .price_band import PriceBandRule
from .quarantine import StrategyQuarantine
from .throttle import OrderThrottle

if TYPE_CHECKING:
    from ..config import RiskConfig


def build_default_rules(config: RiskConfig) -> list[RiskRule]:
    """按配置组装默认规则栈:文章那三条铁律 + 组合层敞口上限 + AI 代理闸门三件套。

    顺序上把熔断放最前(命中即快速拒单),其余规则的结果由引擎统一聚合,
    因此顺序不影响最终裁决,只影响可读性。三件套在默认配置下是空操作
    (相关阈值为 None),由预设或显式配置开启。
    """
    return [
        DrawdownCircuitBreaker(),
        DailyLossLimit(),  # 仅当 max_daily_loss_pct 设值时生效,否则空操作
        MaxPositionLimit(),
        StrategyQuarantine(),
        GrossExposureLimit(),
        NetExposureLimit(),  # 仅当 max_net_exposure_pct 设值时生效,否则空操作
        PriceBandRule(),  # 仅当 max_price_band_pct 设值时生效,否则空操作
        OrderThrottle(),  # 仅当任一频率上限设值时生效,否则空操作
    ]


__all__ = [
    "RiskRule",
    "RuleContext",
    "DailyLossLimit",
    "DrawdownCircuitBreaker",
    "MaxPositionLimit",
    "OrderThrottle",
    "PriceBandRule",
    "StrategyQuarantine",
    "GrossExposureLimit",
    "NetExposureLimit",
    "build_default_rules",
]
