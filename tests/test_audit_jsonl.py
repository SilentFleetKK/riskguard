"""``JsonlAuditSink`` 单元测试。

覆盖追加式 JSONL 审计日志 + 哈希链防篡改后端的全部关键行为分支:

* **一行一事件** —— 每 ``record`` 一次恰好落一行合法 JSON,字段完整
  (seq / timestamp / event_type / payload / prev_hash / hash);
* **完好即可验** —— 未经改动的文件 ``verify()`` 返回 True;
* **中间篡改被抓** —— 改动/删除中间某行后,其后哈希对不上,``verify()`` 返回 False;
* **续写衔接链** —— 关闭后重新打开同一路径,seq 从末条继续、哈希链首尾相接,
  合并后整链仍可 ``verify()``;
* **裁决入账** —— ``record_decision`` 落下的 payload 含 decision / symbol / reasons
  等字段,且与 :class:`RiskDecision` 内容一致;
* **自动建目录** —— 目标路径的父目录不存在时会被自动创建;
* **创世链头** —— 首条记录的 ``prev_hash`` 为 64 个 0(genesis)。

确定性:所有涉及时间的用例都显式注入 datetime;涉及引擎的用例用可变列表时钟
(``lambda: clock[0]``)注入 :class:`RiskEngine`,不依赖 wall clock。文件一律写在
``tmp_path`` 下,无网络、无外部依赖。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from riskguard import (
    AuditEvent,
    JsonlAuditSink,
    Order,
    PaperBroker,
    Portfolio,
    RiskConfig,
    RiskEngine,
    Side,
)

# --------------------------------------------------------------------------- #
# 常量与辅助
# --------------------------------------------------------------------------- #

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
GENESIS = "0" * 64


def _event(n: int, ts: datetime = T0, event_type: str = "monitor") -> AuditEvent:
    """构造一条带序号 payload 的审计事件,便于断言顺序与内容。"""
    return AuditEvent(timestamp=ts, event_type=event_type, payload={"n": n})


def _read_lines(path) -> list[str]:
    """读出文件里所有非空行(strip 后)。"""
    with open(path, "r", encoding="utf-8") as fh:
        return [ln for ln in (line.strip() for line in fh) if ln]


def _read_records(path) -> list[dict]:
    """把每行解析成 dict。"""
    return [json.loads(ln) for ln in _read_lines(path)]


# --------------------------------------------------------------------------- #
# 一行一事件 / 基本写入
# --------------------------------------------------------------------------- #

def test_writes_one_json_line_per_event(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(2))
        sink.record(_event(3))
    finally:
        sink.close()

    lines = _read_lines(path)
    assert len(lines) == 3
    # 每一行都是独立且合法的 JSON。
    for ln in lines:
        json.loads(ln)


def test_record_writes_all_expected_fields(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1, event_type="fill"))
    finally:
        sink.close()

    (rec,) = _read_records(path)
    assert set(rec) >= {
        "seq",
        "timestamp",
        "event_type",
        "payload",
        "prev_hash",
        "hash",
    }
    assert rec["seq"] == 1
    assert rec["event_type"] == "fill"
    assert rec["timestamp"] == T0.isoformat()
    assert rec["payload"] == {"n": 1}


def test_seq_increments_from_one(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        for i in range(1, 6):
            sink.record(_event(i))
    finally:
        sink.close()

    seqs = [r["seq"] for r in _read_records(path)]
    assert seqs == [1, 2, 3, 4, 5]


def test_first_record_prev_hash_is_genesis(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(2))
    finally:
        sink.close()

    recs = _read_records(path)
    assert recs[0]["prev_hash"] == GENESIS
    # 链式:每条的 prev_hash 等于上一条的 hash。
    assert recs[1]["prev_hash"] == recs[0]["hash"]
    # hash 是 64 位十六进制。
    assert len(recs[0]["hash"]) == 64
    assert all(c in "0123456789abcdef" for c in recs[0]["hash"])


def test_hash_differs_per_record(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(1))  # 同 payload,但 seq/prev_hash 不同 -> hash 必不同
    finally:
        sink.close()

    recs = _read_records(path)
    assert recs[0]["hash"] != recs[1]["hash"]


# --------------------------------------------------------------------------- #
# verify:完好文件
# --------------------------------------------------------------------------- #

def test_verify_true_on_intact_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        for i in range(1, 6):
            sink.record(_event(i))
    finally:
        sink.close()

    assert JsonlAuditSink.verify(str(path)) is True


def test_verify_true_on_empty_file(tmp_path):
    """从未写入任何事件的文件(空链)应视为完好。"""
    path = tmp_path / "empty.jsonl"
    sink = JsonlAuditSink(str(path))
    sink.close()
    assert JsonlAuditSink.verify(str(path)) is True


def test_verify_ignores_blank_lines(tmp_path):
    """链中夹带的纯空行不影响验证(实现里会 strip 后跳过)。"""
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(2))
    finally:
        sink.close()

    original = path.read_text(encoding="utf-8")
    # 在中间与结尾各插入空行。
    lines = original.splitlines()
    tampered = "\n".join([lines[0], "", "   ", lines[1], ""]) + "\n"
    path.write_text(tampered, encoding="utf-8")

    assert JsonlAuditSink.verify(str(path)) is True


# --------------------------------------------------------------------------- #
# verify:篡改被抓
# --------------------------------------------------------------------------- #

def test_verify_false_when_middle_line_tampered(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        for i in range(1, 6):
            sink.record(_event(i))
    finally:
        sink.close()

    lines = _read_lines(path)
    assert len(lines) == 5
    # 篡改中间那条(第 3 条)的 payload,但保留其原 hash 字段不变。
    mid = json.loads(lines[2])
    mid["payload"] = {"n": 999}
    lines[2] = json.dumps(mid)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert JsonlAuditSink.verify(str(path)) is False


def test_verify_false_when_middle_line_deleted(tmp_path):
    """删掉中间一行会打断哈希链(其后各条的 prev_hash 对不上)。"""
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        for i in range(1, 6):
            sink.record(_event(i))
    finally:
        sink.close()

    lines = _read_lines(path)
    del lines[2]  # 删掉第 3 条
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert JsonlAuditSink.verify(str(path)) is False


def test_verify_false_when_hash_field_tampered(tmp_path):
    """只改末条的 hash 字段而不动内容,自校验依然失败。"""
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(2))
    finally:
        sink.close()

    lines = _read_lines(path)
    last = json.loads(lines[-1])
    last["hash"] = "f" * 64
    lines[-1] = json.dumps(last)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert JsonlAuditSink.verify(str(path)) is False


def test_verify_false_when_prev_hash_tampered(tmp_path):
    """改动某条的 prev_hash(哪怕 hash 仍与内容自洽)也会被抓——链头断裂。"""
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(2))
    finally:
        sink.close()

    recs = _read_records(path)
    recs[1]["prev_hash"] = "a" * 64  # 与前一条 hash 不符
    lines = [json.dumps(r) for r in recs]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert JsonlAuditSink.verify(str(path)) is False


def test_verify_false_when_reordered(tmp_path):
    """交换两条记录的顺序会破坏链(seq/prev_hash 与实际位置不匹配)。"""
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(2))
        sink.record(_event(3))
    finally:
        sink.close()

    lines = _read_lines(path)
    lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert JsonlAuditSink.verify(str(path)) is False


# --------------------------------------------------------------------------- #
# 续写:重开后 seq 续接、链首尾相连
# --------------------------------------------------------------------------- #

def test_reopen_resumes_seq(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(2))
    finally:
        sink.close()

    sink2 = JsonlAuditSink(str(path))
    try:
        sink2.record(_event(3))
        sink2.record(_event(4))
    finally:
        sink2.close()

    seqs = [r["seq"] for r in _read_records(path)]
    assert seqs == [1, 2, 3, 4]


def test_reopen_continues_valid_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
        sink.record(_event(2))
    finally:
        sink.close()

    sink2 = JsonlAuditSink(str(path))
    try:
        sink2.record(_event(3))
    finally:
        sink2.close()

    # 续写后的第一条,其 prev_hash 必须等于重开前最后一条的 hash。
    recs = _read_records(path)
    assert recs[2]["prev_hash"] == recs[1]["hash"]
    # 合并后的整链仍然完好。
    assert JsonlAuditSink.verify(str(path)) is True


def test_reopen_empty_file_starts_from_genesis(tmp_path):
    """对一个存在但为空的文件重开,应从创世链头、seq=1 起步。"""
    path = tmp_path / "empty.jsonl"
    JsonlAuditSink(str(path)).close()  # 建出空文件

    sink = JsonlAuditSink(str(path))
    try:
        sink.record(_event(1))
    finally:
        sink.close()

    (rec,) = _read_records(path)
    assert rec["seq"] == 1
    assert rec["prev_hash"] == GENESIS
    assert JsonlAuditSink.verify(str(path)) is True


def test_reopen_many_times_keeps_chain_valid(tmp_path):
    """反复开合(每次写一条)不应破坏链或重复 seq。"""
    path = tmp_path / "audit.jsonl"
    for i in range(1, 6):
        sink = JsonlAuditSink(str(path))
        try:
            sink.record(_event(i))
        finally:
            sink.close()

    seqs = [r["seq"] for r in _read_records(path)]
    assert seqs == [1, 2, 3, 4, 5]
    assert JsonlAuditSink.verify(str(path)) is True


# --------------------------------------------------------------------------- #
# 自动建目录
# --------------------------------------------------------------------------- #

def test_makes_parent_dirs(tmp_path):
    """父目录(多层)不存在时构造 sink 会自动创建。"""
    path = tmp_path / "deep" / "nested" / "dirs" / "audit.jsonl"
    assert not path.parent.exists()

    sink = JsonlAuditSink(str(path))
    try:
        assert path.parent.is_dir()
        sink.record(_event(1))
    finally:
        sink.close()

    assert path.exists()
    assert JsonlAuditSink.verify(str(path)) is True


# --------------------------------------------------------------------------- #
# record_decision:裁决入账
# --------------------------------------------------------------------------- #

def _resize_decision_setup():
    """构造一个会触发 RESIZE 的裁决场景(买 1000 股 AAPL,远超 10% 上限)。"""
    clock = [T0]
    broker = PaperBroker(cash=100_000, marks={"AAPL": 200.0})
    engine = RiskEngine(
        RiskConfig(max_position_pct=0.10),
        broker=broker,
        clock=lambda: clock[0],
    )
    order = Order(symbol="AAPL", side=Side.BUY, quantity=1000)
    decision = engine.check(order, broker.get_portfolio())
    return decision


def test_record_decision_payload_contains_core_fields(tmp_path):
    path = tmp_path / "audit.jsonl"
    decision = _resize_decision_setup()

    sink = JsonlAuditSink(str(path))
    try:
        sink.record_decision(decision)
    finally:
        sink.close()

    (rec,) = _read_records(path)
    assert rec["event_type"] == "decision"
    payload = rec["payload"]
    # 关键字段齐备且与裁决一致。
    assert payload["decision"] == decision.decision.value
    assert payload["symbol"] == "AAPL"
    assert payload["reasons"] == decision.reasons()
    assert payload["reasons"]  # RESIZE 场景必有非空原因
    assert payload["side"] == Side.BUY.value
    assert payload["strategy_id"] == decision.original_order.strategy_id
    assert payload["requested_quantity"] == 1000
    assert payload["final_quantity"] == decision.order.quantity


def test_record_decision_payload_includes_per_rule_results(tmp_path):
    path = tmp_path / "audit.jsonl"
    decision = _resize_decision_setup()

    sink = JsonlAuditSink(str(path))
    try:
        sink.record_decision(decision)
    finally:
        sink.close()

    (rec,) = _read_records(path)
    results = rec["payload"]["results"]
    assert isinstance(results, list)
    assert len(results) == len(decision.results)
    for got, src in zip(results, decision.results):
        assert got["rule"] == src.rule
        assert got["action"] == src.action.value
        assert got["passed"] == src.passed
        assert got["message"] == src.message


def test_record_decision_via_engine_audit_hook(tmp_path):
    """引擎挂上 audit 后,每次 check 自动落一条 decision 记录且链完好。"""
    path = tmp_path / "audit.jsonl"
    clock = [T0]
    broker = PaperBroker(cash=100_000, marks={"AAPL": 200.0})
    sink = JsonlAuditSink(str(path))
    engine = RiskEngine(
        RiskConfig(max_position_pct=0.10),
        broker=broker,
        audit=sink,
        clock=lambda: clock[0],
    )
    try:
        engine.check(Order(symbol="AAPL", side=Side.BUY, quantity=1000),
                     broker.get_portfolio())
        engine.check(Order(symbol="AAPL", side=Side.BUY, quantity=10),
                     broker.get_portfolio())
    finally:
        sink.close()

    recs = _read_records(path)
    assert len(recs) == 2
    assert all(r["event_type"] == "decision" for r in recs)
    assert [r["seq"] for r in recs] == [1, 2]
    assert JsonlAuditSink.verify(str(path)) is True


# --------------------------------------------------------------------------- #
# 便捷封装与上下文管理
# --------------------------------------------------------------------------- #

def test_record_event_helper_writes_payload(tmp_path):
    """record_event(**payload) 便捷封装能落任意事件类型与自定义字段。"""
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    try:
        sink.record_event("breaker_trip", T0, reason="dd", equity=42.0)
    finally:
        sink.close()

    (rec,) = _read_records(path)
    assert rec["event_type"] == "breaker_trip"
    assert rec["payload"] == {"reason": "dd", "equity": 42.0}


def test_context_manager_closes_and_persists(tmp_path):
    """作为上下文管理器使用时,退出即 close,写入落盘且链完好。"""
    path = tmp_path / "audit.jsonl"
    with JsonlAuditSink(str(path)) as sink:
        sink.record(_event(1))
        sink.record(_event(2))

    assert JsonlAuditSink.verify(str(path)) is True
    assert [r["seq"] for r in _read_records(path)] == [1, 2]
