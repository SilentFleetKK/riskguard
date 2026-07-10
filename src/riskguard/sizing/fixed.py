"""固定比例仓位:每次都下总权益的固定百分比。

最朴素、也最难被自己骗的仓位法。不传 ``fraction`` 时,默认用配置里的
``max_position_pct``,天然与单笔仓位上限对齐。
"""

from __future__ import annotations

from ..config import RiskConfig
from ..models import Portfolio, Signal
from .base import PositionSizer


class FixedFractionalSizer(PositionSizer):
    """按固定权重下单。"""

    name = "fixed_fractional"

    def __init__(self, fraction: float | None = None) -> None:
        if fraction is not None and not (0.0 < fraction <= 1.0):
            raise ValueError(f"fraction must be in (0, 1], got {fraction}")
        self.fraction = fraction

    def target_weight(
        self, signal: Signal, portfolio: Portfolio, config: RiskConfig
    ) -> float:
        return self.fraction if self.fraction is not None else config.max_position_pct
