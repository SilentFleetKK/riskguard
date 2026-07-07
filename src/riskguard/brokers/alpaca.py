"""Alpaca 券商适配器(可选依赖 ``alpaca-py``)。

把 RiskGuard 券商无关的 :class:`~riskguard.brokers.base.Broker` 接口桥接到
Alpaca 官方 SDK(导入名 ``alpaca``)。该 SDK 是**可选依赖**:核心库零第三方
依赖,只有真正要接 Alpaca 时才装。因此本模块**绝不在顶层 import alpaca**——
否则未装依赖的环境连 ``import riskguard`` 都会炸。依赖统一在 :meth:`__init__`
里惰性导入,缺失时抛 :class:`~riskguard.exceptions.BrokerError` 并给出安装提示。

风格约定:所有数据模型都是冻结 dataclass,只读不改;Alpaca 侧抛出的异常一律
包成 ``BrokerError`` 上抛,不让 SDK 的异常类型泄漏到上层。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from ..exceptions import BrokerError
from ..models import Account, Order, OrderType, Position, Side
from .base import Broker, BrokerOrder

# Alpaca 订单状态 -> RiskGuard 归一化状态。未命中的状态一律视为 "accepted"
# (在途),避免把未知状态误判成终态。
_STATUS_MAP = {
    "new": "accepted",
    "accepted": "accepted",
    "pending_new": "accepted",
    "accepted_for_bidding": "accepted",
    "held": "accepted",
    "filled": "filled",
    "partially_filled": "partially_filled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "expired": "canceled",
    "done_for_day": "canceled",
    "replaced": "canceled",
    "rejected": "rejected",
    "suspended": "rejected",
    "stopped": "rejected",
}


class AlpacaBroker(Broker):
    """Alpaca 执行后端。

    参数
    ----
    api_key / secret_key:
        Alpaca API 密钥对。
    paper:
        是否接纸面模拟盘端点(默认 ``True``,防止误连实盘)。
    """

    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, *, paper: bool = True) -> None:
        # 惰性导入:未装 alpaca-py 时给出可执行的安装提示,而非裸 ImportError。
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise BrokerError(
                'alpaca-py not installed; run: pip install "riskguard[alpaca]"'
            ) from exc

        try:
            self._client = TradingClient(api_key, secret_key, paper=paper)
        except Exception as exc:  # SDK 构造/鉴权异常统一包装
            raise BrokerError(f"failed to init Alpaca trading client: {exc}") from exc

        # 数据客户端可选:装了就留着给 get_marks 用,拿不到就返回空 marks。
        self._data = None
        try:
            from alpaca.data.historical import StockHistoricalDataClient

            self._data = StockHistoricalDataClient(api_key, secret_key)
        except Exception:  # pragma: no cover - 无数据客户端时优雅降级
            self._data = None

    # ------------------------------------------------------------------
    # Broker 接口
    # ------------------------------------------------------------------
    def submit_order(self, order: Order) -> BrokerOrder:
        """把 RiskGuard 订单翻译成 Alpaca 请求并提交,返回归一化回执。"""
        request = self._build_request(order)
        try:
            raw = self._client.submit_order(order_data=request)
        except Exception as exc:
            raise BrokerError(f"Alpaca submit_order failed: {exc}") from exc
        return self._to_broker_order(raw, order)

    def cancel_order(self, broker_order_id: str) -> None:
        """撤销一笔未成交订单。"""
        try:
            self._client.cancel_order_by_id(broker_order_id)
        except Exception as exc:
            raise BrokerError(
                f"Alpaca cancel_order failed for {broker_order_id!r}: {exc}"
            ) from exc

    def get_account(self) -> Account:
        """拉取账户资金快照。"""
        try:
            a = self._client.get_account()
        except Exception as exc:
            raise BrokerError(f"Alpaca get_account failed: {exc}") from exc
        return Account(
            equity=float(a.equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
        )

    def get_positions(self) -> dict[str, Position]:
        """拉取全部持仓,按标的映射。数量带符号(空头为负)。"""
        try:
            raw_positions = self._client.get_all_positions()
        except Exception as exc:
            raise BrokerError(f"Alpaca get_positions failed: {exc}") from exc
        result: dict[str, Position] = {}
        for p in raw_positions:
            result[p.symbol] = Position(
                symbol=p.symbol,
                quantity=float(p.qty),  # Alpaca 空头 qty 本身即为负
                avg_price=float(p.avg_entry_price),
            )
        return result

    def get_open_orders(self) -> Sequence[BrokerOrder]:
        """拉取全部未成交(open)订单。"""
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            raw_orders = self._client.get_orders(filter=req)
        except Exception as exc:
            raise BrokerError(f"Alpaca get_open_orders failed: {exc}") from exc
        return [self._to_broker_order(o, self._reconstruct_order(o)) for o in raw_orders]

    def get_marks(self, symbols: Sequence[str]) -> dict[str, float]:
        """用最新成交价作为标记价;无数据客户端或失败时返回空映射。"""
        if self._data is None or not symbols:
            return {}
        try:
            from alpaca.data.requests import StockLatestTradeRequest

            req = StockLatestTradeRequest(symbol_or_symbols=list(symbols))
            trades = self._data.get_stock_latest_trade(req)
        except Exception:  # 行情不是关键路径,拿不到就退化成空
            return {}
        marks: dict[str, float] = {}
        for sym, trade in trades.items():
            price = getattr(trade, "price", None)
            if price is not None and float(price) > 0:
                marks[sym] = float(price)
        return marks

    # ------------------------------------------------------------------
    # 内部:请求构造与回执映射
    # ------------------------------------------------------------------
    def _build_request(self, order: Order):
        """RiskGuard Order -> Alpaca MarketOrderRequest / LimitOrderRequest。"""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        alpaca_side = OrderSide.BUY if order.side is Side.BUY else OrderSide.SELL
        common = dict(
            symbol=order.symbol,
            qty=order.quantity,
            side=alpaca_side,
            time_in_force=TimeInForce.DAY,
        )
        if order.client_order_id:
            common["client_order_id"] = order.client_order_id

        if order.order_type is OrderType.LIMIT:
            return LimitOrderRequest(limit_price=order.limit_price, **common)
        return MarketOrderRequest(**common)

    def _to_broker_order(self, raw: object, order: Order) -> BrokerOrder:
        """Alpaca 订单对象 -> 归一化 BrokerOrder。"""
        status = self._map_status(getattr(raw, "status", None))
        filled_qty = float(getattr(raw, "filled_qty", None) or 0)
        raw_avg = getattr(raw, "filled_avg_price", None)
        filled_avg_price = float(raw_avg) if raw_avg else None
        return BrokerOrder(
            broker_order_id=str(getattr(raw, "id", "")),
            order=order,
            status=status,
            filled_quantity=filled_qty,
            filled_avg_price=filled_avg_price,
            submitted_at=getattr(raw, "submitted_at", None),
            raw=raw,
        )

    def _reconstruct_order(self, raw: object) -> Order:
        """从 Alpaca 回执反推一个近似的 RiskGuard Order(供 open orders 回填)。

        Alpaca 只回 open 订单的剩余属性,这里尽量还原方向/数量/类型;拿不到限价
        的市价单则按市价单处理。
        """
        raw_side = getattr(raw, "side", None)
        side_val = getattr(raw_side, "value", raw_side)
        side = Side.SELL if str(side_val).lower() == "sell" else Side.BUY

        raw_type = getattr(raw, "order_type", None) or getattr(raw, "type", None)
        type_val = str(getattr(raw_type, "value", raw_type)).lower()
        is_limit = type_val == "limit"

        qty = float(getattr(raw, "qty", None) or 0) or 1.0
        limit_price = getattr(raw, "limit_price", None)
        limit_price = float(limit_price) if limit_price else None

        return Order(
            symbol=str(getattr(raw, "symbol", "")),
            side=side,
            quantity=qty,
            order_type=OrderType.LIMIT if (is_limit and limit_price) else OrderType.MARKET,
            limit_price=limit_price if (is_limit and limit_price) else None,
            client_order_id=getattr(raw, "client_order_id", None),
        )

    @staticmethod
    def _map_status(raw_status: object) -> str:
        """Alpaca 状态枚举/字符串 -> RiskGuard 归一化状态。"""
        value = getattr(raw_status, "value", raw_status)
        return _STATUS_MAP.get(str(value).lower(), "accepted")
