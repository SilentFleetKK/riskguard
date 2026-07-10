"""内置纸面模拟盘券商。

零依赖、纯内存,用来跑文章里说的"游戏币三个月":真实策略逻辑 + 虚拟资金。
刻意内建了**滑点**与**手续费**模型——因为"回测里年化 30% 的策略,扣掉真实摩擦
实盘亏钱是家常便饭",模拟盘如果不模拟摩擦,就又变成一台自欺欺人的机器。

线程安全:所有状态变更加锁,可被监控守护线程并发读取账户/持仓。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from ..exceptions import BrokerError
from ..models import Account, Order, Portfolio, Position
from .base import Broker, BrokerOrder


def _clamp_reduce_only(cur_qty: float, qty_signed: float) -> float:
    """把一笔 reduce_only 订单夹到"只减不增、绝不翻转"。

    多头(cur>0)只有卖出能减仓,且至多卖到平仓;空头(cur<0)只有买入能减仓;
    无持仓则什么都不做。返回夹取后的带符号成交数量(可能为 0)。
    """
    if cur_qty > 0:  # 多头:只有卖(负)能减,至多减到 0
        return 0.0 if qty_signed >= 0 else max(qty_signed, -cur_qty)
    if cur_qty < 0:  # 空头:只有买(正)能减,至多减到 0
        return 0.0 if qty_signed <= 0 else min(qty_signed, -cur_qty)
    return 0.0  # 无持仓,无可减


class PaperBroker(Broker):
    """内存撮合的模拟券商。市价单按标记价 ± 滑点即时成交。

    参数
    ----
    cash:
        初始现金。
    slippage_bps:
        单边滑点(基点)。买单成交价 = 标记价 × (1 + bps),卖单反之。
    commission_per_share:
        每股(每张)固定佣金。
    commission_bps:
        按成交额计的佣金(基点)。
    marks:
        初始标记价映射。
    """

    name = "paper"

    def __init__(
        self,
        cash: float,
        *,
        slippage_bps: float = 0.0,
        commission_per_share: float = 0.0,
        commission_bps: float = 0.0,
        marks: Mapping[str, float] | None = None,
    ) -> None:
        if cash < 0:
            raise ValueError(f"initial cash must be >= 0, got {cash}")
        self._cash = float(cash)
        self._slippage = slippage_bps / 1e4
        self._commission_per_share = commission_per_share
        self._commission_bps = commission_bps / 1e4
        self._positions: dict[str, Position] = {}
        self._marks: dict[str, float] = dict(marks or {})
        self._orders: dict[str, BrokerOrder] = {}
        self._counter = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 行情维护
    # ------------------------------------------------------------------
    def set_marks(self, marks: Mapping[str, float]) -> None:
        """更新标记价(模拟行情推进)。"""
        with self._lock:
            self._marks.update(marks)

    def set_mark(self, symbol: str, price: float) -> None:
        with self._lock:
            self._marks[symbol] = price

    # ------------------------------------------------------------------
    # Broker 接口
    # ------------------------------------------------------------------
    def get_account(self) -> Account:
        with self._lock:
            equity = self._cash
            for sym, pos in self._positions.items():
                mark = self._marks.get(sym, pos.avg_price)
                equity += pos.market_value(mark)
            return Account(equity=equity, cash=self._cash, buying_power=max(0.0, self._cash))

    def get_positions(self) -> dict[str, Position]:
        with self._lock:
            return {s: p for s, p in self._positions.items() if not p.is_flat}

    def get_marks(self, symbols: Sequence[str]) -> dict[str, float]:
        with self._lock:
            return {s: self._marks[s] for s in symbols if s in self._marks}

    def get_portfolio(self, marks: Mapping[str, float] | None = None) -> Portfolio:
        """组装组合快照。与基类不同,这里带上**全部已知标记价**(含尚无持仓的标的),
        这样风控在为一笔新标的的订单计价时也拿得到价格。"""
        with self._lock:
            account = self.get_account()
            positions = {s: p for s, p in self._positions.items() if not p.is_flat}
            all_marks = dict(self._marks)
            if marks:
                all_marks.update(marks)
            return Portfolio(account=account, positions=positions, marks=all_marks)

    def get_open_orders(self) -> Sequence[BrokerOrder]:
        with self._lock:
            return [o for o in self._orders.values() if not o.is_terminal]

    def submit_order(self, order: Order) -> BrokerOrder:
        with self._lock:
            mark = self._marks.get(order.symbol)
            if mark is None:
                mark = order.limit_price
            if mark is None or mark <= 0:
                raise BrokerError(
                    f"PaperBroker has no mark price for {order.symbol!r}; "
                    "call set_mark() or use a limit order"
                )

            fill_price = mark * (1.0 + self._slippage * order.side.sign)
            qty_signed = order.signed_quantity

            # reduce_only:只允许朝零收敛,绝不翻转或超额开出反向仓位。
            # kill-switch 的平仓单全部带 reduce_only,这一步是它的最后一道保险。
            if order.reduce_only:
                cur = self._positions.get(order.symbol)
                qty_signed = _clamp_reduce_only(cur.quantity if cur else 0.0, qty_signed)

            commission = (
                abs(qty_signed) * self._commission_per_share
                + abs(qty_signed * fill_price) * self._commission_bps
            )

            self._apply_fill(order.symbol, qty_signed, fill_price)
            self._cash -= qty_signed * fill_price + commission

            self._counter += 1
            broker_order = BrokerOrder(
                broker_order_id=f"paper-{self._counter}",
                order=order,
                status="filled",
                filled_quantity=abs(qty_signed),
                filled_avg_price=fill_price,
                submitted_at=datetime.now(timezone.utc),
                raw={"commission": commission, "mark": mark},
            )
            self._orders[broker_order.broker_order_id] = broker_order
            return broker_order

    def cancel_order(self, broker_order_id: str) -> None:
        # 市价单即时成交,通常无可撤;仅在存在且未终态时标记为已撤。
        with self._lock:
            existing = self._orders.get(broker_order_id)
            if existing is None:
                raise BrokerError(f"unknown order id {broker_order_id!r}")
            if not existing.is_terminal:
                from dataclasses import replace

                self._orders[broker_order_id] = replace(existing, status="canceled")

    # ------------------------------------------------------------------
    # 内部撮合
    # ------------------------------------------------------------------
    def _apply_fill(self, symbol: str, qty_signed: float, fill_price: float) -> None:
        """更新持仓与加权成本价(处理加仓/减仓/反手三种情形)。"""
        cur = self._positions.get(symbol)
        cur_qty = cur.quantity if cur else 0.0
        cur_avg = cur.avg_price if cur else 0.0
        new_qty = cur_qty + qty_signed

        if abs(new_qty) < 1e-12:
            new_avg = 0.0
            new_qty = 0.0
        elif cur_qty == 0.0:
            new_avg = fill_price
        elif (cur_qty > 0) == (new_qty > 0) and abs(new_qty) > abs(cur_qty):
            # 同向加仓:加权平均
            new_avg = (cur_qty * cur_avg + qty_signed * fill_price) / new_qty
        elif (cur_qty > 0) != (new_qty > 0):
            # 反手:新方向的成本即成交价
            new_avg = fill_price
        else:
            # 同向减仓:成本价不变
            new_avg = cur_avg

        self._positions[symbol] = Position(symbol=symbol, quantity=new_qty, avg_price=new_avg)
