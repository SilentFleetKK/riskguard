"""AI 代理闸门之一:日内亏损熔断线。

与规则二(:class:`~riskguard.rules.drawdown.DrawdownCircuitBreaker`,相对历史
高点、跨日累计)互补:本规则抓的是"今天快速失血"。高点可能是几个月前的事,当日
从平盘急跌 3% 远够不着 15% 的总线——但对一个失控的 AI 代理来说,这正是最该拉闸
的时刻。

熔断的**置位**由引擎在观测权益时完成(镜像总回撤熔断的既有分工),本规则只做
纯读取:已触发则拒绝一切放大风险的单、放行减仓;未触发即放行。触发后**当日粘性**
——哪怕权益回血到线内也不解除,防止"回血一点就重新上杠杆"的赌徒循环;换日
(:attr:`~riskguard.config.RiskConfig.session_boundary_utc`)自动复位。
"""

from __future__ import annotations

from ..models import RuleResult
from ._projection import project
from .base import RiskRule, RuleContext


class DailyLossLimit(RiskRule):
    """日内亏损触线后拒绝新开仓/加仓,仅放行减仓;换日自动复位。"""

    name = "daily_loss_limit"

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        limit = ctx.config.max_daily_loss_pct
        if limit is None:
            return self.approve("daily loss limit disabled")

        state = ctx.state
        if not state.daily_tripped:
            return self.approve(
                f"daily loss {state.daily_loss:.2%} within limit {limit:.2%}",
                daily_loss=state.daily_loss,
            )

        order = ctx.order
        _, _, increasing = project(order, ctx.portfolio)
        if order.reduce_only or not increasing:
            return self.approve(
                "daily loss line tripped but order reduces risk — allowed",
                reduce_only=order.reduce_only,
                trip_reason=state.daily_trip_reason,
            )

        return self.reject(
            f"daily loss line TRIPPED ({state.daily_trip_reason}); new/increasing "
            f"positions blocked until next session "
            f"(boundary {ctx.config.session_boundary_utc} UTC)",
            trip_reason=state.daily_trip_reason,
            daily_loss=state.daily_loss,
        )
