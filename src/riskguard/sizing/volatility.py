"""波动率目标仓位法。

思路:给每个头寸分配相同的"风险预算"而非相同的"资金"。目标权重 =
目标年化波动率 / 标的的实现年化波动率。波动越大的标的仓位越小,波动越小的越大,
从而让组合整体波动趋近目标。信号需提供 ``volatility``(年化实现波动率,以小数计,
如 0.40 表示 40%)。权重由基类夹到 ``max_sizing_leverage`` 并被下游规则二次约束。
"""

from __future__ import annotations

from ..config import RiskConfig
from ..exceptions import ConfigError
from ..models import Portfolio, Signal
from .base import PositionSizer

_MIN_VOL = 1e-6  # 防止除零/给出爆炸性权重


class VolatilityTargetSizer(PositionSizer):
    """按 目标波动率 / 实现波动率 分配权重。需要信号提供 ``volatility``。"""

    name = "volatility_target"

    def target_weight(
        self, signal: Signal, portfolio: Portfolio, config: RiskConfig
    ) -> float:
        vol = signal.volatility
        if vol is None:
            raise ConfigError(
                "VolatilityTargetSizer requires signal.volatility (annualized, fractional)"
            )
        if vol < 0.0:
            raise ConfigError(f"volatility must be >= 0, got {vol}")
        return config.vol_target_annual / max(vol, _MIN_VOL)
