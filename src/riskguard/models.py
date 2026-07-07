"""RiskGuard 核心数据模型。

设计原则(与项目编码规范一致):**一切皆不可变**。所有模型都是冻结
dataclass,任何"修改"都通过返回新对象实现,绝不原地改。不可变数据能杜绝
隐藏副作用、让调试更容易、也让多线程监控守护进程可以安全地共享状态快照。

本模块只依赖标准库和 :mod:`riskguard.exceptions`,不引入 config/state,
以避免循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional

from .exceptions import PriceUnavailable


def _freeze_mapping(m: Mapping) -> Mapping:
    """把任意映射拷贝成只读视图,防止外部持有引用后偷改内部状态。"""
    return MappingProxyType(dict(m))


class Side(str, Enum):
    """订单方向。继承 ``str`` 便于直接序列化成 JSON。"""

    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """买 = +1,卖 = -1,用于把无符号数量转成带符号敞口。"""
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class Decision(str, Enum):
    """单条规则或整体风控的裁决结果。"""

    APPROVE = "approve"  # 放行
    RESIZE = "resize"    # 放行但缩量
    REJECT = "reject"    # 拒单


@dataclass(frozen=True, slots=True)
class Order:
    """一笔待风控审核的订单请求。

    ``quantity`` 恒为正的数量幅度,方向由 ``side`` 表达。``reduce_only`` 标记
    该单是否仅用于减仓——熔断触发后,减仓单仍应被放行(否则风险反而无法收敛)。
    """

    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    strategy_id: str = "default"
    client_order_id: Optional[str] = None
    reduce_only: bool = False
    meta: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("Order.symbol must be a non-empty string")
        if not isinstance(self.side, Side):
            object.__setattr__(self, "side", Side(self.side))
        if not isinstance(self.order_type, OrderType):
            object.__setattr__(self, "order_type", OrderType(self.order_type))
        if self.quantity <= 0:
            raise ValueError(f"Order.quantity must be > 0, got {self.quantity}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError(f"limit_price must be > 0, got {self.limit_price}")
        object.__setattr__(self, "meta", _freeze_mapping(self.meta))

    @property
    def signed_quantity(self) -> float:
        return self.side.sign * self.quantity

    def with_quantity(self, quantity: float) -> "Order":
        """返回一个只改了数量的新订单(缩单用)。"""
        return replace(self, quantity=quantity)


@dataclass(frozen=True, slots=True)
class Position:
    """某标的的持仓。``quantity`` 带符号:正为多头,负为空头。"""

    symbol: str
    quantity: float
    avg_price: float

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    def notional(self, mark: float) -> float:
        """名义敞口(绝对值)。"""
        return abs(self.quantity) * mark

    def market_value(self, mark: float) -> float:
        """带符号市值(多为正、空为负)。"""
        return self.quantity * mark


@dataclass(frozen=True, slots=True)
class Account:
    """账户资金快照。"""

    equity: float
    cash: float = 0.0
    buying_power: float = 0.0

    def __post_init__(self) -> None:
        # 权益理论上可为负(穿仓),不硬性拦截,但买力应非负。
        if self.buying_power < 0:
            object.__setattr__(self, "buying_power", 0.0)


@dataclass(frozen=True, slots=True)
class Portfolio:
    """账户 + 全部持仓 + 计价用的标记价格,组合层风控的输入快照。"""

    account: Account
    positions: Mapping[str, Position] = field(default_factory=dict)
    marks: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", _freeze_mapping(self.positions))
        # 丢弃非正/非有限的标记价(feed 抖动、坏 tick、符号翻转的脏价)。风险层宁可
        # 回退到均价或显式判定"无价",也绝不用一个负价把敞口算成负数、让上限 fail-open。
        clean_marks = {
            s: float(p)
            for s, p in dict(self.marks).items()
            if isinstance(p, (int, float)) and p == p and p > 0.0  # p==p 排除 NaN
        }
        object.__setattr__(self, "marks", _freeze_mapping(clean_marks))

    @property
    def equity(self) -> float:
        return self.account.equity

    def position(self, symbol: str) -> Position:
        """取持仓;无持仓返回一个 flat 的零仓位。"""
        return self.positions.get(symbol, Position(symbol, 0.0, 0.0))

    def mark_for(self, symbol: str) -> Optional[float]:
        """取标记价;没有则回退到该标的的持仓均价;都没有返回 None。"""
        if symbol in self.marks:
            return self.marks[symbol]
        pos = self.positions.get(symbol)
        if pos is not None and pos.avg_price > 0:
            return pos.avg_price
        return None

    def position_notional(self, symbol: str) -> float:
        pos = self.position(symbol)
        mark = self.mark_for(symbol)
        if mark is None:
            return 0.0
        return pos.notional(mark)

    def weight(self, symbol: str) -> float:
        """某标的名义敞口占权益的比例。"""
        eq = self.equity
        return self.position_notional(symbol) / eq if eq > 0 else 0.0

    def gross_exposure(self) -> float:
        """全组合总名义敞口(多空绝对值之和)。"""
        total = 0.0
        for sym, pos in self.positions.items():
            mark = self.mark_for(sym)
            if mark is not None:
                total += pos.notional(mark)
        return total

    def net_exposure(self) -> float:
        """全组合净敞口(多头市值 - 空头市值的代数和)。"""
        total = 0.0
        for sym, pos in self.positions.items():
            mark = self.mark_for(sym)
            if mark is not None:
                total += pos.market_value(mark)
        return total


def resolve_price(portfolio: Portfolio, order: Order) -> float:
    """为一笔订单解析出用于风险计价的价格。

    优先级:组合标记价 > 订单限价 > 持仓均价。全都没有则抛
    :class:`~riskguard.exceptions.PriceUnavailable`——风控宁可显式失败,
    也绝不用猜测价放行。
    """
    mark = portfolio.marks.get(order.symbol)
    if mark is not None and mark > 0:
        return mark
    if order.limit_price is not None and order.limit_price > 0:
        return order.limit_price
    pos = portfolio.positions.get(order.symbol)
    if pos is not None and pos.avg_price > 0:
        return pos.avg_price
    raise PriceUnavailable(
        f"no mark/limit/avg price available to value order on {order.symbol!r}"
    )


@dataclass(frozen=True, slots=True)
class Signal:
    """策略产生的交易意图,交给仓位算法换算成具体订单数量。

    不同算法用不同字段:Kelly 需要 ``win_probability`` 与 ``payoff_ratio``,
    波动率目标法需要 ``volatility``(年化)。字段缺失时,对应算法应显式报错。
    """

    symbol: str
    side: Side
    price: float
    strategy_id: str = "default"
    win_probability: Optional[float] = None
    payoff_ratio: Optional[float] = None
    volatility: Optional[float] = None
    meta: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            object.__setattr__(self, "side", Side(self.side))
        if self.price <= 0:
            raise ValueError(f"Signal.price must be > 0, got {self.price}")
        object.__setattr__(self, "meta", _freeze_mapping(self.meta))


@dataclass(frozen=True, slots=True)
class RuleResult:
    """单条风控规则的裁决。"""

    rule: str
    action: Decision
    passed: bool
    adjusted_quantity: Optional[float] = None
    message: str = ""
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _freeze_mapping(self.detail))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """引擎对一笔订单的最终裁决,聚合了所有规则的结果。"""

    decision: Decision
    order: Order            # 最终订单(可能已缩量)
    original_order: Order   # 调用方最初提交的订单
    results: tuple[RuleResult, ...]
    timestamp: datetime

    @property
    def approved(self) -> bool:
        return self.decision in (Decision.APPROVE, Decision.RESIZE)

    @property
    def rejected(self) -> bool:
        return self.decision is Decision.REJECT

    @property
    def resized(self) -> bool:
        return self.decision is Decision.RESIZE

    def rejections(self) -> tuple[RuleResult, ...]:
        """返回所有导致拒单的规则结果。"""
        return tuple(
            r for r in self.results if r.action is Decision.REJECT and not r.passed
        )

    def reasons(self) -> str:
        """所有未通过规则的可读原因,分号拼接。"""
        return "; ".join(r.message for r in self.results if not r.passed)
