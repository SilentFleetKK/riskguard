"""风控引擎——夹在策略与券商之间的那道闸门。

数据流:策略提交订单(或信号)→ 引擎跑一遍规则 → 放行/缩量/拒单 → 若放行则转发
给券商。成交回来更新组合权益,熔断由此实时评估。引擎是全库唯一持有可变引用的
"服务对象",但它持有的每一个 :class:`~riskguard.state.RiskState` 都是不可变快照,
状态变更靠替换引用完成,并全程加锁,可安全地被监控守护线程并发访问。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from .audit.base import AuditSink
from .brokers.base import Broker, BrokerOrder
from .config import DEFAULT_CONFIG, RiskConfig
from .exceptions import BrokerError, OrderRejected
from .models import Decision, Order, Portfolio, RiskDecision, RuleResult, Signal
from .persistence import StateStore
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
    state:
        初始状态;仅在 ``state_store`` 未配置、或 ``state_store`` 里还没有存档时生效
        (存档存在则存档优先——这是"重启后恢复"的关键)。缺省从零开始。
    state_store:
        可选的状态持久化后端。配置后,构造时**自动从存档恢复**高水位/熔断/策略入役
        时间,此后每次状态变更都会写透;进程重启、甚至换一台机器,只要连到同一个
        store,熔断状态都不会丢——堵住"重启就能绕过风控"的后门。存档读取失败会
        **直接抛出**,拒绝以"一切正常"的假象启动。**一个 store/key 只能有一个活跃
        引擎**,共用会被乐观锁检测到并报错(见 :mod:`riskguard.persistence`),但不会
        静默互相覆盖。⚠️ 配置后,:meth:`check`/:meth:`update_equity` 等方法会在**持锁期间**
        做一次同步磁盘写(sqlite commit),把它们的延迟画像从"纯内存、微秒级"变成
        "阻塞式磁盘 IO、且串行化所有并发调用者"——这是刻意的正确性取舍(与
        :meth:`submit` 锁内做券商网络 IO 同一哲学),高频调用场景请预留 IO 延迟预算。
    raise_on_reject:
        为 True 时,:meth:`submit` 遇到拒单抛 :class:`OrderRejected`;默认返回 None。
    clock:
        时间源,便于测试注入;默认 UTC now(返回带时区的 datetime)。
    on_persist_error:
        状态持久化写入失败时的回调,签名 ``on_persist_error(exc)``;缺省静默忽略。
        写失败不阻断风控裁决,但意味着重启保护暂时失效——生产环境建议务必设置并对它
        告警。⚠️ 该回调在引擎锁**持有期间同步执行**,必须快、不能阻塞、不能派生新
        线程再回调本引擎(会被同一把锁堵住,直到外层调用返回才能获得锁)。例外:
        :meth:`reset_breaker` 的持久化失败**不会**走这个回调,而是直接向调用方抛出
        ——见该方法文档。
    """

    def __init__(
        self,
        config: RiskConfig = DEFAULT_CONFIG,
        rules: Sequence[RiskRule] | None = None,
        *,
        sizer: PositionSizer | None = None,
        broker: Broker | None = None,
        audit: AuditSink | None = None,
        state: RiskState | None = None,
        state_store: StateStore | None = None,
        raise_on_reject: bool = False,
        clock: Callable[[], datetime] = _utc_now,
        on_audit_error: Callable[[BaseException], None] | None = None,
        on_persist_error: Callable[[BaseException], None] | None = None,
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
        self.state_store = state_store
        self.on_persist_error = on_persist_error
        self.raise_on_reject = raise_on_reject
        self._clock = clock

        restored = state_store.load() if state_store is not None else None
        self._state = restored if restored is not None else (state or RiskState.initial())
        self._lock = threading.RLock()

        if state_store is not None and restored is None:
            # 首次启动、存档还是空的:立刻落盘一次,让存档行在构造完成后就存在,
            # 而不是要等到第一次 check()/update_equity() 才出现——外部工具轮询
            # 这个文件时不该看到"引擎已经启动但存档还没有这一行"的空窗期。
            self._persist_locked()

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
            self._persist_locked()

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
            self._persist_locked()
            self._safe_audit(lambda a: a.record_decision(decision))
            return decision

    def submit(self, order: Order, portfolio: Portfolio) -> BrokerOrder | None:
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
    ) -> BrokerOrder | None:
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
            self._persist_locked()
            return self._state

    def reset_breaker(self) -> RiskState:
        """人工复盘后重置熔断,并把权益高点归位到当前权益。

        与其它状态变更方法不同,这里是**先落盘、成功后才切换内存状态**(其余方法
        是先切内存、再尽力落盘)。原因:重置是操作者据以判断"可以恢复交易"的决定
        性动作;如果落盘悄悄失败却假装重置成功,操作者会带着虚假的安全感继续交易,
        进程一重启熔断状态又原样复活——这比"重置失败、留在熔断态"危险得多。因此
        本方法的持久化失败**不会**转交 ``on_persist_error``,而是直接向调用方抛出
        存储层异常(通常是 :class:`~riskguard.exceptions.PersistenceError`),重置
        不生效,熔断继续保持——这是刻意的 fail-closed。
        """
        with self._lock:
            now = self._clock()
            was_tripped = self._state.breaker_tripped
            new_state = self._state.reset_breaker(now)
            if self.state_store is not None:
                self.state_store.save(new_state)  # 失败则直接抛出,不切换内存状态
            self._state = new_state
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
    def _safe_audit(self, action: Callable[[AuditSink], None]) -> None:
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

    def _persist_locked(self) -> None:
        """把当前状态写透到持久化后端。调用方须已持锁,确保存档与内存状态一致。

        写入失败**不阻断风控裁决**(转交 ``on_persist_error``),否则磁盘抖动会变成
        新的拒绝服务面——但这也意味着写失败期间"重启即绕过熔断"的保护暂时失效。
        """
        store = self.state_store
        if store is None:
            return
        try:
            store.save(self._state)
        except Exception as e:  # noqa: BLE001 —— 持久化失败不得中断风控
            if self.on_persist_error is not None:
                try:
                    self.on_persist_error(e)
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
