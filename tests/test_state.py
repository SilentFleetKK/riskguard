"""``RiskState`` 单元测试。

覆盖不可变风控状态快照的全部行为分支:

* ``observe_equity`` —— 高点只上不下、``last_equity`` 每次都刷新;
* ``drawdown`` 派生量 —— 高点非正时为 0,否则按 ``1 - last/hwm`` 且下限截到 0;
* ``trip`` —— 幂等(已触发原样返回),首次触发写入 reason/time;
* ``reset_breaker`` —— 清空熔断三元组并把高点归位到当前权益;
* ``register_strategy`` —— 已存在不覆盖(保留最早入役时间);
* ``strategy_age_days`` —— 未登记返回 None,否则按自然日计;
* ``initial`` —— 高点即初始权益;
* 冻结/不可变契约 —— 任何"更新"都返回新对象,原对象绝不变。

所有涉及时间的用例都显式注入 datetime,不依赖 wall clock;涉及引擎的用例用
可变列表时钟(``lambda: clock[0]``)注入 ``RiskEngine(clock=...)``,保证确定性。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from riskguard import (
    Account,
    Portfolio,
    RiskConfig,
    RiskEngine,
    RiskState,
)

# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #

T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _at(days: float = 0.0) -> datetime:
    """返回 T0 之后 ``days`` 天(可为小数)的时间戳。"""
    return T0 + timedelta(days=days)


def _portfolio(equity: float) -> Portfolio:
    """构造一个只关心权益的最简组合(无持仓、无标记价)。"""
    return Portfolio(account=Account(equity=equity))


def _list_clock(start: datetime) -> tuple[list[datetime], "callable"]:
    """返回 (可变时间盒, 读取器);改盒子里的值即可推进引擎时钟。"""
    box = [start]
    return box, (lambda: box[0])


# --------------------------------------------------------------------------- #
# initial()
# --------------------------------------------------------------------------- #

def test_initial_default_zero():
    st = RiskState.initial()
    assert st.high_water_mark == 0.0
    assert st.last_equity == 0.0
    assert st.breaker_tripped is False
    assert st.tripped_at is None
    assert st.trip_reason == ""
    assert dict(st.strategy_inception) == {}


def test_initial_sets_hwm_to_equity():
    st = RiskState.initial(equity=100_000.0)
    assert st.high_water_mark == 100_000.0
    assert st.last_equity == 100_000.0


def test_initial_ignores_now_argument():
    # now 参数被接受但不影响结果:传与不传等价。
    st_none = RiskState.initial(equity=50.0)
    st_now = RiskState.initial(equity=50.0, now=_at(10))
    assert st_none.high_water_mark == st_now.high_water_mark
    assert st_none.last_equity == st_now.last_equity
    assert st_now.tripped_at is None


def test_initial_negative_equity_kept():
    # 权益理论上可为负(穿仓),initial 不做拦截。
    st = RiskState.initial(equity=-500.0)
    assert st.high_water_mark == -500.0
    assert st.last_equity == -500.0


# --------------------------------------------------------------------------- #
# observe_equity —— 高点只上不下、last_equity 每次刷新
# --------------------------------------------------------------------------- #

def test_observe_equity_raises_hwm_on_new_high():
    st = RiskState.initial(equity=100.0)
    st2 = st.observe_equity(150.0, _at(1))
    assert st2.high_water_mark == 150.0
    assert st2.last_equity == 150.0


def test_observe_equity_does_not_lower_hwm_on_drop():
    st = RiskState.initial(equity=100.0)
    st2 = st.observe_equity(80.0, _at(1))
    # 高点保持不变,但 last_equity 跟随下探。
    assert st2.high_water_mark == 100.0
    assert st2.last_equity == 80.0


def test_observe_equity_equal_keeps_hwm():
    st = RiskState.initial(equity=100.0)
    st2 = st.observe_equity(100.0, _at(1))
    assert st2.high_water_mark == 100.0
    assert st2.last_equity == 100.0


def test_observe_equity_last_equity_always_updated_even_below_hwm():
    st = RiskState.initial(equity=200.0)
    st2 = st.observe_equity(120.0, _at(1))
    st3 = st2.observe_equity(90.0, _at(2))
    assert st3.high_water_mark == 200.0
    assert st3.last_equity == 90.0


def test_observe_equity_sequence_ratchet():
    # 上-下-上:高点只在超越历史时抬升。
    st = RiskState.initial(equity=100.0)
    st = st.observe_equity(130.0, _at(1))  # 抬到 130
    st = st.observe_equity(110.0, _at(2))  # 回落,不降高点
    assert st.high_water_mark == 130.0
    st = st.observe_equity(160.0, _at(3))  # 再创新高
    assert st.high_water_mark == 160.0
    assert st.last_equity == 160.0


def test_observe_equity_does_not_mutate_original():
    st = RiskState.initial(equity=100.0)
    st.observe_equity(500.0, _at(1))
    # 原对象不受影响。
    assert st.high_water_mark == 100.0
    assert st.last_equity == 100.0


def test_observe_equity_returns_new_object():
    st = RiskState.initial(equity=100.0)
    st2 = st.observe_equity(101.0, _at(1))
    assert st2 is not st


def test_observe_equity_does_not_set_breaker():
    # observe_equity 本身不评估熔断(那是引擎的职责),只更新权益。
    st = RiskState.initial(equity=100.0)
    st2 = st.observe_equity(10.0, _at(1))
    assert st2.breaker_tripped is False
    assert st2.tripped_at is None


# --------------------------------------------------------------------------- #
# drawdown 派生量
# --------------------------------------------------------------------------- #

def test_drawdown_zero_at_high_water_mark():
    st = RiskState.initial(equity=100.0)
    assert st.drawdown == 0.0


def test_drawdown_computed_from_hwm():
    st = RiskState.initial(equity=100.0).observe_equity(80.0, _at(1))
    assert st.drawdown == pytest.approx(0.20)


def test_drawdown_uses_hwm_not_start_equity():
    # 先冲高到 200 再回落到 150,回撤基准应是 200 而非初始 100。
    st = RiskState.initial(equity=100.0)
    st = st.observe_equity(200.0, _at(1))
    st = st.observe_equity(150.0, _at(2))
    assert st.drawdown == pytest.approx(0.25)


def test_drawdown_zero_when_hwm_nonpositive():
    st = RiskState.initial(equity=0.0)
    assert st.high_water_mark == 0.0
    assert st.drawdown == 0.0


def test_drawdown_zero_when_hwm_negative():
    # 高点 <= 0 直接短路返回 0,不做除法。
    st = RiskState.initial(equity=-100.0)
    assert st.drawdown == 0.0


def test_drawdown_never_negative_above_hwm():
    # last_equity 超过 hwm 理论上不该发生(observe 会抬高点),
    # 但 drawdown 的 max(0.0, ...) 下限须保证永不为负。
    st = RiskState(high_water_mark=100.0, last_equity=120.0)
    assert st.drawdown == 0.0


def test_drawdown_full_loss():
    st = RiskState.initial(equity=100.0).observe_equity(0.0, _at(1))
    assert st.drawdown == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# trip —— 幂等 + 写入 reason/time
# --------------------------------------------------------------------------- #

def test_trip_sets_flag_reason_and_time():
    st = RiskState.initial(equity=100.0)
    tripped = st.trip("drawdown breach", _at(5))
    assert tripped.breaker_tripped is True
    assert tripped.trip_reason == "drawdown breach"
    assert tripped.tripped_at == _at(5)


def test_trip_idempotent_returns_same_object():
    st = RiskState.initial(equity=100.0).trip("first", _at(1))
    again = st.trip("second", _at(2))
    # 已触发:原样返回同一对象,不覆盖 reason/time。
    assert again is st
    assert again.trip_reason == "first"
    assert again.tripped_at == _at(1)


def test_trip_does_not_mutate_original():
    st = RiskState.initial(equity=100.0)
    st.trip("boom", _at(1))
    assert st.breaker_tripped is False
    assert st.trip_reason == ""
    assert st.tripped_at is None


def test_trip_returns_new_object_on_first_trip():
    st = RiskState.initial(equity=100.0)
    tripped = st.trip("boom", _at(1))
    assert tripped is not st


def test_trip_preserves_equity_fields():
    st = RiskState.initial(equity=100.0).observe_equity(70.0, _at(1))
    tripped = st.trip("dd", _at(2))
    assert tripped.high_water_mark == 100.0
    assert tripped.last_equity == 70.0


# --------------------------------------------------------------------------- #
# reset_breaker —— 清空熔断三元组 + 高点归位到当前权益
# --------------------------------------------------------------------------- #

def test_reset_breaker_clears_flag_and_reason_and_time():
    st = RiskState.initial(equity=100.0).trip("dd", _at(1))
    reset = st.reset_breaker(_at(2))
    assert reset.breaker_tripped is False
    assert reset.trip_reason == ""
    assert reset.tripped_at is None


def test_reset_breaker_moves_hwm_to_last_equity():
    # 冲高 200、回落 150、触发熔断,复位后高点应归位到 150(当前权益),
    # 避免刚复位就因残留高点二次触发。
    st = RiskState.initial(equity=100.0)
    st = st.observe_equity(200.0, _at(1))
    st = st.observe_equity(150.0, _at(2))
    st = st.trip("dd", _at(3))
    assert st.high_water_mark == 200.0

    reset = st.reset_breaker(_at(4))
    assert reset.high_water_mark == 150.0
    assert reset.last_equity == 150.0
    assert reset.drawdown == 0.0


def test_reset_breaker_when_not_tripped_still_moves_hwm():
    # 未触发也可调用:仍会把高点拉回当前权益。
    st = RiskState.initial(equity=100.0).observe_equity(80.0, _at(1))
    reset = st.reset_breaker(_at(2))
    assert reset.breaker_tripped is False
    assert reset.high_water_mark == 80.0
    assert reset.last_equity == 80.0


def test_reset_breaker_does_not_mutate_original():
    st = RiskState.initial(equity=100.0).observe_equity(80.0, _at(1)).trip("dd", _at(2))
    st.reset_breaker(_at(3))
    # 原对象仍处于触发态、高点仍是 100。
    assert st.breaker_tripped is True
    assert st.high_water_mark == 100.0


def test_reset_breaker_returns_new_object():
    st = RiskState.initial(equity=100.0).trip("dd", _at(1))
    reset = st.reset_breaker(_at(2))
    assert reset is not st


# --------------------------------------------------------------------------- #
# register_strategy —— 不覆盖最早入役时间
# --------------------------------------------------------------------------- #

def test_register_strategy_records_inception():
    st = RiskState.initial(equity=100.0).register_strategy("alpha", _at(0))
    assert st.strategy_inception["alpha"] == _at(0)


def test_register_strategy_does_not_overwrite_earliest():
    st = RiskState.initial(equity=100.0)
    st = st.register_strategy("alpha", _at(0))
    st2 = st.register_strategy("alpha", _at(10))
    # 二次登记不改时间:保留最早的 _at(0)。
    assert st2.strategy_inception["alpha"] == _at(0)


def test_register_strategy_second_call_returns_same_object():
    st = RiskState.initial(equity=100.0).register_strategy("alpha", _at(0))
    again = st.register_strategy("alpha", _at(5))
    assert again is st


def test_register_strategy_multiple_distinct():
    st = RiskState.initial(equity=100.0)
    st = st.register_strategy("alpha", _at(0))
    st = st.register_strategy("beta", _at(3))
    assert st.strategy_inception["alpha"] == _at(0)
    assert st.strategy_inception["beta"] == _at(3)
    assert len(st.strategy_inception) == 2


def test_register_strategy_does_not_mutate_original():
    st = RiskState.initial(equity=100.0)
    st.register_strategy("alpha", _at(0))
    # 原对象的 inception 映射仍为空。
    assert "alpha" not in st.strategy_inception
    assert dict(st.strategy_inception) == {}


def test_register_strategy_returns_new_object_on_first():
    st = RiskState.initial(equity=100.0)
    st2 = st.register_strategy("alpha", _at(0))
    assert st2 is not st


def test_register_strategy_inception_mapping_is_read_only():
    # inception 映射是只读视图,外部无法偷改。
    st = RiskState.initial(equity=100.0).register_strategy("alpha", _at(0))
    with pytest.raises(TypeError):
        st.strategy_inception["beta"] = _at(1)  # type: ignore[index]


# --------------------------------------------------------------------------- #
# strategy_age_days —— 未登记 None,否则按自然日
# --------------------------------------------------------------------------- #

def test_strategy_age_days_none_when_unregistered():
    st = RiskState.initial(equity=100.0)
    assert st.strategy_age_days("ghost", _at(30)) is None


def test_strategy_age_days_none_for_other_strategy():
    st = RiskState.initial(equity=100.0).register_strategy("alpha", _at(0))
    assert st.strategy_age_days("beta", _at(30)) is None


def test_strategy_age_days_whole_days():
    st = RiskState.initial(equity=100.0).register_strategy("alpha", _at(0))
    assert st.strategy_age_days("alpha", _at(30)) == pytest.approx(30.0)


def test_strategy_age_days_fractional():
    st = RiskState.initial(equity=100.0).register_strategy("alpha", _at(0))
    # 半天 = 0.5。
    assert st.strategy_age_days("alpha", _at(0.5)) == pytest.approx(0.5)


def test_strategy_age_days_zero_at_inception():
    st = RiskState.initial(equity=100.0).register_strategy("alpha", _at(0))
    assert st.strategy_age_days("alpha", _at(0)) == pytest.approx(0.0)


def test_strategy_age_days_negative_when_now_before_inception():
    # now 早于入役(时钟回拨/异常输入)时得到负数,不抛异常。
    st = RiskState.initial(equity=100.0).register_strategy("alpha", _at(10))
    assert st.strategy_age_days("alpha", _at(9)) == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# 冻结契约 —— dataclass 不可变
# --------------------------------------------------------------------------- #

def test_state_is_frozen():
    st = RiskState.initial(equity=100.0)
    with pytest.raises((AttributeError, TypeError)):
        st.last_equity = 999.0  # type: ignore[misc]


def test_state_hwm_frozen():
    st = RiskState.initial(equity=100.0)
    with pytest.raises((AttributeError, TypeError)):
        st.high_water_mark = 0.0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 与 RiskEngine 的集成:确认状态语义在引擎层如实体现(注入确定性时钟)
# --------------------------------------------------------------------------- #

def test_engine_update_equity_ratchets_hwm():
    box, clock = _list_clock(T0)
    engine = RiskEngine(RiskConfig(max_drawdown_pct=0.15), clock=clock)

    engine.update_equity(_portfolio(100.0))
    assert engine.state.high_water_mark == 100.0

    box[0] = _at(1)
    engine.update_equity(_portfolio(120.0))  # 新高
    assert engine.state.high_water_mark == 120.0

    box[0] = _at(2)
    engine.update_equity(_portfolio(115.0))  # 回落不降高点
    assert engine.state.high_water_mark == 120.0
    assert engine.state.last_equity == 115.0
    assert engine.state.breaker_tripped is False


def test_engine_trips_on_drawdown_breach():
    box, clock = _list_clock(T0)
    engine = RiskEngine(RiskConfig(max_drawdown_pct=0.15), clock=clock)
    engine.update_equity(_portfolio(100.0))

    box[0] = _at(1)
    st = engine.update_equity(_portfolio(80.0))  # 回撤 20% >= 15%
    assert st.breaker_tripped is True
    assert st.tripped_at == _at(1)
    assert "drawdown" in st.trip_reason


def test_engine_trip_is_idempotent_keeps_first_time():
    box, clock = _list_clock(T0)
    engine = RiskEngine(RiskConfig(max_drawdown_pct=0.15), clock=clock)
    engine.update_equity(_portfolio(100.0))

    box[0] = _at(1)
    engine.update_equity(_portfolio(80.0))  # 首次触发
    first_time = engine.state.tripped_at
    first_reason = engine.state.trip_reason

    box[0] = _at(2)
    engine.update_equity(_portfolio(70.0))  # 更深回撤,但已触发
    assert engine.state.tripped_at == first_time
    assert engine.state.trip_reason == first_reason


def test_engine_reset_breaker_regrounds_hwm_to_last_equity():
    box, clock = _list_clock(T0)
    engine = RiskEngine(RiskConfig(max_drawdown_pct=0.15), clock=clock)
    engine.update_equity(_portfolio(100.0))

    box[0] = _at(1)
    engine.update_equity(_portfolio(80.0))  # 触发,高点仍是 100
    assert engine.state.breaker_tripped is True

    box[0] = _at(2)
    st = engine.reset_breaker()
    assert st.breaker_tripped is False
    assert st.trip_reason == ""
    assert st.tripped_at is None
    # 高点归位到当前权益 80,回撤清零,不会立刻二次触发。
    assert st.high_water_mark == 80.0
    assert st.drawdown == 0.0


def test_engine_register_strategy_uses_injected_clock_and_no_overwrite():
    box, clock = _list_clock(T0)
    engine = RiskEngine(clock=clock)

    engine.register_strategy("alpha")
    assert engine.state.strategy_inception["alpha"] == T0

    box[0] = _at(10)
    engine.register_strategy("alpha")  # 二次登记不改最早时间
    assert engine.state.strategy_inception["alpha"] == T0


def test_engine_state_returns_immutable_snapshot():
    box, clock = _list_clock(T0)
    engine = RiskEngine(clock=clock)
    engine.update_equity(_portfolio(100.0))
    snap = engine.state
    with pytest.raises((AttributeError, TypeError)):
        snap.last_equity = 0.0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# AI 代理闸门:交易日会话 / 日内亏损线 / 订单节流状态
# --------------------------------------------------------------------------- #

def test_new_ai_gate_fields_default_empty():
    s = RiskState.initial(100_000.0)
    assert s.session_date is None
    assert s.session_anchor_equity == 0.0
    assert s.daily_tripped is False
    assert s.daily_tripped_at is None
    assert s.daily_trip_reason == ""
    assert s.recent_orders == ()


def test_session_key_for_boundary_midnight():
    from riskguard.state import session_key_for

    d0 = datetime(2024, 3, 5, 0, 0, tzinfo=timezone.utc)
    d1 = datetime(2024, 3, 5, 23, 59, tzinfo=timezone.utc)
    assert session_key_for(d0, "00:00") == "2024-03-05"
    assert session_key_for(d1, "00:00") == "2024-03-05"


def test_session_key_for_custom_boundary():
    """17:00 UTC 换日:16:59 仍属"昨天开始的会话",17:00 起属新会话。"""
    from riskguard.state import session_key_for

    before = datetime(2024, 3, 5, 16, 59, tzinfo=timezone.utc)
    after = datetime(2024, 3, 5, 17, 0, tzinfo=timezone.utc)
    assert session_key_for(before, "17:00") == "2024-03-04"
    assert session_key_for(after, "17:00") == "2024-03-05"


def test_roll_session_anchors_equity_and_clears_daily_trip():
    s = RiskState.initial(100_000.0).trip_daily("bad day", T0)
    assert s.daily_tripped
    rolled = s.roll_session("2024-01-02", 97_000.0)
    assert rolled.session_date == "2024-01-02"
    assert rolled.session_anchor_equity == 97_000.0
    assert rolled.daily_tripped is False
    assert rolled.daily_tripped_at is None
    assert rolled.daily_trip_reason == ""
    # 原对象不可变
    assert s.daily_tripped is True


def test_trip_daily_is_idempotent_keeps_first():
    s = RiskState.initial(100_000.0).trip_daily("first", T0)
    s2 = s.trip_daily("second", _at(0.1))
    assert s2.daily_trip_reason == "first"
    assert s2.daily_tripped_at == T0


def test_daily_loss_property():
    s = RiskState.initial(100_000.0).roll_session("2024-01-01", 100_000.0)
    s = s.observe_equity(97_000.0, T0)
    assert s.daily_loss == pytest.approx(0.03)
    # 盈利日不算亏损
    up = s.observe_equity(103_000.0, T0)
    assert up.daily_loss == 0.0
    # 未锚定(anchor<=0)时恒为 0
    assert RiskState.initial(0.0).daily_loss == 0.0


def test_record_order_appends_and_prunes():
    s = RiskState.initial(100_000.0)
    window = timedelta(hours=1)
    s = s.record_order(T0, False, keep_window=window, max_len=100)
    s = s.record_order(_at(0.01), True, keep_window=window, max_len=100)
    assert len(s.recent_orders) == 2
    # 两小时后再记一单:窗口外的旧记录被修剪
    s = s.record_order(_at(0.1), False, keep_window=window, max_len=100)
    assert len(s.recent_orders) == 1


def test_record_order_enforces_max_len():
    s = RiskState.initial(100_000.0)
    for i in range(10):
        s = s.record_order(T0 + timedelta(seconds=i), False,
                           keep_window=timedelta(days=1), max_len=5)
    assert len(s.recent_orders) == 5
    # 保留的是最新的 5 条
    assert s.recent_orders[0][0] == T0 + timedelta(seconds=5)


def test_orders_in_window_counts_by_bucket():
    s = RiskState.initial(100_000.0)
    kw = dict(keep_window=timedelta(hours=1), max_len=100)
    s = s.record_order(T0, False, **kw)
    s = s.record_order(T0 + timedelta(seconds=10), False, **kw)
    s = s.record_order(T0 + timedelta(seconds=20), True, **kw)
    now = T0 + timedelta(seconds=30)
    assert s.orders_in_window(now, timedelta(minutes=1)) == 3
    assert s.orders_in_window(now, timedelta(minutes=1), reduce_only=False) == 2
    assert s.orders_in_window(now, timedelta(minutes=1), reduce_only=True) == 1
    # 窗口滑走
    later = T0 + timedelta(minutes=5)
    assert s.orders_in_window(later, timedelta(minutes=1)) == 0


def test_reset_breaker_does_not_clear_daily_trip():
    """总熔断复位不清日内线:"今天到此为止"只随换日(或人工显式覆盖)解除。"""
    s = RiskState.initial(100_000.0).trip_daily("daily", T0).trip("total", T0)
    s2 = s.reset_breaker(_at(1))
    assert s2.breaker_tripped is False
    assert s2.daily_tripped is True


def test_session_key_for_treats_naive_datetime_as_utc():
    """注入 datetime.utcnow() 这类 naive 时钟时,绝不能按宿主机本地时区解释——
    那会让会话边界漂移、把已触发的日内熔断在换日之外静默清掉(fail-open)。
    库的其余部分对 naive datetime 都是"当 UTC 用",这里必须一致。"""
    from riskguard.state import session_key_for

    aware = datetime(2024, 3, 5, 23, 0, tzinfo=timezone.utc)
    naive = datetime(2024, 3, 5, 23, 0)  # 意图是 UTC,但没带 tzinfo
    assert session_key_for(naive, "00:00") == session_key_for(aware, "00:00")
    assert session_key_for(naive, "17:00") == session_key_for(aware, "17:00")
