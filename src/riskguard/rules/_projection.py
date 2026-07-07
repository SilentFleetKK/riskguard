"""规则内部共用的敞口投影工具。

多条规则都要回答同一个问题:"这笔单成交后,该标的的持仓会变成多少?它是在
加仓(放大风险)还是减仓?"以及"要把结果控制在某个数量上限内,这笔单最多能下多少?"
把这些算清楚一次,规则实现就能保持一致且简短。
"""

from __future__ import annotations

from ..models import Order, Portfolio

_REL_TOL = 1e-9
_ABS_TOL = 1e-12


def project(order: Order, portfolio: Portfolio) -> tuple[float, float, bool]:
    """返回 ``(当前带符号持仓, 成交后带符号持仓, 是否在放大敞口)``。

    "放大敞口"有两种情形,都算 increasing=True:

    1. **同向加仓**:成交后 |持仓| 变大。
    2. **反手**:持仓符号翻转(多变空或空变多)。反手会平掉旧仓、**开出一个全新的
       反向仓位**,即使新仓 |幅度| 不大于旧仓,它也是不折不扣的新增方向性风险。

    早期版本只用 ``abs(projected) > abs(current)`` 判断,导致"反手到等量或更小幅度"
    的单被当成减仓而绕过所有仓位上限规则**和熔断**——这是一个致命漏洞,现已修复。
    只有"同向减仓(朝零收敛、不翻转)"才是 increasing=False。
    """
    current = portfolio.position(order.symbol).quantity
    projected = current + order.signed_quantity
    flipped = current != 0.0 and projected != 0.0 and (current > 0.0) != (projected > 0.0)
    increasing = flipped or abs(projected) > abs(current) + _ABS_TOL
    return current, projected, increasing


def within(magnitude: float, cap: float) -> bool:
    """带浮点容差地判断 ``magnitude <= cap``。"""
    return magnitude <= cap * (1.0 + _REL_TOL) + _ABS_TOL


def allowed_quantity(order: Order, current_qty: float, cap_qty: float) -> float:
    """在"成交后 |持仓| ≤ cap_qty"约束下,这笔单在其方向上最多允许的数量幅度。

    对减仓单不设限(调用方应先判定 increasing 再决定是否调用本函数)。
    """
    return max(0.0, cap_qty - order.side.sign * current_qty)
