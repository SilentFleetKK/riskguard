"""``GrossExposureLimit`` 组合层敞口上限规则的单元 / 集成测试。

覆盖点:
- 单标的越顶后缩量(resize)、比例数学正确
- 多持仓时其他标的的 ``other_gross`` 被正确计入额度
- 减仓单永远放行(即便当前已超顶)
- 无剩余额度时拒单(reject)
- 空 / 满 / 精确等于上限的边界
- 多空双向、翻仓(flip)、杠杆倍数、限价单取价回退
- 权益非正拒单、无价可用透传 ``PriceUnavailable``
- 经 :class:`RiskEngine` 端到端聚合后的裁决

所有断言的期望值都对照真实实现逐条核算过。测试不触网、确定性;
需要时间时向 :class:`RiskEngine` 注入可变列表时钟。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from riskguard import (
    Account,
    Decision,
    GrossExposureLimit,
    Order,
    OrderType,
    Portfolio,
    Position,
    PriceUnavailable,
    RiskConfig,
    RiskEngine,
    RuleContext,
    Side,
)

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_portfolio(
    equity: float = 100_000.0,
    positions: dict | None = None,
    marks: dict | None = None,
) -> Portfolio:
    """便捷组装一个组合快照。"""
    return Portfolio(
        account=Account(equity=equity, cash=equity),
        positions=positions or {},
        marks=marks or {},
    )


def eval_rule(
    order: Order,
    portfolio: Portfolio,
    config: RiskConfig | None = None,
):
    """直接对单条 ``GrossExposureLimit`` 规则求值,绕开引擎聚合。"""
    cfg = config if config is not None else RiskConfig(max_gross_exposure_pct=1.0)
    ctx = RuleContext(
        order=order,
        portfolio=portfolio,
        config=cfg,
        state=None,  # 本规则不读取 state
        now=_FIXED_NOW,
    )
    return GrossExposureLimit().evaluate(ctx)


def list_clock(times):
    """返回一个可变列表时钟:每次调用弹出下一个时间,耗尽后固定在最后一个。"""

    seq = list(times)

    def _clock():
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0]

    return _clock


# ===========================================================================
# 单标的越顶缩量 + 比例数学
# ===========================================================================


def test_single_symbol_over_gross_cap_resizes():
    """空组合、买入远超 gross 上限 → 缩到额度内。"""
    # equity=100k, cap=1.0*100k=100k, price=200 -> cap_qty=500
    pf = make_portfolio(marks={"AAPL": 200.0})
    order = Order("AAPL", Side.BUY, 1000)

    result = eval_rule(order, pf)

    assert result.action is Decision.RESIZE
    assert result.passed is False
    assert result.adjusted_quantity == pytest.approx(500.0)


def test_resize_ratio_math_correct():
    """越顶时 ``projected_gross_ratio`` 反映的是原始订单投影后的比例。"""
    # 想买 1000@200=200k,占权益 200% -> ratio 2.0
    pf = make_portfolio(marks={"AAPL": 200.0})
    order = Order("AAPL", Side.BUY, 1000)

    result = eval_rule(order, pf)

    assert result.detail["projected_gross_ratio"] == pytest.approx(2.0)
    assert result.detail["cap"] == pytest.approx(1.0)
    assert result.detail["allowed_quantity"] == pytest.approx(500.0)


def test_rule_name_is_stable():
    """规则名恒为 ``gross_exposure_limit``,聚合与审计依赖它。"""
    pf = make_portfolio(marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 1000), pf)
    assert result.rule == "gross_exposure_limit"


def test_resize_detail_keys_present():
    """resize 结果的 detail 应带齐三个键,供上层可读展示。"""
    pf = make_portfolio(marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 1000), pf)
    assert set(result.detail.keys()) == {
        "projected_gross_ratio",
        "cap",
        "allowed_quantity",
    }


# ===========================================================================
# 多持仓:other_gross 计入额度
# ===========================================================================


def test_multi_position_other_gross_accounted():
    """其他标的已占 90k,新标的只剩 10k 额度 -> 缩到 50 股。"""
    # MSFT 300@300=90k(other_gross),equity=100k,cap=100k -> 剩 10k
    # AAPL price 200 -> symbol_cap_qty = 10000/200 = 50
    pf = make_portfolio(
        positions={"MSFT": Position("MSFT", 300.0, 300.0)},
        marks={"AAPL": 200.0, "MSFT": 300.0},
    )
    order = Order("AAPL", Side.BUY, 1000)

    result = eval_rule(order, pf)

    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(50.0)


def test_multi_position_ratio_includes_full_projection():
    """比例分子是 other_gross + 原始订单投影,而非缩量后的值。"""
    # other_gross=90k, 原单投影 1000*200=200k -> (90k+200k)/100k = 2.9
    pf = make_portfolio(
        positions={"MSFT": Position("MSFT", 300.0, 300.0)},
        marks={"AAPL": 200.0, "MSFT": 300.0},
    )
    result = eval_rule(Order("AAPL", Side.BUY, 1000), pf)
    assert result.detail["projected_gross_ratio"] == pytest.approx(2.9)


def test_short_other_position_counts_as_gross():
    """空头持仓按绝对值计入 gross,同样吃掉额度。"""
    # MSFT 空 -300@300 = 90k gross;剩 10k -> AAPL 50 股
    pf = make_portfolio(
        positions={"MSFT": Position("MSFT", -300.0, 300.0)},
        marks={"AAPL": 200.0, "MSFT": 300.0},
    )
    result = eval_rule(Order("AAPL", Side.BUY, 1000), pf)
    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(50.0)


def test_existing_same_symbol_position_excluded_from_other_gross():
    """同标的已有持仓不算进 other_gross,只算成交后的该标的敞口。"""
    # 已有 AAPL 100@200=20k;再买。other_gross 应为 0(排除本标的)
    # cap 100k -> symbol_cap_qty=500;current=100
    # allowed = 500 - (+1)*100 = 400;order 1000 -> resize 400
    pf = make_portfolio(
        positions={"AAPL": Position("AAPL", 100.0, 200.0)},
        marks={"AAPL": 200.0},
    )
    result = eval_rule(Order("AAPL", Side.BUY, 1000), pf)
    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(400.0)


# ===========================================================================
# 减仓永远放行
# ===========================================================================


def test_reducing_position_approved_even_when_over_cap():
    """当前 gross 已 200% 超顶,卖出减仓仍放行。"""
    # AAPL 1000@200=200k,cap=100k,已超。SELL 100 减仓 -> approve
    pf = make_portfolio(
        positions={"AAPL": Position("AAPL", 1000.0, 200.0)},
        marks={"AAPL": 200.0},
    )
    order = Order("AAPL", Side.SELL, 100)

    result = eval_rule(order, pf)

    assert result.action is Decision.APPROVE
    assert result.passed is True
    assert result.adjusted_quantity is None


def test_reducing_short_position_approved():
    """空头减仓(买回)在超顶时同样放行。"""
    pf = make_portfolio(
        positions={"AAPL": Position("AAPL", -1000.0, 200.0)},
        marks={"AAPL": 200.0},
    )
    order = Order("AAPL", Side.BUY, 100)  # 买回,缩小空头
    result = eval_rule(order, pf)
    assert result.action is Decision.APPROVE
    assert result.passed is True


def test_flat_closing_order_approved():
    """恰好平仓(减到 0)是减仓,放行。"""
    pf = make_portfolio(
        positions={"AAPL": Position("AAPL", 1000.0, 200.0)},
        marks={"AAPL": 200.0},
    )
    result = eval_rule(Order("AAPL", Side.SELL, 1000), pf)
    assert result.action is Decision.APPROVE


# ===========================================================================
# 无额度 -> 拒单
# ===========================================================================


def test_reject_when_no_room_left():
    """其他标的已占满 100% gross,新增任何加仓 -> 拒单。"""
    # MSFT 500@200=100k = 满 cap;other_gross=100k,symbol_cap_notional=0 -> allowed 0
    pf = make_portfolio(
        positions={"MSFT": Position("MSFT", 500.0, 200.0)},
        marks={"AAPL": 200.0, "MSFT": 200.0},
    )
    order = Order("AAPL", Side.BUY, 10)

    result = eval_rule(order, pf)

    assert result.action is Decision.REJECT
    assert result.passed is False
    assert result.adjusted_quantity is None
    assert result.detail["allowed_quantity"] == pytest.approx(0.0)


def test_reject_when_other_gross_exceeds_cap():
    """其他标的已超 cap,新标的额度为负被夹到 0 -> 拒单。"""
    # MSFT 700@200=140k > cap 100k;symbol_cap_notional=max(0,100k-140k)=0
    pf = make_portfolio(
        positions={"MSFT": Position("MSFT", 700.0, 200.0)},
        marks={"AAPL": 200.0, "MSFT": 200.0},
    )
    result = eval_rule(Order("AAPL", Side.BUY, 10), pf)
    assert result.action is Decision.REJECT


# ===========================================================================
# 上限边界(within 容差)
# ===========================================================================


def test_exactly_at_cap_approved():
    """成交后 gross 恰好等于上限 -> 放行(含浮点容差)。"""
    # 买 500@200=100k == cap 100k
    pf = make_portfolio(marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 500), pf)
    assert result.action is Decision.APPROVE


def test_just_below_cap_approved():
    """略低于上限 -> 放行。"""
    pf = make_portfolio(marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 499), pf)
    assert result.action is Decision.APPROVE


def test_just_above_cap_resizes():
    """略高于上限 -> 缩到上限对应数量。"""
    pf = make_portfolio(marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 501), pf)
    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(500.0)


def test_within_cap_approve_reports_ratio():
    """放行时 detail 也带 ``projected_gross_ratio``。"""
    pf = make_portfolio(marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 250), pf)  # 50k -> 50%
    assert result.action is Decision.APPROVE
    assert result.detail["projected_gross_ratio"] == pytest.approx(0.5)


# ===========================================================================
# 方向 / 翻仓 / 杠杆 / 取价
# ===========================================================================


def test_short_side_over_cap_resizes():
    """卖空越顶同样缩量(gross 按绝对值算)。"""
    pf = make_portfolio(marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.SELL, 1000), pf)
    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(500.0)


def test_flip_long_to_short_within_cap_approved():
    """多翻空但成交后 |敞口| 仍在额度内 -> 放行。"""
    # long 100@200,other_gross=0,cap 100k,symbol_cap_qty=500
    # sell 300 -> projected -200,|200|<=500 -> approve
    pf = make_portfolio(
        positions={"AAPL": Position("AAPL", 100.0, 200.0)},
        marks={"AAPL": 200.0},
    )
    result = eval_rule(Order("AAPL", Side.SELL, 300), pf)
    assert result.action is Decision.APPROVE


def test_flip_long_to_short_over_cap_resizes():
    """多翻空且越顶 -> 按翻仓额度公式缩量。"""
    # AAPL long 100@200,MSFT 400@200=80k other_gross
    # symbol_cap_notional=100k-80k=20k,symbol_cap_qty=100
    # sell 800 -> projected -700 增大;allowed = 100 - (-1)*100 = 200 -> resize 200
    pf = make_portfolio(
        positions={
            "AAPL": Position("AAPL", 100.0, 200.0),
            "MSFT": Position("MSFT", 400.0, 200.0),
        },
        marks={"AAPL": 200.0, "MSFT": 200.0},
    )
    result = eval_rule(Order("AAPL", Side.SELL, 800), pf)
    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(200.0)


def test_leverage_config_raises_cap():
    """``max_gross_exposure_pct=2.0`` 时允许 2x 敞口 -> 原本越顶的单被放行。"""
    cfg = RiskConfig(max_gross_exposure_pct=2.0)
    pf = make_portfolio(marks={"AAPL": 200.0})
    # 买 1000@200=200k = 200% = cap -> approve
    result = eval_rule(Order("AAPL", Side.BUY, 1000), pf, cfg)
    assert result.action is Decision.APPROVE


def test_tighter_cap_resizes_more_aggressively():
    """收紧 cap 到 0.5 -> 额度减半。"""
    cfg = RiskConfig(max_gross_exposure_pct=0.5)
    pf = make_portfolio(marks={"AAPL": 200.0})
    # cap=50k,price 200 -> cap_qty=250
    result = eval_rule(Order("AAPL", Side.BUY, 1000), pf, cfg)
    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(250.0)


def test_net_exposure_config_ignored_by_gross_rule():
    """本规则只看 gross;即使 net 上限极小也不影响 gross 裁决。"""
    cfg = RiskConfig(max_gross_exposure_pct=1.0, max_net_exposure_pct=0.01)
    pf = make_portfolio(marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 400), pf, cfg)  # 80k gross -> ok
    assert result.action is Decision.APPROVE


def test_limit_price_used_when_no_mark():
    """无 mark 时按限价单的 ``limit_price`` 取价。"""
    pf = make_portfolio(marks={})  # 无标记价
    order = Order(
        "AAPL", Side.BUY, 1000, order_type=OrderType.LIMIT, limit_price=200.0
    )
    result = eval_rule(order, pf)
    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(500.0)


def test_avg_price_used_when_no_mark_no_limit():
    """无 mark、市价单时回退到持仓均价取价。"""
    # 已有 AAPL 100@200,均价 200 作计价;买入加仓
    pf = make_portfolio(
        positions={"AAPL": Position("AAPL", 100.0, 200.0)},
        marks={},
    )
    result = eval_rule(Order("AAPL", Side.BUY, 1000), pf)
    # other_gross=0,cap_qty=500,current=100 -> allowed=400
    assert result.action is Decision.RESIZE
    assert result.adjusted_quantity == pytest.approx(400.0)


def test_mark_takes_priority_over_limit_price():
    """mark 存在时优先于 limit_price 计价。"""
    pf = make_portfolio(marks={"AAPL": 200.0})
    # mark=200 -> cap_qty=500;若误用 limit=100 则会是 1000
    order = Order(
        "AAPL", Side.BUY, 1000, order_type=OrderType.LIMIT, limit_price=100.0
    )
    result = eval_rule(order, pf)
    assert result.adjusted_quantity == pytest.approx(500.0)


# ===========================================================================
# 异常与非正权益
# ===========================================================================


def test_non_positive_equity_rejected():
    """权益为 0 -> 拒单(无法计价占比)。"""
    pf = make_portfolio(equity=0.0, marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 1), pf)
    assert result.action is Decision.REJECT
    assert result.detail["equity"] == pytest.approx(0.0)


def test_negative_equity_rejected():
    """穿仓(权益为负)-> 拒单。"""
    pf = make_portfolio(equity=-500.0, marks={"AAPL": 200.0})
    result = eval_rule(Order("AAPL", Side.BUY, 1), pf)
    assert result.action is Decision.REJECT


def test_price_unavailable_propagates():
    """无 mark / 无限价 / 无持仓均价 -> 透传 ``PriceUnavailable``。"""
    pf = make_portfolio(marks={})  # 全空
    with pytest.raises(PriceUnavailable):
        eval_rule(Order("AAPL", Side.BUY, 10), pf)


# ===========================================================================
# 经 RiskEngine 端到端(只装本规则,隔离其他规则干扰)
# ===========================================================================


def _engine_only_gross(cfg: RiskConfig | None = None) -> RiskEngine:
    cfg = cfg if cfg is not None else RiskConfig(max_gross_exposure_pct=1.0)
    clock = list_clock([_FIXED_NOW])
    return RiskEngine(cfg, rules=[GrossExposureLimit()], clock=clock)


def test_engine_resizes_final_order():
    """引擎聚合后:越顶订单被缩量,原单保留在 ``original_order``。"""
    engine = _engine_only_gross()
    pf = make_portfolio(marks={"AAPL": 200.0})
    decision = engine.check(Order("AAPL", Side.BUY, 1000), pf)

    assert decision.decision is Decision.RESIZE
    assert decision.resized is True
    assert decision.approved is True
    assert decision.order.quantity == pytest.approx(500.0)
    assert decision.original_order.quantity == pytest.approx(1000.0)


def test_engine_rejects_when_no_room():
    """引擎聚合后:无额度 -> 整体拒单。"""
    engine = _engine_only_gross()
    pf = make_portfolio(
        positions={"MSFT": Position("MSFT", 500.0, 200.0)},
        marks={"AAPL": 200.0, "MSFT": 200.0},
    )
    decision = engine.check(Order("AAPL", Side.BUY, 100), pf)

    assert decision.decision is Decision.REJECT
    assert decision.rejected is True
    assert decision.approved is False
    assert len(decision.rejections()) == 1
    assert decision.rejections()[0].rule == "gross_exposure_limit"


def test_engine_approves_reducing_order():
    """引擎聚合后:减仓单放行且不改数量。"""
    engine = _engine_only_gross()
    pf = make_portfolio(
        positions={"AAPL": Position("AAPL", 1000.0, 200.0)},
        marks={"AAPL": 200.0},
    )
    decision = engine.check(Order("AAPL", Side.SELL, 100), pf)

    assert decision.decision is Decision.APPROVE
    assert decision.order.quantity == pytest.approx(100.0)


def test_engine_timestamp_from_injected_clock():
    """引擎裁决时间戳取自注入的时钟,确定可断言。"""
    stamp = datetime(2026, 3, 15, 9, 30, tzinfo=timezone.utc)
    engine = RiskEngine(
        RiskConfig(max_gross_exposure_pct=1.0),
        rules=[GrossExposureLimit()],
        clock=list_clock([stamp]),
    )
    pf = make_portfolio(marks={"AAPL": 200.0})
    decision = engine.check(Order("AAPL", Side.BUY, 100), pf)
    assert decision.timestamp == stamp


def test_engine_advancing_clock_is_deterministic():
    """可变列表时钟按序推进,后一次检查用后一个时间。"""
    t0 = datetime(2026, 4, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)
    engine = RiskEngine(
        RiskConfig(max_gross_exposure_pct=1.0),
        rules=[GrossExposureLimit()],
        clock=list_clock([t0, t1]),
    )
    pf = make_portfolio(marks={"AAPL": 200.0})
    d0 = engine.check(Order("AAPL", Side.BUY, 100), pf)
    d1 = engine.check(Order("AAPL", Side.BUY, 100), pf)
    assert d0.timestamp == t0
    assert d1.timestamp == t1


def test_engine_original_order_immutable_on_resize():
    """缩量返回新订单,原始订单对象不被原地修改(不可变契约)。"""
    engine = _engine_only_gross()
    pf = make_portfolio(marks={"AAPL": 200.0})
    original = Order("AAPL", Side.BUY, 1000)
    decision = engine.check(original, pf)

    assert decision.order is not original
    assert original.quantity == pytest.approx(1000.0)  # 原对象未变
    assert decision.original_order is original
