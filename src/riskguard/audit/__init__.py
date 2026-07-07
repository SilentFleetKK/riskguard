"""审计/事件日志后端集合。

``JsonlAuditSink`` 零依赖、始终可用。``SqliteAuditSink`` 基于标准库 sqlite3;
若适配器文件暂缺则为 ``None``。
"""

from __future__ import annotations

from .base import AuditEvent, AuditSink
from .jsonl import JsonlAuditSink

try:
    from .sqlite import SqliteAuditSink
except Exception:  # pragma: no cover
    SqliteAuditSink = None  # type: ignore[assignment]

__all__ = ["AuditEvent", "AuditSink", "JsonlAuditSink", "SqliteAuditSink"]
