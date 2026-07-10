"""审计/事件日志的抽象基类。

风控的每一次裁决、每一次熔断、每一笔成交,都应留下不可否认的记录。审计层把
这些事件写到某个持久化后端(JSONL、SQLite、或你自己的数据库)。事后复盘、
合规追溯、以及"这单当时到底为什么被拒"的追问,全靠它。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from ..models import RiskDecision


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """一条审计事件(不可变)。"""

    timestamp: datetime
    event_type: str  # "decision" | "breaker_trip" | "breaker_reset" | "fill" | "monitor"
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class AuditSink(ABC):
    """审计后端接口。实现 :meth:`record` 即可;其余为便捷封装。"""

    name: str = "audit"

    @abstractmethod
    def record(self, event: AuditEvent) -> None:
        """持久化一条事件。实现必须做到追加即落盘、不丢事件。"""

    # ---- 便捷封装 ----
    def record_decision(self, decision: RiskDecision) -> None:
        self.record(
            AuditEvent(
                timestamp=decision.timestamp,
                event_type="decision",
                payload={
                    "decision": decision.decision.value,
                    "symbol": decision.original_order.symbol,
                    "strategy_id": decision.original_order.strategy_id,
                    "side": decision.original_order.side.value,
                    "requested_quantity": decision.original_order.quantity,
                    "final_quantity": decision.order.quantity,
                    "reasons": decision.reasons(),
                    "results": [
                        {
                            "rule": r.rule,
                            "action": r.action.value,
                            "passed": r.passed,
                            "message": r.message,
                        }
                        for r in decision.results
                    ],
                },
            )
        )

    def record_event(
        self, event_type: str, timestamp: datetime, **payload: object
    ) -> None:
        self.record(AuditEvent(timestamp=timestamp, event_type=event_type, payload=payload))

    def close(self) -> None:
        """释放资源(文件句柄、数据库连接)。默认无操作。"""

    def __enter__(self) -> AuditSink:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
