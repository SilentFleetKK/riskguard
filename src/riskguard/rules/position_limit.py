"""规则一:单笔仓位上限。

文章三条铁律的第一条——"别把身家压在一个想法上"。任一标的成交后的名义敞口
不得超过总权益的 ``max_position_pct``。减仓单永远放行(风控只拦"把风险做大"的单)。
越界时按配置缩单或拒单。
"""

from __future__ import annotations

from ..models import Portfolio, RuleResult, resolve_price
from ._projection import allowed_quantity, project, within
from .base import RiskRule, RuleContext


class MaxPositionLimit(RiskRule):
    """把任一标的的名义敞口占比压在 ``config.max_position_pct`` 以内。"""

    name = "max_position_limit"

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        order, portfolio, config = ctx.order, ctx.portfolio, ctx.config
        equity = portfolio.equity
        if equity <= 0:
            return self.reject(
                f"non-positive equity ({equity}); cannot size {order.symbol}",
                equity=equity,
            )

        price = resolve_price(portfolio, order)
        cap_qty = (config.max_position_pct * equity) / price
        current, projected, increasing = project(order, portfolio)
        projected_weight = abs(projected) * price / equity

        # 减仓单,或成交后仍在上限内 → 放行
        if not increasing or within(abs(projected), cap_qty):
            return self.approve(
                f"{order.symbol} projected weight {projected_weight:.2%} "
                f"<= cap {config.max_position_pct:.2%}",
                projected_weight=projected_weight,
                cap=config.max_position_pct,
            )

        # 越界
        allowed = allowed_quantity(order, current, cap_qty)
        detail = dict(
            symbol=order.symbol,
            projected_weight=projected_weight,
            cap=config.max_position_pct,
            allowed_quantity=allowed,
        )
        if config.on_position_breach == "reject" or allowed <= 0:
            return self.reject(
                f"{order.symbol} would reach {projected_weight:.2%} of equity, "
                f"over cap {config.max_position_pct:.2%}",
                **detail,
            )
        return self.resize(
            allowed,
            f"{order.symbol} capped to {config.max_position_pct:.2%} of equity "
            f"(qty {order.quantity:g} -> {allowed:g})",
            **detail,
        )
