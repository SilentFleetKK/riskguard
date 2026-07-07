"""规则二:总亏损熔断线。

文章三条铁律的第二条——整体回撤触及红线(默认 15%),系统立刻停止新开仓,
交人工复盘。熔断状态由引擎在观测权益时置位;本规则据此拒绝一切"放大风险"的单,
但**永远放行减仓/平仓单**,否则一旦熔断反而无法收敛风险,那是灾难而非保护。
"""

from __future__ import annotations

from ..models import RuleResult
from ._projection import project
from .base import RiskRule, RuleContext


class DrawdownCircuitBreaker(RiskRule):
    """熔断触发后拒绝新开仓/加仓,仅放行减仓。"""

    name = "drawdown_circuit_breaker"

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        state = ctx.state
        if not state.breaker_tripped:
            return self.approve(
                f"drawdown {state.drawdown:.2%} within limit "
                f"{ctx.config.max_drawdown_pct:.2%}",
                drawdown=state.drawdown,
            )

        order = ctx.order
        _, projected, increasing = project(order, ctx.portfolio)
        # reduce_only 标记,或成交后 |持仓| 不增加 → 视为减仓,放行
        if order.reduce_only or not increasing:
            return self.approve(
                "circuit breaker tripped but order reduces risk — allowed",
                reduce_only=order.reduce_only,
                trip_reason=state.trip_reason,
            )

        return self.reject(
            f"circuit breaker TRIPPED ({state.trip_reason}); new/increasing "
            f"positions blocked until manual reset",
            trip_reason=state.trip_reason,
            drawdown=state.drawdown,
        )
