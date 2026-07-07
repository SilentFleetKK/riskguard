"""仓位算法(sizing)测试。

覆盖三种 sizer 的 ``target_weight`` 公式、缺参/越界报错,以及基类
:meth:`PositionSizer.size` 的通用换算逻辑(名义 = 权益 × 权重、数量 = 名义 / 价格、
权重夹到 ``max_sizing_leverage``、订单 ``meta`` 携带 ``target_weight`` 等)。

一切走公开 API :mod:`riskguard`;不联网、纯确定性计算。
"""

from __future__ import annotations

import math

import pytest

from riskguard import (
    Account,
    ConfigError,
    FixedFractionalSizer,
    KellySizer,
    OrderType,
    Portfolio,
    PositionSizer,
    RiskConfig,
    Side,
    Signal,
    VolatilityTargetSizer,
)


# --------------------------------------------------------------------------- #
# 通用夹具                                                                     #
# --------------------------------------------------------------------------- #
EQUITY = 100_000.0
PRICE = 100.0


def make_portfolio(equity: float = EQUITY, mark: float = PRICE) -> Portfolio:
    """构造一个仅含现金权益、给定标记价的组合快照。"""
    return Portfolio(account=Account(equity=equity), marks={"AAPL": mark})


def make_config(**changes: object) -> RiskConfig:
    """在默认配置基础上覆写若干字段。"""
    return RiskConfig(**changes)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# KellySizer.target_weight                                                     #
# --------------------------------------------------------------------------- #
def test_kelly_canonical_example():
    """p=0.6, b=2, frac=0.5 -> f*=(2*0.6-0.4)/2=0.4,半 Kelly = 0.2。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.6, payoff_ratio=2.0)
    cfg = make_config(kelly_fraction=0.5)
    w = KellySizer().target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(0.2)


def test_kelly_full_fraction_equals_f_star():
    """kelly_fraction=1.0 时目标权重就是满 Kelly f*。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.6, payoff_ratio=2.0)
    cfg = make_config(kelly_fraction=1.0)
    w = KellySizer().target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(0.4)


def test_kelly_fraction_scales_linearly():
    """目标权重与 kelly_fraction 成正比。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.7, payoff_ratio=3.0)
    pf = make_portfolio()
    w_half = KellySizer().target_weight(sig, pf, make_config(kelly_fraction=0.5))
    w_quarter = KellySizer().target_weight(sig, pf, make_config(kelly_fraction=0.25))
    assert w_half == pytest.approx(2.0 * w_quarter)


def test_kelly_p_minus_q_over_b_identity():
    """公式 (b*p - q)/b 应等价于 p - q/b。"""
    p, b, frac = 0.55, 1.8, 0.5
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=p, payoff_ratio=b)
    cfg = make_config(kelly_fraction=frac)
    w = KellySizer().target_weight(sig, make_portfolio(), cfg)
    expected = frac * (p - (1.0 - p) / b)
    assert w == pytest.approx(expected)


def test_kelly_no_edge_zero_expectation_returns_zero():
    """p=0.4, b=1 -> f*=p-q/b=-0.2 <= 0,不下注,权重为 0。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.4, payoff_ratio=1.0)
    w = KellySizer().target_weight(sig, make_portfolio(), make_config())
    assert w == 0.0


def test_kelly_exactly_zero_edge_returns_zero():
    """f* 恰好为 0(p=0.5, b=1)也应返回 0,而非一个极小正数。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.5, payoff_ratio=1.0)
    w = KellySizer().target_weight(sig, make_portfolio(), make_config())
    assert w == 0.0


def test_kelly_negative_edge_returns_zero():
    """更极端的负期望同样返回 0(f* 不为负)。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.1, payoff_ratio=0.5)
    w = KellySizer().target_weight(sig, make_portfolio(), make_config())
    assert w == 0.0


def test_kelly_certain_win_positive_weight():
    """p=1.0(必胜)给出正权重,等于 frac * 1(= p 时 q=0,f*=p=1)。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=1.0, payoff_ratio=2.0)
    cfg = make_config(kelly_fraction=0.5)
    w = KellySizer().target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(0.5)


def test_kelly_missing_win_probability_raises():
    """缺 win_probability 应抛 ConfigError。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, payoff_ratio=2.0)
    with pytest.raises(ConfigError):
        KellySizer().target_weight(sig, make_portfolio(), make_config())


def test_kelly_missing_payoff_ratio_raises():
    """缺 payoff_ratio 应抛 ConfigError。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.6)
    with pytest.raises(ConfigError):
        KellySizer().target_weight(sig, make_portfolio(), make_config())


def test_kelly_missing_both_raises():
    """两个字段都缺也应抛 ConfigError。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    with pytest.raises(ConfigError):
        KellySizer().target_weight(sig, make_portfolio(), make_config())


@pytest.mark.parametrize("bad_p", [-0.01, 1.01, 1.5, -1.0])
def test_kelly_probability_out_of_range_raises(bad_p):
    """win_probability 越出 [0, 1] 抛 ConfigError。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=bad_p, payoff_ratio=2.0)
    with pytest.raises(ConfigError):
        KellySizer().target_weight(sig, make_portfolio(), make_config())


@pytest.mark.parametrize("good_p", [0.0, 1.0])
def test_kelly_probability_boundaries_allowed(good_p):
    """p=0 与 p=1 是合法边界,不应报错。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=good_p, payoff_ratio=2.0)
    # 不抛异常即可(p=0 无期望 -> 0;p=1 必胜 -> 正)
    w = KellySizer().target_weight(sig, make_portfolio(), make_config())
    assert w >= 0.0


@pytest.mark.parametrize("bad_b", [0.0, -0.5, -2.0])
def test_kelly_nonpositive_payoff_ratio_raises(bad_b):
    """payoff_ratio 必须 > 0,否则抛 ConfigError。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.6, payoff_ratio=bad_b)
    with pytest.raises(ConfigError):
        KellySizer().target_weight(sig, make_portfolio(), make_config())


def test_kelly_name_attribute():
    """sizer.name 用于订单 meta,契约值应为 'kelly'。"""
    assert KellySizer().name == "kelly"


# --------------------------------------------------------------------------- #
# VolatilityTargetSizer.target_weight                                          #
# --------------------------------------------------------------------------- #
def test_vol_target_ratio():
    """权重 = vol_target_annual / vol;0.15 / 0.30 = 0.5。"""
    sig = Signal("AAPL", Side.SELL, price=PRICE, volatility=0.30)
    cfg = make_config(vol_target_annual=0.15)
    w = VolatilityTargetSizer().target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(0.5)


def test_vol_target_equal_vol_gives_one():
    """波动率恰等于目标时,权重为 1。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, volatility=0.15)
    cfg = make_config(vol_target_annual=0.15)
    w = VolatilityTargetSizer().target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(1.0)


def test_vol_target_low_vol_gives_weight_above_one():
    """低波动标的给出 > 1 的原始权重(target_weight 本身不夹上限)。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, volatility=0.05)
    cfg = make_config(vol_target_annual=0.15)
    w = VolatilityTargetSizer().target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(3.0)
    assert w > 1.0  # 未在 target_weight 层被夹


def test_vol_target_respects_config_target():
    """更高的目标波动率放大权重。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, volatility=0.20)
    w_low = VolatilityTargetSizer().target_weight(
        sig, make_portfolio(), make_config(vol_target_annual=0.10)
    )
    w_high = VolatilityTargetSizer().target_weight(
        sig, make_portfolio(), make_config(vol_target_annual=0.20)
    )
    assert w_low == pytest.approx(0.5)
    assert w_high == pytest.approx(1.0)


def test_vol_target_zero_vol_uses_min_floor_not_divzero():
    """vol=0 合法(>= 0),内部用极小地板防止除零,给出一个很大的有限权重。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, volatility=0.0)
    w = VolatilityTargetSizer().target_weight(sig, make_portfolio(), make_config())
    assert math.isfinite(w)
    assert w > 0.0


def test_vol_target_missing_volatility_raises():
    """缺 volatility 抛 ConfigError。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    with pytest.raises(ConfigError):
        VolatilityTargetSizer().target_weight(sig, make_portfolio(), make_config())


@pytest.mark.parametrize("bad_vol", [-0.01, -0.5, -1.0])
def test_vol_target_negative_volatility_raises(bad_vol):
    """负波动率无意义,抛 ConfigError。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, volatility=bad_vol)
    with pytest.raises(ConfigError):
        VolatilityTargetSizer().target_weight(sig, make_portfolio(), make_config())


def test_vol_target_size_clamps_to_max_sizing_leverage():
    """低波动 -> 原始权重 3.0,但 size() 会夹到 max_sizing_leverage=1.0。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, volatility=0.05)
    cfg = make_config(vol_target_annual=0.15, max_sizing_leverage=1.0)
    order = VolatilityTargetSizer().size(sig, make_portfolio(), cfg)
    assert order.meta["target_weight"] == pytest.approx(1.0)
    # 名义被夹在 权益 * 1.0,数量 = 100000 / 100 = 1000
    assert order.quantity == pytest.approx(1000.0)


def test_vol_target_size_clamps_to_custom_leverage():
    """自定义 max_sizing_leverage=2.0 时,权重 3.0 被夹到 2.0。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, volatility=0.05)
    cfg = make_config(vol_target_annual=0.15, max_sizing_leverage=2.0)
    order = VolatilityTargetSizer().size(sig, make_portfolio(), cfg)
    assert order.meta["target_weight"] == pytest.approx(2.0)


def test_vol_target_name_attribute():
    assert VolatilityTargetSizer().name == "volatility_target"


# --------------------------------------------------------------------------- #
# FixedFractionalSizer.target_weight                                           #
# --------------------------------------------------------------------------- #
def test_fixed_default_uses_max_position_pct():
    """不传 fraction 时,默认权重 = config.max_position_pct。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    cfg = make_config(max_position_pct=0.10)
    w = FixedFractionalSizer().target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(0.10)


def test_fixed_default_follows_config_change():
    """默认模式跟随配置里的 max_position_pct 变化。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    cfg = make_config(max_position_pct=0.05)
    w = FixedFractionalSizer().target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(0.05)


def test_fixed_explicit_fraction_overrides_config():
    """显式 fraction 覆盖 config.max_position_pct。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    cfg = make_config(max_position_pct=0.10)
    w = FixedFractionalSizer(fraction=0.25).target_weight(sig, make_portfolio(), cfg)
    assert w == pytest.approx(0.25)


@pytest.mark.parametrize("frac", [0.01, 0.5, 1.0])
def test_fixed_valid_fraction_accepted(frac):
    """(0, 1] 内的 fraction 合法。"""
    sizer = FixedFractionalSizer(fraction=frac)
    assert sizer.fraction == frac


@pytest.mark.parametrize("bad_frac", [0.0, -0.1, 1.01, 2.0])
def test_fixed_invalid_fraction_raises_valueerror(bad_frac):
    """越界 fraction 在构造时抛 ValueError(注意:这是 ValueError 而非 ConfigError)。"""
    with pytest.raises(ValueError):
        FixedFractionalSizer(fraction=bad_frac)


def test_fixed_name_attribute():
    assert FixedFractionalSizer().name == "fixed_fractional"


# --------------------------------------------------------------------------- #
# PositionSizer.size(): 通用换算                                               #
# --------------------------------------------------------------------------- #
def test_size_quantity_is_notional_over_price():
    """数量 = (权重 * 权益) / 价格。fixed 0.10 * 100k / 100 = 100 股。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    cfg = make_config(max_position_pct=0.10)
    order = FixedFractionalSizer().size(sig, make_portfolio(), cfg)
    assert order.quantity == pytest.approx(100.0)


def test_size_meta_carries_target_weight_and_fields():
    """订单 meta 应携带 sizer/target_weight/target_notional/signal_price。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    cfg = make_config(max_position_pct=0.10)
    order = FixedFractionalSizer().size(sig, make_portfolio(), cfg)
    assert order.meta["sizer"] == "fixed_fractional"
    assert order.meta["target_weight"] == pytest.approx(0.10)
    assert order.meta["target_notional"] == pytest.approx(10_000.0)
    assert order.meta["signal_price"] == pytest.approx(PRICE)


def test_size_kelly_meta_target_weight_matches_target_weight():
    """Kelly size() 的 meta.target_weight 应等于 target_weight 的返回值。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.6, payoff_ratio=2.0)
    cfg = make_config(kelly_fraction=0.5)
    pf = make_portfolio()
    tw = KellySizer().target_weight(sig, pf, cfg)
    order = KellySizer().size(sig, pf, cfg)
    assert order.meta["target_weight"] == pytest.approx(tw)
    # 20% * 100k / 100 = 200 股
    assert order.quantity == pytest.approx(200.0)


def test_size_weight_clamped_to_max_sizing_leverage():
    """target_weight 超过 max_sizing_leverage 时,size() 夹到上限。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    # fixed fraction 0.8,但 max_sizing_leverage 0.5 -> 夹到 0.5
    cfg = make_config(max_sizing_leverage=0.5)
    order = FixedFractionalSizer(fraction=0.8).size(sig, make_portfolio(), cfg)
    assert order.meta["target_weight"] == pytest.approx(0.5)
    assert order.quantity == pytest.approx(500.0)  # 0.5 * 100k / 100


def test_size_preserves_side_symbol_and_strategy():
    """size() 应保留信号的方向/标的/策略,并产出 MARKET 单。"""
    sig = Signal("AAPL", Side.SELL, price=PRICE, strategy_id="momentum")
    cfg = make_config(max_position_pct=0.10)
    order = FixedFractionalSizer().size(sig, make_portfolio(), cfg)
    assert order.side is Side.SELL
    assert order.symbol == "AAPL"
    assert order.strategy_id == "momentum"
    assert order.order_type is OrderType.MARKET


def test_size_zero_weight_returns_none_no_trade():
    """无正期望 Kelly -> 权重 0 -> size() 返回 None(明确不下注,不伪造幻影单)。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE, win_probability=0.4, payoff_ratio=1.0)
    order = KellySizer().size(sig, make_portfolio(), make_config())
    assert order is None


def test_size_zero_equity_returns_none():
    """权益为 0 时名义为 0 -> size() 返回 None,而非崩溃或哨兵微单。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    pf = make_portfolio(equity=0.0)
    order = FixedFractionalSizer().size(sig, pf, make_config())
    assert order is None


def test_size_meta_is_read_only():
    """Order.meta 被冻结成只读映射,尝试写入应报错(不可变契约)。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    order = FixedFractionalSizer().size(sig, make_portfolio(), make_config())
    with pytest.raises(TypeError):
        order.meta["target_weight"] = 999.0  # type: ignore[index]


def test_size_notional_scales_with_equity():
    """相同权重下,名义与数量随权益线性放大。"""
    sig = Signal("AAPL", Side.BUY, price=PRICE)
    cfg = make_config(max_position_pct=0.10)
    small = FixedFractionalSizer().size(sig, make_portfolio(equity=100_000.0), cfg)
    big = FixedFractionalSizer().size(sig, make_portfolio(equity=200_000.0), cfg)
    assert big.quantity == pytest.approx(2.0 * small.quantity)
    assert big.meta["target_notional"] == pytest.approx(
        2.0 * small.meta["target_notional"]
    )


def test_size_higher_price_fewer_shares():
    """相同名义,价格越高数量越少(数量 = 名义 / 价格)。"""
    cfg = make_config(max_position_pct=0.10)
    cheap = Signal("AAPL", Side.BUY, price=50.0)
    dear = Signal("AAPL", Side.BUY, price=200.0)
    pf = make_portfolio()
    q_cheap = FixedFractionalSizer().size(cheap, pf, cfg).quantity
    q_dear = FixedFractionalSizer().size(dear, pf, cfg).quantity
    # 名义相同(10k),50 元 -> 200 股;200 元 -> 50 股
    assert q_cheap == pytest.approx(200.0)
    assert q_dear == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# 类型契约                                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sizer",
    [KellySizer(), VolatilityTargetSizer(), FixedFractionalSizer()],
)
def test_all_sizers_are_position_sizers(sizer):
    """三个 sizer 都应是 PositionSizer 的实例(可被引擎统一调度)。"""
    assert isinstance(sizer, PositionSizer)
