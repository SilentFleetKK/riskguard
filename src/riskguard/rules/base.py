"""风控规则的抽象基类与上下文。

一条规则就是一个纯函数式的判断:给定订单 + 组合快照 + 配置 + 状态,产出一个
:class:`~riskguard.models.RuleResult`。规则之间互相独立、可自由组合,引擎负责把
它们串起来并聚合裁决。新增一条风控只需实现 :class:`RiskRule` 并加进引擎的规则列表。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..config import RiskConfig
from ..models import Decision, Order, Portfolio, RuleResult
from ..state import RiskState


@dataclass(frozen=True, slots=True)
class RuleContext:
    """规则求值所需的全部输入,不可变。"""

    order: Order
    portfolio: Portfolio
    config: RiskConfig
    state: RiskState
    now: datetime


class RiskRule(ABC):
    """所有风控规则的基类。

    子类需实现 :meth:`evaluate`,并设置类属性 ``name``。基类提供
    :meth:`approve` / :meth:`resize` / :meth:`reject` 三个便捷方法来构造结果,
    保证 ``rule`` 字段一致且减少样板代码。
    """

    name: str = "rule"

    @abstractmethod
    def evaluate(self, ctx: RuleContext) -> RuleResult:
        """对一笔订单求值,返回本规则的裁决。"""

    # ---- 结果构造便捷方法 ----
    def approve(self, message: str = "", **detail: object) -> RuleResult:
        return RuleResult(
            rule=self.name,
            action=Decision.APPROVE,
            passed=True,
            message=message or f"{self.name}: ok",
            detail=detail,
        )

    def resize(self, adjusted_quantity: float, message: str, **detail: object) -> RuleResult:
        return RuleResult(
            rule=self.name,
            action=Decision.RESIZE,
            passed=False,
            adjusted_quantity=max(0.0, adjusted_quantity),
            message=message,
            detail=detail,
        )

    def reject(self, message: str, **detail: object) -> RuleResult:
        return RuleResult(
            rule=self.name,
            action=Decision.REJECT,
            passed=False,
            message=message,
            detail=detail,
        )
