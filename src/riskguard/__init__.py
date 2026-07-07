"""RiskGuard —— 券商无关的开源交易风控层。

补上"量化五层积木"里唯一没有成熟开源标准件的那一层:风控。把纪律写进代码、
由系统在你情绪失控时替你踩刹车。

快速上手::

    from riskguard import RiskEngine, RiskConfig, Order, Side, PaperBroker

    broker = PaperBroker(cash=100_000, marks={"AAPL": 200.0})
    engine = RiskEngine(RiskConfig(max_position_pct=0.10), broker=broker)

    order = Order(symbol="AAPL", side=Side.BUY, quantity=1000)  # 想买 20 万,占比 200%
    decision = engine.check(order, broker.get_portfolio())
    print(decision.decision, decision.order.quantity)  # RESIZE，缩到 10% 上限
"""

from __future__ import annotations

from .audit import AuditEvent, AuditSink, JsonlAuditSink, SqliteAuditSink
from .brokers import AlpacaBroker, Broker, BrokerOrder, PaperBroker
from .config import DEFAULT_CONFIG, RiskConfig
from .engine import RiskEngine
from .exceptions import (
    BrokerError,
    CircuitBreakerTripped,
    ConfigError,
    OrderRejected,
    PriceUnavailable,
    RiskGuardError,
)
from .models import (
    Account,
    Decision,
    Order,
    OrderType,
    Portfolio,
    Position,
    RiskDecision,
    RuleResult,
    Side,
    Signal,
)
from .monitor import RiskMonitor
from .presets import AGGRESSIVE, BALANCED, CONSERVATIVE, PRESETS, get_preset
from .rules import (
    DrawdownCircuitBreaker,
    GrossExposureLimit,
    MaxPositionLimit,
    NetExposureLimit,
    RiskRule,
    RuleContext,
    StrategyQuarantine,
    build_default_rules,
)
from .sizing import (
    FixedFractionalSizer,
    KellySizer,
    PositionSizer,
    VolatilityTargetSizer,
)
from .state import RiskState

__version__ = "1.2.0"

__all__ = [
    "__version__",
    # 引擎与配置
    "RiskEngine",
    "RiskConfig",
    "DEFAULT_CONFIG",
    "RiskState",
    # 配置预设
    "CONSERVATIVE",
    "BALANCED",
    "AGGRESSIVE",
    "PRESETS",
    "get_preset",
    # 数据模型
    "Order",
    "OrderType",
    "Side",
    "Position",
    "Account",
    "Portfolio",
    "Signal",
    "Decision",
    "RiskDecision",
    "RuleResult",
    # 规则
    "RiskRule",
    "RuleContext",
    "MaxPositionLimit",
    "DrawdownCircuitBreaker",
    "GrossExposureLimit",
    "NetExposureLimit",
    "StrategyQuarantine",
    "build_default_rules",
    # 仓位算法
    "PositionSizer",
    "FixedFractionalSizer",
    "KellySizer",
    "VolatilityTargetSizer",
    # 券商
    "Broker",
    "BrokerOrder",
    "PaperBroker",
    "AlpacaBroker",
    # 审计
    "AuditSink",
    "AuditEvent",
    "JsonlAuditSink",
    "SqliteAuditSink",
    # 监控
    "RiskMonitor",
    # 异常
    "RiskGuardError",
    "ConfigError",
    "PriceUnavailable",
    "CircuitBreakerTripped",
    "OrderRejected",
    "BrokerError",
]
