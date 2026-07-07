"""组合层敞口上限:总名义敞口(多空绝对值之和)不得超过权益的某个倍数。

单笔仓位上限管的是"单个标的",本规则管的是"整个组合"——防止你用一堆各自
合规的小仓位,累加成一个过度杠杆的大敞口。默认 ``max_gross_exposure_pct=1.0``
即不加杠杆。可选的净敞口上限用于约束方向性风险。
"""

from __future__ import annotations

from ..models import RuleResult, resolve_price
from ._projection import allowed_quantity, project, within
from .base import RiskRule, RuleContext


class GrossExposureLimit(RiskRule):
    """把全组合总名义敞口压在 ``config.max_gross_exposure_pct * equity`` 以内。"""

    name = "gross_exposure_limit"

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        order, portfolio, config = ctx.order, ctx.portfolio, ctx.config
        equity = portfolio.equity
        if equity <= 0:
            return self.reject(f"non-positive equity ({equity})", equity=equity)

        price = resolve_price(portfolio, order)
        cap = config.max_gross_exposure_pct * equity

        current, projected, increasing = project(order, portfolio)
        cur_symbol_notional = portfolio.position_notional(order.symbol)
        other_gross = portfolio.gross_exposure() - cur_symbol_notional
        projected_symbol_notional = abs(projected) * price
        projected_gross = other_gross + projected_symbol_notional
        projected_ratio = projected_gross / equity

        if not increasing or within(projected_gross, cap):
            return self.approve(
                f"gross exposure {projected_ratio:.2%} <= cap "
                f"{config.max_gross_exposure_pct:.2%}",
                projected_gross_ratio=projected_ratio,
            )

        # 该标的还能占用的敞口额度
        symbol_cap_notional = max(0.0, cap - other_gross)
        symbol_cap_qty = symbol_cap_notional / price
        allowed = allowed_quantity(order, current, symbol_cap_qty)
        detail = dict(
            projected_gross_ratio=projected_ratio,
            cap=config.max_gross_exposure_pct,
            allowed_quantity=allowed,
        )
        if allowed <= 0:
            return self.reject(
                f"gross exposure would reach {projected_ratio:.2%}, over cap "
                f"{config.max_gross_exposure_pct:.2%}",
                **detail,
            )
        return self.resize(
            allowed,
            f"gross exposure capped at {config.max_gross_exposure_pct:.2%} "
            f"(qty {order.quantity:g} -> {allowed:g})",
            **detail,
        )
