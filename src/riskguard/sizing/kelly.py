"""Kelly 判据仓位法(分数 Kelly)。

Kelly 公式给出理论最优下注比例:

    f* = p - q/b = (b·p - q) / b

其中 p 为胜率,q = 1 - p 为败率,b 为盈亏比(平均盈利 / 平均亏损)。满 Kelly
波动极大、实务几乎不用;这里乘以 ``config.kelly_fraction``(默认 0.5)做分数
Kelly。无正期望(f* ≤ 0)时返回 0——不下注就是最好的下注。最终权重仍会被
基类夹到 ``max_sizing_leverage``,并被下游仓位上限规则二次约束。
"""

from __future__ import annotations

from ..config import RiskConfig
from ..exceptions import ConfigError
from ..models import Portfolio, Signal
from .base import PositionSizer


class KellySizer(PositionSizer):
    """分数 Kelly 仓位法。需要信号提供 ``win_probability`` 与 ``payoff_ratio``。"""

    name = "kelly"

    def target_weight(
        self, signal: Signal, portfolio: Portfolio, config: RiskConfig
    ) -> float:
        p = signal.win_probability
        b = signal.payoff_ratio
        if p is None or b is None:
            raise ConfigError(
                "KellySizer requires signal.win_probability and signal.payoff_ratio"
            )
        if not (0.0 <= p <= 1.0):
            raise ConfigError(f"win_probability must be in [0, 1], got {p}")
        if b <= 0.0:
            raise ConfigError(f"payoff_ratio must be > 0, got {b}")

        q = 1.0 - p
        f_star = (b * p - q) / b  # 等价于 p - q/b
        if f_star <= 0.0:
            return 0.0  # 无正期望,不下注
        return config.kelly_fraction * f_star
