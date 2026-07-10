"""仓位算法的抽象基类。

一个 sizer 把策略信号(:class:`~riskguard.models.Signal`)换算成一笔具体订单
(:class:`~riskguard.models.Order`)。注意:sizer 只负责"下多大注",最终是否放行、
是否再被缩量,仍由风控引擎的规则层说了算——两层职责严格分离。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from ..config import RiskConfig
from ..models import Order, OrderType, Portfolio, Signal


class PositionSizer(ABC):
    """所有仓位算法的基类。"""

    name: str = "sizer"

    @abstractmethod
    def target_weight(self, signal: Signal, portfolio: Portfolio, config: RiskConfig) -> float:
        """返回目标权重(名义敞口 / 权益),恒为非负;方向由信号决定。

        子类实现各自的公式(固定比例 / Kelly / 波动率目标)。基类的
        :meth:`size` 会据此换算成订单数量,并统一施加 ``max_sizing_leverage`` 上限。
        """

    def size(
        self, signal: Signal, portfolio: Portfolio, config: RiskConfig
    ) -> Order | None:
        """把信号换算成订单;权重会被夹在 ``[0, max_sizing_leverage]``。

        当目标权重/数量收敛到 0(例如 Kelly 判定"无正期望,不下注")时,返回
        ``None`` —— 明确表示"这一笔不交易",而不是伪造一笔极小的幻影单去成交、
        白扣手续费。调用方(及 :meth:`RiskEngine.size_and_submit`)据此跳过提交。
        """
        weight = self.target_weight(signal, portfolio, config)
        if not math.isfinite(weight) or weight < 0:
            weight = 0.0
        weight = min(weight, config.max_sizing_leverage)

        equity = portfolio.equity
        notional = max(0.0, weight * equity)
        quantity = notional / signal.price if signal.price > 0 else 0.0

        if quantity <= 0:
            return None  # 不下注就是最好的下注

        return Order(
            symbol=signal.symbol,
            side=signal.side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            strategy_id=signal.strategy_id,
            meta={
                "sizer": self.name,
                "target_weight": weight,
                "target_notional": notional,
                "signal_price": signal.price,
            },
        )
