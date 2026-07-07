"""端到端集成测试:把 PaperBroker + RiskEngine + JsonlAuditSink + FixedFractionalSizer
串成一条完整链路,跑一场"迷你交易时段",验证各组件协同后的整体行为。

覆盖的主线剧情
--------------
1. 建仓:提交两笔想买 20% 的订单,被单笔仓位上限(10%)缩量后成交;
2. 崩盘:通过 :meth:`update_equity` 把标记价打下来,回撤越过 15% 红线触发熔断;
3. 冻结:熔断后新开/加仓单被拒,而减仓/平仓单仍放行(风险要能收敛);
4. 审计:哈希链完好可校验,且确实落下了一条 ``breaker_trip`` 事件;
5. 复位::meth:`reset_breaker` 人工复盘后重新开闸,买单恢复放行。

外加若干边界与协同用例:``raise_on_reject`` 抛异常路径、``size_and_submit``
走仓位算法、审计链的防篡改校验、跨进程续写哈希链、reduce_only 单在熔断下放行、
以及"单个 10% 仓位在数学上无法触发 15% 回撤"这一护栏假设的显式回归。

设计约束(与全库一致):所有涉及时间的地方都用可变列表时钟注入
``RiskEngine(clock=...)``,不依赖 wall clock;文件全部落在 ``tmp_path``;
只从公共 API ``riskguard`` 导入;无任何网络访问。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from riskguard import (
    Decision,
    FixedFractionalSizer,
    JsonlAuditSink,
    Order,
    OrderRejected,
    PaperBroker,
    RiskConfig,
    RiskEngine,
    Side,
    Signal,
)

# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #

T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# 初始行情:AAPL 200、MSFT 400。初始现金 10 万。
_INITIAL_MARKS = {"AAPL": 200.0, "MSFT": 400.0}
_INITIAL_CASH = 100_000.0


def _list_clock(start: datetime = T0):
    """返回 ``(可变时间盒, 读取器)``;改盒子里的值即可确定性地推进引擎时钟。"""
    box = [start]
    return box, (lambda: box[0])


def _new_broker() -> PaperBroker:
    """全新的、无摩擦(零滑点零佣金)的纸面盘,便于精确断言权益数字。"""
    return PaperBroker(cash=_INITIAL_CASH, marks=dict(_INITIAL_MARKS))


def _audit_path(tmp_path) -> str:
    return str(tmp_path / "audit.jsonl")


def _event_types(path: str) -> list[str]:
    """按行读出审计文件里的 event_type 序列。"""
    types: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            types.append(json.loads(line)["event_type"])
    return types


def _records(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- #
# 主线:完整的一场迷你交易时段
# --------------------------------------------------------------------------- #

def test_full_session_build_trip_freeze_reset(tmp_path):
    """一条龙:建仓缩量 → 崩盘熔断 → 冻结买单/放行卖单 → 审计可验证 → 复位开闸。"""
    path = _audit_path(tmp_path)
    box, clock = _list_clock()
    broker = _new_broker()

    # 审计后端全程持有(用 with 保证最终 close),引擎注入默认规则栈 + 固定比例仓位。
    with JsonlAuditSink(path) as audit:
        engine = RiskEngine(
            broker=broker,
            audit=audit,
            sizer=FixedFractionalSizer(),
            clock=clock,
        )

        # --- 阶段 1:建仓,两笔都想吃 20%,被 10% 上限缩量 ---
        # 想买 100 股 AAPL = 2 万 = 权益 20% → 缩到 10% = 50 股。
        aapl_order = Order("AAPL", Side.BUY, 100)
        aapl_decision = engine.check(aapl_order, broker.get_portfolio())
        assert aapl_decision.decision is Decision.RESIZE
        assert aapl_decision.resized is True
        assert aapl_decision.order.quantity == pytest.approx(50.0)
        # 缩量原因来自单笔仓位上限规则。
        capped = [r for r in aapl_decision.results if not r.passed]
        assert any(r.rule == "max_position_limit" for r in capped)

        aapl_fill = engine.submit(aapl_order, broker.get_portfolio())
        assert aapl_fill is not None
        assert aapl_fill.is_filled
        assert aapl_fill.filled_quantity == pytest.approx(50.0)

        # 想买 100 股 MSFT = 4 万 = 权益 40% → 缩到 10% = 25 股。
        msft_order = Order("MSFT", Side.BUY, 100)
        msft_fill = engine.submit(msft_order, broker.get_portfolio())
        assert msft_fill is not None
        assert msft_fill.filled_quantity == pytest.approx(25.0)

        # 无摩擦成交:建仓不改变权益,只是现金换成持仓。
        acct = broker.get_account()
        assert acct.equity == pytest.approx(100_000.0)
        assert acct.cash == pytest.approx(80_000.0)  # 10 万 - 1 万 AAPL - 1 万 MSFT
        # 高点被观测记录为 10 万。
        assert engine.state.high_water_mark == pytest.approx(100_000.0)
        assert engine.state.breaker_tripped is False

        # --- 阶段 2:崩盘,把两只标的打到脚踝,回撤越过 15% 红线 ---
        # 持仓 50 AAPL + 25 MSFT,现金 8 万。把两只都砍到十分之一:
        # 权益 = 80000 + 50*20 + 25*40 = 82000 → 回撤 18% ≥ 15%,熔断。
        box[0] = T0 + timedelta(days=1)
        broker.set_marks({"AAPL": 20.0, "MSFT": 40.0})
        state_after_crash = engine.update_equity(broker.get_portfolio())
        assert broker.get_account().equity == pytest.approx(82_000.0)
        assert state_after_crash.drawdown == pytest.approx(0.18, abs=1e-6)
        assert state_after_crash.breaker_tripped is True
        assert engine.breaker_tripped is True
        assert state_after_crash.tripped_at == box[0]
        assert "drawdown" in state_after_crash.trip_reason

        # --- 阶段 3:冻结期,新买单被拒、卖单放行 ---
        buy_decision = engine.check(Order("AAPL", Side.BUY, 1), broker.get_portfolio())
        assert buy_decision.decision is Decision.REJECT
        assert buy_decision.rejected is True
        # 拒单原因必须来自熔断规则(而不是别的规则)。
        rejected_rules = {r.rule for r in buy_decision.rejections()}
        assert "drawdown_circuit_breaker" in rejected_rules

        # 卖出(减仓)在熔断下仍应放行,并真的成交。
        sell_decision = engine.check(Order("AAPL", Side.SELL, 10), broker.get_portfolio())
        assert sell_decision.approved is True
        sell_fill = engine.submit(Order("AAPL", Side.SELL, 10), broker.get_portfolio())
        assert sell_fill is not None
        assert sell_fill.filled_quantity == pytest.approx(10.0)
        # 卖出后持仓由 50 减到 40。
        assert broker.get_positions()["AAPL"].quantity == pytest.approx(40.0)

        # --- 阶段 4:审计链在复位前先校验一次 ---
        # (verify 读文件,而写入已 flush,可安全在 sink 未关闭时校验。)
        assert JsonlAuditSink.verify(path) is True
        types_before_reset = _event_types(path)
        assert "breaker_trip" in types_before_reset
        # 期间应记录了若干 decision + 两笔 fill + 一条 breaker_trip。
        assert types_before_reset.count("fill") == 3  # 2 建仓 + 1 减仓
        assert types_before_reset.count("breaker_trip") == 1

        # --- 阶段 5:人工复盘 → 复位熔断 → 重新开闸 ---
        box[0] = T0 + timedelta(days=2)
        reset_state = engine.reset_breaker()
        assert reset_state.breaker_tripped is False
        assert engine.breaker_tripped is False
        # 复位把高点归位到当前权益,避免立刻二次触发。
        assert reset_state.high_water_mark == pytest.approx(reset_state.last_equity)

        # 复位后买单恢复放行(现金充裕、权益已重设,10% 上限内)。
        rebuy = engine.check(Order("AAPL", Side.BUY, 1), broker.get_portfolio())
        assert rebuy.approved is True

    # with 退出后 sink 关闭:整条链依然完好,且含 breaker_trip / breaker_reset。
    assert JsonlAuditSink.verify(path) is True
    final_types = _event_types(path)
    assert "breaker_trip" in final_types
    assert "breaker_reset" in final_types


# --------------------------------------------------------------------------- #
# 熔断:单个 10% 仓位在数学上无法触发 15% 回撤(护栏假设回归)
# --------------------------------------------------------------------------- #

def test_single_position_cannot_trip_drawdown(tmp_path):
    """只有一个被 10% 上限压住的仓位时,即便标记价归零,回撤上限也只有 10%。

    这解释了为什么主线剧情必须用两只标的凑到 20% 敞口才能越过 15% 红线——
    是一条防止"以为单仓能触发熔断"的显式回归。
    """
    box, clock = _list_clock()
    broker = _new_broker()
    engine = RiskEngine(broker=broker, sizer=FixedFractionalSizer(), clock=clock)

    engine.submit(Order("AAPL", Side.BUY, 100), broker.get_portfolio())  # 缩到 50 股
    assert engine.state.high_water_mark == pytest.approx(100_000.0)

    # 现金 9 万 + 50*mark。mark 打到 0,权益地板也有 9 万 → 回撤最多 10% < 15%。
    box[0] = T0 + timedelta(days=1)
    broker.set_mark("AAPL", 0.01)  # 近乎归零
    state = engine.update_equity(broker.get_portfolio())
    assert state.drawdown < 0.15
    assert state.breaker_tripped is False
    # 买单因此仍受理(会被仓位规则再评估),而非被熔断拒绝。
    decision = engine.check(Order("AAPL", Side.SELL, 1), broker.get_portfolio())
    assert decision.approved is True


# --------------------------------------------------------------------------- #
# raise_on_reject:熔断后 submit 买单应抛 OrderRejected,卖单照常成交
# --------------------------------------------------------------------------- #

def test_raise_on_reject_blocks_buy_but_allows_sell(tmp_path):
    path = _audit_path(tmp_path)
    box, clock = _list_clock()
    broker = _new_broker()

    with JsonlAuditSink(path) as audit:
        engine = RiskEngine(
            broker=broker,
            audit=audit,
            sizer=FixedFractionalSizer(),
            clock=clock,
            raise_on_reject=True,
        )
        engine.submit(Order("AAPL", Side.BUY, 100), broker.get_portfolio())
        engine.submit(Order("MSFT", Side.BUY, 100), broker.get_portfolio())

        box[0] = T0 + timedelta(days=1)
        broker.set_marks({"AAPL": 20.0, "MSFT": 40.0})
        engine.update_equity(broker.get_portfolio())
        assert engine.breaker_tripped is True

        # 买单:raise_on_reject=True 应抛 OrderRejected,并带上可读的拒单原因。
        with pytest.raises(OrderRejected) as excinfo:
            engine.submit(Order("AAPL", Side.BUY, 1), broker.get_portfolio())
        assert "circuit breaker" in str(excinfo.value).lower()
        # 异常上挂着完整裁决对象,便于调用方复盘。
        assert excinfo.value.decision.rejected is True

        # 卖单:即便 raise_on_reject=True,减仓单也应放行并成交,不抛异常。
        sell = engine.submit(Order("AAPL", Side.SELL, 5), broker.get_portfolio())
        assert sell is not None
        assert sell.filled_quantity == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# reduce_only 单在熔断下放行(即使名义上是新开仓的方向)
# --------------------------------------------------------------------------- #

def test_reduce_only_order_allowed_while_tripped(tmp_path):
    box, clock = _list_clock()
    broker = _new_broker()
    engine = RiskEngine(broker=broker, sizer=FixedFractionalSizer(), clock=clock)

    engine.submit(Order("AAPL", Side.BUY, 100), broker.get_portfolio())
    engine.submit(Order("MSFT", Side.BUY, 100), broker.get_portfolio())
    box[0] = T0 + timedelta(days=1)
    broker.set_marks({"AAPL": 20.0, "MSFT": 40.0})
    engine.update_equity(broker.get_portfolio())
    assert engine.breaker_tripped is True

    # 一笔标了 reduce_only 的减仓单:熔断规则应据标记放行。
    reduce_order = Order("AAPL", Side.SELL, 5, reduce_only=True)
    decision = engine.check(reduce_order, broker.get_portfolio())
    assert decision.approved is True
    dd_result = next(r for r in decision.results if r.rule == "drawdown_circuit_breaker")
    assert dd_result.passed is True


# --------------------------------------------------------------------------- #
# size_and_submit:走仓位算法把信号换算成订单,再经风控成交
# --------------------------------------------------------------------------- #

def test_size_and_submit_uses_sizer_weight(tmp_path):
    """FixedFractionalSizer 默认吃 config.max_position_pct=10%,信号应下到刚好 10%。"""
    box, clock = _list_clock()
    broker = _new_broker()
    engine = RiskEngine(broker=broker, sizer=FixedFractionalSizer(), clock=clock)

    # 权益 10 万,10% = 1 万,AAPL@200 → 目标 50 股。恰在上限,应 APPROVE(不缩量)。
    signal = Signal(symbol="AAPL", side=Side.BUY, price=200.0)
    fill = engine.size_and_submit(signal, broker.get_portfolio())
    assert fill is not None
    assert fill.filled_quantity == pytest.approx(50.0)
    assert broker.get_positions()["AAPL"].quantity == pytest.approx(50.0)


def test_size_and_submit_smaller_fraction_stays_within_cap(tmp_path):
    """显式给 sizer 5% 权重时,信号下到 25 股,低于 10% 上限 → 直接放行不缩量。"""
    box, clock = _list_clock()
    broker = _new_broker()
    engine = RiskEngine(
        broker=broker, sizer=FixedFractionalSizer(fraction=0.05), clock=clock
    )
    signal = Signal(symbol="AAPL", side=Side.BUY, price=200.0)
    decision = engine.check(
        engine.sizer.size(signal, broker.get_portfolio(), engine.config),
        broker.get_portfolio(),
    )
    assert decision.decision is Decision.APPROVE
    fill = engine.size_and_submit(signal, broker.get_portfolio())
    assert fill is not None
    assert fill.filled_quantity == pytest.approx(25.0)  # 5% * 10万 / 200


# --------------------------------------------------------------------------- #
# 审计:哈希链防篡改,以及独立 verify 能抓出中途改动
# --------------------------------------------------------------------------- #

def test_audit_chain_detects_tampering(tmp_path):
    path = _audit_path(tmp_path)
    box, clock = _list_clock()
    broker = _new_broker()

    with JsonlAuditSink(path) as audit:
        engine = RiskEngine(
            broker=broker, audit=audit, sizer=FixedFractionalSizer(), clock=clock
        )
        engine.submit(Order("AAPL", Side.BUY, 100), broker.get_portfolio())
        engine.submit(Order("MSFT", Side.BUY, 100), broker.get_portfolio())
        box[0] = T0 + timedelta(days=1)
        broker.set_marks({"AAPL": 20.0, "MSFT": 40.0})
        engine.update_equity(broker.get_portfolio())

    # 未篡改前:链完好。
    assert JsonlAuditSink.verify(path) is True

    # 篡改首条 decision 的 payload(改一个成交量),其后所有 hash 应失配。
    records = _records(path)
    assert len(records) >= 3
    records[0]["payload"]["final_quantity"] = 999999.0
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    assert JsonlAuditSink.verify(path) is False


def test_audit_chain_resumes_across_reopen(tmp_path):
    """关闭 sink 后用同一路径新开一个 sink,应续接哈希链(序号/prev_hash 衔接)而非从创世重来。

    注意:RiskState 不随审计文件持久化——重开的是审计 sink,不是引擎状态。这里只验证
    审计层的"续写"契约:新 sink 从文件尾恢复序号与哈希,新事件接在后面且整链仍可校验。
    """
    path = _audit_path(tmp_path)
    box, clock = _list_clock()
    broker = _new_broker()

    # 第一段:开仓两笔。
    with JsonlAuditSink(path) as audit:
        engine = RiskEngine(
            broker=broker, audit=audit, sizer=FixedFractionalSizer(), clock=clock
        )
        engine.submit(Order("AAPL", Side.BUY, 100), broker.get_portfolio())
        engine.submit(Order("MSFT", Side.BUY, 100), broker.get_portfolio())

    first_seg = _records(path)
    assert len(first_seg) > 0
    last_seq_seg1 = first_seg[-1]["seq"]
    last_hash_seg1 = first_seg[-1]["hash"]
    assert JsonlAuditSink.verify(path) is True

    # 第二段:换一个新引擎/新 sink,续写到同一文件(再下一小笔单,产生新事件)。
    box[0] = T0 + timedelta(days=1)
    with JsonlAuditSink(path) as audit2:
        engine2 = RiskEngine(
            broker=broker, audit=audit2, sizer=FixedFractionalSizer(), clock=clock
        )
        engine2.submit(Order("AAPL", Side.SELL, 5), broker.get_portfolio())  # 减仓,必放行

    all_records = _records(path)
    # 续写:总行数增加,序号连续无断裂。
    assert len(all_records) > len(first_seg)
    seqs = [r["seq"] for r in all_records]
    assert seqs == list(range(1, len(all_records) + 1))
    # 跨段衔接:第二段首条的 seq/prev_hash 精确接上第一段末条。
    first_new = all_records[len(first_seg)]
    assert first_new["seq"] == last_seq_seg1 + 1
    assert first_new["prev_hash"] == last_hash_seg1
    # 整条(跨两段的)哈希链仍然完好。
    assert JsonlAuditSink.verify(path) is True


# --------------------------------------------------------------------------- #
# reset_breaker:未触发时复位不应落 breaker_reset 事件(避免噪声)
# --------------------------------------------------------------------------- #

def test_reset_when_not_tripped_emits_no_event(tmp_path):
    path = _audit_path(tmp_path)
    box, clock = _list_clock()
    broker = _new_broker()

    with JsonlAuditSink(path) as audit:
        engine = RiskEngine(
            broker=broker, audit=audit, sizer=FixedFractionalSizer(), clock=clock
        )
        engine.update_equity(broker.get_portfolio())  # 观测,但未熔断
        assert engine.breaker_tripped is False
        state = engine.reset_breaker()
        assert state.breaker_tripped is False

    # 从未触发过熔断 → 不应有 breaker_trip / breaker_reset 噪声事件。
    types = _event_types(path)
    assert "breaker_trip" not in types
    assert "breaker_reset" not in types


# --------------------------------------------------------------------------- #
# 决策审计:被缩量的订单在审计里应如实记录 requested vs final 数量
# --------------------------------------------------------------------------- #

def test_resize_decision_recorded_with_original_and_final(tmp_path):
    path = _audit_path(tmp_path)
    box, clock = _list_clock()
    broker = _new_broker()

    with JsonlAuditSink(path) as audit:
        engine = RiskEngine(
            broker=broker, audit=audit, sizer=FixedFractionalSizer(), clock=clock
        )
        engine.check(Order("AAPL", Side.BUY, 100), broker.get_portfolio())  # 100 → 50

    records = _records(path)
    decision_recs = [r for r in records if r["event_type"] == "decision"]
    assert len(decision_recs) == 1
    payload = decision_recs[0]["payload"]
    assert payload["decision"] == "resize"
    assert payload["requested_quantity"] == pytest.approx(100.0)
    assert payload["final_quantity"] == pytest.approx(50.0)
    assert payload["symbol"] == "AAPL"
    # 明细里应含缩量规则的结果记录。
    rule_names = {r["rule"] for r in payload["results"]}
    assert "max_position_limit" in rule_names


# --------------------------------------------------------------------------- #
# 熔断幂等:重复 update_equity 只落一条 breaker_trip
# --------------------------------------------------------------------------- #

def test_breaker_trip_recorded_once_even_if_observed_repeatedly(tmp_path):
    path = _audit_path(tmp_path)
    box, clock = _list_clock()
    broker = _new_broker()

    with JsonlAuditSink(path) as audit:
        engine = RiskEngine(
            broker=broker, audit=audit, sizer=FixedFractionalSizer(), clock=clock
        )
        engine.submit(Order("AAPL", Side.BUY, 100), broker.get_portfolio())
        engine.submit(Order("MSFT", Side.BUY, 100), broker.get_portfolio())
        box[0] = T0 + timedelta(days=1)
        broker.set_marks({"AAPL": 20.0, "MSFT": 40.0})

        # 连观测三次:第一次触发,后两次应保持已触发但不再重复落 breaker_trip。
        engine.update_equity(broker.get_portfolio())
        engine.update_equity(broker.get_portfolio())
        engine.update_equity(broker.get_portfolio())
        assert engine.breaker_tripped is True

    types = _event_types(path)
    assert types.count("breaker_trip") == 1


# --------------------------------------------------------------------------- #
# 无券商配置:check 可用,但 submit 放行后应报 BrokerError
# --------------------------------------------------------------------------- #

def test_submit_without_broker_raises(tmp_path):
    from riskguard import BrokerError

    box, clock = _list_clock()
    broker = _new_broker()  # 仅用于取组合快照,不挂到引擎上
    engine = RiskEngine(clock=clock)  # 无 broker、无 sizer

    portfolio = broker.get_portfolio()
    # check 仍可跑:一个 5% 的小买单应放行。
    decision = engine.check(Order("AAPL", Side.BUY, 25), portfolio)
    assert decision.approved is True
    # 但 submit 放行后要真正下单时,没有 broker → BrokerError。
    with pytest.raises(BrokerError):
        engine.submit(Order("AAPL", Side.BUY, 25), portfolio)


# --------------------------------------------------------------------------- #
# 自定义配置协同:更严的回撤线让熔断更早触发
# --------------------------------------------------------------------------- #

def test_tighter_drawdown_config_trips_earlier(tmp_path):
    """把回撤线收紧到 5%,较小的跌幅即触发熔断,验证配置真正驱动行为。"""
    box, clock = _list_clock()
    broker = _new_broker()
    config = RiskConfig(max_drawdown_pct=0.05)
    engine = RiskEngine(
        config=config, broker=broker, sizer=FixedFractionalSizer(), clock=clock
    )

    engine.submit(Order("AAPL", Side.BUY, 100), broker.get_portfolio())
    engine.submit(Order("MSFT", Side.BUY, 100), broker.get_portfolio())
    assert engine.state.high_water_mark == pytest.approx(100_000.0)

    # 权益 10 万,仅需跌 5000(5%)即熔断。把 AAPL 从 20000 名义砍掉一半:
    # 50 股从 200 → 100,权益 = 80000 + 50*100 + 25*400 = 95000 → 回撤 5%。
    box[0] = T0 + timedelta(days=1)
    broker.set_marks({"AAPL": 100.0})
    state = engine.update_equity(broker.get_portfolio())
    assert broker.get_account().equity == pytest.approx(95_000.0)
    assert state.drawdown == pytest.approx(0.05, abs=1e-6)
    assert state.breaker_tripped is True

    # 而同样的跌幅在默认 15% 线下不会熔断(对照)。
    broker2 = _new_broker()
    engine2 = RiskEngine(broker=broker2, sizer=FixedFractionalSizer(), clock=clock)
    engine2.submit(Order("AAPL", Side.BUY, 100), broker2.get_portfolio())
    engine2.submit(Order("MSFT", Side.BUY, 100), broker2.get_portfolio())
    broker2.set_marks({"AAPL": 100.0})
    state2 = engine2.update_equity(broker2.get_portfolio())
    assert state2.breaker_tripped is False
