"""规则三:新策略隔离观察。

文章三条铁律的第三条——任何新策略,先用最小的钱跑,活过隔离期(默认 90 天)
再考虑加仓。策略入役时间由引擎自动登记(首次见到该策略即登记),也可显式
:meth:`RiskEngine.register_strategy`。隔离期内,该策略单标的仓位受
``quarantine_max_position_pct`` 更严格约束。
"""

from __future__ import annotations

from ..models import RuleResult, resolve_price
from ._projection import allowed_quantity, project, within
from .base import RiskRule, RuleContext


class StrategyQuarantine(RiskRule):
    """隔离期内的策略适用更严格的单标的仓位上限。"""

    name = "strategy_quarantine"

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        order, portfolio, config = ctx.order, ctx.portfolio, ctx.config
        age = ctx.state.strategy_age_days(order.strategy_id, ctx.now)

        # 未登记(理论上引擎会登记)或已出观察期 → 不额外约束
        if age is None or age >= config.quarantine_days:
            return self.approve(
                f"strategy {order.strategy_id!r} out of quarantine "
                f"(age={age if age is None else round(age, 1)}d)",
                age_days=age,
            )

        equity = portfolio.equity
        if equity <= 0:
            return self.reject(f"non-positive equity ({equity})", equity=equity)

        price = resolve_price(portfolio, order)
        cap_qty = (config.quarantine_max_position_pct * equity) / price
        current, projected, increasing = project(order, portfolio)
        projected_weight = abs(projected) * price / equity

        if not increasing or within(abs(projected), cap_qty):
            return self.approve(
                f"quarantined strategy {order.strategy_id!r}: weight "
                f"{projected_weight:.2%} <= cap {config.quarantine_max_position_pct:.2%}",
                age_days=age,
                projected_weight=projected_weight,
            )

        allowed = allowed_quantity(order, current, cap_qty)
        detail = dict(
            strategy_id=order.strategy_id,
            age_days=age,
            projected_weight=projected_weight,
            cap=config.quarantine_max_position_pct,
            allowed_quantity=allowed,
        )
        if allowed <= 0:
            return self.reject(
                f"quarantined strategy {order.strategy_id!r} capped at "
                f"{config.quarantine_max_position_pct:.2%}; order rejected",
                **detail,
            )
        return self.resize(
            allowed,
            f"quarantined strategy {order.strategy_id!r} capped at "
            f"{config.quarantine_max_position_pct:.2%} (qty {order.quantity:g} -> {allowed:g})",
            **detail,
        )
