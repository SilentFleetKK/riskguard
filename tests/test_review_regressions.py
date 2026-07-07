"""对抗式审查发现的问题的回归测试。

这一组测试对应 v1.0 发布前那轮多智能体审查逮到的真实缺陷(3 critical + 4 high)。
每个用例都先复现"修复前会漏风"的场景,断言"修复后正确收敛风险"。它们是这些
致命漏洞永不复发的护栏。
"""

from __future__ import annotations

import json
import threading

import pytest

from riskguard import (
    JsonlAuditSink,
    Order,
    PaperBroker,
    RiskConfig,
    RiskEngine,
    RiskMonitor,
    Side,
    SqliteAuditSink,
)


# ---------------------------------------------------------------------------
# CRITICAL #1:持仓反手绕过所有仓位上限规则
# ---------------------------------------------------------------------------
def test_flip_to_equal_or_smaller_magnitude_is_capped():
    """空 100 反手买 200 -> 多 100(100% 权益)。修复前 |100|<=|-100| 被当减仓放行;
    修复后必须按 10% 上限缩单。"""
    broker = PaperBroker(cash=10_000, marks={"X": 100.0})
    broker.submit_order(Order("X", Side.SELL, 100))  # 直接建空 100 仓
    engine = RiskEngine(RiskConfig(max_position_pct=0.10), broker=broker)

    decision = engine.check(Order("X", Side.BUY, 200), broker.get_portfolio())

    assert not (decision.approved and decision.order.quantity == 200)
    assert decision.resized
    # 缩单后成交,结果持仓幅度不得超过 10% 上限(cap_qty = 10)
    current = -100.0
    projected = current + decision.order.quantity  # 买入为正
    assert abs(projected) <= 10.0 + 1e-6


# ---------------------------------------------------------------------------
# CRITICAL #3:PaperBroker 执行 reduce_only,超额减仓不得反向开仓
# ---------------------------------------------------------------------------
def test_reduce_only_clamps_at_flat_never_flips():
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    broker.submit_order(Order("AAPL", Side.BUY, 100))  # 多 100
    # reduce_only 卖 500:修复前会翻成空 400,修复后至多平到 0
    bo = broker.submit_order(Order("AAPL", Side.SELL, 500, reduce_only=True))
    assert bo.filled_quantity == 100  # 只成交了平仓所需的 100
    assert "AAPL" not in broker.get_positions()  # 已平仓,不是反手做空


def test_reduce_only_on_flat_is_noop():
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    bo = broker.submit_order(Order("AAPL", Side.BUY, 50, reduce_only=True))
    assert bo.filled_quantity == 0
    assert broker.get_positions() == {}
    assert broker.get_account().cash == 100_000  # 没动钱


# ---------------------------------------------------------------------------
# CRITICAL #2:审计防篡改的真实边界 —— HMAC 防伪 + expected_count 防尾部截断
# ---------------------------------------------------------------------------
def test_hmac_key_prevents_forgery(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    with JsonlAuditSink(path, hmac_key="s3cret") as sink:
        sink.record_event("decision", _now(), symbol="AAPL")
        sink.record_event("fill", _now(), symbol="AAPL")

    assert JsonlAuditSink.verify(path, hmac_key="s3cret") is True
    # 没有密钥(或密钥错误)一律判不通过 —— 没有密钥就无法伪造/重写
    assert JsonlAuditSink.verify(path) is False
    assert JsonlAuditSink.verify(path, hmac_key="wrong") is False


def test_expected_count_detects_tail_truncation(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    with JsonlAuditSink(path) as sink:
        for _ in range(3):
            sink.record_event("decision", _now())

    lines = open(path, encoding="utf-8").read().splitlines()
    with open(path, "w", encoding="utf-8") as fh:  # 删掉最后一条(尾部截断)
        fh.write("\n".join(lines[:-1]) + "\n")

    # 纯链自洽,截断检测不到;带上外部锚点 expected_count 就能揪出来
    assert JsonlAuditSink.verify(path) is True
    assert JsonlAuditSink.verify(path, expected_count=3) is False
    assert JsonlAuditSink.verify(path, expected_count=2) is True


def test_verify_returns_false_on_corrupt_line_not_raise(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    with JsonlAuditSink(path) as sink:
        sink.record_event("decision", _now())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("GARBAGE NOT JSON\n")
    # 坏行不应让 verify 崩溃,而是判为链已断
    assert JsonlAuditSink.verify(path) is False


def test_verify_missing_file_returns_false(tmp_path):
    assert JsonlAuditSink.verify(str(tmp_path / "nope.jsonl")) is False


def test_sqlite_hmac_and_count_parity(tmp_path):
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path, hmac_key="k")
    sink.record_event("decision", _now())
    sink.record_event("fill", _now())
    sink.close()
    assert SqliteAuditSink.verify(path, hmac_key="k") is True
    assert SqliteAuditSink.verify(path) is False
    assert SqliteAuditSink.verify(path, hmac_key="k", expected_count=2) is True
    assert SqliteAuditSink.verify(path, hmac_key="k", expected_count=5) is False


# ---------------------------------------------------------------------------
# HIGH:非正标记价不得让组合敞口 fail-open
# ---------------------------------------------------------------------------
def test_negative_mark_cannot_open_exposure_headroom():
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    broker.submit_order(Order("AAPL", Side.BUY, 500))  # 多 500,名义 50k
    # 注入一个坏价:负数 mark。修复前 gross 会被算成负数 -> 以为额度巨大
    pf = broker.get_portfolio(marks={"AAPL": -100.0})
    # 坏价被丢弃 -> 回退到均价计价,敞口仍为正,gross 上限照常生效
    assert pf.gross_exposure() > 0


# ---------------------------------------------------------------------------
# HIGH:RiskMonitor._tick 串行化,并发也只平仓一次、不重复反手
# ---------------------------------------------------------------------------
def test_concurrent_ticks_liquidate_once_and_stay_flat():
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    engine = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15, max_position_pct=1.0), broker=broker
    )
    broker.submit_order(Order("AAPL", Side.BUY, 900))  # 多 900,占满权益
    engine.update_equity(broker.get_portfolio())  # hwm = 100k

    trips = {"n": 0}
    monitor = RiskMonitor(
        engine, broker, auto_liquidate=True, on_trip=lambda s: trips.__setitem__("n", trips["n"] + 1)
    )

    broker.set_mark("AAPL", 80.0)  # 权益跌到 ~82k,-18% -> 触发熔断

    threads = [threading.Thread(target=monitor._tick) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert trips["n"] == 1  # 只处理了一次熔断(未重复平仓)
    assert broker.get_positions() == {}  # 已平仓,且没有被反手开成空仓


# ---------------------------------------------------------------------------
# 第二轮审查:NaN/inf 权益不得让熔断永不触发(fail-open)
# ---------------------------------------------------------------------------
def test_non_finite_equity_is_ignored_by_state():
    from riskguard import RiskState

    s = RiskState.initial(100_000.0)
    assert s.observe_equity(float("nan"), _now()).last_equity == 100_000.0
    assert s.observe_equity(float("inf"), _now()).last_equity == 100_000.0


def test_nan_equity_does_not_disable_circuit_breaker():
    from riskguard import Account, Portfolio

    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    engine = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15, max_position_pct=1.0), broker=broker
    )
    engine.update_equity(broker.get_portfolio())  # hwm = 100k
    # 一次 NaN 权益读数(feed 抖动):必须被忽略,不污染 last_equity、不禁用熔断
    engine.update_equity(Portfolio(Account(equity=float("nan"))))
    assert not engine.breaker_tripped
    assert engine.state.last_equity == 100_000.0
    # 之后真实跌破红线 -> 熔断照常触发
    broker._cash = 80_000
    engine.update_equity(broker.get_portfolio())
    assert engine.breaker_tripped


# ---------------------------------------------------------------------------
# 第二轮审查:equity<=0(爆仓)时 reduce_only 减仓单仍必须放行
# ---------------------------------------------------------------------------
def test_reduce_only_allowed_even_at_zero_equity():
    from riskguard import Account, Portfolio, Position

    engine = RiskEngine(RiskConfig())
    pf = Portfolio(
        Account(equity=0.0),
        positions={"AAPL": Position("AAPL", 50, 100.0)},
        marks={"AAPL": 100.0},
    )
    assert engine.check(Order("AAPL", Side.SELL, 10, reduce_only=True), pf).approved
    # 但爆仓时放大敞口的新开仓仍被拒
    assert engine.check(Order("AAPL", Side.BUY, 10), pf).rejected


# ---------------------------------------------------------------------------
# 第二轮审查:max_net_exposure_pct 不再是死配置
# ---------------------------------------------------------------------------
def test_net_exposure_limit_enforced_when_configured():
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    cfg = RiskConfig(
        max_net_exposure_pct=0.20, max_gross_exposure_pct=10.0, max_position_pct=1.0
    )
    engine = RiskEngine(cfg, broker=broker)
    # 买 300 @100 = 净敞口 30k = 30% > 20% 上限 -> 缩到 200 股(20k)
    d = engine.check(Order("AAPL", Side.BUY, 300), broker.get_portfolio())
    assert d.resized
    assert d.order.quantity == pytest.approx(200.0)


def test_net_exposure_disabled_is_noop_by_default():
    from datetime import datetime, timezone

    from riskguard import Account, Portfolio, RiskState
    from riskguard.rules import NetExposureLimit, RuleContext

    pf = Portfolio(Account(equity=100_000.0), marks={"AAPL": 100.0})
    ctx = RuleContext(
        Order("AAPL", Side.BUY, 100_000), pf, RiskConfig(),
        RiskState.initial(100_000.0), datetime.now(timezone.utc),
    )
    res = NetExposureLimit().evaluate(ctx)
    assert res.passed and res.action.value == "approve"


# ---------------------------------------------------------------------------
# 第二轮审查:审计写入失败不得中断风控裁决
# ---------------------------------------------------------------------------
def test_audit_failure_does_not_break_risk_decision():
    from riskguard.audit.base import AuditSink

    class BoomAudit(AuditSink):
        name = "boom"

        def record(self, event):
            raise OSError("disk full")

    errors = []
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    engine = RiskEngine(
        RiskConfig(max_position_pct=0.10),
        broker=broker,
        audit=BoomAudit(),
        on_audit_error=errors.append,
    )
    # 审计会抛异常,但裁决必须照常返回(缩单),错误转交回调
    d = engine.check(Order("AAPL", Side.BUY, 1000), broker.get_portfolio())
    assert d.resized
    assert len(errors) == 1 and isinstance(errors[0], OSError)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
