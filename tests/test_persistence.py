"""状态持久化测试:核心承诺是"重启无法绕过熔断"。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from riskguard import (
    Order,
    PaperBroker,
    RiskConfig,
    RiskEngine,
    RiskState,
    Side,
    SqliteStateStore,
)
from riskguard.exceptions import PersistenceError


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# SqliteStateStore 本身:序列化往返
# --------------------------------------------------------------------------- #
def test_fresh_store_load_returns_none(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    assert store.load() is None
    store.close()


def test_round_trip_preserves_all_fields(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    now = _now()
    state = RiskState(
        high_water_mark=123_456.0,
        last_equity=100_000.0,
        breaker_tripped=True,
        tripped_at=now,
        trip_reason="drawdown 20.00% >= limit 15.00%",
        strategy_inception={"momentum": now, "mean_reversion": now},
    )
    store.save(state)
    restored = store.load()
    store.close()

    assert restored.high_water_mark == state.high_water_mark
    assert restored.last_equity == state.last_equity
    assert restored.breaker_tripped is True
    assert restored.trip_reason == state.trip_reason
    assert restored.tripped_at == now
    assert set(restored.strategy_inception) == {"momentum", "mean_reversion"}


def test_save_overwrites_not_appends(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    store.save(RiskState.initial(100_000.0))
    store.save(RiskState.initial(50_000.0))
    restored = store.load()
    store.close()
    assert restored.high_water_mark == 50_000.0  # 覆盖式,不是追加


def test_reopen_same_file_resumes(tmp_path):
    path = str(tmp_path / "s.db")
    s1 = SqliteStateStore(path)
    s1.save(RiskState(high_water_mark=1000.0, last_equity=1000.0, breaker_tripped=True))
    s1.close()

    s2 = SqliteStateStore(path)  # 模拟重开进程,连到同一文件
    restored = s2.load()
    s2.close()
    assert restored.breaker_tripped is True
    assert restored.high_water_mark == 1000.0


def test_different_keys_isolated(tmp_path):
    path = str(tmp_path / "s.db")
    a = SqliteStateStore(path, key="strategy_a")
    b = SqliteStateStore(path, key="strategy_b")
    a.save(RiskState.initial(111.0))
    b.save(RiskState.initial(222.0))
    assert a.load().high_water_mark == 111.0
    assert b.load().high_water_mark == 222.0
    a.close()
    b.close()


def test_corrupted_payload_raises_persistence_error(tmp_path):
    path = str(tmp_path / "s.db")
    store = SqliteStateStore(path)
    store.save(RiskState.initial(100.0))
    store.close()

    # 直接改坏底层数据(模拟存档损坏)
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("UPDATE risk_state SET payload = 'not json at all'")
    conn.commit()
    conn.close()

    store2 = SqliteStateStore(path)
    with pytest.raises(PersistenceError):
        store2.load()
    store2.close()


def test_missing_field_raises_persistence_error(tmp_path):
    path = str(tmp_path / "s.db")
    store = SqliteStateStore(path)
    store.save(RiskState.initial(100.0))
    store.close()

    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("UPDATE risk_state SET payload = '{\"only_this_key\": 1}'")
    conn.commit()
    conn.close()

    store2 = SqliteStateStore(path)
    with pytest.raises(PersistenceError):
        store2.load()
    store2.close()


# --------------------------------------------------------------------------- #
# 核心承诺:重启无法绕过熔断
# --------------------------------------------------------------------------- #
def test_restart_preserves_tripped_breaker(tmp_path):
    path = str(tmp_path / "s.db")

    store1 = SqliteStateStore(path)
    broker1 = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    engine1 = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15, max_position_pct=1.0),
        broker=broker1,
        state_store=store1,
    )
    engine1.update_equity(broker1.get_portfolio())
    broker1._cash = 80_000  # -20%,触发熔断
    engine1.update_equity(broker1.get_portfolio())
    assert engine1.breaker_tripped
    store1.close()

    # "重启":全新引擎、全新 broker,只是连到同一个 store
    store2 = SqliteStateStore(path)
    broker2 = PaperBroker(cash=80_000, marks={"AAPL": 100.0})
    engine2 = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15, max_position_pct=1.0),
        broker=broker2,
        state_store=store2,
    )
    assert engine2.breaker_tripped  # 重启后依然触发
    assert engine2.state.high_water_mark == 100_000.0

    # 新开仓仍被拒
    d = engine2.check(Order("AAPL", Side.BUY, 1), broker2.get_portfolio())
    assert d.rejected
    # 减仓仍放行
    d2 = engine2.check(Order("AAPL", Side.SELL, 1, reduce_only=True), broker2.get_portfolio())
    assert d2.approved
    store2.close()


def test_reset_breaker_persists_across_restart(tmp_path):
    path = str(tmp_path / "s.db")

    store1 = SqliteStateStore(path)
    engine1 = RiskEngine(RiskConfig(max_drawdown_pct=0.15), state_store=store1)
    from riskguard import Account, Portfolio

    engine1.update_equity(Portfolio(Account(equity=100_000)))
    engine1.update_equity(Portfolio(Account(equity=80_000)))
    assert engine1.breaker_tripped
    engine1.reset_breaker()  # 人工复盘后重置
    assert not engine1.breaker_tripped
    store1.close()

    store2 = SqliteStateStore(path)
    engine2 = RiskEngine(RiskConfig(max_drawdown_pct=0.15), state_store=store2)
    assert not engine2.breaker_tripped  # 重置后的状态也持久化了,不会"复活"
    store2.close()


def test_strategy_quarantine_survives_restart(tmp_path):
    path = str(tmp_path / "s.db")

    store1 = SqliteStateStore(path)
    engine1 = RiskEngine(RiskConfig(quarantine_days=90), state_store=store1)
    engine1.register_strategy("newstrat")
    age1 = engine1.state.strategy_age_days("newstrat", _now())
    store1.close()

    store2 = SqliteStateStore(path)
    engine2 = RiskEngine(RiskConfig(quarantine_days=90), state_store=store2)
    age2 = engine2.state.strategy_age_days("newstrat", _now())
    assert age2 is not None
    assert abs(age2 - age1) < 1.0  # 入役时间没有因重启而重新计算
    store2.close()


# --------------------------------------------------------------------------- #
# 优先级:存档 > 显式 state 参数 > 全新初始状态
# --------------------------------------------------------------------------- #
def test_persisted_state_overrides_explicit_state_param(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    store.save(RiskState(high_water_mark=999.0, last_equity=999.0, breaker_tripped=True))

    engine = RiskEngine(
        RiskConfig(), state=RiskState.initial(1.0), state_store=store
    )
    assert engine.state.high_water_mark == 999.0  # 存档优先于显式 state
    assert engine.breaker_tripped
    store.close()


def test_explicit_state_used_when_store_empty(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))  # 空存档
    seed = RiskState.initial(5000.0)
    engine = RiskEngine(RiskConfig(), state=seed, state_store=store)
    assert engine.state.high_water_mark == 5000.0
    store.close()


# --------------------------------------------------------------------------- #
# 写失败不阻断风控裁决
# --------------------------------------------------------------------------- #
def test_save_failure_does_not_block_decision(tmp_path):
    class BoomStore:
        def load(self):
            return None

        def save(self, state):
            raise OSError("disk full")

    errors = []
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    engine = RiskEngine(
        RiskConfig(max_position_pct=0.10),
        broker=broker,
        state_store=BoomStore(),
        on_persist_error=errors.append,
    )
    # 构造期(存档为空,尝试落盘一次)已经触发过一次 on_persist_error
    assert len(errors) == 1 and isinstance(errors[0], OSError)

    d = engine.check(Order("AAPL", Side.BUY, 1000), broker.get_portfolio())
    assert d.resized  # 裁决照常返回,即便持久化持续失败
    assert len(errors) == 2 and all(isinstance(e, OSError) for e in errors)


def test_load_failure_propagates_at_construction(tmp_path):
    class BoomLoadStore:
        def load(self):
            raise PersistenceError("corrupted")

        def save(self, state):
            pass

    with pytest.raises(PersistenceError):
        RiskEngine(RiskConfig(), state_store=BoomLoadStore())


# --------------------------------------------------------------------------- #
# 并发写入不损坏数据
# --------------------------------------------------------------------------- #
def test_concurrent_saves_do_not_corrupt(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))

    def hammer(i):
        store.save(RiskState.initial(float(i)))

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    restored = store.load()
    assert restored is not None
    assert isinstance(restored.high_water_mark, float)
    store.close()


# --------------------------------------------------------------------------- #
# 第二轮审查:乐观锁 CAS —— 拒绝静默覆盖,而不是让第二个写者悄悄清空第一个
# --------------------------------------------------------------------------- #
def test_cas_rejects_second_writer_never_loaded(tmp_path):
    """两个 store 指向同一文件同一 key,谁都没 load() 就直接 save()——
    第一个成功,第二个必须冲突失败(不能"偷看当前版本当基线"蒙混过关)。"""
    path = str(tmp_path / "s.db")
    a = SqliteStateStore(path)
    b = SqliteStateStore(path)

    a.save(RiskState.initial(100.0))
    with pytest.raises(PersistenceError):
        b.save(RiskState.initial(200.0))

    # A 的写入必须原样保留,没被 B 覆盖
    assert a.load().high_water_mark == 100.0
    a.close()
    b.close()


def test_cas_rejects_stale_writer_after_load(tmp_path):
    """两个 store 都先 load() 过(拿到同一基线),A 写入后 B 用旧版本号再写 ->
    必须冲突失败,A 的写入不能被 B 的旧快照覆盖。"""
    path = str(tmp_path / "s.db")
    a = SqliteStateStore(path)
    b = SqliteStateStore(path)
    a.load()  # 空存档,版本 0
    b.load()  # 同样看到空存档,版本 0

    a.save(RiskState.initial(111.0))  # A: 0 -> 1
    with pytest.raises(PersistenceError):
        b.save(RiskState.initial(222.0))  # B 仍以为自己在版本 0,冲突

    assert a.load().high_water_mark == 111.0  # A 的写入原样保留
    a.close()
    b.close()


def test_two_engines_sharing_key_do_not_silently_clobber_tripped_breaker(tmp_path):
    """对应审查发现的 critical:两个独立 RiskEngine 共用同一 store/key,engineA
    触发熔断并落盘后,engineB 用旧快照写入必须冲突失败,而不是把 A 的熔断状态
    静默抹掉。这是"重启绕过熔断"以"并发写者"形式借尸还魂的那个场景。"""
    from riskguard import Account, Portfolio

    path = str(tmp_path / "s.db")
    store_a = SqliteStateStore(path)
    engine_a = RiskEngine(RiskConfig(max_drawdown_pct=0.15), state_store=store_a)
    store_b = SqliteStateStore(path)
    engine_b = RiskEngine(RiskConfig(max_drawdown_pct=0.15), state_store=store_b)

    engine_a.update_equity(Portfolio(Account(equity=100_000)))
    engine_a.update_equity(Portfolio(Account(equity=80_000)))  # -20% -> 熔断
    assert engine_a.breaker_tripped

    errors = []
    engine_b.on_persist_error = errors.append
    engine_b.register_strategy("some_strategy")  # B 用过期版本写 -> 应该冲突
    assert len(errors) == 1
    assert isinstance(errors[0], PersistenceError)

    # 关键断言:磁盘上的熔断状态必须还是 True,没被 B 静默覆盖成 False
    verifier = SqliteStateStore(path)
    disk_state = verifier.load()
    assert disk_state.breaker_tripped is True
    verifier.close()
    store_a.close()
    store_b.close()


def test_different_keys_never_conflict(tmp_path):
    """不同 key 天然隔离,不该触发 CAS 冲突——这是正确用法,必须畅通无阻。"""
    path = str(tmp_path / "s.db")
    a = SqliteStateStore(path, key="strategy_a")
    b = SqliteStateStore(path, key="strategy_b")
    a.save(RiskState.initial(1.0))
    b.save(RiskState.initial(2.0))  # 不同 key,不冲突
    a.save(RiskState.initial(1.5))  # 各自还能继续正常写
    b.save(RiskState.initial(2.5))
    assert a.load().high_water_mark == 1.5
    assert b.load().high_water_mark == 2.5
    a.close()
    b.close()


# --------------------------------------------------------------------------- #
# 第二轮审查:非有限权益(NaN/inf)在写入和读取两端都必须被拒绝
# --------------------------------------------------------------------------- #
def test_save_rejects_non_finite_high_water_mark(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    bad = RiskState(high_water_mark=float("nan"), last_equity=100.0)
    with pytest.raises(PersistenceError):
        store.save(bad)
    store.close()


def test_save_rejects_infinite_last_equity(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    bad = RiskState(high_water_mark=100.0, last_equity=float("inf"))
    with pytest.raises(PersistenceError):
        store.save(bad)
    store.close()


def test_load_rejects_non_finite_payload_smuggled_in(tmp_path):
    """即便有人绕过 save() 直接往库里塞 NaN(比如手改数据),load() 也必须拒绝,
    不能让它蒙混过关变成"回撤恒为 0、熔断永久失效"的定时炸弹。"""
    import sqlite3

    path = str(tmp_path / "s.db")
    store = SqliteStateStore(path)
    store.save(RiskState.initial(100.0))
    store.close()

    conn = sqlite3.connect(path)
    conn.execute(
        'UPDATE risk_state SET payload = \'{"high_water_mark": NaN, "last_equity": 1.0, '
        '"breaker_tripped": false, "tripped_at": null, "trip_reason": "", '
        '"strategy_inception": {}}\''
    )
    conn.commit()
    conn.close()

    store2 = SqliteStateStore(path)
    with pytest.raises(PersistenceError):
        store2.load()
    store2.close()


# --------------------------------------------------------------------------- #
# 第二轮审查:reset_breaker() 先落盘、成功后才切内存(fail-closed)
# --------------------------------------------------------------------------- #
def test_reset_breaker_persist_failure_keeps_breaker_tripped():
    """持久化失败时,reset_breaker() 必须直接抛出、且**不切换内存状态**——
    熔断继续保持,而不是"看起来重置成功了"但存档没跟上。"""
    from riskguard import Account, Portfolio

    class BoomOnResetStore:
        def load(self):
            return None

        def save(self, state):
            if not state.breaker_tripped:  # 只在"重置"这次写入时故意失败
                raise OSError("disk full during reset")

    engine = RiskEngine(RiskConfig(max_drawdown_pct=0.15), state_store=BoomOnResetStore())
    engine.update_equity(Portfolio(Account(equity=100_000)))
    engine.update_equity(Portfolio(Account(equity=80_000)))
    assert engine.breaker_tripped

    with pytest.raises(OSError):
        engine.reset_breaker()

    # 关键:reset 抛出后,熔断必须依然保持触发,不能"假装重置成功"
    assert engine.breaker_tripped


def test_reset_breaker_persist_failure_not_routed_to_on_persist_error():
    """reset_breaker() 的持久化失败走的是直接抛出,不是 on_persist_error 回调
    ——这是刻意的不对称(见 persistence.py 文档),回调不该被调用。"""
    from riskguard import Account, Portfolio

    class BoomStore:
        def load(self):
            return None

        def save(self, state):
            raise OSError("boom")

    errors = []
    engine = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15),
        state_store=BoomStore(),
        on_persist_error=errors.append,
    )
    errors.clear()  # 清掉构造期落盘失败的那一条,只看 reset_breaker 的行为
    engine.update_equity(Portfolio(Account(equity=100_000)))
    engine.update_equity(Portfolio(Account(equity=80_000)))
    errors.clear()

    with pytest.raises(OSError):
        engine.reset_breaker()
    assert errors == []  # 没有转交 on_persist_error


def test_reset_breaker_succeeds_normally_when_store_healthy(tmp_path):
    from riskguard import Account, Portfolio

    store = SqliteStateStore(str(tmp_path / "s.db"))
    engine = RiskEngine(RiskConfig(max_drawdown_pct=0.15), state_store=store)
    engine.update_equity(Portfolio(Account(equity=100_000)))
    engine.update_equity(Portfolio(Account(equity=80_000)))
    assert engine.breaker_tripped

    engine.reset_breaker()
    assert not engine.breaker_tripped

    # 落盘也确实写进去了
    verifier = SqliteStateStore(str(tmp_path / "s.db"))
    assert verifier.load().breaker_tripped is False
    verifier.close()
    store.close()


# --------------------------------------------------------------------------- #
# 第二轮审查:构造期立刻落盘一次(存档行不该等到第一次 check() 才出现)
# --------------------------------------------------------------------------- #
def test_fresh_engine_persists_immediately_on_construction(tmp_path):
    path = str(tmp_path / "s.db")
    store = SqliteStateStore(path)
    RiskEngine(RiskConfig(), state_store=store)  # 不调用任何方法

    verifier = SqliteStateStore(path)
    assert verifier.load() is not None  # 存档行已经存在,不用等第一次 check()
    verifier.close()
    store.close()
