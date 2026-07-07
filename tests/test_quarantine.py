"""``StrategyQuarantine`` 隔离观察规则的行为测试。

覆盖文章三条铁律的第三条——"新策略先用最小的钱跑,活过隔离期再加仓"。重点验证:

* 未登记的策略:隔离规则**不额外**约束(放行,由其它规则如仓位上限兜底);
* ``auto_register_strategies=True`` 时,首次见到的新策略首单即进隔离,被压到
  ``quarantine_max_position_pct``;
* **显式** :meth:`RiskEngine.register_strategy` 登记、且仍在观察期内的策略被紧紧封顶;
* 用可控时钟把 ``now`` 推过观察期后,策略出役、恢复到正常仓位上限;
* ``quarantine_days=0`` 等价于关闭隔离。

所有涉及时间的用例都通过 ``RiskEngine(clock=...)`` 注入一个可变列表时钟,保证确定性、
零网络、零真实墙钟依赖。规则本身也直接以手搓的 :class:`RuleContext` 单测,便于精确
命中每条分支。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from riskguard import (
    Account,
    Decision,
    Order,
    Portfolio,
    Position,
    RiskConfig,
    RiskEngine,
    RuleContext,
    Side,
    StrategyQuarantine,
)
from riskguard.state import RiskState

# ---------------------------------------------------------------------------
# 测试夹具 / 工具
# ---------------------------------------------------------------------------

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SYMBOL = "AAPL"
MARK = 100.0
EQUITY = 100_000.0
DAY = timedelta(days=1)


class MutableClock:
    """一个可变时钟:``clock()`` 返回当前时间,``advance()`` 推进它。

    注入 :class:`RiskEngine` 后即可完全掌控引擎眼里的"现在",从而在测试里
    确定性地跨越隔离观察期,而不依赖任何真实墙钟。
    """

    def __init__(self, start: datetime = T0) -> None:
        self._now = [start]

    def __call__(self) -> datetime:
        return self._now[0]

    def advance(self, delta: timedelta) -> None:
        self._now[0] = self._now[0] + delta

    def set(self, when: datetime) -> None:
        self._now[0] = when


def make_portfolio(
    *,
    equity: float = EQUITY,
    positions: dict[str, Position] | None = None,
    mark: float | None = MARK,
) -> Portfolio:
    """构造一个用于风控的组合快照。``mark=None`` 表示不提供标记价。"""
    account = Account(equity=equity, cash=equity)
    marks = {SYMBOL: mark} if mark is not None else {}
    return Portfolio(account=account, positions=positions or {}, marks=marks)


def buy(qty: float, *, strategy_id: str = "s", symbol: str = SYMBOL) -> Order:
    return Order(symbol=symbol, side=Side.BUY, quantity=qty, strategy_id=strategy_id)


def sell(qty: float, *, strategy_id: str = "s", symbol: str = SYMBOL) -> Order:
    return Order(symbol=symbol, side=Side.SELL, quantity=qty, strategy_id=strategy_id)


def quarantine_result(decision):
    """从一次裁决里挑出隔离规则的那条 RuleResult。"""
    matches = [r for r in decision.results if r.rule == StrategyQuarantine.name]
    assert len(matches) == 1, "隔离规则应恰好产出一条结果"
    return matches[0]


# 在默认 10% 仓位上限、1% 隔离上限、mark=100、equity=100k 下的换算基准数量:
#   仓位上限 10%  -> 100k * 0.10 / 100 = 100 股
#   隔离上限 1%   -> 100k * 0.01 / 100 = 10 股
NORMAL_CAP_QTY = 100.0
QUARANTINE_CAP_QTY = 10.0


# ---------------------------------------------------------------------------
# 1. 未登记策略:隔离规则不介入,由其它规则治理
# ---------------------------------------------------------------------------


def test_unregistered_strategy_quarantine_approves():
    """未登记(auto_register 关闭)的策略,隔离规则直接放行,age 为 None。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(), clock=clock)  # auto_register 默认 False
    decision = engine.check(buy(1000, strategy_id="unregistered"), make_portfolio())

    q = quarantine_result(decision)
    assert q.action is Decision.APPROVE
    assert q.passed is True
    assert q.detail.get("age_days") is None


def test_unregistered_strategy_still_governed_by_position_limit():
    """隔离不介入,但整体裁决仍受单笔仓位上限(10%)约束 -> 缩到 100 股。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(), clock=clock)
    decision = engine.check(buy(1000, strategy_id="unregistered"), make_portfolio())

    # 隔离放行,但 max_position_limit 把 1000 股缩到 100 股(10% 上限)。
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(NORMAL_CAP_QTY)
    assert quarantine_result(decision).action is Decision.APPROVE


def test_unregistered_small_order_fully_approved():
    """未登记 + 订单本就在所有上限内 -> 整体 APPROVE,数量不变。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(), clock=clock)
    decision = engine.check(buy(50, strategy_id="unregistered"), make_portfolio())

    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == pytest.approx(50)
    assert quarantine_result(decision).action is Decision.APPROVE


# ---------------------------------------------------------------------------
# 2. auto_register_strategies=True:新策略首单即进隔离
# ---------------------------------------------------------------------------


def test_auto_register_caps_fresh_strategy_at_quarantine_pct():
    """开启自动登记后,首次见到的新策略被压到隔离上限(1% -> 10 股)。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(auto_register_strategies=True), clock=clock)
    decision = engine.check(buy(1000, strategy_id="fresh"), make_portfolio())

    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(QUARANTINE_CAP_QTY)
    q = quarantine_result(decision)
    assert q.action is Decision.RESIZE
    assert q.passed is False
    assert q.adjusted_quantity == pytest.approx(QUARANTINE_CAP_QTY)


def test_auto_register_records_inception_in_state():
    """自动登记会把策略入役时间写进状态,时间等于首见时刻。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(auto_register_strategies=True), clock=clock)
    engine.check(buy(10, strategy_id="fresh"), make_portfolio())

    inception = engine.state.strategy_inception
    assert "fresh" in inception
    assert inception["fresh"] == T0


def test_auto_register_does_not_move_inception_on_repeat():
    """自动登记只在首见时记录;后续下单不刷新入役时间(隔离期不会被重置)。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(auto_register_strategies=True), clock=clock)
    engine.check(buy(5, strategy_id="fresh"), make_portfolio())
    first = engine.state.strategy_inception["fresh"]

    clock.advance(10 * DAY)
    engine.check(buy(5, strategy_id="fresh"), make_portfolio())

    assert engine.state.strategy_inception["fresh"] == first == T0


def test_auto_register_disabled_leaves_state_untouched():
    """auto_register 关闭时,check() 不会登记任何策略。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(auto_register_strategies=False), clock=clock)
    engine.check(buy(5, strategy_id="ghost"), make_portfolio())

    assert "ghost" not in engine.state.strategy_inception


def test_auto_register_small_order_within_quarantine_cap_approved():
    """自动登记 + 订单本就在 1% 隔离上限内 -> 整体 APPROVE。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(auto_register_strategies=True), clock=clock)
    decision = engine.check(buy(5, strategy_id="fresh"), make_portfolio())

    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == pytest.approx(5)
    assert quarantine_result(decision).action is Decision.APPROVE


# ---------------------------------------------------------------------------
# 3. 显式登记 + 观察期内:紧紧封顶
# ---------------------------------------------------------------------------


def test_explicit_register_within_window_tightly_capped():
    """显式登记、当天下单(age≈0d)-> 被压到隔离上限 10 股。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(), clock=clock)
    engine.register_strategy("watched")
    decision = engine.check(buy(1000, strategy_id="watched"), make_portfolio())

    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(QUARANTINE_CAP_QTY)
    q = quarantine_result(decision)
    assert q.action is Decision.RESIZE
    assert q.adjusted_quantity == pytest.approx(QUARANTINE_CAP_QTY)


def test_explicit_register_partway_through_window_still_capped():
    """观察期过了一半(45/90 天)仍在期内 -> 依旧被隔离上限封顶。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(quarantine_days=90), clock=clock)
    engine.register_strategy("watched")
    clock.advance(45 * DAY)
    decision = engine.check(buy(1000, strategy_id="watched"), make_portfolio())

    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(QUARANTINE_CAP_QTY)


def test_explicit_register_just_before_window_end_capped():
    """观察期结束前一刻(89 天)仍在期内 -> 被封顶。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(quarantine_days=90), clock=clock)
    engine.register_strategy("watched")
    clock.advance(89 * DAY)
    decision = engine.check(buy(1000, strategy_id="watched"), make_portfolio())

    assert quarantine_result(decision).action is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(QUARANTINE_CAP_QTY)


def test_quarantined_order_rejected_when_no_room_left():
    """已持有到隔离上限(10 股),再加仓 -> allowed=0 -> 隔离规则拒单,整体 REJECT。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(), clock=clock)
    engine.register_strategy("watched")
    held = {SYMBOL: Position(SYMBOL, QUARANTINE_CAP_QTY, MARK)}
    decision = engine.check(
        buy(5, strategy_id="watched"), make_portfolio(positions=held)
    )

    q = quarantine_result(decision)
    assert q.action is Decision.REJECT
    assert q.passed is False
    assert decision.decision is Decision.REJECT
    assert decision.rejected is True


def test_quarantined_partial_room_resizes_to_remaining():
    """已持 4 股、上限 10 股,买 100 股 -> 只允许再加 6 股。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(), clock=clock)
    engine.register_strategy("watched")
    held = {SYMBOL: Position(SYMBOL, 4.0, MARK)}
    decision = engine.check(
        buy(100, strategy_id="watched"), make_portfolio(positions=held)
    )

    q = quarantine_result(decision)
    assert q.action is Decision.RESIZE
    assert q.adjusted_quantity == pytest.approx(6.0)
    assert decision.order.quantity == pytest.approx(6.0)


def test_quarantined_reduce_only_sell_approved():
    """隔离期内的减仓单(卖出、缩小敞口)不受隔离上限约束 -> 放行。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(), clock=clock)
    engine.register_strategy("watched")
    held = {SYMBOL: Position(SYMBOL, 100.0, MARK)}  # 远超隔离上限的既有多头
    decision = engine.check(
        sell(50, strategy_id="watched"), make_portfolio(positions=held)
    )

    q = quarantine_result(decision)
    assert q.action is Decision.APPROVE
    assert q.passed is True


def test_quarantined_non_positive_equity_rejected():
    """隔离期内权益非正(无法计价封顶)-> 保守拒单。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(), clock=clock)
    engine.register_strategy("watched")
    decision = engine.check(
        buy(10, strategy_id="watched"), make_portfolio(equity=0.0)
    )

    q = quarantine_result(decision)
    assert q.action is Decision.REJECT
    assert q.passed is False


# ---------------------------------------------------------------------------
# 4. 推进时钟越过观察期:出役、恢复正常上限
# ---------------------------------------------------------------------------


def test_strategy_released_after_window_elapses():
    """把时钟推过 90 天观察期后,隔离规则放行(只剩正常仓位上限治理)。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(quarantine_days=90), clock=clock)
    engine.register_strategy("graduated")

    # 观察期内:被封顶
    decision_in = engine.check(buy(1000, strategy_id="graduated"), make_portfolio())
    assert quarantine_result(decision_in).action is Decision.RESIZE
    assert decision_in.order.quantity == pytest.approx(QUARANTINE_CAP_QTY)

    # 推进到 91 天:出役
    clock.advance(91 * DAY)
    decision_out = engine.check(buy(1000, strategy_id="graduated"), make_portfolio())

    q = quarantine_result(decision_out)
    assert q.action is Decision.APPROVE
    assert q.passed is True
    # 出役后回到正常 10% 仓位上限 -> 缩到 100 股(不再是 10 股)
    assert decision_out.decision is Decision.RESIZE
    assert decision_out.order.quantity == pytest.approx(NORMAL_CAP_QTY)


def test_strategy_released_exactly_at_window_boundary():
    """age 恰好等于 quarantine_days(90d)-> 出役(判定用 >=)。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(quarantine_days=90), clock=clock)
    engine.register_strategy("boundary")
    clock.set(T0 + 90 * DAY)  # age == 90.0
    decision = engine.check(buy(1000, strategy_id="boundary"), make_portfolio())

    assert quarantine_result(decision).action is Decision.APPROVE


def test_strategy_capped_just_inside_window_boundary():
    """age 恰好差一点点(89 天 23 小时)-> 仍在期内,被封顶。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(quarantine_days=90), clock=clock)
    engine.register_strategy("boundary")
    clock.set(T0 + timedelta(days=89, hours=23))
    decision = engine.check(buy(1000, strategy_id="boundary"), make_portfolio())

    assert quarantine_result(decision).action is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(QUARANTINE_CAP_QTY)


def test_short_quarantine_window_releases_after_one_day():
    """自定义 1 天观察期:第 2 天即出役。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(quarantine_days=1), clock=clock)
    engine.register_strategy("quick")

    decision_in = engine.check(buy(1000, strategy_id="quick"), make_portfolio())
    assert quarantine_result(decision_in).action is Decision.RESIZE

    clock.advance(2 * DAY)
    decision_out = engine.check(buy(1000, strategy_id="quick"), make_portfolio())
    assert quarantine_result(decision_out).action is Decision.APPROVE


# ---------------------------------------------------------------------------
# 5. quarantine_days=0 关闭隔离
# ---------------------------------------------------------------------------


def test_quarantine_days_zero_disables_rule_for_registered_strategy():
    """quarantine_days=0:即便显式登记,策略也立即出役 -> 隔离放行。"""
    clock = MutableClock()
    engine = RiskEngine(RiskConfig(quarantine_days=0), clock=clock)
    engine.register_strategy("watched")
    decision = engine.check(buy(1000, strategy_id="watched"), make_portfolio())

    q = quarantine_result(decision)
    assert q.action is Decision.APPROVE
    assert q.passed is True
    # 仅受正常 10% 仓位上限治理
    assert decision.order.quantity == pytest.approx(NORMAL_CAP_QTY)


def test_quarantine_days_zero_disables_even_with_auto_register():
    """quarantine_days=0 + auto_register=True:自动登记但立即出役 -> 放行。"""
    clock = MutableClock()
    engine = RiskEngine(
        RiskConfig(quarantine_days=0, auto_register_strategies=True), clock=clock
    )
    decision = engine.check(buy(1000, strategy_id="fresh"), make_portfolio())

    assert quarantine_result(decision).action is Decision.APPROVE
    # 仍被自动登记(只是隔离窗口为 0 天,规则不生效)
    assert "fresh" in engine.state.strategy_inception


# ---------------------------------------------------------------------------
# 6. 直接对规则做单元测试(手搓 RuleContext,精确命中分支)
# ---------------------------------------------------------------------------


def _ctx(order: Order, state: RiskState, now: datetime, config: RiskConfig | None = None,
         *, equity: float = EQUITY, positions: dict[str, Position] | None = None,
         mark: float | None = MARK) -> RuleContext:
    return RuleContext(
        order=order,
        portfolio=make_portfolio(equity=equity, positions=positions, mark=mark),
        config=config or RiskConfig(),
        state=state,
        now=now,
    )


def test_rule_unregistered_returns_approve_with_none_age():
    """规则层:未登记策略 age=None -> APPROVE。"""
    rule = StrategyQuarantine()
    state = RiskState.initial(EQUITY)  # 无任何登记
    result = rule.evaluate(_ctx(buy(1000, strategy_id="nobody"), state, T0 + DAY))

    assert result.action is Decision.APPROVE
    assert result.passed is True
    assert result.detail.get("age_days") is None


def test_rule_within_window_resizes_to_cap():
    """规则层:登记 1 天(<90)-> RESIZE 到 10 股,detail 带上诊断字段。"""
    rule = StrategyQuarantine()
    state = RiskState.initial(EQUITY).register_strategy("s", T0)
    result = rule.evaluate(_ctx(buy(1000, strategy_id="s"), state, T0 + DAY))

    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(QUARANTINE_CAP_QTY)
    assert result.detail["strategy_id"] == "s"
    assert result.detail["cap"] == pytest.approx(0.01)
    assert result.detail["age_days"] == pytest.approx(1.0)


def test_rule_out_of_window_returns_approve():
    """规则层:登记满 90 天(age>=window)-> APPROVE。"""
    rule = StrategyQuarantine()
    state = RiskState.initial(EQUITY).register_strategy("s", T0)
    result = rule.evaluate(_ctx(buy(1000, strategy_id="s"), state, T0 + 90 * DAY))

    assert result.action is Decision.APPROVE
    assert result.detail.get("age_days") == pytest.approx(90.0)


def test_rule_zero_days_config_returns_approve_immediately():
    """规则层:quarantine_days=0 -> age(>=0)>=0 恒成立 -> APPROVE。"""
    rule = StrategyQuarantine()
    state = RiskState.initial(EQUITY).register_strategy("s", T0)
    result = rule.evaluate(
        _ctx(buy(1000, strategy_id="s"), state, T0, config=RiskConfig(quarantine_days=0))
    )

    assert result.action is Decision.APPROVE


def test_rule_reject_when_price_missing_raises():
    """规则层:观察期内且无任何可用价格 -> resolve_price 抛 PriceUnavailable。"""
    from riskguard import PriceUnavailable

    rule = StrategyQuarantine()
    state = RiskState.initial(EQUITY).register_strategy("s", T0)
    # mark=None 且订单为市价单无 limit_price、无持仓均价 -> 无从计价
    ctx = _ctx(buy(1000, strategy_id="s"), state, T0 + DAY, mark=None)
    with pytest.raises(PriceUnavailable):
        rule.evaluate(ctx)


def test_rule_negative_equity_rejects():
    """规则层:观察期内权益为负 -> REJECT(不试图计价)。"""
    rule = StrategyQuarantine()
    state = RiskState.initial(EQUITY).register_strategy("s", T0)
    result = rule.evaluate(
        _ctx(buy(10, strategy_id="s"), state, T0 + DAY, equity=-1.0)
    )

    assert result.action is Decision.REJECT
    assert result.passed is False


def test_rule_result_detail_is_immutable():
    """规则产出的 RuleResult.detail 为只读映射,不能被外部原地篡改。"""
    rule = StrategyQuarantine()
    state = RiskState.initial(EQUITY).register_strategy("s", T0)
    result = rule.evaluate(_ctx(buy(1000, strategy_id="s"), state, T0 + DAY))

    with pytest.raises(TypeError):
        result.detail["cap"] = 0.99  # type: ignore[index]
