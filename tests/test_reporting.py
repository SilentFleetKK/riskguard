"""riskguard.reporting 测试:每日体检(digest)+ 压力测试(stress)。

压力测试模块的头号承诺是"绝对只读、零副作用"——这是这组测试里最重要的部分,
每个场景都要顺带验证引擎的真实状态没有被悄悄改动过。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from riskguard import Account, Order, PaperBroker, Portfolio, Position, RiskConfig, RiskEngine, Side
from riskguard.reporting import (
    DigestReport,
    PositionBreach,
    PositionStanding,
    QuarantineStanding,
    StressResult,
    build_digest,
    render_digest_text,
    render_stress_text,
    run_stress_test,
)


def _now():
    return datetime.now(timezone.utc)


def _make_engine(**cfg_kwargs) -> RiskEngine:
    return RiskEngine(RiskConfig(**cfg_kwargs))


def _flat_portfolio(equity: float) -> Portfolio:
    return Portfolio(Account(equity=equity, cash=equity))


# --------------------------------------------------------------------------- #
# stress.py:参数校验
# --------------------------------------------------------------------------- #
def test_stress_rejects_non_finite_shock():
    engine = _make_engine()
    with pytest.raises(ValueError):
        run_stress_test(engine, _flat_portfolio(100_000), float("nan"))
    with pytest.raises(ValueError):
        run_stress_test(engine, _flat_portfolio(100_000), float("inf"))


def test_stress_rejects_shock_at_or_below_negative_100_pct():
    engine = _make_engine()
    with pytest.raises(ValueError):
        run_stress_test(engine, _flat_portfolio(100_000), -1.0)
    with pytest.raises(ValueError):
        run_stress_test(engine, _flat_portfolio(100_000), -1.5)


def test_stress_zero_shock_leaves_equity_unchanged():
    engine = _make_engine()
    pf = Portfolio(
        Account(equity=100_000, cash=50_000),
        {"AAPL": Position("AAPL", 250, 200.0)},
        {"AAPL": 200.0},
    )
    result = run_stress_test(engine, pf, 0.0)
    assert result.shocked_equity == pytest.approx(result.current_equity)
    assert result.equity_change_pct == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# stress.py:核心正确性——用市值变动量做加法,而不是从零重算
# --------------------------------------------------------------------------- #
def test_stress_uses_delta_not_recompute_from_scratch():
    """即便调用方传入的 Account.equity 和 cash+持仓市值本就不一致(现实很常见,
    比如权益来自真实券商回执),冲击后权益也该是"当前权益 + 市值变动量",
    而不是拿 cash+持仓从零算出一个和 current_equity 不可比的数字。"""
    engine = _make_engine()
    # 故意让 equity(100000)和 cash+持仓市值(25000+50000=75000)不一致
    pf = Portfolio(
        Account(equity=100_000, cash=25_000),
        {"AAPL": Position("AAPL", 250, 200.0)},
        {"AAPL": 200.0},
    )
    result = run_stress_test(engine, pf, -0.10)
    # 市值变动量 = 250*(180-200) = -5000;冲击后权益应为 100000-5000=95000
    assert result.shocked_equity == pytest.approx(95_000.0)
    assert result.current_equity == pytest.approx(100_000.0)  # 尊重权威值,不被重算覆盖


def test_stress_hedged_portfolio_shock_direction():
    """多头+空头对冲组合,同一个"整体下跌"的冲击对净值影响应远小于纯多头。"""
    engine = _make_engine()
    pf = Portfolio(
        Account(equity=100_000, cash=50_000),
        {
            "AAPL": Position("AAPL", 100, 200.0),  # 多头 20000
            "TSLA": Position("TSLA", -80, 250.0),  # 空头 20000(市值 -20000)
        },
        {"AAPL": 200.0, "TSLA": 250.0},
    )
    result = run_stress_test(engine, pf, -0.20)
    # AAPL 亏 100*(160-200)=-4000;TSLA 空头在跌 20% 时反而赚 -80*(200-250)=4000
    # 两者几乎完全对冲,净值变动接近 0
    assert abs(result.shocked_equity - result.current_equity) < 100.0


def test_stress_skips_positions_without_mark():
    engine = _make_engine()
    pf = Portfolio(
        Account(equity=100_000, cash=100_000),
        {"GHOST": Position("GHOST", 10, 0.0)},  # 无有效均价、也没提供 mark
        {},
    )
    result = run_stress_test(engine, pf, -0.10)
    assert result.shocked_equity == pytest.approx(result.current_equity)  # 该仓位被跳过,不参与计算


# --------------------------------------------------------------------------- #
# stress.py:熔断预判
# --------------------------------------------------------------------------- #
def test_stress_predicts_breaker_trip():
    # 全仓单一标的:整体冲击幅度直接等于权益变动幅度,方便断言阈值。
    engine = _make_engine(max_drawdown_pct=0.15, max_position_pct=1.0)
    pf = Portfolio(
        Account(equity=100_000, cash=0.0),
        {"AAPL": Position("AAPL", 500, 200.0)},
        {"AAPL": 200.0},
    )
    engine.update_equity(pf)  # hwm=100000

    small_shock = run_stress_test(engine, pf, -0.05)
    assert small_shock.would_trip_breaker is False

    big_shock = run_stress_test(engine, pf, -0.20)
    assert big_shock.would_trip_breaker is True


def test_stress_already_tripped_reports_that_not_would_trip():
    engine = _make_engine(max_drawdown_pct=0.15)
    engine.update_equity(_flat_portfolio(100_000))
    engine.update_equity(_flat_portfolio(80_000))  # -20%,触发熔断
    assert engine.breaker_tripped

    pf = Portfolio(Account(equity=80_000, cash=80_000))
    result = run_stress_test(engine, pf, -0.05)
    assert result.already_tripped is True
    assert result.would_trip_breaker is False  # 已经触发的,不算"这次冲击会触发"


def test_stress_zero_high_water_mark_reports_zero_drawdown():
    engine = _make_engine()  # 从未 observe 过,hwm=0
    pf = Portfolio(Account(equity=100_000, cash=100_000))
    result = run_stress_test(engine, pf, -0.50)
    assert result.current_drawdown == 0.0
    assert result.shocked_drawdown == 0.0
    assert result.would_trip_breaker is False


# --------------------------------------------------------------------------- #
# stress.py:仓位超限检测
# --------------------------------------------------------------------------- #
def test_stress_reports_position_breaches():
    engine = _make_engine(max_position_pct=0.10)
    pf = Portfolio(
        Account(equity=100_000, cash=50_000),
        {"AAPL": Position("AAPL", 200, 200.0)},  # 冲击前就已是 40%
        {"AAPL": 200.0},
    )
    result = run_stress_test(engine, pf, 0.0)
    assert len(result.position_breaches) == 1
    breach = result.position_breaches[0]
    assert isinstance(breach, PositionBreach)
    assert breach.symbol == "AAPL"
    assert breach.cap == 0.10


def test_stress_no_breach_within_cap():
    engine = _make_engine(max_position_pct=0.50)
    pf = Portfolio(
        Account(equity=100_000, cash=80_000),
        {"AAPL": Position("AAPL", 100, 200.0)},  # 20%,在 50% 上限内
        {"AAPL": 200.0},
    )
    result = run_stress_test(engine, pf, -0.10)
    assert result.position_breaches == ()


# --------------------------------------------------------------------------- #
# stress.py:核心承诺——绝对零副作用
# --------------------------------------------------------------------------- #
def test_stress_never_mutates_engine_state():
    broker = PaperBroker(cash=100_000, marks={"AAPL": 200.0})
    engine = RiskEngine(RiskConfig(max_drawdown_pct=0.15), broker=broker)
    engine.submit(Order("AAPL", Side.BUY, 100), broker.get_portfolio())
    engine.update_equity(broker.get_portfolio())

    state_before = engine.state
    for shock in (-0.05, -0.10, -0.30, -0.60, 0.20, 0.50):
        run_stress_test(engine, broker.get_portfolio(), shock)

    assert engine.state is state_before  # 状态对象引用都没变,说明压根没写过
    assert engine.state.high_water_mark == state_before.high_water_mark
    assert engine.breaker_tripped == state_before.breaker_tripped


def test_stress_never_calls_broker():
    """压力测试不该碰 broker——用一个"一碰就炸"的假 broker 验证。"""
    from riskguard.brokers.base import Broker

    class BoomBroker(Broker):
        name = "boom"

        def submit_order(self, order):
            raise AssertionError("stress test must never submit orders")

        def cancel_order(self, broker_order_id):
            raise AssertionError("stress test must never cancel orders")

        def get_account(self):
            raise AssertionError("stress test must never call broker")

        def get_positions(self):
            raise AssertionError("stress test must never call broker")

    engine = RiskEngine(RiskConfig(), broker=BoomBroker())
    pf = Portfolio(Account(equity=100_000, cash=100_000))
    run_stress_test(engine, pf, -0.20)  # 不该抛出 AssertionError


def test_stress_never_writes_audit(tmp_path):
    from riskguard import JsonlAuditSink

    audit_path = str(tmp_path / "audit.jsonl")
    with JsonlAuditSink(audit_path) as audit:
        engine = RiskEngine(RiskConfig(), audit=audit)
        engine.update_equity(_flat_portfolio(100_000))
        before_lines = open(audit_path).read().count("\n")
        run_stress_test(engine, _flat_portfolio(100_000), -0.30)
        after_lines = open(audit_path).read().count("\n")
    assert after_lines == before_lines  # 压力测试没有多写一条审计


# --------------------------------------------------------------------------- #
# stress.py:输出
# --------------------------------------------------------------------------- #
def test_stress_to_dict_and_render_text():
    engine = _make_engine()
    result = run_stress_test(engine, _flat_portfolio(100_000), -0.10)
    d = result.to_dict()
    assert d["shock_pct"] == -0.10
    assert isinstance(d["position_breaches"], list)
    text = render_stress_text(result)
    assert "压力测试" in text
    assert isinstance(text, str) and len(text) > 0


def test_stress_gross_net_exposure_ratios():
    engine = _make_engine()
    pf = Portfolio(
        Account(equity=100_000, cash=50_000),
        {
            "AAPL": Position("AAPL", 100, 200.0),   # 多头 20000
            "TSLA": Position("TSLA", -80, 250.0),   # 空头 -20000
        },
        {"AAPL": 200.0, "TSLA": 250.0},
    )
    result = run_stress_test(engine, pf, 0.0)  # 零冲击,方便手算核对
    # gross = |20000| + |-20000| = 40000; net = 20000 - 20000 = 0
    assert result.gross_exposure_ratio == pytest.approx(40_000 / 100_000)
    assert result.net_exposure_ratio == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# digest.py:基础字段
# --------------------------------------------------------------------------- #
def test_digest_basic_fields():
    engine = _make_engine(max_drawdown_pct=0.15)
    engine.update_equity(_flat_portfolio(100_000))
    engine.update_equity(_flat_portfolio(90_000))
    report = build_digest(engine, _flat_portfolio(90_000))
    assert isinstance(report, DigestReport)
    assert report.equity == 90_000
    assert report.high_water_mark == 100_000
    assert report.drawdown == pytest.approx(0.10)
    assert report.headroom_to_breaker == pytest.approx(0.05)
    assert report.breaker_tripped is False


def test_digest_reflects_tripped_breaker():
    engine = _make_engine(max_drawdown_pct=0.15)
    engine.update_equity(_flat_portfolio(100_000))
    engine.update_equity(_flat_portfolio(80_000))
    assert engine.breaker_tripped
    report = build_digest(engine, _flat_portfolio(80_000))
    assert report.breaker_tripped is True
    assert report.trip_reason != ""
    assert report.headroom_to_breaker < 0  # 已经越过熔断线


# --------------------------------------------------------------------------- #
# digest.py:持仓 vs 上限
# --------------------------------------------------------------------------- #
def test_digest_position_standing():
    engine = _make_engine(max_position_pct=0.10)
    pf = Portfolio(
        Account(equity=100_000, cash=80_000),
        {"AAPL": Position("AAPL", 100, 200.0)},  # 20%,超出 10% 上限
        {"AAPL": 200.0},
    )
    report = build_digest(engine, pf)
    assert len(report.positions) == 1
    standing = report.positions[0]
    assert isinstance(standing, PositionStanding)
    assert standing.symbol == "AAPL"
    assert standing.weight == pytest.approx(0.20)
    assert standing.headroom == pytest.approx(0.10 - 0.20)  # 负数,已超限


def test_digest_excludes_flat_positions():
    engine = _make_engine()
    pf = Portfolio(
        Account(equity=100_000, cash=100_000),
        {"AAPL": Position("AAPL", 0.0, 200.0)},  # 平仓
        {"AAPL": 200.0},
    )
    report = build_digest(engine, pf)
    assert report.positions == ()


# --------------------------------------------------------------------------- #
# digest.py:隔离观察中的策略
# --------------------------------------------------------------------------- #
def test_digest_lists_quarantined_strategy():
    inception = datetime(2026, 1, 1, tzinfo=timezone.utc)
    engine = RiskEngine(
        RiskConfig(quarantine_days=90), clock=lambda: inception
    )  # 确定性时钟:register_strategy 用这个时刻登记入役
    engine.register_strategy("newstrat")
    later = datetime(2026, 1, 10, tzinfo=timezone.utc)  # 登记后 9 天
    report = build_digest(engine, _flat_portfolio(100_000), now=later)
    assert len(report.quarantined_strategies) == 1
    q = report.quarantined_strategies[0]
    assert isinstance(q, QuarantineStanding)
    assert q.strategy_id == "newstrat"
    assert q.age_days == pytest.approx(9.0, abs=0.01)
    assert q.days_remaining == pytest.approx(81.0, abs=0.01)


def test_digest_excludes_strategy_past_quarantine():
    engine = _make_engine(quarantine_days=90)
    engine.register_strategy("veteran")
    far_future = datetime(2027, 1, 1, tzinfo=timezone.utc)
    report = build_digest(engine, _flat_portfolio(100_000), now=far_future)
    assert report.quarantined_strategies == ()


def test_digest_no_quarantine_when_never_registered():
    engine = _make_engine(quarantine_days=90)
    report = build_digest(engine, _flat_portfolio(100_000))
    assert report.quarantined_strategies == ()


# --------------------------------------------------------------------------- #
# digest.py:核心承诺——零副作用(不观测、不触发熔断)
# --------------------------------------------------------------------------- #
def test_digest_never_mutates_engine_state():
    engine = _make_engine(max_drawdown_pct=0.15)
    engine.update_equity(_flat_portfolio(100_000))
    state_before = engine.state

    # 传一个会导致熔断的权益给 build_digest —— 它不该顺带观测/触发
    build_digest(engine, _flat_portfolio(50_000))

    assert engine.state is state_before
    assert engine.breaker_tripped is False  # 没有被 build_digest 悄悄触发


def test_digest_gross_net_exposure_and_zero_equity_guard():
    engine = _make_engine()
    pf = Portfolio(Account(equity=0.0, cash=0.0))
    report = build_digest(engine, pf)
    assert math.isinf(report.gross_exposure_ratio)
    assert math.isinf(report.net_exposure_ratio)


# --------------------------------------------------------------------------- #
# digest.py:输出
# --------------------------------------------------------------------------- #
def test_digest_to_dict_and_render_text():
    engine = _make_engine()
    engine.update_equity(_flat_portfolio(100_000))
    report = build_digest(engine, _flat_portfolio(100_000))
    d = report.to_dict()
    assert d["equity"] == 100_000
    assert "generated_at" in d and isinstance(d["generated_at"], str)
    text = render_digest_text(report)
    assert "每日体检" in text


# --------------------------------------------------------------------------- #
# 第二轮审查回归:equity <= 0 时,总量指标(inf)和分项指标(单个持仓/单个
# breach)必须一致,不能出现"总敞口报警、单个持仓却显示健康"的自相矛盾
# --------------------------------------------------------------------------- #
def test_stress_position_breach_not_false_negative_when_equity_wiped_out():
    """账户被冲击打穿仓(shocked_equity <= 0)时,仍持有仓位的标的必须被报告为
    超限,而不是因为 Portfolio.weight() 的除零防御性默认值 0.0 而被漏报——
    这恰恰是压力测试最该揪出来的场景。"""
    engine = _make_engine(max_position_pct=0.10)
    pf = Portfolio(
        Account(equity=10_000, cash=-90_000),
        {"AAPL": Position("AAPL", 1000, 100.0)},
        {"AAPL": 100.0},
    )
    # 恰好打到 equity == 0 的边界,以及更极端的负权益,两个都不能漏报
    for shock in (-0.20, -0.90):
        result = run_stress_test(engine, pf, shock)
        assert result.shocked_equity <= 0
        assert len(result.position_breaches) == 1
        assert result.position_breaches[0].symbol == "AAPL"
        assert result.position_breaches[0].shocked_weight == float("inf")
    assert result.gross_exposure_ratio == float("inf")  # 总量指标本就正确


def test_stress_flat_position_not_falsely_breached_when_equity_wiped_out():
    """账户穿仓但某标的已经平仓,不该被误报为超限(inf 只给非平仓持仓)。"""
    engine = _make_engine(max_position_pct=0.10)
    pf = Portfolio(
        Account(equity=-1000, cash=-1000),
        {"AAPL": Position("AAPL", 0.0, 100.0)},  # 平仓
        {"AAPL": 100.0},
    )
    result = run_stress_test(engine, pf, -0.10)
    assert result.position_breaches == ()


def test_stress_equity_change_pct_no_false_positive_direction_when_negative():
    """current_equity 为负数时,equity_change_pct 不该算出一个方向反转的
    "上涨"假象(分母为负会让公式的符号语义反转)。"""
    engine = _make_engine()
    pf = Portfolio(
        Account(equity=-3000, cash=-3000),
        {"AAPL": Position("AAPL", 10, 100.0)},
        {"AAPL": 100.0},
    )
    result = run_stress_test(engine, pf, -0.10)
    assert result.equity_change_pct == 0.0  # 不给出误导性的方向


def test_digest_position_standing_matches_gross_ratio_when_equity_wiped_out():
    """日报里"总敞口/权益"(inf)和"单个持仓超限情况"必须口径一致——不能一个说
    "非常危险"、另一个却说"完全没超限、还有正的 headroom"。"""
    engine = _make_engine(max_position_pct=0.10)
    pf = Portfolio(
        Account(equity=-5000, cash=0),
        {"AAPL": Position("AAPL", 1000, 100.0)},
        {"AAPL": 100.0},
    )
    report = build_digest(engine, pf)
    assert report.gross_exposure_ratio == float("inf")
    assert len(report.positions) == 1
    assert report.positions[0].weight == float("inf")
    assert report.positions[0].headroom == float("-inf")  # 不是正数


def test_digest_flat_position_shows_healthy_weight_even_when_equity_negative():
    engine = _make_engine(max_position_pct=0.10)
    pf = Portfolio(
        Account(equity=-1000, cash=-1000),
        {"AAPL": Position("AAPL", 0.0, 100.0)},  # 平仓
        {"AAPL": 100.0},
    )
    report = build_digest(engine, pf)
    assert report.positions == ()  # 平仓持仓本就不进入列表,不受此问题影响


def test_reporting_render_text_survives_infinite_weight():
    """inf 权重不该让文本渲染崩溃(Python 的 .1% 格式化对 inf 是安全的)。"""
    engine = _make_engine(max_position_pct=0.10)
    pf = Portfolio(
        Account(equity=-5000, cash=0),
        {"AAPL": Position("AAPL", 1000, 100.0)},
        {"AAPL": 100.0},
    )
    report = build_digest(engine, pf)
    text = render_digest_text(report)
    assert "inf" in text  # 没有崩溃,且如实呈现了这个极端值

    result = run_stress_test(engine, pf, -0.90)
    stress_text = render_stress_text(result)
    assert isinstance(stress_text, str) and len(stress_text) > 0
