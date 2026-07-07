"""``SqliteAuditSink`` 单元测试。

覆盖基于 SQLite 的哈希链审计后端的全部关键分支:

* ``record`` —— 事件逐条落表、哈希链推进、跨重启从表尾续写;
* ``verify`` —— 完好链返回 True;直接 SQL 改一行 / 删一行后返回 False;
* **哈希跨后端一致性** —— 同一事件序列喂给 ``SqliteAuditSink`` 与
  ``JsonlAuditSink``,末条 hash 必须逐位相同(时间戳固定注入);
* 表名白名单防注入、空库、关闭后写入、幂等关闭、自定义表名等边界。

所有涉及时间的用例都显式注入固定 datetime,或用可变列表时钟
(``lambda: box[0]``)注入 ``RiskEngine(clock=...)``,保证确定性;所有文件
一律落在 ``tmp_path`` 下,不碰真实磁盘、无网络。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from riskguard import (
    Account,
    JsonlAuditSink,
    Order,
    Portfolio,
    RiskConfig,
    RiskEngine,
    Side,
    SqliteAuditSink,
)
from riskguard.audit import AuditEvent

# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _event(event_type: str = "decision", **payload: object) -> AuditEvent:
    """构造一条固定时间戳的审计事件(默认 T0)。"""
    ts = payload.pop("timestamp", T0)
    return AuditEvent(timestamp=ts, event_type=event_type, payload=payload)


def _sample_events() -> list[AuditEvent]:
    """一组覆盖多种 payload 形态(标量/布尔/空串/嵌套)的事件序列。"""
    return [
        _event("decision", symbol="AAPL", qty=100, ok=True, reasons=""),
        _event("breaker_trip", reason="drawdown 16% >= 15%", equity=84_000.0),
        _event("breaker_reset", equity=84_000.0),
        _event("fill", nested={"a": 1, "b": [1, 2, 3]}, price=None),
    ]


def _feed(sink, events) -> None:
    """把事件逐条喂给某个 sink。"""
    for ev in events:
        sink.record(ev)


def _list_clock(start: datetime):
    """返回 (可变时间盒, 读取器);改盒子里的值即可推进引擎时钟。"""
    box = [start]
    return box, (lambda: box[0])


def _portfolio(equity: float, **marks: float) -> Portfolio:
    """构造一个只关心权益(可选标记价)的最简组合。"""
    return Portfolio(account=Account(equity=equity), marks=marks)


# --------------------------------------------------------------------------- #
# record + verify:基本正路
# --------------------------------------------------------------------------- #

def test_record_creates_table_and_rows(tmp_path):
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    events = _sample_events()
    _feed(sink, events)
    sink.close()

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT seq, event_type FROM audit_events ORDER BY seq ASC"
        ).fetchall()
    finally:
        conn.close()

    assert [r[0] for r in rows] == [1, 2, 3, 4]
    assert [r[1] for r in rows] == [e.event_type for e in events]


def test_verify_true_on_clean_chain(tmp_path):
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    _feed(sink, _sample_events())
    sink.close()

    assert SqliteAuditSink.verify(path) is True


def test_seq_starts_at_one_and_increments(tmp_path):
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    sink.record(_event("decision", i=0))
    assert sink._seq == 1
    sink.record(_event("decision", i=1))
    assert sink._seq == 2
    sink.close()


def test_prev_hash_of_first_row_is_genesis(tmp_path):
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    sink.record(_event("decision", i=0))
    sink.close()

    conn = sqlite3.connect(path)
    try:
        prev_hash = conn.execute(
            "SELECT prev_hash FROM audit_events WHERE seq = 1"
        ).fetchone()[0]
    finally:
        conn.close()

    assert prev_hash == "0" * 64


def test_hash_column_chains_prev_to_next(tmp_path):
    """第 N 条的 prev_hash 必须等于第 N-1 条的 hash。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    _feed(sink, _sample_events())
    sink.close()

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT seq, prev_hash, hash FROM audit_events ORDER BY seq ASC"
        ).fetchall()
    finally:
        conn.close()

    prev = "0" * 64
    for _seq, prev_hash, digest in rows:
        assert prev_hash == prev
        prev = digest


def test_empty_db_verify_true(tmp_path):
    """空表(无事件)哈希链平凡完好,verify 返回 True。"""
    path = str(tmp_path / "empty.db")
    sink = SqliteAuditSink(path)
    sink.close()

    assert SqliteAuditSink.verify(path) is True


# --------------------------------------------------------------------------- #
# verify:篡改检测
# --------------------------------------------------------------------------- #

def test_verify_false_after_corrupting_payload(tmp_path):
    """直接 SQL 改掉某行 payload,后续哈希对不上 -> verify 返回 False。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    _feed(sink, _sample_events())
    sink.close()
    assert SqliteAuditSink.verify(path) is True

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE audit_events SET payload = ? WHERE seq = 2",
            (json.dumps({"reason": "TAMPERED", "equity": 0.0}),),
        )
        conn.commit()
    finally:
        conn.close()

    assert SqliteAuditSink.verify(path) is False


def test_verify_false_after_corrupting_event_type(tmp_path):
    """改掉 event_type 字段同样破坏规范化内容 -> 哈希不符。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    _feed(sink, _sample_events())
    sink.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE audit_events SET event_type = 'forged' WHERE seq = 1"
        )
        conn.commit()
    finally:
        conn.close()

    assert SqliteAuditSink.verify(path) is False


def test_verify_false_after_corrupting_hash_column(tmp_path):
    """只改 hash 列(内容不变)也应被 verify 抓到:重算值与存值不符。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    _feed(sink, _sample_events())
    sink.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE audit_events SET hash = ? WHERE seq = 3", ("f" * 64,)
        )
        conn.commit()
    finally:
        conn.close()

    assert SqliteAuditSink.verify(path) is False


def test_verify_false_after_corrupting_prev_hash_column(tmp_path):
    """篡改 prev_hash 列断开链接 -> verify 返回 False。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    _feed(sink, _sample_events())
    sink.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE audit_events SET prev_hash = ? WHERE seq = 3", ("0" * 64,)
        )
        conn.commit()
    finally:
        conn.close()

    assert SqliteAuditSink.verify(path) is False


def test_verify_false_after_deleting_middle_row(tmp_path):
    """删掉链中一条记录,后续行 prev_hash 与重算 prev 断裂 -> False。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    _feed(sink, _sample_events())
    sink.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM audit_events WHERE seq = 2")
        conn.commit()
    finally:
        conn.close()

    assert SqliteAuditSink.verify(path) is False


def test_verify_false_after_corrupting_timestamp(tmp_path):
    """时间戳进入规范化哈希;直接改它 -> verify 返回 False。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    _feed(sink, _sample_events())
    sink.close()

    tampered = (T0 + timedelta(days=1)).isoformat()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE audit_events SET timestamp = ? WHERE seq = 1", (tampered,)
        )
        conn.commit()
    finally:
        conn.close()

    assert SqliteAuditSink.verify(path) is False


# --------------------------------------------------------------------------- #
# 跨后端哈希一致性:SQLite vs JSONL 逐位相同
# --------------------------------------------------------------------------- #

def test_final_hash_matches_jsonl_backend(tmp_path):
    """同一事件序列(固定时间戳)喂两个后端,末条 hash 必须完全相同。"""
    events = _sample_events()

    jpath = str(tmp_path / "audit.jsonl")
    spath = str(tmp_path / "audit.db")
    jsink = JsonlAuditSink(jpath)
    ssink = SqliteAuditSink(spath)
    _feed(jsink, events)
    _feed(ssink, events)

    assert ssink._prev_hash == jsink._prev_hash
    jsink.close()
    ssink.close()

    # 两个后端各自的独立 verify 都应通过。
    assert JsonlAuditSink.verify(jpath) is True
    assert SqliteAuditSink.verify(spath) is True


def test_every_row_hash_matches_jsonl(tmp_path):
    """不仅末条,逐条 hash 都应与 JSONL 后端一致。"""
    events = _sample_events()

    jpath = str(tmp_path / "audit.jsonl")
    spath = str(tmp_path / "audit.db")
    jsink = JsonlAuditSink(jpath)
    ssink = SqliteAuditSink(spath)
    _feed(jsink, events)
    _feed(ssink, events)
    jsink.close()
    ssink.close()

    # SQLite 侧逐行 hash。
    conn = sqlite3.connect(spath)
    try:
        sqlite_hashes = [
            row[0]
            for row in conn.execute(
                "SELECT hash FROM audit_events ORDER BY seq ASC"
            )
        ]
    finally:
        conn.close()

    # JSONL 侧逐行 hash。
    jsonl_hashes = []
    with open(jpath, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                jsonl_hashes.append(json.loads(line)["hash"])

    assert sqlite_hashes == jsonl_hashes
    assert len(sqlite_hashes) == len(events)


def test_hash_parity_with_engine_driven_events(tmp_path):
    """用可变列表时钟驱动 RiskEngine,把同一批裁决落到两个后端,末条 hash 相同。

    引擎的 ``check`` 用注入的 clock 打时间戳,两个后端因此收到逐位相同的事件,
    末条 hash 必然相同——这验证了"跨后端可互换、审计可交叉复核"的契约。
    """
    box, clock = _list_clock(T0)

    jpath = str(tmp_path / "engine.jsonl")
    spath = str(tmp_path / "engine.db")
    jsink = JsonlAuditSink(jpath)
    ssink = SqliteAuditSink(spath)

    config = RiskConfig(max_position_pct=0.10, on_position_breach="resize")
    j_engine = RiskEngine(config, audit=jsink, clock=clock)
    s_engine = RiskEngine(config, audit=ssink, clock=clock)

    portfolio = _portfolio(100_000.0, AAPL=200.0)
    # 三笔订单:放行 / 缩量 / (再一笔)——两引擎跑相同输入相同时钟。
    orders = [
        Order(symbol="AAPL", side=Side.BUY, quantity=10),      # 2000 名义,放行
        Order(symbol="AAPL", side=Side.BUY, quantity=1000),    # 20 万,超 10% -> 缩量
        Order(symbol="AAPL", side=Side.SELL, quantity=5),
    ]
    for i, order in enumerate(orders):
        box[0] = T0 + timedelta(minutes=i)
        j_engine.check(order, portfolio)
        s_engine.check(order, portfolio)

    assert ssink._prev_hash == jsink._prev_hash
    jsink.close()
    ssink.close()
    assert JsonlAuditSink.verify(jpath) is True
    assert SqliteAuditSink.verify(spath) is True


# --------------------------------------------------------------------------- #
# 续写:跨重启从表尾恢复哈希链
# --------------------------------------------------------------------------- #

def test_reopen_continues_hash_chain(tmp_path):
    """关闭后重开同一路径,应从表尾恢复 seq 与 prev_hash 并续写。"""
    path = str(tmp_path / "audit.db")

    sink1 = SqliteAuditSink(path)
    _feed(sink1, _sample_events()[:2])
    seq_after_first = sink1._seq
    hash_after_first = sink1._prev_hash
    sink1.close()

    sink2 = SqliteAuditSink(path)
    assert sink2._seq == seq_after_first
    assert sink2._prev_hash == hash_after_first
    _feed(sink2, _sample_events()[2:])
    sink2.close()

    # 续写后整条链仍完好。
    assert SqliteAuditSink.verify(path) is True

    conn = sqlite3.connect(path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    finally:
        conn.close()
    assert count == len(_sample_events())


def test_reopen_hash_chain_equals_single_session(tmp_path):
    """分两段写 == 一段写:相同事件序列末条 hash 应相同。"""
    events = _sample_events()

    split_path = str(tmp_path / "split.db")
    s1 = SqliteAuditSink(split_path)
    _feed(s1, events[:2])
    s1.close()
    s2 = SqliteAuditSink(split_path)
    _feed(s2, events[2:])
    split_final = s2._prev_hash
    s2.close()

    once_path = str(tmp_path / "once.db")
    s3 = SqliteAuditSink(once_path)
    _feed(s3, events)
    once_final = s3._prev_hash
    s3.close()

    assert split_final == once_final


# --------------------------------------------------------------------------- #
# 便捷封装:record_decision / record_event 走同一条链
# --------------------------------------------------------------------------- #

def test_record_event_helper_persists(tmp_path):
    """基类 record_event 便捷封装应正常落表并保持链完好。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    sink.record_event("monitor", T0, equity=100_000.0, note="ok")
    sink.record_event("monitor", T0 + timedelta(hours=1), equity=99_000.0)
    sink.close()

    assert SqliteAuditSink.verify(path) is True
    conn = sqlite3.connect(path)
    try:
        payloads = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM audit_events ORDER BY seq ASC"
            )
        ]
    finally:
        conn.close()
    assert payloads[0]["equity"] == 100_000.0
    assert payloads[0]["note"] == "ok"


# --------------------------------------------------------------------------- #
# 表名:自定义 + 白名单防注入
# --------------------------------------------------------------------------- #

def test_custom_table_name(tmp_path):
    """自定义表名应被创建,并可用同名传参独立 verify。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path, table="risk_log")
    _feed(sink, _sample_events())
    sink.close()

    conn = sqlite3.connect(path)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='risk_log'"
        ).fetchone()
    finally:
        conn.close()
    assert exists is not None
    assert SqliteAuditSink.verify(path, table="risk_log") is True


def test_underscore_table_name_allowed(tmp_path):
    """纯下划线/字母数字组合的表名应被接受。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path, table="my_events_2")
    sink.record(_event("decision", i=0))
    sink.close()
    assert SqliteAuditSink.verify(path, table="my_events_2") is True


@pytest.mark.parametrize(
    "bad_table",
    [
        "audit-events",        # 连字符
        "audit events",        # 空格
        "events;DROP TABLE x", # 分号注入
        "tbl'--",              # 引号注入
        "",                    # 空串
    ],
)
def test_illegal_table_name_rejected(tmp_path, bad_table):
    """非字母数字下划线的表名一律拒绝,防拼接注入。"""
    path = str(tmp_path / "audit.db")
    with pytest.raises(ValueError):
        SqliteAuditSink(path, table=bad_table)


def test_illegal_table_name_rejected_in_verify(tmp_path):
    """verify 侧也走同一白名单校验。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    sink.record(_event("decision", i=0))
    sink.close()
    with pytest.raises(ValueError):
        SqliteAuditSink.verify(path, table="bad;name")


# --------------------------------------------------------------------------- #
# 生命周期:关闭 / 幂等 / 关闭后写入 / 上下文管理器
# --------------------------------------------------------------------------- #

def test_record_after_close_raises(tmp_path):
    """关闭后再写应显式抛错,绝不静默丢事件。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    sink.close()
    with pytest.raises(RuntimeError):
        sink.record(_event("decision", i=0))


def test_close_is_idempotent(tmp_path):
    """close 可重复调用而不报错。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    sink.record(_event("decision", i=0))
    sink.close()
    sink.close()  # 第二次应无操作、不抛错
    assert SqliteAuditSink.verify(path) is True


def test_context_manager_closes_sink(tmp_path):
    """作为上下文管理器使用时,退出即关闭连接。"""
    path = str(tmp_path / "audit.db")
    with SqliteAuditSink(path) as sink:
        _feed(sink, _sample_events())
    # 退出后连接已关,再写应抛错。
    with pytest.raises(RuntimeError):
        sink.record(_event("decision", i=99))
    assert SqliteAuditSink.verify(path) is True


# --------------------------------------------------------------------------- #
# payload 形态鲁棒性
# --------------------------------------------------------------------------- #

def test_empty_payload_roundtrips(tmp_path):
    """空 payload 事件应可写、可校验。"""
    path = str(tmp_path / "audit.db")
    sink = SqliteAuditSink(path)
    sink.record(AuditEvent(timestamp=T0, event_type="heartbeat", payload={}))
    sink.close()

    assert SqliteAuditSink.verify(path) is True
    conn = sqlite3.connect(path)
    try:
        payload = conn.execute(
            "SELECT payload FROM audit_events WHERE seq = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(payload) == {}


def test_nested_payload_hash_parity(tmp_path):
    """含嵌套结构的 payload,两后端末条 hash 仍逐位相同。"""
    events = [
        _event("decision", results=[{"rule": "pos", "passed": True}], meta={"x": {"y": 1}}),
        _event("fill", legs=[{"q": 1}, {"q": 2}], avg=None, flag=False),
    ]
    jpath = str(tmp_path / "n.jsonl")
    spath = str(tmp_path / "n.db")
    jsink = JsonlAuditSink(jpath)
    ssink = SqliteAuditSink(spath)
    _feed(jsink, events)
    _feed(ssink, events)
    assert ssink._prev_hash == jsink._prev_hash
    jsink.close()
    ssink.close()
    assert SqliteAuditSink.verify(spath) is True
