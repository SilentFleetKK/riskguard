"""针对下单频率节流(:class:`riskguard.OrderThrottle`)的完整测试。

拦住失控的 AI 循环:滚动窗口内已批准订单数触顶后拒绝新单。设计要点:

* 计数只算**已被批准**的历史订单(记录发生在引擎聚合之后)——被拒的单不消耗预算,
  所以 cap=N 意味着窗口内恰好允许 N 单,第 N+1 单被拒;
* 窗口滑动:旧订单滑出窗口后配额自动恢复;
* 分钟与小时配额相互独立,任一触顶即拒;
* 减仓单默认**完全豁免**("减仓永远放行"核心原则原样保留);显式设置
  ``reduce_only_throttle_factor`` 后才单独计桶、上限 = cap × factor——
  opt-in 的有限上限,让减仓循环终会被封但比普通单难触发 factor 倍;
* 节流状态随 ``state_store`` 持久化:重启不清零(重启不是绕过风控的后门);
* ``check()`` 消耗节流预算(它本就不是纯查询)——监控请走 ``build_digest``;
* 未启用(两个 cap 都为 None)= 空操作。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from riskguard import (
    Account,
    Decision,
    Order,
    Portfolio,
    RiskConfig,
    RiskEngine,
    Side,
    SqliteStateStore,
)

SYMBOL = "AAPL"
MARK = 100.0


class ListClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


def make_portfolio(equity: float = 1_000_000.0) -> Portfolio:
    return Portfolio(
        account=Account(equity=equity, cash=equity), positions={}, marks={SYMBOL: MARK}
    )


def make_engine(clock: ListClock | None = None, **config_kwargs: object) -> RiskEngine:
    config_kwargs.setdefault("max_orders_per_minute", 3)
    cfg = RiskConfig(**config_kwargs)  # type: ignore[arg-type]
    return RiskEngine(cfg, clock=clock or ListClock())


def buy(qty: float = 1.0, **kwargs: object) -> Order:
    return Order(SYMBOL, Side.BUY, qty, **kwargs)  # type: ignore[arg-type]


def throttle_result(decision):
    return next(r for r in decision.results if r.rule == "order_throttle")


# ---------------------------------------------------------------------------
# 基本配额
# ---------------------------------------------------------------------------
def test_cap_allows_exactly_n_orders_then_rejects():
    clock = ListClock()
    engine = make_engine(clock, max_orders_per_minute=3)
    portfolio = make_portfolio()
    for _ in range(3):
        clock.advance(seconds=1)
        assert engine.check(buy(), portfolio).approved
    clock.advance(seconds=1)
    decision = engine.check(buy(), portfolio)
    assert decision.decision is Decision.REJECT
    assert not throttle_result(decision).passed
    assert "throttle" in throttle_result(decision).message.lower()


def test_window_slides_and_budget_recovers():
    clock = ListClock()
    engine = make_engine(clock, max_orders_per_minute=2)
    portfolio = make_portfolio()
    assert engine.check(buy(), portfolio).approved
    clock.advance(seconds=1)
    assert engine.check(buy(), portfolio).approved
    clock.advance(seconds=1)
    assert not engine.check(buy(), portfolio).approved
    # 61 秒后最早那单滑出窗口
    clock.advance(seconds=61)
    assert engine.check(buy(), portfolio).approved


def test_hour_cap_independent_of_minute_cap():
    clock = ListClock()
    engine = make_engine(clock, max_orders_per_minute=2, max_orders_per_hour=3)
    portfolio = make_portfolio()
    assert engine.check(buy(), portfolio).approved
    clock.advance(seconds=1)
    assert engine.check(buy(), portfolio).approved
    # 分钟配额满;推进 2 分钟让它恢复,但小时配额只剩 1
    clock.advance(minutes=2)
    assert engine.check(buy(), portfolio).approved
    clock.advance(minutes=2)
    decision = engine.check(buy(), portfolio)  # 小时窗口内已 3 单
    assert not decision.approved
    assert "hour" in throttle_result(decision).message.lower()


def test_rejected_orders_do_not_consume_budget():
    """被(任何规则)拒掉的单不占节流配额——预算只记真正放行的。"""
    clock = ListClock()
    engine = make_engine(
        clock, max_orders_per_minute=2, max_position_pct=0.10, on_position_breach="reject"
    )
    portfolio = make_portfolio(equity=1_000.0)
    # 巨额单被仓位上限拒掉(1000 股 × $100 = 10 万,权益只有 1000)
    for _ in range(5):
        clock.advance(seconds=1)
        assert not engine.check(buy(1_000.0), portfolio).approved
    # 配额毫发无损:还能放行 2 个小单(0.01 股 × $100 = $1 ≤ 10% × $1000)
    clock.advance(seconds=1)
    assert engine.check(buy(0.01), portfolio).approved
    clock.advance(seconds=1)
    assert engine.check(buy(0.01), portfolio).approved
    clock.advance(seconds=1)
    assert not engine.check(buy(0.01), portfolio).approved


# ---------------------------------------------------------------------------
# 减仓单:单独计桶 × factor,最终也会被封
# ---------------------------------------------------------------------------
def test_reduce_only_uses_separate_wider_bucket():
    clock = ListClock()
    engine = make_engine(
        clock, max_orders_per_minute=1, reduce_only_throttle_factor=3.0
    )
    portfolio = make_portfolio()
    # 普通单:1 单后封
    assert engine.check(buy(), portfolio).approved
    clock.advance(seconds=1)
    assert not engine.check(buy(), portfolio).approved
    # 减仓单不受普通桶影响,自己的桶是 1×3=3
    for _ in range(3):
        clock.advance(seconds=1)
        assert engine.check(buy(reduce_only=True), portfolio).approved
    clock.advance(seconds=1)
    decision = engine.check(buy(reduce_only=True), portfolio)
    assert not decision.approved
    assert "reduce" in throttle_result(decision).message.lower()


def test_reduce_only_exempt_by_default():
    """默认(factor=None)减仓单完全豁免节流——"减仓永远放行"核心原则钉死。"""
    clock = ListClock()
    engine = make_engine(clock, max_orders_per_minute=1)
    portfolio = make_portfolio()
    assert engine.check(buy(), portfolio).approved
    clock.advance(seconds=1)
    assert not engine.check(buy(), portfolio).approved  # 普通桶已封
    for _ in range(100):  # 减仓单永远放行
        clock.advance(seconds=0.2)
        assert engine.check(buy(reduce_only=True), portfolio).approved
    # 豁免的减仓单也不挤占缓冲:普通桶计数不受影响
    assert engine.state.orders_in_window(clock(), timedelta(minutes=1), reduce_only=True) == 0


def test_runaway_reduce_loop_is_eventually_blocked():
    """opt-in 的有限上限:显式设 factor 后,减仓循环不会被无限放行。"""
    clock = ListClock()
    engine = make_engine(clock, max_orders_per_minute=2,
                         reduce_only_throttle_factor=5.0)  # 减仓桶 2×5=10
    portfolio = make_portfolio()
    approvals = 0
    for _ in range(50):
        clock.advance(seconds=0.5)
        if engine.check(buy(reduce_only=True), portfolio).approved:
            approvals += 1
    assert approvals == 10


# ---------------------------------------------------------------------------
# 持久化与未启用
# ---------------------------------------------------------------------------
def test_throttle_state_survives_restart(tmp_path):
    path = str(tmp_path / "s.db")
    clock = ListClock()
    portfolio = make_portfolio()

    store = SqliteStateStore(path)
    engine = RiskEngine(
        RiskConfig(max_orders_per_minute=2), clock=clock, state_store=store
    )
    assert engine.check(buy(), portfolio).approved
    clock.advance(seconds=1)
    assert engine.check(buy(), portfolio).approved
    store.close()

    # "重启":新进程、同一存档——配额不清零
    store2 = SqliteStateStore(path)
    engine2 = RiskEngine(
        RiskConfig(max_orders_per_minute=2), clock=clock, state_store=store2
    )
    clock.advance(seconds=1)
    assert not engine2.check(buy(), portfolio).approved
    store2.close()


def test_check_only_calls_consume_budget():
    """钉住文档承诺:check() 不是纯查询,它消耗节流预算。"""
    clock = ListClock()
    engine = make_engine(clock, max_orders_per_minute=1)
    portfolio = make_portfolio()
    assert engine.check(buy(), portfolio).approved  # 纯 check,没有 submit
    clock.advance(seconds=1)
    assert not engine.check(buy(), portfolio).approved


def test_disabled_throttle_is_noop():
    clock = ListClock()
    engine = make_engine(clock, max_orders_per_minute=None)
    portfolio = make_portfolio()
    for _ in range(100):
        clock.advance(seconds=0.1)
        assert engine.check(buy(), portfolio).approved


def test_state_stays_bounded_even_with_huge_caps():
    """recent_orders 有界:合法范围内的大配额,状态也不无限膨胀。
    (大到缓冲装不下的配额会在构造时被直接拒绝,见 pathological caps 用例。)"""
    clock = ListClock()
    engine = make_engine(clock, max_orders_per_minute=1_000, max_orders_per_hour=1_600)
    portfolio = make_portfolio()
    for _ in range(500):
        clock.advance(seconds=0.01)
        engine.check(buy(), portfolio)
    assert len(engine.state.recent_orders) <= 500  # 全部保留(都在窗口内)
    # 推进 2 小时,再下一单:窗口外的全被修剪
    clock.advance(hours=2)
    engine.check(buy(), portfolio)
    assert len(engine.state.recent_orders) == 1


def test_pathological_caps_that_defeat_recording_are_rejected():
    """节流配额大到记录缓冲装不下时,orders_in_window 永远数不到 cap,
    节流静默失效——fail-closed:这种配置直接拒绝构造引擎。"""
    import pytest

    from riskguard.exceptions import ConfigError

    # per_hour × (1 + factor) = 2001 × 6 > 10000 硬上限 → 拒绝
    with pytest.raises(ConfigError):
        RiskEngine(RiskConfig(max_orders_per_hour=2001, reduce_only_throttle_factor=5.0))
    # 合法边界:2000 × (1+4) = 10000 → 可以
    RiskEngine(RiskConfig(max_orders_per_hour=2000, reduce_only_throttle_factor=4.0))
