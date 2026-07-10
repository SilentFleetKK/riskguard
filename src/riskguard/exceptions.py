"""RiskGuard 异常层次。

所有库内异常都继承自 :class:`RiskGuardError`,方便调用方用一个 ``except``
兜住整个库,同时又能针对具体子类做细分处理。
"""

from __future__ import annotations


class RiskGuardError(Exception):
    """RiskGuard 所有异常的基类。"""


class ConfigError(RiskGuardError):
    """风控配置非法(参数越界、互相矛盾等)。"""


class PriceUnavailable(RiskGuardError):
    """无法为某个标的解析出用于风险计价的价格。

    风控要把订单换算成名义金额才能判断仓位占比,拿不到价格时必须显式失败,
    绝不能静默用 0 或猜测值蒙混过去——那会让风控形同虚设。
    """


class CircuitBreakerTripped(RiskGuardError):
    """总亏损熔断已触发,拒绝新开仓订单。"""


class OrderRejected(RiskGuardError):
    """订单被风控拒绝。

    仅当引擎以 ``raise_on_reject=True`` 运行时抛出;默认引擎返回
    :class:`~riskguard.models.RiskDecision` 而不抛异常,让调用方自行决策。
    """

    def __init__(self, decision) -> None:  # decision: RiskDecision
        self.decision = decision
        reasons = "; ".join(
            r.message for r in getattr(decision, "results", ()) if not r.passed
        )
        super().__init__(reasons or "order rejected by risk engine")


class BrokerError(RiskGuardError):
    """券商适配器层的错误(下单失败、连接异常、依赖缺失等)。"""


class PersistenceError(RiskGuardError):
    """状态持久化后端的错误(存档损坏、无法反序列化等)。

    :class:`~riskguard.persistence.StateStore` 读档失败必须抛出它而不是静默返回
    ``None``——一次读档失败若被误当成"首次启动",熔断状态就会悄悄丢失,
    这正是持久化本要堵住的后门。"""
