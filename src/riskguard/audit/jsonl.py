"""追加式 JSONL 审计日志,带哈希链防篡改。

每条事件写成一行 JSON,并携带一个 ``hash`` 字段 = 摘要(前一条 hash + 本条规范化内容)。
删改或重排**中间任意一条**记录,其后所有 hash 都对不上,:meth:`JsonlAuditSink.verify`
立即发现。

**诚实地说清楚边界**(别把它当万能防伪):

* **默认纯哈希链(无密钥)** 只挡"修改 / 重排中间记录"。它挡不住两件事——①**尾部截断**
  (删掉最后 N 条,剩下仍自洽);②**整体重写**(用公开的创世值把整份日志重算一遍)。
  因为创世值是公开的、没有秘密。
* **要真正防伪**:给构造器传 ``hmac_key``,改用 HMAC-SHA256,密钥保存在日志**之外**,
  没有密钥就无法伪造或整体重写。
* **要检测尾部截断**:调用 ``verify(path, expected_count=N)``,把"应有多少条"这个外部
  锚点带进来核对。

零依赖,纯标准库。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading

from .base import AuditEvent, AuditSink

_GENESIS = "0" * 64


def _coerce_key(hmac_key: str | bytes | None) -> bytes | None:
    """把字符串密钥统一成 bytes;None 表示不启用 HMAC(退化为纯哈希链)。"""
    if hmac_key is None:
        return None
    return hmac_key if isinstance(hmac_key, bytes) else str(hmac_key).encode("utf-8")


def _chain_digest(prev_hash: str, body: str, hmac_key: bytes | None) -> str:
    """链式摘要:有密钥用 HMAC-SHA256(防伪),无密钥退化为 SHA-256(仅防中间篡改)。"""
    data = (prev_hash + body).encode("utf-8")
    if hmac_key is not None:
        return hmac.new(hmac_key, data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


def _canonical(seq: int, event: AuditEvent) -> str:
    """把一条事件序列化成稳定(键有序)的 JSON,用于计算哈希。"""
    return json.dumps(
        {
            "seq": seq,
            "timestamp": event.timestamp.isoformat(),
            "event_type": event.event_type,
            "payload": event.payload if isinstance(event.payload, dict) else dict(event.payload),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class JsonlAuditSink(AuditSink):
    """把审计事件追加写入一个 ``.jsonl`` 文件,并维护哈希链。

    参数
    ----
    path:
        日志文件路径;不存在则创建,已存在则续写并自动衔接哈希链。
    fsync:
        每写一条是否强制刷盘。默认 False(仅 flush);对不可丢事件可设 True。
    hmac_key:
        可选的 HMAC 密钥(str 或 bytes)。一旦提供,链式摘要改用 HMAC-SHA256,
        没有密钥便无法伪造或整体重写日志。**密钥必须保存在日志之外**,否则形同虚设。
        校验时需把同一密钥传给 ``verify(..., hmac_key=...)``。
    """

    name = "jsonl_audit"

    def __init__(
        self,
        path: str,
        *,
        fsync: bool = False,
        hmac_key: str | bytes | None = None,
    ) -> None:
        self.path = path
        self._fsync = fsync
        self._hmac_key = _coerce_key(hmac_key)
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._seq, self._prev_hash = self._load_tail(path)
        self._file = open(path, "a", encoding="utf-8")

    @staticmethod
    def _load_tail(path: str) -> tuple[int, str]:
        """读取已有文件末条记录,恢复序号与哈希链尾;文件不存在则从创世开始。"""
        if not os.path.exists(path):
            return 0, _GENESIS
        last_seq = 0
        last_hash = _GENESIS
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    last_seq = int(rec.get("seq", last_seq))
                    last_hash = str(rec.get("hash", last_hash))
                except (json.JSONDecodeError, ValueError):
                    continue
        return last_seq, last_hash

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            seq = self._seq + 1
            body = _canonical(seq, event)
            digest = _chain_digest(self._prev_hash, body, self._hmac_key)
            record = {
                "seq": seq,
                "timestamp": event.timestamp.isoformat(),
                "event_type": event.event_type,
                "payload": dict(event.payload),
                "prev_hash": self._prev_hash,
                "hash": digest,
            }
            self._file.write(json.dumps(record, default=str) + "\n")
            self._file.flush()
            if self._fsync:
                os.fsync(self._file.fileno())
            self._seq = seq
            self._prev_hash = digest

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()

    @staticmethod
    def verify(
        path: str,
        *,
        hmac_key: str | bytes | None = None,
        expected_count: int | None = None,
    ) -> bool:
        """独立校验哈希链完好性,返回布尔而非抛异常。

        参数
        ----
        hmac_key:
            若日志用 HMAC 密钥写入,必须传同一密钥,否则一律判为不通过。
        expected_count:
            外部锚点:期望的记录条数。传入后可检测**尾部截断**(末条 seq 必须等于它)。

        任一情况返回 ``False``:文件缺失、出现无法解析的坏行(链已断)、某条 hash/
        prev_hash 对不上、或末条 seq 与 ``expected_count`` 不符。
        """
        if not os.path.exists(path):
            return False
        key = _coerce_key(hmac_key)
        prev = _GENESIS
        last_seq = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    event = AuditEvent(
                        timestamp=_parse_ts(rec["timestamp"]),
                        event_type=rec["event_type"],
                        payload=rec.get("payload", {}),
                    )
                    seq = int(rec["seq"])
                except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                    return False  # 坏行 = 链已断,判不通过而非崩溃
                body = _canonical(seq, event)
                expected = _chain_digest(prev, body, key)
                if expected != rec.get("hash") or rec.get("prev_hash") != prev:
                    return False
                prev = rec["hash"]
                last_seq = seq
        if expected_count is not None and last_seq != expected_count:
            return False
        return True


def _parse_ts(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)
