"""MaxPositionLimit(规则一:单笔仓位上限)的分支覆盖测试。

覆盖点(与 ``src/riskguard/rules/position_limit.py`` 的每条分支一一对应):

* 加仓越界 → 按 ``on_position_breach`` 缩单(resize)或拒单(reject);
* 缩仓 / 减仓单(含 ``reduce_only``)即便当前已超限也一律放行;
* 反手翻仓后 |敞口| 变大时按上限约束(缩到允许量);
* 空头侧与多头侧对称;
* 恰好落在上限边界上放行(带浮点容差);
* 权益非正直接拒单;
* 价格解析回退链(mark → limit → avg)与 PriceUnavailable。

所有测试确定性、无网络:引擎只装 ``MaxPositionLimit`` 一条规则以隔离被测行为,
需要时间时通过可变列表时钟注入。数据模型均为冻结 dataclass,测试只读不改。
"""

from __future__ import annotations

import datetime as _dt

import pytest

from riskguard import (
    Account,
    Decision,
    MaxPositionLimit,
    Order,
    OrderType,
    Portfolio,
    Position,
    PriceUnavailable,
    RiskConfig,
    RiskEngine,
    Side,
)

# --------------------------------------------------------------------------- #
# 常量与辅助工厂
# --------------------------------------------------------------------------- #

EQUITY = 100_000.0
PRICE = 100.0
CAP_PCT = 0.10
# cap_qty = max_position_pct * equity / price = 0.10 * 100_000 / 100 = 100 股
CAP_QTY = CAP_PCT * EQUITY / PRICE  # == 100.0


def _make_portfolio(
    equity: float = EQUITY,
    positions: dict | None = None,
    marks: dict | None = None,
) -> Portfolio:
    """组装一个只含所需信息的组合快照。"""
    return Portfolio(
        account=Account(equity=equity),
        positions=positions or {},
        marks=marks if marks is not None else {"AAPL": PRICE},
    )


def _pos(symbol: str, quantity: float, avg_price: float = PRICE) -> dict:
    return {symbol: Position(symbol=symbol, quantity=quantity, avg_price=avg_price)}


def _engine(
    *,
    on_position_breach: str = "resize",
    max_position_pct: float = CAP_PCT,
    clock=None,
) -> RiskEngine:
    """只装 MaxPositionLimit 一条规则的引擎,隔离被测规则。

    ``max_gross_exposure_pct`` 放到 100 倍,避免反手/翻仓这类大敞口用例
    被组合层敞口规则误伤——但本引擎根本没装那条规则,这里只是配置留白。
    """
    cfg = RiskConfig(
        max_position_pct=max_position_pct,
        on_position_breach=on_position_breach,
        max_gross_exposure_pct=100.0,
    )
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return RiskEngine(cfg, rules=[MaxPositionLimit()], **kwargs)


def _fixed_clock():
    """返回 ``(clock_callable, mutable_list)``;改列表首元即改时间(确定性)。"""
    holder = [_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)]
    return (lambda: holder[0]), holder


def _sole_result(decision):
    """引擎只装一条规则,取出该规则唯一的 RuleResult。"""
    assert len(decision.results) == 1
    result = decision.results[0]
    assert result.rule == "max_position_limit"
    return result


# --------------------------------------------------------------------------- #
# 越界缩单(resize,多头)
# --------------------------------------------------------------------------- #


def test_flat_buy_over_cap_resizes_to_exact_cap_qty():
    """空仓买入超 10% 上限 → 缩到恰好等于上限数量。"""
    eng = _engine()
    order = Order("AAPL", Side.BUY, 250)  # 想买 250 股(25% 敞口)
    decision = eng.check(order, _make_portfolio())

    assert decision.decision is Decision.RESIZE
    assert decision.resized is True
    assert decision.approved is True  # RESIZE 也算放行
    assert decision.order.quantity == pytest.approx(CAP_QTY)  # == 100
    # 原始订单不被改动(不可变):original_order 保留最初数量
    assert decision.original_order.quantity == 250
    assert order.quantity == 250


def test_resize_preserves_a_new_order_object_not_mutating_original():
    """缩单产出的是新 Order 对象,输入订单不被原地修改。"""
    eng = _engine()
    order = Order("AAPL", Side.BUY, 250)
    decision = eng.check(order, _make_portfolio())

    assert decision.order is not order
    assert decision.original_order is order
    assert order.quantity == 250  # 冻结对象未被动过


def test_resize_result_carries_allowed_quantity_detail():
    """resize 结果里 adjusted_quantity 与 detail.allowed_quantity 一致。"""
    eng = _engine()
    decision = eng.check(Order("AAPL", Side.BUY, 250), _make_portfolio())
    result = _sole_result(decision)

    assert result.action is Decision.RESIZE
    assert result.passed is False
    assert result.adjusted_quantity == pytest.approx(CAP_QTY)
    assert result.detail["allowed_quantity"] == pytest.approx(CAP_QTY)
    assert result.detail["cap"] == pytest.approx(CAP_PCT)
    assert result.detail["projected_weight"] == pytest.approx(0.25)


def test_partial_position_buy_over_cap_resizes_to_remaining_room():
    """已有部分持仓再加仓越界 → 缩到剩余可用额度(cap - current)。"""
    eng = _engine()
    # 已持 30 股(3%),再买 250 股 → 投影 280,允许量 = 100 - 30 = 70
    decision = eng.check(
        Order("AAPL", Side.BUY, 250),
        _make_portfolio(positions=_pos("AAPL", 30)),
    )
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(70.0)


# --------------------------------------------------------------------------- #
# 越界拒单(on_position_breach="reject")
# --------------------------------------------------------------------------- #


def test_reject_mode_rejects_instead_of_resizing():
    """配置 on_position_breach='reject' 时越界直接拒单,不缩量。"""
    eng = _engine(on_position_breach="reject")
    decision = eng.check(Order("AAPL", Side.BUY, 250), _make_portfolio())

    assert decision.decision is Decision.REJECT
    assert decision.rejected is True
    assert decision.approved is False
    assert decision.order.quantity == 250  # 拒单不改数量
    rejections = decision.rejections()
    assert len(rejections) == 1
    assert rejections[0].rule == "max_position_limit"


def test_reject_mode_within_cap_still_approves():
    """reject 模式下,只要没越界依然正常放行。"""
    eng = _engine(on_position_breach="reject")
    decision = eng.check(Order("AAPL", Side.BUY, 50), _make_portfolio())
    assert decision.decision is Decision.APPROVE


def test_adding_to_already_over_cap_position_rejects_when_no_room():
    """当前已超上限还想加仓 → 允许量 <=0,即便 resize 模式也只能拒单。"""
    eng = _engine(on_position_breach="resize")
    # 已持 150 股(15%,已超 10% 上限),再买 10 股 → 允许量 = 100 - 150 = -50 → 拒单
    decision = eng.check(
        Order("AAPL", Side.BUY, 10),
        _make_portfolio(positions=_pos("AAPL", 150)),
    )
    assert decision.decision is Decision.REJECT
    result = _sole_result(decision)
    assert result.action is Decision.REJECT
    assert result.detail["allowed_quantity"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 减仓 / reduce-only 一律放行
# --------------------------------------------------------------------------- #


def test_reducing_sell_while_over_cap_is_approved():
    """当前已超限,普通减仓卖出仍放行(风控只拦放大风险的单)。"""
    eng = _engine()
    # 持 150 股(超限),卖 30 → 投影 120,|120| < |150| 为减仓 → 放行
    decision = eng.check(
        Order("AAPL", Side.SELL, 30),
        _make_portfolio(positions=_pos("AAPL", 150)),
    )
    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == 30  # 减仓单不缩量


def test_reduce_only_sell_while_over_cap_is_approved():
    """reduce_only 减仓单在超限持仓上同样放行,且不缩量。"""
    eng = _engine()
    decision = eng.check(
        Order("AAPL", Side.SELL, 60, reduce_only=True),
        _make_portfolio(positions=_pos("AAPL", 150)),
    )
    # 投影 90 < 当前 150,为减仓 → 放行
    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == 60


def test_reduce_only_oversell_that_flips_but_ends_smaller_is_approved():
    """减仓卖穿反手,只要 |新敞口| 不大于原敞口即视为减仓,放行。"""
    eng = _engine()
    # 持 150 多头,卖 200 → 投影 -50,|{-50}|=50 < 150,不算放大 → 放行(不缩量)
    decision = eng.check(
        Order("AAPL", Side.SELL, 200, reduce_only=True),
        _make_portfolio(positions=_pos("AAPL", 150)),
    )
    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == 200


def test_buy_that_reduces_a_short_is_approved():
    """空头持仓上的买入(减空)是减仓方向 → 放行,即便当前超限。"""
    eng = _engine()
    # 持 -150 空头(超限),买 30 → 投影 -120,|{-120}| < 150 减仓 → 放行
    decision = eng.check(
        Order("AAPL", Side.BUY, 30),
        _make_portfolio(positions=_pos("AAPL", -150)),
    )
    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == 30


# --------------------------------------------------------------------------- #
# 反手翻仓后变大 → 受约束
# --------------------------------------------------------------------------- #


def test_flip_long_to_larger_short_is_constrained():
    """多翻空且 |新敞口| 变大 → 缩到方向允许量。"""
    eng = _engine()
    # 持 +50 多头,卖 200 → 投影 -150,|150| > |50| 放大。
    # 允许量 = cap_qty - side.sign*current = 100 - (-1)*50 = 150。
    decision = eng.check(
        Order("AAPL", Side.SELL, 200),
        _make_portfolio(positions=_pos("AAPL", 50)),
    )
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(150.0)


def test_flip_short_to_larger_long_is_constrained():
    """空翻多且 |新敞口| 变大 → 缩到方向允许量(与上一用例对称)。"""
    eng = _engine()
    # 持 -50 空头,买 200 → 投影 +150,放大。
    # 允许量 = cap_qty - side.sign*current = 100 - (1)*(-50) = 150。
    decision = eng.check(
        Order("AAPL", Side.BUY, 200),
        _make_portfolio(positions=_pos("AAPL", -50)),
    )
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(150.0)


def test_flip_reject_mode_rejects_oversized_flip():
    """reject 模式下,放大式反手同样被拒(而非缩量)。"""
    eng = _engine(on_position_breach="reject")
    decision = eng.check(
        Order("AAPL", Side.SELL, 200),
        _make_portfolio(positions=_pos("AAPL", 50)),
    )
    assert decision.decision is Decision.REJECT


# --------------------------------------------------------------------------- #
# 空头侧对称
# --------------------------------------------------------------------------- #


def test_flat_short_over_cap_resizes_to_exact_cap_qty():
    """空仓卖出建空超限 → 缩到恰好等于上限数量(与做多对称)。"""
    eng = _engine()
    decision = eng.check(Order("AAPL", Side.SELL, 250), _make_portfolio())
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(CAP_QTY)  # 100


def test_growing_existing_short_beyond_cap_rejects_when_no_room():
    """已超限的空头再加空,无剩余额度 → 拒单(空头版无额度拒单)。"""
    eng = _engine()
    # 持 -150(超限),再卖 10 → 允许量 = 100 - (-1)*(-150) = 100 - 150 = -50 → 拒单
    decision = eng.check(
        Order("AAPL", Side.SELL, 10),
        _make_portfolio(positions=_pos("AAPL", -150)),
    )
    assert decision.decision is Decision.REJECT


def test_short_partial_then_grow_resizes_to_remaining_room():
    """已持部分空头再加空越界 → 缩到剩余空头额度。"""
    eng = _engine()
    # 持 -30(3% 空),再卖 250 → 投影 -280,允许量 = 100 - (-1)*(-30) = 70
    decision = eng.check(
        Order("AAPL", Side.SELL, 250),
        _make_portfolio(positions=_pos("AAPL", -30)),
    )
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(70.0)


# --------------------------------------------------------------------------- #
# 边界:恰好落在上限
# --------------------------------------------------------------------------- #


def test_buy_exactly_at_cap_is_approved():
    """恰好买到上限数量(投影权重 == 上限)→ 放行,不缩量。"""
    eng = _engine()
    decision = eng.check(Order("AAPL", Side.BUY, CAP_QTY), _make_portfolio())
    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == CAP_QTY


def test_short_exactly_at_cap_is_approved():
    """恰好卖到上限数量(空头边界)→ 放行。"""
    eng = _engine()
    decision = eng.check(Order("AAPL", Side.SELL, CAP_QTY), _make_portfolio())
    assert decision.decision is Decision.APPROVE


def test_just_below_cap_is_approved():
    """略低于上限 → 放行。"""
    eng = _engine()
    decision = eng.check(Order("AAPL", Side.BUY, CAP_QTY - 1), _make_portfolio())
    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == CAP_QTY - 1


def test_tiny_overshoot_beyond_cap_resizes():
    """越过上限一点点(超出浮点容差)→ 触发缩单。"""
    eng = _engine()
    decision = eng.check(Order("AAPL", Side.BUY, CAP_QTY + 1), _make_portfolio())
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(CAP_QTY)


def test_within_float_tolerance_of_cap_is_approved():
    """与上限相差在浮点容差内 → 视为不越界,放行。"""
    eng = _engine()
    # cap_qty=100,加 1e-9 级别的极小超出,应被 within() 的容差吸收
    decision = eng.check(Order("AAPL", Side.BUY, CAP_QTY + 1e-7), _make_portfolio())
    assert decision.decision is Decision.APPROVE


# --------------------------------------------------------------------------- #
# 权益非正 → 拒单
# --------------------------------------------------------------------------- #


def test_zero_equity_rejects():
    """权益为 0 无法计价 → 拒单。"""
    eng = _engine()
    decision = eng.check(Order("AAPL", Side.BUY, 10), _make_portfolio(equity=0.0))
    assert decision.decision is Decision.REJECT
    result = _sole_result(decision)
    assert result.detail["equity"] == 0.0


def test_negative_equity_rejects():
    """权益为负(穿仓)→ 拒单。"""
    eng = _engine()
    decision = eng.check(Order("AAPL", Side.BUY, 10), _make_portfolio(equity=-5.0))
    assert decision.decision is Decision.REJECT


def test_non_positive_equity_still_allows_reduce_only():
    """权益归零(爆仓)时,reduce_only 减仓单**仍必须放行**——减仓永远放行铁律,
    不因 equity<=0 而被拒(修复前会误拒)。"""
    eng = _engine()
    decision = eng.check(
        Order("AAPL", Side.SELL, 10, reduce_only=True),
        _make_portfolio(equity=0.0, positions=_pos("AAPL", 50)),
    )
    assert decision.approved


def test_non_positive_equity_rejects_increasing_order():
    """但权益非正时,放大敞口的新开仓单仍应被拒(无法用无效权益给它定量)。"""
    eng = _engine()
    decision = eng.check(
        Order("AAPL", Side.BUY, 10),
        _make_portfolio(equity=0.0, positions=_pos("AAPL", 50)),
    )
    assert decision.decision is Decision.REJECT


# --------------------------------------------------------------------------- #
# 价格解析回退链
# --------------------------------------------------------------------------- #


def test_price_falls_back_to_limit_price_when_no_mark():
    """无标记价时用订单限价计价,越界照常缩单。"""
    eng = _engine()
    order = Order(
        "MSFT", Side.BUY, 250, order_type=OrderType.LIMIT, limit_price=PRICE
    )
    # 无 mark、无持仓 → 用 limit_price=100 计价,cap_qty=100
    decision = eng.check(order, _make_portfolio(marks={}))
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(CAP_QTY)


def test_price_falls_back_to_avg_price_when_no_mark_or_limit():
    """无标记价、无限价时回退到持仓均价计价。"""
    eng = _engine()
    # 持 10 股,均价 100,买 250 → 用 avg=100 计价;允许量 = 100 - 10 = 90
    decision = eng.check(
        Order("MSFT", Side.BUY, 250),
        _make_portfolio(marks={}, positions=_pos("MSFT", 10)),
    )
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(90.0)


def test_missing_price_raises_price_unavailable():
    """标记价/限价/均价全无 → resolve_price 抛 PriceUnavailable(宁可显式失败)。"""
    eng = _engine()
    with pytest.raises(PriceUnavailable):
        eng.check(Order("ZZZ", Side.BUY, 10), _make_portfolio(marks={}))


def test_mark_takes_priority_over_limit_price():
    """标记价优先于限价:用 mark 计价而非 limit_price。"""
    eng = _engine()
    # mark=100(cap_qty=100),但 limit=200(若用它 cap_qty=50)。用 mark → 缩到 100。
    order = Order(
        "AAPL", Side.BUY, 250, order_type=OrderType.LIMIT, limit_price=200.0
    )
    decision = eng.check(order, _make_portfolio(marks={"AAPL": PRICE}))
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(CAP_QTY)  # 100,证明用了 mark


# --------------------------------------------------------------------------- #
# 阈值参数化 + 时钟注入 + 无副作用
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cap_pct, expected_qty",
    [
        (0.05, 50.0),
        (0.10, 100.0),
        (0.20, 200.0),
        (0.50, 500.0),
    ],
)
def test_cap_scales_with_configured_max_position_pct(cap_pct, expected_qty):
    """缩单后的上限数量随 max_position_pct 线性变化。"""
    eng = _engine(max_position_pct=cap_pct)
    # 买一个必然越界的大单,验证缩到对应上限
    decision = eng.check(Order("AAPL", Side.BUY, 10_000), _make_portfolio())
    assert decision.decision is Decision.RESIZE
    assert decision.order.quantity == pytest.approx(expected_qty)


def test_decision_timestamp_uses_injected_clock():
    """注入的可变列表时钟决定裁决时间戳(确定性)。"""
    clock, holder = _fixed_clock()
    eng = _engine(clock=clock)
    decision = eng.check(Order("AAPL", Side.BUY, 50), _make_portfolio())
    assert decision.timestamp == holder[0]

    # 推进时钟,新裁决拿到新时间戳,证明时钟被实时读取
    holder[0] = _dt.datetime(2026, 6, 30, tzinfo=_dt.timezone.utc)
    decision2 = eng.check(Order("AAPL", Side.BUY, 50), _make_portfolio())
    assert decision2.timestamp == holder[0]
    assert decision2.timestamp != decision.timestamp


def test_repeated_checks_are_pure_and_deterministic():
    """同一订单+组合重复检查结果稳定,不产生副作用。"""
    eng = _engine()
    portfolio = _make_portfolio()
    order = Order("AAPL", Side.BUY, 250)
    first = eng.check(order, portfolio)
    second = eng.check(order, portfolio)
    assert first.decision is second.decision is Decision.RESIZE
    assert first.order.quantity == second.order.quantity == pytest.approx(CAP_QTY)
    # 输入组合与订单均未被改动
    assert order.quantity == 250
    assert portfolio.position("AAPL").quantity == 0.0
