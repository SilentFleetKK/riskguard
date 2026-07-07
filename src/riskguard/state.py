"""风控运行时状态。

``RiskState`` 是不可变快照:记录权益高点(high-water mark)、熔断开关、
各策略的"入役时间"(用于隔离观察期)。所有"变更"都返回一个新的 RiskState,
绝不原地修改——历史状态因此永远可追溯、可回放。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Optional


def _freeze(m: Mapping[str, datetime]) -> Mapping[str, datetime]:
    return MappingProxyType(dict(m))


@dataclass(frozen=True, slots=True)
class RiskState:
    """引擎在某一时刻的风控状态快照(不可变)。"""

    high_water_mark: float = 0.0
    """观测到的权益历史最高点,回撤的基准。"""

    last_equity: float = 0.0
    """最近一次观测到的权益。"""

    breaker_tripped: bool = False
    """总亏损熔断是否已触发。"""

    tripped_at: Optional[datetime] = None
    """熔断触发时间。"""

    trip_reason: str = ""
    """熔断触发原因的可读描述。"""

    strategy_inception: Mapping[str, datetime] = field(default_factory=dict)
    """各策略首次被登记的时间,隔离观察期由此计算。"""

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_inception", _freeze(self.strategy_inception))

    # ---- 派生量 ----
    @property
    def drawdown(self) -> float:
        """当前相对高点的回撤(0.0 表示在高点,0.2 表示回撤 20%)。"""
        if self.high_water_mark <= 0.0:
            return 0.0
        return max(0.0, 1.0 - self.last_equity / self.high_water_mark)

    # ---- 不可变更新 ----
    def observe_equity(self, equity: float, now: datetime) -> "RiskState":
        """观测一笔新的权益值,返回更新了 last_equity / 高点的新状态。"""
        hwm = max(self.high_water_mark, equity)
        return replace(self, high_water_mark=hwm, last_equity=equity)

    def trip(self, reason: str, now: datetime) -> "RiskState":
        """触发熔断,返回新状态(幂等:已触发则原样返回)。"""
        if self.breaker_tripped:
            return self
        return replace(self, breaker_tripped=True, tripped_at=now, trip_reason=reason)

    def reset_breaker(self, now: datetime) -> "RiskState":
        """人工复盘后重置熔断,并把高点重置到当前权益,避免立刻二次触发。"""
        return replace(
            self,
            breaker_tripped=False,
            tripped_at=None,
            trip_reason="",
            high_water_mark=self.last_equity,
        )

    def register_strategy(self, strategy_id: str, now: datetime) -> "RiskState":
        """登记一个策略的入役时间;已存在则不覆盖(保留最早时间)。"""
        if strategy_id in self.strategy_inception:
            return self
        merged = dict(self.strategy_inception)
        merged[strategy_id] = now
        return replace(self, strategy_inception=merged)

    def strategy_age_days(self, strategy_id: str, now: datetime) -> Optional[float]:
        """策略入役至今的天数;未登记返回 None。"""
        inception = self.strategy_inception.get(strategy_id)
        if inception is None:
            return None
        return (now - inception).total_seconds() / 86400.0

    @classmethod
    def initial(cls, equity: float = 0.0, now: Optional[datetime] = None) -> "RiskState":
        """用初始权益构造起始状态,高点即为初始权益。"""
        return cls(high_water_mark=equity, last_equity=equity)
