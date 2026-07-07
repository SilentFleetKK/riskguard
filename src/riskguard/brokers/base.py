"""券商无关的执行抽象层。

RiskGuard 不绑定任何一家券商。任何执行后端——纸面模拟盘、Alpaca、盈透、
加密交易所——只要实现 :class:`Broker` 这几个方法,就能接入引擎。风控逻辑只依赖
这个抽象接口,不依赖具体券商,因此换券商对上层零改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional, Sequence

from ..models import Account, Order, Portfolio, Position


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """券商回执:一笔已提交订单在券商侧的状态快照。"""

    broker_order_id: str
    order: Order
    status: str  # "accepted" | "filled" | "partially_filled" | "canceled" | "rejected"
    filled_quantity: float = 0.0
    filled_avg_price: Optional[float] = None
    submitted_at: Optional[datetime] = None
    raw: object = None  # 原始券商返回对象,便于排查

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @property
    def is_terminal(self) -> bool:
        return self.status in ("filled", "canceled", "rejected")


class Broker(ABC):
    """执行后端的统一接口。"""

    name: str = "broker"

    @abstractmethod
    def submit_order(self, order: Order) -> BrokerOrder:
        """提交一笔订单,返回券商回执。"""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None:
        """撤销一笔未成交订单。"""

    @abstractmethod
    def get_account(self) -> Account:
        """拉取账户资金快照。"""

    @abstractmethod
    def get_positions(self) -> dict[str, Position]:
        """拉取全部持仓,按标的映射。"""

    def get_open_orders(self) -> Sequence[BrokerOrder]:
        """拉取未成交订单;默认不支持,子类可覆盖。"""
        raise NotImplementedError(f"{self.name} does not implement get_open_orders")

    def get_marks(self, symbols: Sequence[str]) -> dict[str, float]:
        """拉取标记价;默认空实现,子类按需覆盖。"""
        return {}

    def get_portfolio(self, marks: Optional[Mapping[str, float]] = None) -> Portfolio:
        """便捷方法:组装账户 + 持仓 + 标记价成一个 Portfolio 快照。

        未显式传入 ``marks`` 时,尝试用 :meth:`get_marks` 拉取所有持仓标的的价格。
        """
        account = self.get_account()
        positions = self.get_positions()
        if marks is None:
            try:
                marks = self.get_marks(list(positions.keys()))
            except NotImplementedError:
                marks = {}
        return Portfolio(account=account, positions=positions, marks=dict(marks or {}))
