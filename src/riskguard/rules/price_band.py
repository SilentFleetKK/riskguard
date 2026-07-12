"""AI 代理闸门之二:fat-finger 价格保护带。

限价单价格偏离参考价超过 ``max_price_band_pct`` 即拒——AI 把 200 打成 2000、
把小数点挪了一位,这类错单不该走到券商。

诚实的边界与刻意的取舍:

* **只约束限价单**:市价单没有声明价可校验,假装能管反而是安全剧场。
* **减仓单豁免**:任何时候不阻止风险收敛(核心原则)。
* **参考价只认 ``portfolio.marks``**:不用 :func:`~riskguard.models.resolve_price`
  ——它对限价单会回退到限价自身,自己跟自己比恒为零偏离,规则形同虚设;也不用
  持仓的 ``avg_price``——陈旧的入场价不是市价。
* **无参考价 → 拒单**(fail-closed):拿不到市价就没有资格判断"这个价对不对",
  宁可拒单也不猜,与全库"绝不静默失败"哲学一致。
"""

from __future__ import annotations

from ..models import OrderType, RuleResult
from .base import RiskRule, RuleContext


class PriceBandRule(RiskRule):
    """限价偏离 mark 超带宽即拒;无 mark 拒单(fail-closed)。"""

    name = "price_band"

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        band = ctx.config.max_price_band_pct
        if band is None:
            return self.approve("price band disabled")

        order = ctx.order
        if order.order_type is not OrderType.LIMIT or order.limit_price is None:
            return self.approve("market order carries no stated price to band")
        if order.reduce_only:
            return self.approve("reduce-only order exempt from price band")

        mark = ctx.portfolio.marks.get(order.symbol)
        if mark is None or mark <= 0.0:
            return self.reject(
                f"no reference price (mark) for {order.symbol} to validate limit "
                "price against — fail closed",
                limit_price=order.limit_price,
            )

        deviation = abs(order.limit_price - mark) / mark
        # 与 _projection.within 同款浮点容差:恰好压线的单不因二进制误差被误杀
        if deviation <= band * (1.0 + 1e-9) + 1e-12:
            return self.approve(
                f"limit price {order.limit_price} within ±{band:.2%} of mark {mark}",
                deviation=deviation,
            )

        return self.reject(
            f"limit price {order.limit_price} deviates {deviation:.2%} from mark "
            f"{mark} — outside ±{band:.2%} fat-finger band",
            deviation=deviation,
            mark=mark,
            limit_price=order.limit_price,
        )
