"""``RiskEngine`` 单元测试。

引擎是夹在策略与券商之间的那道闸门,本模块用**桩规则 / 桩券商 / 桩仓位算法 /
桩审计**精确驱动每一条分支,确保聚合逻辑、下单转发、熔断状态机、审计落记与并发
安全全都符合契约:

* ``check`` 聚合 —— 取各规则缩量的最小值;任一拒单即整体拒单;缩量 < 原始 =>
  RESIZE;== 原始 => APPROVE;缩到 <= 0 视同拒单;
* ``submit`` —— 放行则把**最终(可能已缩量)**订单转发给券商并回传 ``BrokerOrder``;
  被拒返回 ``None``,``raise_on_reject=True`` 时改抛 :class:`OrderRejected`;无券商
  时放行也会报 :class:`BrokerError`;
* ``size_and_submit`` —— 走注入的 sizer 换算订单再提交;无 sizer 报错;
* ``update_equity`` —— 回撤触及阈值触发熔断,审计 ``breaker_trip`` **只落一次**;
* ``reset_breaker`` —— 复位并审计 ``breaker_reset``(仅在此前确已触发时);
* ``register_strategy`` / ``state`` 属性 —— 登记入役、返回不可变快照;
* 轻量并发 —— 多线程并发 ``check()`` 不抛异常,且熔断状态最终一致。

所有涉及时间的用例都用**可变列表时钟**(``lambda: box[0]``)注入
``RiskEngine(clock=...)``,推进时钟只需改盒子里的值,保证完全确定、不依赖 wall clock。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from riskguard import (
    Account,
    BrokerError,
    BrokerOrder,
    Decision,
    JsonlAuditSink,
    Order,
    OrderRejected,
    Portfolio,
    Position,
    RiskConfig,
    RiskDecision,
    RiskEngine,
    RiskState,
    RuleResult,
    Side,
    Signal,
)
from riskguard.audit.base import AuditEvent, AuditSink
from riskguard.brokers.base import Broker
from riskguard.rules.base import RiskRule, RuleContext
from riskguard.sizing.base import PositionSizer

# --------------------------------------------------------------------------- #
# 常量 / 时钟辅助
# --------------------------------------------------------------------------- #

T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _list_clock(start: datetime = T0):
    """返回 (可变时间盒, 读取器);改 box[0] 即可推进引擎时钟。"""
    box = [start]
    return box, (lambda: box[0])


def _portfolio(equity: float = 100_000.0, **kw) -> Portfolio:
    """构造一个只关心权益的最简组合快照。"""
    return Portfolio(account=Account(equity=equity), **kw)


def _order(quantity: float = 100.0, **kw) -> Order:
    defaults = dict(symbol="AAPL", side=Side.BUY, quantity=quantity)
    defaults.update(kw)
    return Order(**defaults)


# --------------------------------------------------------------------------- #
# 桩:规则 / 券商 / 仓位算法 / 审计
# --------------------------------------------------------------------------- #

class _StubRule(RiskRule):
    """产出预设裁决的桩规则;可选记录被调用次数与并发峰值。"""

    def __init__(self, result_fn, name: str = "stub"):
        self.name = name
        self._result_fn = result_fn
        self.calls = 0

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        self.calls += 1
        return self._result_fn(self, ctx)


def _approve_rule(name: str = "approve") -> _StubRule:
    return _StubRule(lambda self, ctx: self.approve("ok"), name=name)


def _resize_rule(qty: float, name: str = "resize") -> _StubRule:
    return _StubRule(
        lambda self, ctx: self.resize(qty, f"cap to {qty}"), name=name
    )


def _reject_rule(name: str = "reject") -> _StubRule:
    return _StubRule(lambda self, ctx: self.reject("blocked"), name=name)


class _RecordingBroker(Broker):
    """记录收到的订单;可配置为提交时抛错。"""

    name = "recording"

    def __init__(self, *, fail: bool = False):
        self.submitted: list[Order] = []
        self._fail = fail
        self._counter = 0

    def submit_order(self, order: Order) -> BrokerOrder:
        if self._fail:
            raise BrokerError("broker down")
        self.submitted.append(order)
        self._counter += 1
        return BrokerOrder(
            broker_order_id=f"rec-{self._counter}",
            order=order,
            status="filled",
            filled_quantity=order.quantity,
            filled_avg_price=200.0,
        )

    def cancel_order(self, broker_order_id: str) -> None:  # pragma: no cover - 未用
        raise NotImplementedError

    def get_account(self) -> Account:  # pragma: no cover - 未用
        return Account(equity=0.0)

    def get_positions(self) -> dict[str, Position]:  # pragma: no cover - 未用
        return {}


class _StubSizer(PositionSizer):
    """把信号固定换算成给定数量的桩仓位算法。"""

    name = "stub_sizer"

    def __init__(self, quantity: float):
        self._qty = quantity

    def target_weight(self, signal, portfolio, config) -> float:  # pragma: no cover
        # size() 被下面覆盖,这里不会被调用;仍需实现以满足抽象基类。
        return 0.0

    def size(self, signal: Signal, portfolio: Portfolio, config: RiskConfig) -> Order:
        return Order(
            symbol=signal.symbol,
            side=signal.side,
            quantity=self._qty,
            strategy_id=signal.strategy_id,
        )


class _MemoryAudit(AuditSink):
    """把事件收进内存列表,便于断言事件类型/次数/载荷。"""

    name = "memory"

    def __init__(self):
        self.events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self.events.append(event)

    def types(self) -> list[str]:
        return [e.event_type for e in self.events]

    def count(self, event_type: str) -> int:
        return sum(1 for e in self.events if e.event_type == event_type)


# =========================================================================== #
# check(): 聚合裁决
# =========================================================================== #

def test_check_all_approve_returns_approve():
    _, clock = _list_clock()
    eng = RiskEngine(rules=[_approve_rule("a"), _approve_rule("b")], clock=clock)
    decision = eng.check(_order(100), _portfolio())

    assert decision.decision is Decision.APPROVE
    assert decision.approved and not decision.rejected and not decision.resized
    assert decision.order.quantity == 100.0
    assert decision.order is decision.original_order  # APPROVE 不换新订单
    assert decision.timestamp == T0


def test_check_resize_below_original_yields_resize():
    eng = RiskEngine(rules=[_resize_rule(40.0)], clock=_list_clock()[1])
    decision = eng.check(_order(100), _portfolio())

    assert decision.decision is Decision.RESIZE
    assert decision.resized and decision.approved
    assert decision.order.quantity == 40.0
    assert decision.original_order.quantity == 100.0  # 原单不被篡改
    # 缩量单是新对象,其余字段保持一致
    assert decision.order is not decision.original_order
    assert decision.order.symbol == decision.original_order.symbol


def test_check_takes_minimum_of_multiple_resizes():
    """多条 RESIZE 取最小值(最保守的那条说了算)。"""
    eng = RiskEngine(
        rules=[_resize_rule(80.0, "a"), _resize_rule(30.0, "b"), _resize_rule(55.0, "c")],
        clock=_list_clock()[1],
    )
    decision = eng.check(_order(100), _portfolio())

    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == 30.0


def test_check_resize_equal_to_original_is_approve():
    """缩量目标恰等于原始数量时不算缩量,应为 APPROVE。"""
    eng = RiskEngine(rules=[_resize_rule(100.0)], clock=_list_clock()[1])
    decision = eng.check(_order(100), _portfolio())

    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == 100.0


def test_check_resize_larger_than_original_does_not_upsize():
    """规则给出的缩量目标比原单还大时,min() 保证不会放大,结果为 APPROVE。"""
    eng = RiskEngine(rules=[_resize_rule(500.0)], clock=_list_clock()[1])
    decision = eng.check(_order(100), _portfolio())

    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == 100.0


def test_check_any_reject_yields_reject():
    eng = RiskEngine(
        rules=[_approve_rule("a"), _reject_rule("bad"), _resize_rule(50.0, "c")],
        clock=_list_clock()[1],
    )
    decision = eng.check(_order(100), _portfolio())

    assert decision.decision is Decision.REJECT
    assert decision.rejected and not decision.approved
    # 拒单时最终订单退回原单,不做缩量
    assert decision.order is decision.original_order
    assert decision.order.quantity == 100.0


def test_check_reject_dominates_even_with_smaller_resize():
    """即便另有更激进的缩量,只要有拒单,整体就是拒单。"""
    eng = RiskEngine(
        rules=[_resize_rule(1.0, "tiny"), _reject_rule("veto")],
        clock=_list_clock()[1],
    )
    decision = eng.check(_order(100), _portfolio())
    assert decision.decision is Decision.REJECT


def test_check_resize_to_zero_is_treated_as_reject():
    """缩量到 0(或负)等于无法下单,聚合为 REJECT。"""
    eng = RiskEngine(rules=[_resize_rule(0.0)], clock=_list_clock()[1])
    decision = eng.check(_order(100), _portfolio())

    assert decision.decision is Decision.REJECT
    assert decision.order is decision.original_order


def test_check_empty_rules_approves():
    """没有任何规则时一律放行(数量不变)。"""
    eng = RiskEngine(rules=[], clock=_list_clock()[1])
    decision = eng.check(_order(77), _portfolio())

    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == 77.0
    assert decision.results == ()


def test_check_evaluates_every_rule_and_collects_results():
    r1, r2, r3 = _approve_rule("a"), _resize_rule(60.0, "b"), _approve_rule("c")
    eng = RiskEngine(rules=[r1, r2, r3], clock=_list_clock()[1])
    decision = eng.check(_order(100), _portfolio())

    assert r1.calls == r2.calls == r3.calls == 1
    assert len(decision.results) == 3
    assert [r.rule for r in decision.results] == ["a", "b", "c"]


def test_check_passes_context_fields_to_rules():
    """规则拿到的上下文里 order/portfolio/config/now 都是引擎实际使用的那些。"""
    captured = {}

    def _capture(self, ctx: RuleContext):
        captured["order"] = ctx.order
        captured["portfolio"] = ctx.portfolio
        captured["config"] = ctx.config
        captured["now"] = ctx.now
        captured["state"] = ctx.state
        return self.approve()

    cfg = RiskConfig(max_position_pct=0.2)
    order, pf = _order(100), _portfolio(50_000)
    eng = RiskEngine(cfg, rules=[_StubRule(_capture)], clock=_list_clock()[1])
    eng.check(order, pf)

    assert captured["order"] is order
    assert captured["portfolio"] is pf
    assert captured["config"] is cfg
    assert captured["now"] == T0
    assert isinstance(captured["state"], RiskState)


def test_check_does_not_call_broker_or_place_order():
    """check() 只裁决,绝不下单。"""
    broker = _RecordingBroker()
    eng = RiskEngine(rules=[_approve_rule()], broker=broker, clock=_list_clock()[1])
    eng.check(_order(100), _portfolio())
    assert broker.submitted == []


def test_check_uses_injected_clock_for_timestamp():
    box, clock = _list_clock()
    eng = RiskEngine(rules=[_approve_rule()], clock=clock)

    d1 = eng.check(_order(10), _portfolio())
    box[0] = datetime(2025, 6, 6, tzinfo=timezone.utc)
    d2 = eng.check(_order(10), _portfolio())

    assert d1.timestamp == T0
    assert d2.timestamp == datetime(2025, 6, 6, tzinfo=timezone.utc)


# =========================================================================== #
# check(): 审计落记
# =========================================================================== #

def test_check_records_decision_to_audit():
    audit = _MemoryAudit()
    eng = RiskEngine(rules=[_resize_rule(40.0)], audit=audit, clock=_list_clock()[1])
    eng.check(_order(100), _portfolio())

    assert audit.count("decision") == 1
    payload = audit.events[0].payload
    assert payload["decision"] == "resize"
    assert payload["requested_quantity"] == 100.0
    assert payload["final_quantity"] == 40.0


def test_check_without_audit_does_not_crash():
    eng = RiskEngine(rules=[_approve_rule()], audit=None, clock=_list_clock()[1])
    assert eng.check(_order(1), _portfolio()).approved


# =========================================================================== #
# submit(): 放行则转发,拒单返回 None / 抛异常
# =========================================================================== #

def test_submit_forwards_approved_order_and_returns_broker_order():
    broker = _RecordingBroker()
    eng = RiskEngine(rules=[_approve_rule()], broker=broker, clock=_list_clock()[1])
    result = eng.submit(_order(100), _portfolio())

    assert isinstance(result, BrokerOrder)
    assert result.broker_order_id == "rec-1"
    assert len(broker.submitted) == 1
    assert broker.submitted[0].quantity == 100.0


def test_submit_forwards_the_resized_order_not_the_original():
    """放行的是缩量后的最终订单——转发给券商的数量应是缩量值。"""
    broker = _RecordingBroker()
    eng = RiskEngine(rules=[_resize_rule(25.0)], broker=broker, clock=_list_clock()[1])
    result = eng.submit(_order(100), _portfolio())

    assert result is not None
    assert broker.submitted[0].quantity == 25.0


def test_submit_returns_none_on_reject_by_default():
    broker = _RecordingBroker()
    eng = RiskEngine(rules=[_reject_rule()], broker=broker, clock=_list_clock()[1])
    result = eng.submit(_order(100), _portfolio())

    assert result is None
    assert broker.submitted == []  # 拒单绝不下单


def test_submit_raises_when_raise_on_reject():
    broker = _RecordingBroker()
    eng = RiskEngine(
        rules=[_reject_rule("nope")],
        broker=broker,
        raise_on_reject=True,
        clock=_list_clock()[1],
    )
    with pytest.raises(OrderRejected) as exc:
        eng.submit(_order(100), _portfolio())

    # 异常携带原始裁决,便于调用方复盘
    assert exc.value.decision.rejected
    assert broker.submitted == []


def test_submit_without_broker_raises_broker_error():
    """放行但没配券商时应显式报错,而非静默吞掉。"""
    eng = RiskEngine(rules=[_approve_rule()], broker=None, clock=_list_clock()[1])
    with pytest.raises(BrokerError):
        eng.submit(_order(100), _portfolio())


def test_submit_reject_without_broker_returns_none():
    """拒单路径在券商检查之前返回,即使没配券商也不该报错。"""
    eng = RiskEngine(rules=[_reject_rule()], broker=None, clock=_list_clock()[1])
    assert eng.submit(_order(100), _portfolio()) is None


def test_submit_records_fill_event_to_audit():
    audit = _MemoryAudit()
    broker = _RecordingBroker()
    eng = RiskEngine(
        rules=[_approve_rule()], broker=broker, audit=audit, clock=_list_clock()[1]
    )
    eng.submit(_order(100), _portfolio())

    # 一条 decision + 一条 fill
    assert audit.count("decision") == 1
    assert audit.count("fill") == 1
    fill = next(e for e in audit.events if e.event_type == "fill")
    assert fill.payload["broker_order_id"] == "rec-1"
    assert fill.payload["symbol"] == "AAPL"
    assert fill.payload["status"] == "filled"


def test_submit_propagates_broker_errors():
    broker = _RecordingBroker(fail=True)
    eng = RiskEngine(rules=[_approve_rule()], broker=broker, clock=_list_clock()[1])
    with pytest.raises(BrokerError):
        eng.submit(_order(100), _portfolio())


# =========================================================================== #
# size_and_submit(): 用 sizer 换算再提交
# =========================================================================== #

def _signal(**kw) -> Signal:
    defaults = dict(symbol="AAPL", side=Side.BUY, price=200.0)
    defaults.update(kw)
    return Signal(**defaults)


def test_size_and_submit_uses_sizer_quantity():
    broker = _RecordingBroker()
    eng = RiskEngine(
        rules=[_approve_rule()],
        sizer=_StubSizer(quantity=42.0),
        broker=broker,
        clock=_list_clock()[1],
    )
    result = eng.size_and_submit(_signal(), _portfolio())

    assert isinstance(result, BrokerOrder)
    assert broker.submitted[0].quantity == 42.0


def test_size_and_submit_still_subject_to_rules():
    """sizer 只决定下多大注,规则层仍可缩量;转发的是缩量后的量。"""
    broker = _RecordingBroker()
    eng = RiskEngine(
        rules=[_resize_rule(10.0)],
        sizer=_StubSizer(quantity=42.0),
        broker=broker,
        clock=_list_clock()[1],
    )
    eng.size_and_submit(_signal(), _portfolio())
    assert broker.submitted[0].quantity == 10.0


def test_size_and_submit_reject_returns_none():
    broker = _RecordingBroker()
    eng = RiskEngine(
        rules=[_reject_rule()],
        sizer=_StubSizer(quantity=42.0),
        broker=broker,
        clock=_list_clock()[1],
    )
    assert eng.size_and_submit(_signal(), _portfolio()) is None
    assert broker.submitted == []


def test_size_and_submit_without_sizer_raises():
    broker = _RecordingBroker()
    eng = RiskEngine(rules=[_approve_rule()], broker=broker, clock=_list_clock()[1])
    with pytest.raises(BrokerError):
        eng.size_and_submit(_signal(), _portfolio())


# =========================================================================== #
# update_equity(): 熔断状态机 + breaker_trip 只落一次
# =========================================================================== #

def test_update_equity_tracks_high_water_mark():
    _, clock = _list_clock()
    eng = RiskEngine(rules=[], clock=clock)

    eng.update_equity(_portfolio(100_000))
    st = eng.update_equity(_portfolio(120_000))
    assert st.high_water_mark == 120_000
    assert st.last_equity == 120_000
    assert not st.breaker_tripped


def test_update_equity_trips_breaker_at_threshold():
    """回撤触及 max_drawdown_pct 即熔断。"""
    cfg = RiskConfig(max_drawdown_pct=0.15)
    eng = RiskEngine(cfg, rules=[], clock=_list_clock()[1])

    eng.update_equity(_portfolio(100_000))          # 建立高点
    st = eng.update_equity(_portfolio(85_000))      # 回撤正好 15%
    assert st.breaker_tripped
    assert st.trip_reason  # 有可读原因
    assert eng.breaker_tripped is True


def test_update_equity_below_threshold_does_not_trip():
    cfg = RiskConfig(max_drawdown_pct=0.15)
    eng = RiskEngine(cfg, rules=[], clock=_list_clock()[1])

    eng.update_equity(_portfolio(100_000))
    st = eng.update_equity(_portfolio(86_000))  # 回撤 14% < 15%
    assert not st.breaker_tripped


def test_update_equity_records_breaker_trip_exactly_once():
    """熔断已触发后继续观测更差的权益,不应重复落 breaker_trip。"""
    audit = _MemoryAudit()
    cfg = RiskConfig(max_drawdown_pct=0.15)
    eng = RiskEngine(cfg, rules=[], audit=audit, clock=_list_clock()[1])

    eng.update_equity(_portfolio(100_000))
    eng.update_equity(_portfolio(80_000))   # 触发
    eng.update_equity(_portfolio(70_000))   # 更差,但已熔断
    eng.update_equity(_portfolio(60_000))

    assert audit.count("breaker_trip") == 1
    trip = next(e for e in audit.events if e.event_type == "breaker_trip")
    assert "drawdown" in trip.payload
    assert trip.payload["reason"]


def test_breaker_trip_also_records_during_check():
    """check() 内部也会观测权益;权益足够低时会在裁决前触发熔断并落一次记录。"""
    audit = _MemoryAudit()
    cfg = RiskConfig(max_drawdown_pct=0.15)
    eng = RiskEngine(cfg, rules=[_approve_rule()], audit=audit, clock=_list_clock()[1])

    eng.check(_order(1), _portfolio(100_000))  # 高点
    eng.check(_order(1), _portfolio(80_000))   # 触发熔断
    eng.check(_order(1), _portfolio(75_000))   # 已熔断,不再重复落记

    assert audit.count("breaker_trip") == 1
    assert eng.breaker_tripped


def test_update_equity_returns_state_snapshot():
    eng = RiskEngine(rules=[], clock=_list_clock()[1])
    st = eng.update_equity(_portfolio(100_000))
    assert isinstance(st, RiskState)
    assert st.last_equity == 100_000


def test_update_equity_no_trip_without_high_water_mark():
    """高点为 0(从未观测过正权益)时不应触发熔断。"""
    eng = RiskEngine(rules=[], clock=_list_clock()[1])
    st = eng.update_equity(_portfolio(0.0))
    assert not st.breaker_tripped
    assert st.high_water_mark == 0.0


# =========================================================================== #
# reset_breaker(): 复位 + breaker_reset 审计
# =========================================================================== #

def test_reset_breaker_clears_trip_and_records_event():
    audit = _MemoryAudit()
    cfg = RiskConfig(max_drawdown_pct=0.15)
    eng = RiskEngine(cfg, rules=[], audit=audit, clock=_list_clock()[1])

    eng.update_equity(_portfolio(100_000))
    eng.update_equity(_portfolio(80_000))  # 熔断
    assert eng.breaker_tripped

    st = eng.reset_breaker()
    assert not st.breaker_tripped
    assert st.trip_reason == ""
    # 高点归位到当前权益,避免立刻二次触发
    assert st.high_water_mark == st.last_equity == 80_000
    assert audit.count("breaker_reset") == 1


def test_reset_breaker_without_prior_trip_does_not_record():
    """从未熔断就复位,不应凭空落一条 breaker_reset。"""
    audit = _MemoryAudit()
    eng = RiskEngine(rules=[], audit=audit, clock=_list_clock()[1])

    eng.update_equity(_portfolio(100_000))
    st = eng.reset_breaker()

    assert not st.breaker_tripped
    assert audit.count("breaker_reset") == 0


def test_reset_breaker_returns_state_snapshot():
    eng = RiskEngine(rules=[], clock=_list_clock()[1])
    assert isinstance(eng.reset_breaker(), RiskState)


def test_breaker_reset_then_retrip_records_again():
    """复位后再次触发应再落一条 trip;整条链路 trip=2, reset=1。"""
    audit = _MemoryAudit()
    cfg = RiskConfig(max_drawdown_pct=0.15)
    eng = RiskEngine(cfg, rules=[], audit=audit, clock=_list_clock()[1])

    eng.update_equity(_portfolio(100_000))
    eng.update_equity(_portfolio(80_000))   # trip #1
    eng.reset_breaker()                      # reset,高点归位到 80_000
    eng.update_equity(_portfolio(60_000))   # 回撤 25% > 15% => trip #2

    assert audit.count("breaker_trip") == 2
    assert audit.count("breaker_reset") == 1


# =========================================================================== #
# register_strategy() / state 属性
# =========================================================================== #

def test_register_strategy_records_inception_time():
    box, clock = _list_clock()
    eng = RiskEngine(rules=[], clock=clock)
    eng.register_strategy("alpha")

    assert eng.state.strategy_inception["alpha"] == T0


def test_register_strategy_keeps_earliest_time():
    """重复登记不覆盖,保留最早入役时间。"""
    box, clock = _list_clock()
    eng = RiskEngine(rules=[], clock=clock)
    eng.register_strategy("alpha")
    box[0] = datetime(2025, 1, 1, tzinfo=timezone.utc)
    eng.register_strategy("alpha")

    assert eng.state.strategy_inception["alpha"] == T0


def test_state_property_returns_immutable_snapshot():
    eng = RiskEngine(rules=[], clock=_list_clock()[1])
    st = eng.state
    assert isinstance(st, RiskState)
    # 冻结 dataclass:任何字段赋值都被拒绝
    with pytest.raises((AttributeError, TypeError)):
        st.last_equity = 999  # type: ignore[misc]


def test_state_reflects_updates():
    eng = RiskEngine(rules=[], clock=_list_clock()[1])
    assert eng.state.last_equity == 0.0
    eng.update_equity(_portfolio(50_000))
    assert eng.state.last_equity == 50_000


def test_auto_register_strategies_on_check():
    """auto_register_strategies=True 时,首次见到某策略下单即自动登记入役。"""
    cfg = RiskConfig(auto_register_strategies=True)
    eng = RiskEngine(cfg, rules=[_approve_rule()], clock=_list_clock()[1])

    eng.check(_order(1, strategy_id="momentum"), _portfolio())
    assert "momentum" in eng.state.strategy_inception


def test_no_auto_register_by_default():
    eng = RiskEngine(rules=[_approve_rule()], clock=_list_clock()[1])
    eng.check(_order(1, strategy_id="momentum"), _portfolio())
    assert "momentum" not in eng.state.strategy_inception


# =========================================================================== #
# 默认规则栈的冒烟(用真实规则跑一遍,确认引擎组装无误)
# =========================================================================== #

def test_engine_builds_default_rules_when_none_given():
    eng = RiskEngine(RiskConfig(), clock=_list_clock()[1])
    assert len(eng.rules) == 4  # drawdown / position / quarantine / gross


def test_default_rules_resize_oversized_position():
    """真实的 MaxPositionLimit:想买 200% 权益应被缩到 10% 上限(RESIZE)。"""
    cfg = RiskConfig(max_position_pct=0.10, on_position_breach="resize")
    eng = RiskEngine(cfg, clock=_list_clock()[1])
    pf = _portfolio(100_000, marks={"AAPL": 200.0})
    # 想买 1000 股 * 200 = 200k = 200% 权益
    decision = eng.check(_order(1000, symbol="AAPL"), pf)

    assert decision.decision is Decision.RESIZE
    # 10% * 100k / 200 = 50 股
    assert decision.order.quantity == pytest.approx(50.0)


def test_default_rules_approve_small_position():
    cfg = RiskConfig(max_position_pct=0.10)
    eng = RiskEngine(cfg, clock=_list_clock()[1])
    pf = _portfolio(100_000, marks={"AAPL": 200.0})
    decision = eng.check(_order(10, symbol="AAPL"), pf)  # 10*200 = 2000 = 2%
    assert decision.decision is Decision.APPROVE


# =========================================================================== #
# 审计与真实 JSONL sink 的集成(tmp_path)
# =========================================================================== #

def test_engine_writes_jsonl_audit(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    sink = JsonlAuditSink(path)
    broker = _RecordingBroker()
    eng = RiskEngine(rules=[_approve_rule()], broker=broker, audit=sink, clock=_list_clock()[1])

    eng.submit(_order(100), _portfolio())
    sink.close()

    # 哈希链完好可自证
    assert JsonlAuditSink.verify(path)
    contents = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(contents) == 2  # decision + fill


# =========================================================================== #
# 并发:多线程 check() 不抛异常且熔断状态一致
# =========================================================================== #

def test_concurrent_checks_do_not_raise_and_state_consistent():
    """多线程并发 check(),既不抛异常,状态也保持自洽。

    权益始终远低于高点,理应在某一刻触发熔断并从此保持;不管调度如何交错,
    breaker_trip 只应落一次,且最终 breaker_tripped 为 True。
    """
    audit = _MemoryAudit()
    cfg = RiskConfig(max_drawdown_pct=0.15)
    eng = RiskEngine(cfg, rules=[_approve_rule()], audit=audit, clock=_list_clock()[1])

    # 先建立高点
    eng.update_equity(_portfolio(100_000))

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            for _ in range(50):
                # 权益 80k,回撤 20% > 15%,应触发并保持熔断
                eng.check(_order(1), _portfolio(80_000))
        except BaseException as exc:  # noqa: BLE001 - 测试需捕获一切
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []                       # 并发下无异常
    assert eng.breaker_tripped is True        # 状态最终一致
    assert audit.count("breaker_trip") == 1   # 熔断只落一次,无竞态重复


def test_concurrent_checks_all_return_decisions():
    """并发路径下每次 check() 都拿到合法裁决对象。"""
    eng = RiskEngine(rules=[_resize_rule(50.0)], clock=_list_clock()[1])
    results: list[RiskDecision] = []
    lock = threading.Lock()

    def worker():
        for _ in range(25):
            d = eng.check(_order(100), _portfolio(100_000))
            with lock:
                results.append(d)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6 * 25
    assert all(isinstance(d, RiskDecision) for d in results)
    assert all(d.decision is Decision.RESIZE and d.order.quantity == 50.0 for d in results)
