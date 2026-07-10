"""框架无关的风险叠加层:把"目标持仓"翻译成"风控批准的下一步订单"。

回测框架里的策略,通常想的是**目标**——"我想满仓做多"(目标权重 +1)、"清仓"
(目标权重 0)。:class:`RiskOverlay` 把这个目标翻译成从当前持仓到目标的差额订单,
过一遍 :class:`~riskguard.engine.RiskEngine`(仓位上限 / 回撤熔断 / 敞口),返回
**批准或缩量后**的订单;熔断期间的新增仓位会被拦下。

这是 backtesting.py / vectorbt 适配器共用的核心,本身零框架依赖、可离线测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import RiskConfig
from ..engine import RiskEngine
from ..models import Order, Portfolio, RiskDecision, Side

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class OverlayResult:
    """一次"目标 → 订单"翻译的结果。"""

    order: Order | None  # 风控批准/缩量后的下一步订单;None = 本 bar 不动作
    decision: RiskDecision | None
    halted: bool  # 熔断中,想要的新增/加仓被拦下
    approved_weight: float = 0.0  # 批准后应持有的权重(供按权重再平衡的框架直接用)

    @property
    def acted(self) -> bool:
        return self.order is not None


class RiskOverlay:
    """把目标持仓翻译成风控批准的订单,并累计回测期间的风控统计。

    参数
    ----
    config:
        风控配置;当未传 ``engine`` 时用它构造一个引擎。
    engine:
        已有的 :class:`RiskEngine`(可带 broker/audit);优先于 ``config``。
    symbol:
        本叠加层管理的标的代码(回测通常单标的)。
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        *,
        engine: RiskEngine | None = None,
        symbol: str = "ASSET",
    ) -> None:
        self.engine = engine if engine is not None else RiskEngine(config or RiskConfig())
        self.symbol = symbol
        self._was_tripped = False
        self.stats: dict[str, int] = {
            "orders": 0,      # 提交给风控的差额订单数
            "resized": 0,     # 被缩量的次数
            "rejected": 0,    # 被拒的次数
            "breaker_trips": 0,  # 熔断触发次数
            "halted_bars": 0,    # 因熔断而拦下新增仓位的 bar 数
        }

    # ------------------------------------------------------------------
    def observe(self, portfolio: Portfolio) -> None:
        """观测一次权益(刷新熔断),并累计熔断触发次数。

        每个 bar 开盘调用一次即可;:meth:`target_weight_to_order` 内部也会观测,
        因此本方法主要用于"这个 bar 没有下单意图、但仍要推进熔断状态"的场景。
        """
        state = self.engine.update_equity(portfolio)
        if state.breaker_tripped and not self._was_tripped:
            self.stats["breaker_trips"] += 1
        self._was_tripped = state.breaker_tripped

    # ------------------------------------------------------------------
    def target_weight_to_order(
        self, target_weight: float, price: float, portfolio: Portfolio
    ) -> OverlayResult:
        """目标权重(名义敞口/权益,带符号:+ 多 / − 空 / 0 平)→ 风控订单。

        价格或权益非正(坏 tick / 尚未入金)时:直接返回无动作结果,**不触碰引擎、
        不改统计**——坏 tick 应被忽略,绝不当成"清仓"意图去下平仓单。
        """
        equity = portfolio.equity
        if price <= 0 or equity <= 0:
            return OverlayResult(None, None, self.engine.breaker_tripped, 0.0)
        target_qty = target_weight * equity / price
        return self.target_qty_to_order(target_qty, price, portfolio)

    def target_qty_to_order(
        self, target_qty: float, price: float, portfolio: Portfolio
    ) -> OverlayResult:
        """目标持仓股数(带符号)→ 风控批准的差额订单。"""
        current = portfolio.position(self.symbol).quantity
        equity = portfolio.equity
        cur_weight = current * price / equity if equity > 0 and price > 0 else 0.0
        delta = target_qty - current
        if abs(delta) < _EPS:
            # 已在目标位,只需推进熔断状态
            self.observe(portfolio)
            return OverlayResult(None, None, self._was_tripped, cur_weight)

        side = Side.BUY if delta > 0 else Side.SELL
        # 朝零收敛(同向减小或清仓)→ reduce_only,任何时候都放行
        reduce_only = current != 0.0 and (
            target_qty == 0.0
            or (abs(target_qty) < abs(current) and (target_qty > 0) == (current > 0))
        )
        order = Order(
            self.symbol, side, abs(delta), reduce_only=reduce_only, strategy_id="backtest"
        )

        was_tripped = self.engine.breaker_tripped
        decision = self.engine.check(order, portfolio)
        newly_tripped = self.engine.breaker_tripped and not was_tripped
        if newly_tripped:
            self.stats["breaker_trips"] += 1
        self._was_tripped = self.engine.breaker_tripped

        self.stats["orders"] += 1
        if decision.rejected:
            self.stats["rejected"] += 1
            halted = self.engine.breaker_tripped
            if halted:
                self.stats["halted_bars"] += 1
            return OverlayResult(None, decision, halted, cur_weight)  # 权重不变
        if decision.resized:
            self.stats["resized"] += 1
        approved_weight = (current + decision.order.signed_quantity) * price / equity
        return OverlayResult(decision.order, decision, False, approved_weight)

    def approved_target_weight(
        self, target_weight: float, price: float, portfolio: Portfolio
    ) -> float:
        """返回**风控批准后**应持有的目标权重,供按权重再平衡的框架直接使用。

        ⚠️ 这是 :meth:`target_weight_to_order` 的便捷封装,会**跑一次完整的预交易检查**
        (观测权益、可能触发熔断、并累计统计)。因此它是**有副作用的**:请把它当作某个
        bar 对某个目标的**唯一** overlay 调用,不要同一 bar 再调 target_qty_to_order,
        否则统计会重复计数。被缩量则返回缩量后权重,被熔断拦下则返回当前权重(不变)。
        """
        return self.target_weight_to_order(target_weight, price, portfolio).approved_weight
