"""针对 fat-finger 价格保护带(:class:`riskguard.PriceBandRule`)的完整测试。

约束限价单价格不得偏离参考价(mark)超过 ``max_price_band_pct``。设计边界:

* 只约束限价单——市价单没有声明价可校验(诚实边界,文档写明);
* 减仓单豁免(核心原则:任何时候不阻止风险收敛);
* 参考价只认 ``portfolio.marks``:不用 ``resolve_price()``(它会回退到限价
  自身——自己跟自己比恒等于零偏离,规则形同虚设),也不用 ``avg_price``
  (陈旧的入场价不是市价);
* 无参考价 → 拒单(fail-closed,与"拿不到价宁可拒单"的库哲学一致);
* 未启用(None)= 空操作。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from riskguard import (
    Account,
    Decision,
    Order,
    OrderType,
    Portfolio,
    Position,
    PriceBandRule,
    RiskConfig,
    RiskEngine,
    RiskState,
    Side,
)
from riskguard.rules.base import RuleContext

SYMBOL = "AAPL"
MARK = 100.0
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_portfolio(
    *, mark: float | None = MARK, position_qty: float = 0.0, equity: float = 1_000_000.0
) -> Portfolio:
    positions: dict[str, Position] = {}
    if position_qty != 0.0:
        positions[SYMBOL] = Position(symbol=SYMBOL, quantity=position_qty, avg_price=90.0)
    marks = {SYMBOL: mark} if mark is not None else {}
    return Portfolio(
        account=Account(equity=equity, cash=equity), positions=positions, marks=marks
    )


def evaluate(order: Order, portfolio: Portfolio, **config_kwargs: object):
    config_kwargs.setdefault("max_price_band_pct", 0.10)
    ctx = RuleContext(
        order=order,
        portfolio=portfolio,
        config=RiskConfig(**config_kwargs),  # type: ignore[arg-type]
        state=RiskState.initial(1_000_000.0),
        now=NOW,
    )
    return PriceBandRule().evaluate(ctx)


def limit_buy(price: float, qty: float = 1.0, **kwargs: object) -> Order:
    return Order(
        SYMBOL, Side.BUY, qty, order_type=OrderType.LIMIT, limit_price=price, **kwargs
    )  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 带内 / 带外
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("price", [91.0, 100.0, 109.0, 110.0, 90.0])
def test_limit_within_band_approves(price):
    """±10% 保护带:[90, 110] 内(含边界,带浮点容差)放行。"""
    assert evaluate(limit_buy(price), make_portfolio()).passed


@pytest.mark.parametrize("price", [110.2, 150.0, 89.8, 50.0, 0.5])
def test_limit_outside_band_rejects(price):
    result = evaluate(limit_buy(price), make_portfolio())
    assert not result.passed
    assert result.action is Decision.REJECT
    assert "band" in result.message.lower()


def test_band_applies_to_sell_limits_too():
    sell = Order(SYMBOL, Side.SELL, 1.0, order_type=OrderType.LIMIT, limit_price=80.0)
    assert not evaluate(sell, make_portfolio()).passed


# ---------------------------------------------------------------------------
# 边界与豁免
# ---------------------------------------------------------------------------
def test_market_order_passes():
    """市价单没有声明价可校验——诚实的边界,不假装能管。"""
    assert evaluate(Order(SYMBOL, Side.BUY, 1.0), make_portfolio()).passed


def test_reduce_only_exempt_even_at_crazy_price():
    order = limit_buy(500.0, reduce_only=True)
    assert evaluate(order, make_portfolio(position_qty=-10.0)).passed


def test_no_mark_rejects_fail_closed():
    result = evaluate(limit_buy(100.0), make_portfolio(mark=None))
    assert not result.passed
    assert "reference" in result.message.lower() or "mark" in result.message.lower()


def test_avg_price_is_not_a_reference_price():
    """持仓的入场均价存在、但没有 mark → 仍然拒:陈旧价不是市价。"""
    portfolio = make_portfolio(mark=None, position_qty=10.0)
    assert not evaluate(limit_buy(100.0), portfolio).passed


def test_disabled_band_is_noop():
    result = evaluate(limit_buy(500.0), make_portfolio(), max_price_band_pct=None)
    assert result.passed


# ---------------------------------------------------------------------------
# 引擎集成:默认规则栈已包含本规则
# ---------------------------------------------------------------------------
def test_engine_default_stack_enforces_band():
    engine = RiskEngine(RiskConfig(max_price_band_pct=0.10))
    decision = engine.check(limit_buy(150.0, qty=1.0), make_portfolio())
    assert decision.decision is Decision.REJECT
    assert any(r.rule == "price_band" and not r.passed for r in decision.results)
