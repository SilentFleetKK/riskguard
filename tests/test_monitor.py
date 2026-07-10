"""针对 :class:`riskguard.RiskMonitor` 实时监控守护进程的完整测试。

覆盖点(尽量走 ``_tick()`` 以保证确定性,不依赖真实计时):

* **熔断触发**:把权益驱动到回撤阈值以下 -> 一次 tick 触发熔断,
  ``on_trip`` 恰好回调**一次**(幂等:后续 tick 不再重复回调);
* **自动平仓**:``auto_liquidate=True`` 时,熔断后向 broker **直接**提交一笔
  ``reduce_only`` 的减仓单,持仓被打平(position goes flat);
* **不自动平仓**:``auto_liquidate=False`` 时只回调、不动 broker(持仓不变);
* **kill-switch 绕过风控**:平仓单直接打到 broker,即便引擎已熔断也能成交;
* **重新武装**:人工 ``reset_breaker`` 后 ``_handled_trip`` 复位,下次熔断能再次响应;
* **marks_provider**:每 tick 取一次外部标记价并透传给 ``broker.get_portfolio``;
* **审计**:引擎配了 audit 时,熔断落 ``breaker_trip``、平仓落 ``monitor`` 事件;
* **异常兜底**:一次 tick 抛异常被兜住并转交 ``on_error``,缺省则静默;
* **生命周期**:``start()/stop()`` 用 ``interval=0.01`` 不挂死,``is_running`` 正确翻转,
  ``start`` 幂等,上下文管理器可用;
* **边界**:未触发时不回调 / 空持仓平仓无碍 / 多头空头都能打平 / 未配 on_trip 也不崩。

设计约束:纯 pytest、无网络、确定性(需要时间时用可变列表时钟注入
:class:`RiskEngine`)。尽量从 ``riskguard`` 公共 API 导入。文件用 ``tmp_path``。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from riskguard import (
    Account,
    JsonlAuditSink,
    Order,
    PaperBroker,
    Portfolio,
    Position,
    RiskConfig,
    RiskEngine,
    RiskMonitor,
    Side,
)
from riskguard.brokers.base import Broker, BrokerOrder


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------
SYMBOL = "AAPL"
START_MARK = 100.0
START_CASH = 100_000.0

# 建立一个"高杠杆"仓位,让价格小幅回落即可把权益打穿回撤线:
# 买 900 股 @100 => 成本 90000,现金剩 10000,权益仍约 100000(高点)。
# 价格跌到 80 => 持仓市值 72000,权益 82000 => 回撤 18% > 默认 15% 阈值,熔断。
BIG_QTY = 900.0
CRASH_MARK = 80.0


class ListClock:
    """可变列表时钟:确定性时间源,可在测试中推进而无需真实 sleep。

    调用返回当前时刻;:meth:`advance` 往前推进,让每次观测/裁决落在
    不同(但可控)的时刻上。用一个单元素列表持有"现在",体现"可变列表时钟"。
    """

    def __init__(self, start: Optional[datetime] = None) -> None:
        self._now = [start or datetime(2026, 1, 1, tzinfo=timezone.utc)]

    def __call__(self) -> datetime:
        return self._now[0]

    def advance(self, **kwargs: float) -> None:
        self._now[0] = self._now[0] + timedelta(**kwargs)


def make_paper_broker(marks: Optional[dict] = None) -> PaperBroker:
    """默认现金 + 起始标记价的纸面券商。"""
    return PaperBroker(cash=START_CASH, marks=dict(marks or {SYMBOL: START_MARK}))


def make_engine(
    broker: Broker,
    *,
    clock: Optional[ListClock] = None,
    audit=None,
    max_drawdown_pct: float = 0.15,
) -> RiskEngine:
    """构造一个只关心回撤熔断的引擎,注入确定性时钟。"""
    cfg = RiskConfig(max_drawdown_pct=max_drawdown_pct)
    kwargs: dict = {"broker": broker, "audit": audit}
    if clock is not None:
        kwargs["clock"] = clock
    return RiskEngine(cfg, **kwargs)


def open_big_long(broker: PaperBroker, qty: float = BIG_QTY) -> None:
    """在 broker 上直接建一个大多头仓位(绕过引擎,纯粹造场景)。"""
    broker.submit_order(Order(symbol=SYMBOL, side=Side.BUY, quantity=qty))


def drive_to_trip(broker: PaperBroker, monitor: RiskMonitor) -> None:
    """先 tick 一次确立高点,再打崩价格并 tick 一次触发熔断。"""
    monitor._tick()  # 建立 high-water mark
    broker.set_mark(SYMBOL, CRASH_MARK)
    monitor._tick()  # 权益回撤到阈值以下 -> 熔断


# ---------------------------------------------------------------------------
# 熔断触发 + on_trip 回调
# ---------------------------------------------------------------------------
def test_tick_below_hwm_no_trip_no_callback() -> None:
    """价格不动、未触发回撤时,tick 不应触发熔断、不应回调 on_trip。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock())
    trips: list = []
    mon = RiskMonitor(engine, broker, on_trip=trips.append)

    mon._tick()  # 建立高点
    mon._tick()  # 价格没动,依旧在高点

    assert engine.breaker_tripped is False
    assert trips == []


def test_tick_trips_and_fires_on_trip_once() -> None:
    """驱动权益跌破阈值 -> 熔断触发,on_trip 恰好回调一次。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock())
    trips: list = []
    mon = RiskMonitor(engine, broker, on_trip=trips.append)

    drive_to_trip(broker, mon)

    assert engine.breaker_tripped is True
    assert len(trips) == 1
    # 回调收到的是更新后的 RiskState 快照
    state = trips[0]
    assert state.breaker_tripped is True
    assert state.drawdown >= engine.config.max_drawdown_pct
    assert state is engine.state


def test_on_trip_is_idempotent_across_ticks() -> None:
    """熔断只处理一次:后续多次 tick 不再重复回调 on_trip。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock())
    trips: list = []
    mon = RiskMonitor(engine, broker, on_trip=trips.append)

    drive_to_trip(broker, mon)
    assert len(trips) == 1

    # 再 tick 几次,熔断仍在但不该再回调。
    mon._tick()
    mon._tick()
    assert len(trips) == 1
    assert engine.breaker_tripped is True


def test_trip_without_on_trip_callback_does_not_raise() -> None:
    """未配置 on_trip(且不自动平仓)时,熔断 tick 也不应抛异常。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock())
    mon = RiskMonitor(engine, broker)  # 无 on_trip、无 auto_liquidate

    drive_to_trip(broker, mon)

    assert engine.breaker_tripped is True
    # 未自动平仓,持仓保持不变。
    assert broker.get_positions()[SYMBOL].quantity == BIG_QTY


# ---------------------------------------------------------------------------
# 自动平仓(kill-switch)
# ---------------------------------------------------------------------------
def test_auto_liquidate_flattens_long_position() -> None:
    """auto_liquidate=True:熔断后自动市价平仓,多头持仓被打平。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock())
    trips: list = []
    mon = RiskMonitor(engine, broker, auto_liquidate=True, on_trip=trips.append)

    assert broker.get_positions()[SYMBOL].quantity == BIG_QTY
    drive_to_trip(broker, mon)

    assert len(trips) == 1
    # 平仓后不再有该标的的非平仓头寸。
    assert SYMBOL not in broker.get_positions()
    assert broker.get_positions() == {}


def test_auto_liquidate_submits_reduce_only_sell_for_long() -> None:
    """平仓单应是 reduce_only 的卖单,并直接打到 broker(绕过风控闸门)。

    用一个包装 broker 记录 submit_order 收到的订单,验证它带 reduce_only 标记、
    方向为 SELL、数量等于原多头数量,且策略号为 risk_monitor。
    """
    inner = make_paper_broker()
    open_big_long(inner)
    recorder = RecordingBroker(inner)
    engine = make_engine(recorder, clock=ListClock())
    mon = RiskMonitor(engine, recorder, auto_liquidate=True)

    drive_to_trip(recorder, mon)

    # 找出监控发出的平仓单(策略号 risk_monitor)。
    liq_orders = [o for o in recorder.submitted if o.strategy_id == "risk_monitor"]
    assert len(liq_orders) == 1
    liq = liq_orders[0]
    assert liq.reduce_only is True
    assert liq.side is Side.SELL
    assert liq.quantity == BIG_QTY
    assert liq.symbol == SYMBOL
    # 持仓已打平。
    assert inner.get_positions() == {}


def test_auto_liquidate_flattens_short_position() -> None:
    """空头也应被打平:平仓单方向为 BUY。"""
    broker = make_paper_broker()
    # 直接卖出建立空头 -900 股。
    broker.submit_order(Order(symbol=SYMBOL, side=Side.SELL, quantity=BIG_QTY))
    assert broker.get_positions()[SYMBOL].is_short

    engine = make_engine(broker, clock=ListClock())
    mon = RiskMonitor(engine, broker, auto_liquidate=True)

    # 空头下,价格上涨才亏钱:先确立高点,再把价格拉高触发回撤。
    mon._tick()
    broker.set_mark(SYMBOL, 120.0)  # 空 900 股,涨到 120 => 亏 18000 => 回撤 18%
    mon._tick()

    assert engine.breaker_tripped is True
    assert broker.get_positions() == {}


def test_no_auto_liquidate_leaves_positions_untouched() -> None:
    """auto_liquidate=False:熔断只回调,不碰 broker,持仓保持原样。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock())
    trips: list = []
    mon = RiskMonitor(engine, broker, auto_liquidate=False, on_trip=trips.append)

    drive_to_trip(broker, mon)

    assert len(trips) == 1
    # 未平仓:仓位与数量不变。
    assert broker.get_positions()[SYMBOL].quantity == BIG_QTY


def test_auto_liquidate_with_no_positions_is_noop() -> None:
    """空持仓时熔断 + 自动平仓不应报错(无仓可平)。

    通过直接把账户权益驱动到回撤线以下来触发熔断,但账户没有持仓。
    """
    broker = ScriptedBroker(equities=[100_000.0, 80_000.0], positions={})
    engine = make_engine(broker, clock=ListClock())
    trips: list = []
    mon = RiskMonitor(engine, broker, auto_liquidate=True, on_trip=trips.append)

    mon._tick()  # equity 100000 -> 高点
    mon._tick()  # equity 80000 -> 回撤 20% -> 熔断

    assert engine.breaker_tripped is True
    assert len(trips) == 1
    # 无持仓 => broker 未收到任何平仓单。
    assert broker.submitted == []


# ---------------------------------------------------------------------------
# 重新武装(reset 后可再次触发)
# ---------------------------------------------------------------------------
def test_rearm_after_reset_breaker() -> None:
    """人工 reset_breaker 后 _handled_trip 复位,下次熔断能再次回调。"""
    broker = make_paper_broker()
    open_big_long(broker)
    clock = ListClock()
    engine = make_engine(broker, clock=clock)
    trips: list = []
    mon = RiskMonitor(engine, broker, on_trip=trips.append)

    drive_to_trip(broker, mon)
    assert len(trips) == 1
    assert engine.breaker_tripped is True

    # 人工复盘:reset 并把价格恢复到高点位置,tick 一次让守护进程重新武装。
    engine.reset_breaker()
    broker.set_mark(SYMBOL, START_MARK)
    mon._tick()
    assert engine.breaker_tripped is False

    # 再次打崩 -> 应再触发一次回调。
    broker.set_mark(SYMBOL, CRASH_MARK)
    mon._tick()
    assert engine.breaker_tripped is True
    assert len(trips) == 2


# ---------------------------------------------------------------------------
# marks_provider 透传
# ---------------------------------------------------------------------------
def test_marks_provider_called_each_tick_and_forwarded() -> None:
    """配置 marks_provider 时:每 tick 调一次,且返回的价被透传给
    ``broker.get_portfolio(marks)`` 用于估值,进而驱动熔断。

    用 :class:`MarkDrivenBroker`——它的权益直接由传入的 marks 决定,因此能确证
    provider 的返回值真正被用上(纸面券商的权益取自其内部行情,无法证明这点)。
    """
    calls = {"n": 0}
    forwarded: list = []
    # 第一次给高点价,之后给崩盘价,验证 provider 的返回值真正被用上。
    prices = [START_MARK, CRASH_MARK, CRASH_MARK]

    def provider() -> dict:
        idx = min(calls["n"], len(prices) - 1)
        calls["n"] += 1
        return {SYMBOL: prices[idx]}

    broker = MarkDrivenBroker(qty=BIG_QTY, cash=10_000.0, seen=forwarded)
    engine = make_engine(broker, clock=ListClock())
    mon = RiskMonitor(engine, broker, marks_provider=provider)

    mon._tick()  # provider -> 100 => 权益 100000,高点
    assert engine.breaker_tripped is False
    mon._tick()  # provider -> 80 => 权益 82000,回撤 18%,熔断
    assert engine.breaker_tripped is True

    # provider 每 tick 恰好调一次,且返回值原样透传给了 get_portfolio。
    assert calls["n"] == 2
    assert forwarded == [{SYMBOL: START_MARK}, {SYMBOL: CRASH_MARK}]


def test_no_marks_provider_passes_none_to_broker() -> None:
    """未配 marks_provider 时,传给 broker.get_portfolio 的 marks 应为 None。"""
    forwarded: list = []
    broker = MarkDrivenBroker(qty=BIG_QTY, cash=10_000.0, seen=forwarded, default_mark=START_MARK)
    engine = make_engine(broker, clock=ListClock())
    mon = RiskMonitor(engine, broker)  # 无 marks_provider

    mon._tick()

    assert forwarded == [None]


# ---------------------------------------------------------------------------
# 审计落库
# ---------------------------------------------------------------------------
def test_audit_records_breaker_trip_and_liquidate(tmp_path) -> None:
    """引擎配了 audit 时,熔断落 breaker_trip、平仓落 monitor(liquidate)事件。"""
    import json

    path = tmp_path / "audit.jsonl"
    audit = JsonlAuditSink(str(path))
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock(), audit=audit)
    mon = RiskMonitor(engine, broker, auto_liquidate=True)

    drive_to_trip(broker, mon)
    audit.close()

    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    types = [r["event_type"] for r in records]
    assert "breaker_trip" in types

    monitor_events = [r["payload"] for r in records if r["event_type"] == "monitor"]
    assert len(monitor_events) == 1
    payload = monitor_events[0]
    assert payload["action"] == "liquidate"
    assert payload["symbol"] == SYMBOL
    assert payload["side"] == Side.SELL.value
    assert payload["quantity"] == BIG_QTY
    assert payload["status"] == "filled"
    # 哈希链完好。
    assert JsonlAuditSink.verify(str(path)) is True


def test_no_audit_configured_does_not_break_liquidation() -> None:
    """引擎未配 audit 时,平仓照常进行,不应因缺审计而崩。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock(), audit=None)
    mon = RiskMonitor(engine, broker, auto_liquidate=True)

    drive_to_trip(broker, mon)

    assert engine.breaker_tripped is True
    assert broker.get_positions() == {}


# ---------------------------------------------------------------------------
# 异常兜底:on_error / 单头寸隔离
# ---------------------------------------------------------------------------
def test_tick_exception_propagates_when_called_directly() -> None:
    """直接调用 _tick() 时异常会外抛(便于测试);_loop 才负责兜住。"""
    broker = ExplodingBroker()
    engine = make_engine(make_paper_broker(), clock=ListClock())
    mon = RiskMonitor(engine, broker)

    with pytest.raises(RuntimeError, match="boom"):
        mon._tick()


def test_liquidate_per_position_error_is_isolated() -> None:
    """某个头寸平仓失败,不应中断其余头寸的平仓,且错误转交 on_error。

    用一个只对特定标的抛错的 broker:两个持仓,一个平仓抛错,另一个应成功平掉。
    """
    positions = {
        "AAA": Position(symbol="AAA", quantity=100.0, avg_price=100.0),
        "BBB": Position(symbol="BBB", quantity=100.0, avg_price=100.0),
    }
    broker = SelectiveFailBroker(
        equities=[100_000.0, 80_000.0],
        positions=positions,
        fail_symbol="AAA",
    )
    engine = make_engine(make_paper_broker(), clock=ListClock())
    errors: list = []
    mon = RiskMonitor(engine, broker, auto_liquidate=True, on_error=errors.append)

    mon._tick()  # 高点
    mon._tick()  # 熔断 + 平仓

    assert engine.breaker_tripped is True
    # AAA 平仓抛错被隔离并转交 on_error;BBB 成功提交平仓单。
    assert any("AAA" in repr(e) or "fail" in repr(e) for e in errors)
    assert "BBB" in broker.submitted_symbols
    assert "AAA" in broker.submitted_symbols  # 也尝试过 AAA(只是抛错)


def test_get_open_orders_not_implemented_is_tolerated() -> None:
    """broker 不支持 get_open_orders 时,平仓流程跳过撤单、继续平掉持仓。"""
    positions = {SYMBOL: Position(symbol=SYMBOL, quantity=100.0, avg_price=100.0)}
    broker = ScriptedBroker(
        equities=[100_000.0, 80_000.0],
        positions=positions,
        open_orders_error=NotImplementedError("no open orders"),
    )
    engine = make_engine(make_paper_broker(), clock=ListClock())
    mon = RiskMonitor(engine, broker, auto_liquidate=True)

    mon._tick()
    mon._tick()

    assert engine.breaker_tripped is True
    # 尽管取挂单抛 NotImplementedError,持仓仍被平掉。
    assert SYMBOL in broker.submitted_symbols


# ---------------------------------------------------------------------------
# 生命周期 start / stop / is_running
# ---------------------------------------------------------------------------
def test_start_stop_lifecycle_toggles_is_running() -> None:
    """start()/stop() 用 interval=0.01 不挂死,is_running 正确翻转。"""
    broker = make_paper_broker()
    engine = make_engine(broker, clock=ListClock())
    mon = RiskMonitor(engine, broker, interval=0.01)

    assert mon.is_running is False
    ret = mon.start()
    assert ret is mon  # start 返回自身,便于链式调用
    # 给线程一点时间起来。
    _wait_until(lambda: mon.is_running, timeout=2.0)
    assert mon.is_running is True

    mon.stop(timeout=2.0)
    _wait_until(lambda: not mon.is_running, timeout=2.0)
    assert mon.is_running is False


def test_start_is_idempotent() -> None:
    """重复 start() 不新建线程,is_running 恒为 True,返回同一实例。"""
    broker = make_paper_broker()
    engine = make_engine(broker, clock=ListClock())
    mon = RiskMonitor(engine, broker, interval=0.01)

    try:
        mon.start()
        _wait_until(lambda: mon.is_running, timeout=2.0)
        first_thread = mon._thread
        mon.start()  # 幂等
        assert mon._thread is first_thread
        assert mon.is_running is True
    finally:
        mon.stop(timeout=2.0)


def test_stop_before_start_is_safe() -> None:
    """从未 start 就 stop 不应抛异常。"""
    broker = make_paper_broker()
    engine = make_engine(broker, clock=ListClock())
    mon = RiskMonitor(engine, broker, interval=0.01)

    mon.stop(timeout=1.0)  # 不应抛
    assert mon.is_running is False


def test_running_loop_actually_ticks_and_trips() -> None:
    """真正跑起后台线程,让它在 tick 中把权益打穿并触发熔断(计时鲁棒)。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock())
    trips: list = []
    mon = RiskMonitor(engine, broker, interval=0.01, on_trip=trips.append)

    try:
        mon.start()
        # 先让它 tick 建立高点。
        _wait_until(lambda: engine.state.high_water_mark > 0, timeout=2.0)
        # 打崩价格,后台循环应在若干个周期内触发熔断。
        broker.set_mark(SYMBOL, CRASH_MARK)
        _wait_until(lambda: engine.breaker_tripped, timeout=2.0)
    finally:
        mon.stop(timeout=2.0)

    assert engine.breaker_tripped is True
    assert len(trips) == 1
    assert mon.is_running is False


def test_context_manager_starts_and_stops() -> None:
    """上下文管理器进入即 start、退出即 stop。"""
    broker = make_paper_broker()
    engine = make_engine(broker, clock=ListClock())
    mon = RiskMonitor(engine, broker, interval=0.01)

    with mon as m:
        assert m is mon
        _wait_until(lambda: mon.is_running, timeout=2.0)
        assert mon.is_running is True

    _wait_until(lambda: not mon.is_running, timeout=2.0)
    assert mon.is_running is False


def test_loop_swallows_tick_error_and_forwards_to_on_error() -> None:
    """后台循环里一次 tick 抛异常被兜住并转交 on_error,线程继续存活。"""
    broker = ExplodingBroker()
    engine = make_engine(make_paper_broker(), clock=ListClock())
    errors: list = []
    mon = RiskMonitor(engine, broker, interval=0.01, on_error=errors.append)

    try:
        mon.start()
        _wait_until(lambda: len(errors) >= 1, timeout=2.0)
        # 线程没被异常打死,仍在运行。
        assert mon.is_running is True
    finally:
        mon.stop(timeout=2.0)

    assert len(errors) >= 1
    assert isinstance(errors[0], RuntimeError)


def test_loop_without_on_error_survives_tick_exception() -> None:
    """未配 on_error 时,tick 异常被静默兜住,线程照样活着不挂死。"""
    broker = ExplodingBroker()
    engine = make_engine(make_paper_broker(), clock=ListClock())
    mon = RiskMonitor(engine, broker, interval=0.01)  # 无 on_error

    try:
        mon.start()
        _wait_until(lambda: broker.calls >= 2, timeout=2.0)
        # 反复抛异常也没让线程死。
        assert mon.is_running is True
    finally:
        mon.stop(timeout=2.0)

    assert broker.calls >= 2


# ---------------------------------------------------------------------------
# 测试替身(broker doubles)
# ---------------------------------------------------------------------------
def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.005) -> None:
    """轮询等待谓词为真;超时即返回(由后续断言给出清晰失败信息)。

    比固定 sleep 更鲁棒:一旦条件满足立即返回,不做多余等待。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)


class RecordingBroker(Broker):
    """包装一个真实 broker,记录所有经 submit_order 提交的订单。"""

    name = "recording"

    def __init__(self, inner: Broker) -> None:
        self._inner = inner
        self.submitted: list[Order] = []

    def submit_order(self, order: Order) -> BrokerOrder:
        self.submitted.append(order)
        return self._inner.submit_order(order)

    def cancel_order(self, broker_order_id: str) -> None:
        self._inner.cancel_order(broker_order_id)

    def get_account(self) -> Account:
        return self._inner.get_account()

    def get_positions(self) -> dict[str, Position]:
        return self._inner.get_positions()

    def get_marks(self, symbols):
        return self._inner.get_marks(symbols)

    def get_portfolio(self, marks=None) -> Portfolio:
        return self._inner.get_portfolio(marks)

    def get_open_orders(self):
        return self._inner.get_open_orders()

    def set_mark(self, symbol: str, price: float) -> None:
        # 透传给内部 PaperBroker,方便测试驱动价格。
        self._inner.set_mark(symbol, price)  # type: ignore[attr-defined]


class ScriptedBroker(Broker):
    """按脚本返回账户权益序列的假 broker,便于精确驱动回撤。

    每次 :meth:`get_account` 取权益序列的下一个值(用尽后停在末值)。
    """

    name = "scripted"

    def __init__(
        self,
        *,
        equities: list[float],
        positions: dict[str, Position],
        open_orders_error: Optional[BaseException] = None,
    ) -> None:
        self._equities = list(equities)
        self._idx = 0
        self._positions = dict(positions)
        self._open_orders_error = open_orders_error
        self.submitted: list[Order] = []
        self.submitted_symbols: list[str] = []

    def _next_equity(self) -> float:
        eq = self._equities[min(self._idx, len(self._equities) - 1)]
        self._idx += 1
        return eq

    def submit_order(self, order: Order) -> BrokerOrder:
        self.submitted.append(order)
        self.submitted_symbols.append(order.symbol)
        # 提交即视作打平该头寸(从持仓中移除)。
        self._positions.pop(order.symbol, None)
        return BrokerOrder(broker_order_id=f"x-{len(self.submitted)}", order=order, status="filled")

    def cancel_order(self, broker_order_id: str) -> None:  # pragma: no cover - 未触及
        pass

    def get_account(self) -> Account:
        eq = self._next_equity()
        return Account(equity=eq, cash=eq)

    def get_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def get_open_orders(self):
        if self._open_orders_error is not None:
            raise self._open_orders_error
        return []

    def get_portfolio(self, marks=None) -> Portfolio:
        return Portfolio(account=self.get_account(), positions=self.get_positions(), marks={})


class SelectiveFailBroker(ScriptedBroker):
    """在 ScriptedBroker 基础上,对特定标的的平仓单抛错(验证隔离)。"""

    name = "selective_fail"

    def __init__(self, *, fail_symbol: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._fail_symbol = fail_symbol

    def submit_order(self, order: Order) -> BrokerOrder:
        self.submitted_symbols.append(order.symbol)
        if order.symbol == self._fail_symbol:
            raise RuntimeError(f"cannot liquidate {order.symbol}")
        self.submitted.append(order)
        self._positions.pop(order.symbol, None)
        return BrokerOrder(broker_order_id=f"x-{len(self.submitted)}", order=order, status="filled")


class MarkDrivenBroker(Broker):
    """权益由传入 marks 决定的假 broker,用于验证 marks_provider 透传。

    持有一个多头 ``qty`` 股的头寸;每次 :meth:`get_portfolio` 记录收到的 ``marks``
    参数,并用其中的价格给持仓估值得出权益(``cash + qty * mark``)。缺省价用于
    未提供 marks 时兜底。
    """

    name = "mark_driven"

    def __init__(
        self,
        *,
        qty: float,
        cash: float,
        seen: list,
        default_mark: float = START_MARK,
    ) -> None:
        self._qty = qty
        self._cash = cash
        self._seen = seen
        self._default_mark = default_mark
        self._position = Position(symbol=SYMBOL, quantity=qty, avg_price=START_MARK)

    def _mark_from(self, marks) -> float:
        if marks and SYMBOL in marks:
            return float(marks[SYMBOL])
        return self._default_mark

    def get_portfolio(self, marks=None) -> Portfolio:
        self._seen.append(marks)
        mark = self._mark_from(marks)
        equity = self._cash + self._qty * mark
        account = Account(equity=equity, cash=self._cash)
        return Portfolio(
            account=account,
            positions={SYMBOL: self._position},
            marks={SYMBOL: mark},
        )

    def submit_order(self, order: Order) -> BrokerOrder:  # pragma: no cover - 未触及
        return BrokerOrder(broker_order_id="md-1", order=order, status="filled")

    def cancel_order(self, broker_order_id: str) -> None:  # pragma: no cover - 未触及
        pass

    def get_account(self) -> Account:  # pragma: no cover - 未触及
        return Account(equity=self._cash + self._qty * self._default_mark, cash=self._cash)

    def get_positions(self) -> dict[str, Position]:  # pragma: no cover - 未触及
        return {SYMBOL: self._position}


class ExplodingBroker(Broker):
    """每次 get_portfolio 都抛异常,用于验证 tick/loop 的异常兜底。"""

    name = "exploding"

    def __init__(self) -> None:
        self.calls = 0

    def get_portfolio(self, marks=None) -> Portfolio:
        self.calls += 1
        raise RuntimeError("boom")

    def submit_order(self, order: Order) -> BrokerOrder:  # pragma: no cover - 未触及
        raise RuntimeError("boom")

    def cancel_order(self, broker_order_id: str) -> None:  # pragma: no cover - 未触及
        pass

    def get_account(self) -> Account:  # pragma: no cover - 未触及
        raise RuntimeError("boom")

    def get_positions(self) -> dict[str, Position]:  # pragma: no cover - 未触及
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# 第二轮审查:_handled_trip 从 engine.breaker_tripped 播种(重启后不重复响应)
# ---------------------------------------------------------------------------
def test_handled_trip_seeded_true_when_engine_already_tripped() -> None:
    """模拟"进程重启":引擎从存档恢复出已触发的熔断状态,新建的 monitor 不该
    把它当成一次全新的触发去重复回调/重复平仓——它把"已经是触发态"当成
    "已经被处理过"。"""
    broker = make_paper_broker()
    open_big_long(broker)
    # 先用一个"旧" monitor 把引擎带到熔断态(模拟上一个进程已经处理过一次)
    old_engine = make_engine(broker, clock=ListClock())
    old_monitor = RiskMonitor(old_engine, broker)
    drive_to_trip(broker, old_monitor)
    assert old_engine.breaker_tripped is True

    # "重启":同一个(已经处于熔断态的)引擎,构造一个全新的 monitor 实例
    trips: list = []
    new_monitor = RiskMonitor(old_engine, broker, on_trip=trips.append, auto_liquidate=True)
    assert new_monitor._handled_trip is True  # 播种为"已处理",不是硬编码 False

    new_monitor._tick()  # 第一个 tick:熔断仍是 True,但不该重新拉响
    assert trips == []  # 不重复回调 on_trip
    # 不重复平仓:仓位应保持"重启前"的样子(未被 new_monitor 二次强平)
    assert broker.get_positions().get(SYMBOL) is not None


def test_handled_trip_seeded_false_when_engine_not_tripped() -> None:
    """引擎当前未触发熔断时,新 monitor 照旧从 False 开始,不影响正常首次触发。"""
    broker = make_paper_broker()
    open_big_long(broker)
    engine = make_engine(broker, clock=ListClock())
    monitor = RiskMonitor(engine, broker)
    assert monitor._handled_trip is False

    trips: list = []
    monitor2 = RiskMonitor(engine, broker, on_trip=trips.append)
    drive_to_trip(broker, monitor2)
    assert len(trips) == 1  # 正常首次触发依然生效
