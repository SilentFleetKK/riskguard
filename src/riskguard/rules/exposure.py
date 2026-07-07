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
        current, projected, increasing = project(order, portfolio)

        # 减仓 / reduce_only 永远放行,且不依赖 equity 是否有效。
        if order.reduce_only or not increasing:
            return self.approve("reducing/flat — always allowed")

        equity = portfolio.equity
        if equity <= 0:
            return self.reject(
                f"non-positive equity ({equity}); cannot size a risk-increasing order",
                equity=equity,
            )

        price = resolve_price(portfolio, order)
        cap = config.max_gross_exposure_pct * equity

        cur_symbol_notional = portfolio.position_notional(order.symbol)
        other_gross = portfolio.gross_exposure() - cur_symbol_notional
        projected_symbol_notional = abs(projected) * price
        projected_gross = other_gross + projected_symbol_notional
        projected_ratio = projected_gross / equity

        if within(projected_gross, cap):
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


class NetExposureLimit(RiskRule):
    """把全组合**净敞口**(多头市值 − 空头市值)压在 ``±max_net_exposure_pct × equity``。

    总敞口(gross)管的是"总杠杆",净敞口(net)管的是"方向性风险":一个多空对冲组合
    可能 gross 很大但 net 接近 0。``max_net_exposure_pct`` 为 ``None``(默认)时本规则是
    无害的空操作;一旦设值,就真正生效——不再是"设了却没人读"的死配置。
    """

    name = "net_exposure_limit"

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        order, portfolio, config = ctx.order, ctx.portfolio, ctx.config
        cap_pct = config.max_net_exposure_pct
        if cap_pct is None:
            return self.approve("net exposure limit disabled (max_net_exposure_pct=None)")

        current, projected, _ = project(order, portfolio)
        if order.reduce_only:
            return self.approve("reduce_only — always allowed")

        price = resolve_price(portfolio, order)
        other_net = 0.0
        for sym, pos in portfolio.positions.items():
            if sym == order.symbol:
                continue
            mark = portfolio.mark_for(sym)
            if mark is not None:
                other_net += pos.market_value(mark)
        current_net = other_net + current * price
        projected_net = other_net + projected * price

        # 使 |净敞口| 变小(降低方向性风险)→ 永远放行,不依赖 equity。
        if abs(projected_net) <= abs(current_net) + 1e-9:
            return self.approve(
                f"net exposure moves toward flat ({projected_net:+.0f})",
                projected_net=projected_net,
            )

        equity = portfolio.equity
        if equity <= 0:
            return self.reject(
                f"non-positive equity ({equity}); cannot increase net exposure",
                equity=equity,
            )

        cap_val = cap_pct * equity
        projected_ratio = projected_net / equity
        if within(abs(projected_net), cap_val):
            return self.approve(
                f"net exposure {projected_ratio:+.2%} within cap ±{cap_pct:.2%}",
                net_ratio=projected_ratio,
            )

        allowed = max(0.0, cap_val - order.side.sign * current_net) / price
        detail = dict(net_ratio=projected_ratio, cap=cap_pct, allowed_quantity=allowed)
        if allowed <= 0:
            return self.reject(
                f"net exposure would reach {projected_ratio:+.2%}, over cap ±{cap_pct:.2%}",
                **detail,
            )
        return self.resize(
            allowed,
            f"net exposure capped at ±{cap_pct:.2%} (qty {order.quantity:g} -> {allowed:g})",
            **detail,
        )
