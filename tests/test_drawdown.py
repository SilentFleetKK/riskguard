"""针对 :class:`riskguard.DrawdownCircuitBreaker` 的完整测试。

覆盖点(全部由 :meth:`RiskEngine.update_equity` 驱动熔断,贴合真实用法):

* 未熔断 -> 放行,且规则给出 "within limit" 的说明;
* 熔断触发后 -> 拒绝新开仓 / 加仓(含做多加仓、做空加仓);
* 熔断触发后 -> 放行 ``reduce_only`` 单;
* 熔断触发后 -> 放行"减少风险"的卖出/买入平仓单;
* 拒单信息里含熔断触发原因(trip reason);
* 边界:恰好触及阈值 / 差一点点不触及 / 反手单被拦 / 首次登顶前回撤为 0。

设计约束:纯 pytest、无网络、确定性(需要时间时用可变列表时钟注入
:class:`RiskEngine`)。尽量走 ``riskguard`` 公共 API。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from riskguard import (
    Account,
    Decision,
    DrawdownCircuitBreaker,
    Order,
    Portfolio,
    Position,
    RiskConfig,
    RiskEngine,
    Side,
)


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------
SYMBOL = "AAPL"
MARK = 100.0


class ListClock:
    """可变列表时钟:确定性时间源,可在测试中推进而无需真实 sleep。

    调用返回列表当前头部时间;:meth:`advance` 往前推进,让每次观测/裁决
    落在不同(但可控)的时刻上。
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


def make_portfolio(
    equity: float,
    *,
    position_qty: float = 0.0,
    avg_price: float = MARK,
    mark: float = MARK,
    symbol: str = SYMBOL,
) -> Portfolio:
    """构造一个组合快照。

    ``equity`` 直接落在 :class:`Account` 上,用于驱动回撤/熔断;持仓与标记价
    仅用于让熔断规则判断某笔单是加仓还是减仓,二者互不牵连(这样测试回撤逻辑时
    可以自由设置权益,而不必让权益与持仓市值严格自洽)。
    """
    account = Account(equity=equity, cash=equity)
    positions: dict[str, Position] = {}
    if position_qty != 0.0:
        positions[symbol] = Position(symbol=symbol, quantity=position_qty, avg_price=avg_price)
    return Portfolio(account=account, positions=positions, marks={symbol: mark})


def make_engine(clock: ListClock | None = None, **config_kwargs: object) -> RiskEngine:
    """构造只装了熔断规则的引擎,隔离被测规则、排除其它规则干扰。"""
    config = RiskConfig(**config_kwargs) if config_kwargs else RiskConfig()
    return RiskEngine(
        config,
        rules=[DrawdownCircuitBreaker()],
        clock=clock or ListClock(),
    )


def trip_breaker(engine: RiskEngine, high: float, low: float) -> None:
    """通过两次 update_equity 把权益从 high 打到 low,驱动熔断触发。"""
    engine.update_equity(make_portfolio(high))
    engine.update_equity(make_portfolio(low))


def breaker_result(decision):
    """从裁决里取出熔断规则那条 RuleResult(本引擎只有这一条规则)。"""
    for r in decision.results:
        if r.rule == "drawdown_circuit_breaker":
            return r
    raise AssertionError("drawdown_circuit_breaker result not found")


# ---------------------------------------------------------------------------
# 未熔断 -> 放行
# ---------------------------------------------------------------------------
def test_not_tripped_approves_new_long():
    engine = make_engine()
    engine.update_equity(make_portfolio(100_000.0))
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    decision = engine.check(order, make_portfolio(100_000.0))
    assert decision.approved
    assert decision.decision is Decision.APPROVE
    assert not engine.breaker_tripped


def test_not_tripped_result_passed_and_message_within_limit():
    engine = make_engine()
    engine.update_equity(make_portfolio(100_000.0))
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    decision = engine.check(order, make_portfolio(100_000.0))
    result = breaker_result(decision)
    assert result.passed is True
    assert result.action is Decision.APPROVE
    assert "within limit" in result.message


def test_shallow_drawdown_below_limit_still_approves():
    # 回撤 10% < 15% 阈值 -> 不熔断,放行。
    engine = make_engine(max_drawdown_pct=0.15)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(90_000.0))  # -10%
    assert not engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    decision = engine.check(order, make_portfolio(90_000.0))
    assert decision.approved


def test_zero_high_water_mark_never_trips():
    # 高点从未 > 0(权益一直为 0),drawdown 恒为 0,不应熔断。
    engine = make_engine()
    engine.update_equity(make_portfolio(0.0))
    engine.update_equity(make_portfolio(0.0))
    assert not engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=1)
    decision = engine.check(order, make_portfolio(0.0))
    assert decision.approved


# ---------------------------------------------------------------------------
# 熔断触发 -> 拒绝新开仓 / 加仓
# ---------------------------------------------------------------------------
def test_tripped_rejects_new_long():
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)  # -20% > 15%
    assert engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    decision = engine.check(order, make_portfolio(80_000.0))
    assert decision.rejected
    assert decision.decision is Decision.REJECT


def test_tripped_rejects_new_short():
    # 新开空头也是"放大敞口",应被拒。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.SELL, quantity=10)
    decision = engine.check(order, make_portfolio(80_000.0))  # 当前无持仓
    assert decision.rejected


def test_tripped_rejects_increasing_long_position():
    # 已有多头,继续买入加仓 -> |持仓| 增大 -> 拒。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=5)
    pf = make_portfolio(80_000.0, position_qty=10)  # 已持多 10 股
    decision = engine.check(order, pf)
    assert decision.rejected


def test_tripped_rejects_increasing_short_position():
    # 已有空头,继续卖出加空 -> |持仓| 增大 -> 拒。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.SELL, quantity=5)
    pf = make_portfolio(80_000.0, position_qty=-10)  # 已持空 10 股
    decision = engine.check(order, pf)
    assert decision.rejected


def test_reject_message_and_detail_contain_trip_reason():
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    decision = engine.check(order, make_portfolio(80_000.0))
    result = breaker_result(decision)
    assert result.passed is False
    assert result.action is Decision.REJECT
    # 触发原因来自 state.trip_reason,含 "drawdown ... >= limit"。
    reason = engine.state.trip_reason
    assert reason  # 非空
    assert "drawdown" in reason
    assert reason in result.message
    assert result.detail["trip_reason"] == reason
    # 裁决的可读原因聚合里也应含拒单信息。
    assert "circuit breaker" in decision.reasons().lower()


def test_reject_message_mentions_manual_reset():
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    decision = engine.check(order, make_portfolio(80_000.0))
    result = breaker_result(decision)
    assert "manual reset" in result.message.lower()


# ---------------------------------------------------------------------------
# 熔断触发 -> 放行 reduce_only / 减仓单
# ---------------------------------------------------------------------------
def test_tripped_approves_reduce_only_order():
    # reduce_only 标记的单,即便看似加仓也应放行(信任调用方声明)。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10, reduce_only=True)
    decision = engine.check(order, make_portfolio(80_000.0))
    assert decision.approved
    result = breaker_result(decision)
    assert result.passed is True
    assert result.detail["reduce_only"] is True


def test_tripped_approves_risk_reducing_sell():
    # 持多 10 股,卖出 5 股 -> |持仓| 由 10 降到 5 -> 减仓 -> 放行。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.SELL, quantity=5)
    pf = make_portfolio(80_000.0, position_qty=10)
    decision = engine.check(order, pf)
    assert decision.approved
    result = breaker_result(decision)
    assert result.passed is True
    assert "reduces risk" in result.message


def test_tripped_approves_full_close_long():
    # 持多 10 股,卖出恰好 10 股平仓 -> |持仓| 归零 -> 减仓 -> 放行。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.SELL, quantity=10)
    pf = make_portfolio(80_000.0, position_qty=10)
    decision = engine.check(order, pf)
    assert decision.approved


def test_tripped_approves_risk_reducing_buy_to_cover():
    # 持空 10 股,买入 5 股回补 -> |持仓| 由 10 降到 5 -> 减仓 -> 放行。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=5)
    pf = make_portfolio(80_000.0, position_qty=-10)
    decision = engine.check(order, pf)
    assert decision.approved
    result = breaker_result(decision)
    assert result.passed is True


def test_tripped_reversal_order_is_rejected_when_magnitude_grows():
    # 持多 10 股,卖出 25 股会反手成空 15 股 -> |持仓| 由 10 增到 15 -> 放大 -> 拒。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.SELL, quantity=25)
    pf = make_portfolio(80_000.0, position_qty=10)
    decision = engine.check(order, pf)
    assert decision.rejected


def test_tripped_reversal_to_smaller_short_is_still_rejected():
    # 持多 10 股,卖出 15 股会反手成空 5 股。虽然 |持仓| 由 10 降到 5,但这是**平掉旧多仓、
    # 开出一个全新的空仓**——熔断期间任何新方向敞口都必须拦下(修复前这里会漏放行)。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.SELL, quantity=15)
    pf = make_portfolio(80_000.0, position_qty=10)
    decision = engine.check(order, pf)
    assert decision.rejected


def test_tripped_pure_reduction_same_side_is_approved():
    # 持多 10 股,卖出 6 股 -> 仍是多 4 股 -> 同向减仓、不翻转 -> 熔断中也放行。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    order = Order(symbol=SYMBOL, side=Side.SELL, quantity=6)
    pf = make_portfolio(80_000.0, position_qty=10)
    decision = engine.check(order, pf)
    assert decision.approved


# ---------------------------------------------------------------------------
# 阈值边界
# ---------------------------------------------------------------------------
def test_drawdown_exactly_at_limit_trips():
    # 回撤恰好 == 阈值:规则用 >= 判断,应触发熔断。
    engine = make_engine(max_drawdown_pct=0.15)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(85_000.0))  # 恰好 -15%
    assert engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    decision = engine.check(order, make_portfolio(85_000.0))
    assert decision.rejected


def test_drawdown_just_below_limit_does_not_trip():
    # 回撤 14.999% < 15% -> 不熔断。
    engine = make_engine(max_drawdown_pct=0.15)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(85_010.0))  # -14.99%
    assert not engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    decision = engine.check(order, make_portfolio(85_010.0))
    assert decision.approved


def test_tighter_limit_trips_on_smaller_drawdown():
    # 收紧阈值到 5%,-6% 即熔断。
    engine = make_engine(max_drawdown_pct=0.05)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(94_000.0))  # -6% > 5%
    assert engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    assert engine.check(order, make_portfolio(94_000.0)).rejected


# ---------------------------------------------------------------------------
# 熔断的时序 / 状态语义
# ---------------------------------------------------------------------------
def test_high_water_mark_climbs_before_drawdown():
    # 先涨到新高再回撤:回撤以最高点为基准。10万->12万->10.1万 回撤约 15.8% -> 熔断。
    engine = make_engine(max_drawdown_pct=0.15)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(120_000.0))  # 新高
    engine.update_equity(make_portfolio(101_000.0))  # 相对 12 万回撤 ~15.83%
    assert engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    assert engine.check(order, make_portfolio(101_000.0)).rejected


def test_breaker_stays_tripped_even_if_equity_recovers():
    # 熔断是黏着的:触发后即便权益反弹回高点,check 也不会自动解除,仍拒新仓。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    assert engine.breaker_tripped
    # 权益反弹回 10 万(drawdown 归 0),但熔断状态不清除。
    engine.update_equity(make_portfolio(100_000.0))
    assert engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    assert engine.check(order, make_portfolio(100_000.0)).rejected


def test_reset_breaker_reopens_new_positions():
    # 人工复盘 reset_breaker 后应可重新开仓。
    engine = make_engine(max_drawdown_pct=0.15)
    trip_breaker(engine, high=100_000.0, low=80_000.0)
    assert engine.breaker_tripped
    engine.reset_breaker()
    assert not engine.breaker_tripped
    order = Order(symbol=SYMBOL, side=Side.BUY, quantity=10)
    # reset 把高点归位到当前权益(8 万),此后 check 观测同一权益不会二次触发。
    decision = engine.check(order, make_portfolio(80_000.0))
    assert decision.approved


def test_reset_uses_injected_clock_for_state_timestamps():
    # 用可变时钟驱动,验证熔断/复盘的时间戳走注入时钟而非真实时间。
    clock = ListClock(datetime(2026, 3, 1, tzinfo=timezone.utc))
    engine = make_engine(clock=clock, max_drawdown_pct=0.15)
    engine.update_equity(make_portfolio(100_000.0))
    clock.advance(hours=1)
    engine.update_equity(make_portfolio(80_000.0))
    assert engine.breaker_tripped
    assert engine.state.tripped_at == datetime(2026, 3, 1, 1, tzinfo=timezone.utc)
    clock.advance(hours=2)
    engine.reset_breaker()
    assert not engine.breaker_tripped
    assert engine.state.tripped_at is None


# ---------------------------------------------------------------------------
# 规则单元:直接构造 RuleContext 求值(不经引擎)
# ---------------------------------------------------------------------------
def test_rule_direct_not_tripped_returns_approve():
    from riskguard import RuleContext, RiskState

    rule = DrawdownCircuitBreaker()
    ctx = RuleContext(
        order=Order(symbol=SYMBOL, side=Side.BUY, quantity=10),
        portfolio=make_portfolio(100_000.0),
        config=RiskConfig(),
        state=RiskState(high_water_mark=100_000.0, last_equity=100_000.0),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    result = rule.evaluate(ctx)
    assert result.passed is True
    assert result.action is Decision.APPROVE


def test_rule_direct_tripped_rejects_new_position():
    from riskguard import RuleContext, RiskState

    rule = DrawdownCircuitBreaker()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = RiskState(
        high_water_mark=100_000.0,
        last_equity=80_000.0,
        breaker_tripped=True,
        tripped_at=now,
        trip_reason="drawdown 20.00% >= limit 15.00%",
    )
    ctx = RuleContext(
        order=Order(symbol=SYMBOL, side=Side.BUY, quantity=10),
        portfolio=make_portfolio(80_000.0),
        config=RiskConfig(),
        state=state,
        now=now,
    )
    result = rule.evaluate(ctx)
    assert result.passed is False
    assert result.action is Decision.REJECT
    assert "drawdown 20.00% >= limit 15.00%" in result.message


# ---------------------------------------------------------------------------
# 与审计后端联动:熔断触发时应落一条 breaker_trip 事件
# ---------------------------------------------------------------------------
def test_breaker_trip_writes_audit_event(tmp_path):
    from riskguard import JsonlAuditSink

    log_path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(log_path))
    engine = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15),
        rules=[DrawdownCircuitBreaker()],
        audit=sink,
        clock=ListClock(),
    )
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(80_000.0))
    sink.close()

    assert engine.breaker_tripped
    assert JsonlAuditSink.verify(str(log_path))
    contents = log_path.read_text(encoding="utf-8")
    assert "breaker_trip" in contents
    # 熔断事件应带上触发原因,便于事后审计。
    assert "drawdown" in contents


def test_no_audit_event_when_not_tripped(tmp_path):
    from riskguard import JsonlAuditSink

    log_path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(log_path))
    engine = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15),
        rules=[DrawdownCircuitBreaker()],
        audit=sink,
        clock=ListClock(),
    )
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(95_000.0))  # -5% < 15%
    sink.close()

    assert not engine.breaker_tripped
    contents = log_path.read_text(encoding="utf-8")
    assert "breaker_trip" not in contents
