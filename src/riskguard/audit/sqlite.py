"""基于 SQLite 的审计日志后端,带哈希链防篡改。

与 :class:`~riskguard.audit.jsonl.JsonlAuditSink` 语义完全一致:每条事件携带一个
``hash`` 字段 = SHA-256(前一条 hash + 本条规范化内容)。为保证对同一事件序列产出
的哈希与 JSONL 后端**逐位相同**,本模块直接复用 JSONL 里的 :func:`_canonical` 与
``_GENESIS``,绝不另行实现哈希逻辑。

数据落在单张表里(默认 ``audit_events``),用同一个 SQLite 连接续写;进程重启后从表尾
恢复序号与哈希链尾,继续衔接。纯标准库 ``sqlite3``,零第三方依赖。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime

from .base import AuditEvent, AuditSink
from .jsonl import _GENESIS, _canonical, _chain_digest, _coerce_key

__all__ = ["SqliteAuditSink"]


class SqliteAuditSink(AuditSink):
    """把审计事件写入 SQLite 单表,并维护与 JSONL 后端一致的哈希链。

    参数
    ----
    path:
        SQLite 数据库文件路径(``":memory:"`` 亦可);不存在则创建,已存在则续写并
        自动衔接哈希链。
    table:
        存放事件的表名,默认 ``"audit_events"``。仅接受字母/数字/下划线,防止拼接注入。
    """

    name = "sqlite_audit"

    def __init__(
        self,
        path: str,
        *,
        table: str = "audit_events",
        hmac_key: str | bytes | None = None,
    ) -> None:
        self.path = path
        self.table = _safe_table(table)
        self._hmac_key = _coerce_key(hmac_key)
        self._lock = threading.RLock()
        # check_same_thread=False:允许跨线程调用;所有写操作自身用 RLock 串行化。
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            path, check_same_thread=False
        )
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table} ("
                "seq INTEGER PRIMARY KEY, "
                "timestamp TEXT, "
                "event_type TEXT, "
                "payload TEXT, "
                "prev_hash TEXT, "
                "hash TEXT)"
            )
            self._conn.commit()
            self._seq, self._prev_hash = self._load_tail()

    def _load_tail(self) -> tuple[int, str]:
        """读取表中最大 seq 的记录,恢复序号与哈希链尾;表空则从创世开始。"""
        assert self._conn is not None
        cur = self._conn.execute(
            f"SELECT seq, hash FROM {self.table} ORDER BY seq DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return 0, _GENESIS
        return int(row[0]), str(row[1])

    def record(self, event: AuditEvent) -> None:
        """持久化一条事件:计算哈希、写入并提交,随后推进链尾。"""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("SqliteAuditSink 已关闭,无法写入。")
            seq = self._seq + 1
            body = _canonical(seq, event)
            digest = _chain_digest(self._prev_hash, body, self._hmac_key)
            payload = json.dumps(dict(event.payload), default=str)
            self._conn.execute(
                f"INSERT INTO {self.table} "
                "(seq, timestamp, event_type, payload, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    event.timestamp.isoformat(),
                    event.event_type,
                    payload,
                    self._prev_hash,
                    digest,
                ),
            )
            self._conn.commit()
            self._seq = seq
            self._prev_hash = digest

    def close(self) -> None:
        """关闭数据库连接(幂等,可重复调用)。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @staticmethod
    def verify(
        path: str,
        table: str = "audit_events",
        *,
        hmac_key: str | bytes | None = None,
        expected_count: int | None = None,
    ) -> bool:
        """独立校验哈希链完好性,语义与 :meth:`JsonlAuditSink.verify` 一致。

        按 seq 升序逐条重算哈希;任一条 ``hash``/``prev_hash`` 对不上即 ``False``。
        用 HMAC 密钥写入的日志必须传同一 ``hmac_key``;传 ``expected_count`` 可检测
        尾部截断(末条 seq 必须等于它)。
        """
        table = _safe_table(table)
        key = _coerce_key(hmac_key)
        conn = sqlite3.connect(path)
        try:
            cur = conn.execute(
                f"SELECT seq, timestamp, event_type, payload, prev_hash, hash "
                f"FROM {table} ORDER BY seq ASC"
            )
            prev = _GENESIS
            last_seq = 0
            for seq, ts, event_type, payload, prev_hash, digest in cur:
                event = AuditEvent(
                    timestamp=datetime.fromisoformat(ts),
                    event_type=event_type,
                    payload=json.loads(payload) if payload else {},
                )
                body = _canonical(int(seq), event)
                expected = _chain_digest(prev, body, key)
                if expected != digest or prev_hash != prev:
                    return False
                prev = digest
                last_seq = int(seq)
            if expected_count is not None and last_seq != expected_count:
                return False
            return True
        finally:
            conn.close()


def _safe_table(table: str) -> str:
    """校验表名(仅字母/数字/下划线),表名无法参数化,只能白名单防注入。"""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"非法表名: {table!r}")
    return table
