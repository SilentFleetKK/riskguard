"""针对日内亏损熔断线(:class:`riskguard.DailyLossLimit`)的完整测试。

与 :class:`DrawdownCircuitBreaker`(相对历史高点、跨日累计)互补:日内线抓的是
"今天快速失血"。全部由 :meth:`RiskEngine.update_equity` / :meth:`check` 驱动,
贴合真实用法;时间用 ListClock 注入,确定性。

覆盖点:

* 触线拉闸:当日亏损 >= ``max_daily_loss_pct`` 后拒绝加仓单;
* 减仓/平仓单在日内熔断期间永远放行;反手单被拦;
* 粘性:日内权益回升到线内也不解除(今天到此为止就是到此为止);
* 换日自动复位:跨过 ``session_boundary_utc`` 后重新锚定、恢复交易;
* 与总回撤熔断相互独立:日内触发时总线可以安然无恙,反之亦然;
* ``reset_breaker()`` 不清除日内熔断;
* 未启用(None)时规则是纯空操作;
* 审计留痕:``daily_breaker_trip`` / ``daily_breaker_reset`` 事件。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from riskguard import (
    Account,
    Decision,
    Order,
    Portfolio,
    Position,
    RiskConfig,
    RiskEngine,
    Side,
)
from riskguard.audit.base import AuditEvent, AuditSink

SYMBOL = "AAPL"
MARK = 100.0


class ListClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


class RecordingAudit(AuditSink):
    name = "recording"

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.event_type for e in self.events]


def make_portfolio(equity: float, *, position_qty: float = 0.0) -> Portfolio:
    account = Account(equity=equity, cash=equity)
    positions: dict[str, Position] = {}
    if position_qty != 0.0:
        positions[SYMBOL] = Position(symbol=SYMBOL, quantity=position_qty, avg_price=MARK)
    return Portfolio(account=account, positions=positions, marks={SYMBOL: MARK})


def make_engine(
    clock: ListClock | None = None,
    audit: AuditSink | None = None,
    **config_kwargs: object,
) -> RiskEngine:
    config_kwargs.setdefault("max_daily_loss_pct", 0.03)
    # 权益波动测试里不想被总线干扰:总回撤线放宽
    config_kwargs.setdefault("max_drawdown_pct", 0.50)
    cfg = RiskConfig(**config_kwargs)  # type: ignore[arg-type]
    return RiskEngine(cfg, clock=clock or ListClock(), audit=audit)


def buy(qty: float = 1.0, **kwargs: object) -> Order:
    return Order(SYMBOL, Side.BUY, qty, **kwargs)  # type: ignore[arg-type]


def sell(qty: float = 1.0, **kwargs: object) -> Order:
    return Order(SYMBOL, Side.SELL, qty, **kwargs)  # type: ignore[arg-type]


def rule_result(decision, name="daily_loss_limit"):
    return next(r for r in decision.results if r.rule == name)


# ---------------------------------------------------------------------------
# 触发与放行
# ---------------------------------------------------------------------------
def test_within_daily_limit_approves():
    engine = make_engine()
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(98_000.0))  # -2% < 3%
    decision = engine.check(buy(), make_portfolio(98_000.0))
    assert rule_result(decision).passed


def test_daily_loss_at_limit_trips_and_blocks_increase():
    engine = make_engine()
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(97_000.0))  # -3% == 限值
    assert engine.state.daily_tripped
    decision = engine.check(buy(), make_portfolio(97_000.0))
    assert decision.decision is Decision.REJECT
    assert not rule_result(decision).passed
    assert "daily" in rule_result(decision).message.lower()


def test_daily_trip_allows_reduce_only_and_decreasing():
    engine = make_engine()
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(96_000.0))
    holding = make_portfolio(96_000.0, position_qty=10.0)
    # reduce_only 标记
    d1 = engine.check(sell(5.0, reduce_only=True), holding)
    assert rule_result(d1).passed
    # 无标记但确实在减仓
    d2 = engine.check(sell(5.0), holding)
    assert rule_result(d2).passed
    # 反手(卖出超过持仓)= 新增方向性风险,拦
    d3 = engine.check(sell(25.0), holding)
    assert not rule_result(d3).passed


def test_daily_trip_is_sticky_within_session():
    """当日回血也不解锁——防止"回血一点就重新上杠杆"的赌徒循环。"""
    engine = make_engine()
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(96_000.0))
    engine.update_equity(make_portfolio(99_500.0))  # 回血到 -0.5%
    assert engine.state.daily_tripped
    decision = engine.check(buy(), make_portfolio(99_500.0))
    assert decision.decision is Decision.REJECT


# ---------------------------------------------------------------------------
# 换日复位与会话边界
# ---------------------------------------------------------------------------
def test_session_rollover_resets_daily_trip_and_reanchors():
    clock = ListClock()
    engine = make_engine(clock)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(96_000.0))
    assert engine.state.daily_tripped

    clock.advance(days=1)
    engine.update_equity(make_portfolio(96_000.0))
    assert not engine.state.daily_tripped
    assert engine.state.session_anchor_equity == 96_000.0
    decision = engine.check(buy(), make_portfolio(96_000.0))
    assert rule_result(decision).passed


def test_custom_session_boundary_respected():
    """边界 17:00:16:59 仍在昨天的会话里,17:00 起才换日复位。"""
    clock = ListClock(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    engine = make_engine(clock, session_boundary_utc="17:00")
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(96_000.0))
    assert engine.state.daily_tripped

    clock.advance(hours=4, minutes=59)  # 16:59,仍是同一会话
    engine.update_equity(make_portfolio(96_000.0))
    assert engine.state.daily_tripped

    clock.advance(minutes=1)  # 17:00,换日
    engine.update_equity(make_portfolio(96_000.0))
    assert not engine.state.daily_tripped


# ---------------------------------------------------------------------------
# 与总回撤熔断的独立性
# ---------------------------------------------------------------------------
def test_daily_line_trips_while_total_drawdown_is_fine():
    """高点是很久以前的事,当日 -3% 远够不着 50% 总线——日内线单独拉闸。"""
    engine = make_engine()
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(97_000.0))
    assert engine.state.daily_tripped
    assert not engine.state.breaker_tripped


def test_reset_breaker_does_not_clear_daily_trip():
    engine = make_engine(max_drawdown_pct=0.05)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(94_000.0))  # 同时打穿两条线
    assert engine.state.breaker_tripped
    assert engine.state.daily_tripped
    engine.reset_breaker()
    assert not engine.state.breaker_tripped
    assert engine.state.daily_tripped  # 日内线只随换日解除


# ---------------------------------------------------------------------------
# 未启用 = 空操作
# ---------------------------------------------------------------------------
def test_disabled_daily_loss_is_noop():
    engine = make_engine(max_daily_loss_pct=None)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(60_000.0))  # -40%,但日内线未启用
    assert not engine.state.daily_tripped
    decision = engine.check(buy(), make_portfolio(60_000.0))
    assert rule_result(decision).passed


# ---------------------------------------------------------------------------
# 审计留痕
# ---------------------------------------------------------------------------
def test_audit_records_daily_trip_and_rollover_reset():
    clock = ListClock()
    audit = RecordingAudit()
    engine = make_engine(clock, audit=audit)
    engine.update_equity(make_portfolio(100_000.0))
    engine.update_equity(make_portfolio(96_000.0))
    assert "daily_breaker_trip" in audit.types()

    clock.advance(days=1)
    engine.update_equity(make_portfolio(96_000.0))
    assert "daily_breaker_reset" in audit.types()


def test_rollover_without_trip_emits_no_reset_event():
    """平静的换日不该刷审计噪音:没触发过就没有 reset 事件。"""
    clock = ListClock()
    audit = RecordingAudit()
    engine = make_engine(clock, audit=audit)
    engine.update_equity(make_portfolio(100_000.0))
    clock.advance(days=1)
    engine.update_equity(make_portfolio(100_000.0))
    assert "daily_breaker_reset" not in audit.types()
