"""AI 代理闸门之三:下单频率节流。

一个失控的 AI 循环可以在一分钟里打出几百笔"各自看起来都合规"的订单——单笔仓位、
敞口、熔断全拦不住它,因为每一笔都不越线。节流规则按滚动窗口对**已批准**的订单
计数,触顶即拒,让失控循环最多烧掉一个窗口的配额。

计数依据是 :attr:`~riskguard.state.RiskState.recent_orders`(引擎在每次批准后
记录,被拒的单不占配额)。**当前这笔单在评估时尚未入账**,所以 cap=N 恰好允许
窗口内 N 单、第 N+1 单被拒。

减仓单默认**完全豁免**("减仓永远放行"核心原则原样保留)。显式设置
:attr:`~riskguard.config.RiskConfig.reduce_only_throttle_factor` 后才对减仓单
生效:单独计桶、上限放宽到 cap × factor——这是给"连减仓循环也要有限"的运营者
的一次 opt-in 偏离(无界减仓循环也有代价:券商限频、手续费、失控代理反复平仓
停不下来),放宽倍数保证它几乎不可能误伤真实的风险收敛,但循环终会被封。
"""

from __future__ import annotations

from datetime import timedelta

from ..models import RuleResult
from .base import RiskRule, RuleContext

_MINUTE = timedelta(seconds=60)
_HOUR = timedelta(seconds=3600)


class OrderThrottle(RiskRule):
    """滚动窗口订单数触顶即拒;减仓单单独计桶(cap × factor)。"""

    name = "order_throttle"

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        per_minute = ctx.config.max_orders_per_minute
        per_hour = ctx.config.max_orders_per_hour
        if per_minute is None and per_hour is None:
            return self.approve("order throttle disabled")

        reduce_only = ctx.order.reduce_only
        if reduce_only and ctx.config.reduce_only_throttle_factor is None:
            return self.approve(
                "reduce-only order exempt from throttle (core principle; set "
                "reduce_only_throttle_factor to opt into a finite cap)"
            )
        factor = ctx.config.reduce_only_throttle_factor if reduce_only else 1.0
        bucket = "reduce-only" if reduce_only else "normal"

        for window, cap, label in (
            (_MINUTE, per_minute, "minute"),
            (_HOUR, per_hour, "hour"),
        ):
            if cap is None:
                continue
            effective_cap = int(cap * factor)
            count = ctx.state.orders_in_window(
                ctx.now, window, reduce_only=reduce_only
            )
            if count >= effective_cap:
                return self.reject(
                    f"order throttle: {count} {bucket} orders approved in the last "
                    f"{label} >= cap {effective_cap} — runaway loop protection",
                    window=label,
                    count=count,
                    cap=effective_cap,
                    reduce_only=reduce_only,
                )

        return self.approve(
            f"order throttle: {bucket} bucket within budget",
            reduce_only=reduce_only,
        )
