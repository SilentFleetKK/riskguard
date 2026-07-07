"""风控引擎——夹在策略与券商之间的那道闸门。

数据流:策略提交订单(或信号)→ 引擎跑一遍规则 → 放行/缩量/拒单 → 若放行则转发
给券商。成交回来更新组合权益,熔断由此实时评估。引擎是全库唯一持有可变引用的
"服务对象",但它持有的每一个 :class:`~riskguard.state.RiskState` 都是不可变快照,
状态变更靠替换引用完成,并全程加锁,可安全地被监控守护线程并发访问。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from .audit.base import AuditSink
from .brokers.base import Broker, BrokerOrder
from .config import DEFAULT_CONFIG, RiskConfig
from .exceptions import BrokerError, OrderRejected
from .models import Decision, Order, Portfolio, RiskDecision, RuleResult, Signal
from .rules.base import RiskRule, RuleContext
from .sizing.base import PositionSizer
from .state import RiskState


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RiskEngine:
    """预交易风控闸门 + 熔断状态机。

    参数
    ----
    config:
        风控配置,决定所有阈值。
    rules:
        规则列表;传 ``None`` 时用 :func:`riskguard.rules.build_default_rules`
        按配置组装出文章那三条铁律 + 组合敞口规则。
    sizer:
        可选的仓位算法,配合 :meth:`size_and_submit` 使用。
    broker:
        可选的执行后端;不配则 :meth:`submit` 会报错(仅能用 :meth:`check`)。
    audit:
        可选的审计后端;每次裁决/熔断/成交都会落一条记录。
    raise_on_reject:
        为 True 时,:meth:`submit` 遇到拒单抛 :class:`OrderRejected`;默认返回 None。
    clock:
        时间源,便于测试注入;默认 UTC now(返回带时区的 datetime)。
    """

    def __init__(
        self,
        config: RiskConfig = DEFAULT_CONFIG,
        rules: Optional[Sequence[RiskRule]] = None,
        *,
        sizer: Optional[PositionSizer] = None,
        broker: Optional[Broker] = None,
        audit: Optional[AuditSink] = None,
        state: Optional[RiskState] = None,
        raise_on_reject: bool = False,
        clock: Callable[[], datetime] = _utc_now,
        on_audit_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self.config = config
        if rules is None:
            from .rules import build_default_rules  # 延迟导入,避免叶子模块循环依赖

            rules = build_default_rules(config)
        self.rules: tuple[RiskRule, ...] = tuple(rules)
        self.sizer = sizer
        self.broker = broker
        self.audit = audit
        self.on_audit_error = on_audit_error
        self.raise_on_reject = raise_on_reject
        self._clock = clock
        self._state = state if state is not None else RiskState.initial()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 状态访问
    # ------------------------------------------------------------------
    @property
    def state(self) -> RiskState:
        """当前风控状态快照(不可变)。"""
        with self._lock:
            return self._state

    @property
    def breaker_tripped(self) -> bool:
        with self._lock:
            return self._state.breaker_tripped

    def register_strategy(self, strategy_id: str) -> None:
        """显式登记策略入役时间(隔离观察期从此刻算起)。"""
        with self._lock:
            self._state = self._state.register_strategy(strategy_id, self._clock())

    # ------------------------------------------------------------------
    # 核心:预交易检查
    # ------------------------------------------------------------------
    def check(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        """对一笔订单跑完整风控,返回裁决(不下单、不抛异常)。

        注意:check 不是无副作用的纯查询——它会顺带**观测组合权益**(更新权益高点),
        并可能因此**触发熔断**;当 ``auto_register_strategies=True`` 时还会登记策略
        入役时间。这是刻意的:预交易检查本就该以最新的熔断状态为准。若需要绝对只读的
        预览,请勿把实时权益传进来。
        """
        with self._lock:
            now = self._clock()
            if self.config.auto_register_strategies:
                self._state = self._state.register_strategy(order.strategy_id, now)
            self._observe_locked(portfolio, now)
            ctx = RuleContext(
                order=order,
                portfolio=portfolio,
                config=self.config,
                state=self._state,
                now=now,
            )
            results = tuple(rule.evaluate(ctx) for rule in self.rules)
            decision = self._aggregate(order, results, now)
            self._safe_audit(lambda a: a.record_decision(decision))
            return decision

    def submit(self, order: Order, portfolio: Portfolio) -> Optional[BrokerOrder]:
        """先风控、放行则下单。返回券商回执;被拒返回 None(或抛异常)。

        风控检查与向 broker 提交在**同一把锁**内完成,不给监控守护线程留下"检查时
        没熔断、提交时已熔断"的窗口——否则一笔在熔断前被放行的单,可能在 kill-switch
        已经平掉全部头寸之后才打到 broker,凭空把刚被平掉的仓位又开回来。
        代价:提交期间会短暂持锁(对真实券商的网络往返而言,是刻意的正确性取舍)。
        """
        with self._lock:
            decision = self.check(order, portfolio)
            if not decision.approved:
                if self.raise_on_reject:
                    raise OrderRejected(decision)
                return None
            if self.broker is None:
                raise BrokerError(
                    "submit() requires a broker; none configured on engine"
                )
            broker_order = self.broker.submit_order(decision.order)
            self._safe_audit(
                lambda a: a.record_event(
                    "fill",
                    self._clock(),
                    broker_order_id=broker_order.broker_order_id,
                    symbol=decision.order.symbol,
                    status=broker_order.status,
                    filled_quantity=broker_order.filled_quantity,
                    filled_avg_price=broker_order.filled_avg_price,
                )
            )
            return broker_order

    def size_and_submit(
        self, signal: Signal, portfolio: Portfolio
    ) -> Optional[BrokerOrder]:
        """用配置的仓位算法把信号换算成订单,再走 :meth:`submit`。

        若仓位算法判定"不下注"(:meth:`PositionSizer.size` 返回 ``None``,如 Kelly
        无正期望),直接返回 ``None``,不提交任何订单。
        """
        if self.sizer is None:
            raise BrokerError("size_and_submit() requires a sizer; none configured")
        order = self.sizer.size(signal, portfolio, self.config)
        if order is None:
            return None
        return self.submit(order, portfolio)

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------
    def update_equity(self, portfolio: Portfolio) -> RiskState:
        """在下单流程之外观测一次权益(供监控守护进程周期性调用)。

        会更新权益高点,并在回撤触及阈值时触发熔断。返回更新后的状态。
        """
        with self._lock:
            self._observe_locked(portfolio, self._clock())
            return self._state

    def reset_breaker(self) -> RiskState:
        """人工复盘后重置熔断,并把权益高点归位到当前权益。"""
        with self._lock:
            now = self._clock()
            was_tripped = self._state.breaker_tripped
            self._state = self._state.reset_breaker(now)
            if was_tripped:
                self._safe_audit(
                    lambda a: a.record_event(
                        "breaker_reset", now, equity=self._state.last_equity
                    )
                )
            return self._state

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _safe_audit(self, action: "Callable[[AuditSink], None]") -> None:
        """执行一次审计写入,**绝不让审计失败阻断风控裁决**。

        审计是次要职责:磁盘满、IO 异常等绝不能把 allow/deny 的主判决带崩。失败时
        转交 ``on_audit_error`` 回调(缺省静默),风控裁决照常返回。
        """
        audit = self.audit
        if audit is None:
            return
        try:
            action(audit)
        except Exception as e:  # noqa: BLE001 —— 审计失败不得中断风控
            if self.on_audit_error is not None:
                try:
                    self.on_audit_error(e)
                except Exception:  # noqa: BLE001
                    pass

    def _observe_locked(self, portfolio: Portfolio, now: datetime) -> None:
        """在已持锁的前提下观测权益并按需触发熔断。"""
        was_tripped = self._state.breaker_tripped
        new_state = self._state.observe_equity(portfolio.equity, now)
        if (
            not new_state.breaker_tripped
            and new_state.high_water_mark > 0
            and new_state.drawdown >= self.config.max_drawdown_pct
        ):
            reason = (
                f"drawdown {new_state.drawdown:.2%} >= limit "
                f"{self.config.max_drawdown_pct:.2%}"
            )
            new_state = new_state.trip(reason, now)
        self._state = new_state
        if not was_tripped and new_state.breaker_tripped:
            self._safe_audit(
                lambda a: a.record_event(
                    "breaker_trip",
                    now,
                    reason=new_state.trip_reason,
                    equity=new_state.last_equity,
                    high_water_mark=new_state.high_water_mark,
                    drawdown=new_state.drawdown,
                )
            )

    def _aggregate(
        self, order: Order, results: tuple[RuleResult, ...], now: datetime
    ) -> RiskDecision:
        """把各规则结果聚合成一个最终裁决:任一拒单即拒,否则取最保守的缩量。"""
        final_qty = order.quantity
        rejected = False
        for r in results:
            if r.action is Decision.REJECT and not r.passed:
                rejected = True
            elif r.action is Decision.RESIZE and r.adjusted_quantity is not None:
                final_qty = min(final_qty, r.adjusted_quantity)

        if rejected or final_qty <= 0:
            decision_type = Decision.REJECT
            final_order = order
        elif final_qty < order.quantity:
            decision_type = Decision.RESIZE
            final_order = order.with_quantity(final_qty)
        else:
            decision_type = Decision.APPROVE
            final_order = order

        return RiskDecision(
            decision=decision_type,
            order=final_order,
            original_order=order,
            results=results,
            timestamp=now,
        )
