"""riskguard.backtest 回测接线模块测试。"""

from __future__ import annotations

import pytest

from riskguard import Account, Order, PaperBroker, Portfolio, Position, RiskConfig, Side
from riskguard.backtest import (
    ReplayResult,
    RiskOverlay,
    compare,
    from_signals_with_risk,
    kelly_weights,
    make_riskguard_strategy,
    replay,
    risk_capped_weights,
)


# --------------------------------------------------------------------------- #
# RiskOverlay
# --------------------------------------------------------------------------- #
def test_overlay_caps_oversized_target_weight():
    broker = PaperBroker(100_000, marks={"ASSET": 100.0})
    ov = RiskOverlay(config=RiskConfig(max_position_pct=0.10), symbol="ASSET")
    res = ov.target_weight_to_order(2.0, 100.0, broker.get_portfolio())  # 想要 200%
    assert res.order is not None
    assert res.order.quantity <= 100.0 + 1e-6  # 结果 <= 10% 权益(cap_qty=100)
    assert ov.stats["resized"] >= 1


def test_approved_target_weight_returns_capped_weight():
    broker = PaperBroker(100_000, marks={"ASSET": 100.0})
    ov = RiskOverlay(config=RiskConfig(max_position_pct=0.10), symbol="ASSET")
    assert abs(ov.approved_target_weight(2.0, 100.0, broker.get_portfolio()) - 0.10) < 1e-9


def test_overlay_no_action_when_already_at_target():
    broker = PaperBroker(100_000, marks={"ASSET": 100.0})
    broker.submit_order(Order("ASSET", Side.BUY, 50))
    ov = RiskOverlay(config=RiskConfig(max_position_pct=1.0), symbol="ASSET")
    # 当前 50 股 = 5% ;目标 5% -> 无动作
    res = ov.target_weight_to_order(0.05, 100.0, broker.get_portfolio())
    assert res.order is None


def test_overlay_breaker_halts_increase_allows_reduce():
    ov = RiskOverlay(
        config=RiskConfig(max_drawdown_pct=0.15, max_position_pct=1.0), symbol="ASSET"
    )
    pos = {"ASSET": Position("ASSET", 500, 100.0)}
    ov.observe(Portfolio(Account(equity=100_000), pos, {"ASSET": 100.0}))  # hwm 100k
    pf_low = Portfolio(Account(equity=80_000), pos, {"ASSET": 80.0})
    ov.observe(pf_low)  # -20% -> 熔断
    assert ov.engine.breaker_tripped

    # 想加仓 -> 被熔断拦下
    inc = ov.target_weight_to_order(1.0, 80.0, pf_low)
    assert inc.halted and inc.order is None
    # 想减仓 -> 熔断中也放行
    red = ov.target_weight_to_order(0.2, 80.0, pf_low)
    assert red.order is not None
    assert ov.stats["breaker_trips"] == 1
    assert ov.stats["halted_bars"] >= 1


# --------------------------------------------------------------------------- #
# replay / compare
# --------------------------------------------------------------------------- #
def test_replay_reduces_drawdown_on_crash():
    prices = [100, 96, 90, 82, 75, 70, 64, 60, 57, 55]
    result = compare(
        prices,
        lambda i, p, ps: 1.0,  # 永远想满仓做多
        config=RiskConfig(max_position_pct=0.10, max_drawdown_pct=0.15),
        cash=100_000,
    )
    assert result["guarded"].max_drawdown < result["naive"].max_drawdown
    assert result["guarded"].final_equity > result["naive"].final_equity


def test_replay_result_fields_and_flat_strategy():
    r = replay([100, 101, 102], lambda i, p, ps: 0.0, cash=50_000)
    assert isinstance(r, ReplayResult)
    assert len(r.equity_curve) == 4  # 起始资本 + 3 个 bar
    assert r.equity_curve[0] == 50_000  # 第 0 点即起始资本
    assert r.final_equity == 50_000  # 一直空仓,权益不变
    assert r.trades == 0
    assert r.total_return == 0.0


def test_replay_empty_prices_raises():
    with pytest.raises(ValueError):
        replay([], lambda i, p, ps: 0.0)


def test_replay_survives_bad_tick_symmetric():
    # 坏 tick(价格 0)不能让任一路径崩溃,compare 也不能抛
    buyhold = lambda i, p, ps: 1.0  # noqa: E731
    r = compare([100.0, 0.0, 120.0], buyhold, cash=100_000)
    assert r["guarded"].final_equity > 0
    assert r["naive"].final_equity > 0


def test_max_drawdown_matches_returned_curve():
    # 回撤必须能从返回的权益曲线本身复算出来(基线一致,含起始资本点)
    r = replay(
        [100, 101, 102, 103],
        lambda i, p, ps: 1.0,
        cash=100_000,
        slippage_bps=100,
        commission_bps=20,
        risk_managed=False,
    )
    peak = r.equity_curve[0]
    dd = 0.0
    for e in r.equity_curve:
        peak = max(peak, e)
        dd = max(dd, 1.0 - e / peak)
    assert abs(dd - r.max_drawdown) < 1e-9
    assert r.equity_curve[0] == 100_000  # 曲线从起始资本起


def test_overlay_result_carries_approved_weight():
    broker = PaperBroker(100_000, marks={"ASSET": 100.0})
    ov = RiskOverlay(config=RiskConfig(max_position_pct=0.10), symbol="ASSET")
    res = ov.target_weight_to_order(2.0, 100.0, broker.get_portfolio())
    assert abs(res.approved_weight - 0.10) < 1e-9  # 200% 目标被缩到 10%


# --------------------------------------------------------------------------- #
# vectorbt 纯辅助
# --------------------------------------------------------------------------- #
def test_risk_capped_weights_preserves_sign():
    out = risk_capped_weights([1.0, -0.8, 0.05, 2.0], RiskConfig(max_position_pct=0.10))
    assert out == [0.10, -0.10, 0.05, 0.10]


def test_kelly_weights_formula_and_cap():
    cfg = RiskConfig(kelly_fraction=0.5, max_position_pct=0.10)
    out = kelly_weights([0.6, 0.3], [2.0, 1.0], cfg)
    assert out[0] == 0.10  # 半Kelly 0.2 封顶到 0.10
    assert out[1] == 0.0   # 无正期望

    uncapped = kelly_weights([0.6], [2.0], RiskConfig(kelly_fraction=0.5, max_position_pct=1.0))
    assert abs(uncapped[0] - 0.2) < 1e-9


# --------------------------------------------------------------------------- #
# 可选依赖适配器:未安装时报清晰的 ImportError
# --------------------------------------------------------------------------- #
def test_make_riskguard_strategy_requires_backtesting():
    try:
        import backtesting  # noqa: F401
        pytest.skip("backtesting.py 已安装")
    except ImportError:
        pass
    with pytest.raises(ImportError):
        make_riskguard_strategy()


def test_from_signals_with_risk_requires_vectorbt():
    try:
        import vectorbt  # noqa: F401
        pytest.skip("vectorbt 已安装")
    except ImportError:
        pass
    with pytest.raises(ImportError):
        from_signals_with_risk([1, 2, 3], [True, False, True], [False, True, False])
