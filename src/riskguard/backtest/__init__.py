"""回测接线:把 RiskGuard 作为"风险叠加层"接进回测框架。

连通量化五层积木里的「回测 → 风控」两层。

- :class:`RiskOverlay` —— 框架无关的核心:目标持仓 → 风控批准的订单/权重。
- :func:`replay` / :func:`compare` —— 轻量价格重放器,用来**看见/测试**风控行为
  (非通用回测框架)。
- :func:`make_riskguard_strategy` —— backtesting.py 适配器(可选依赖,懒加载)。
- :func:`risk_capped_weights` / :func:`kelly_weights` / :func:`from_signals_with_risk`
  —— vectorbt 辅助(前两个纯 Python 无需装 vectorbt)。
"""

from __future__ import annotations

from .backtesting_py import make_riskguard_strategy
from .overlay import OverlayResult, RiskOverlay
from .replay import ReplayResult, compare, replay
from .vectorbt import from_signals_with_risk, kelly_weights, risk_capped_weights

__all__ = [
    "RiskOverlay",
    "OverlayResult",
    "replay",
    "compare",
    "ReplayResult",
    "make_riskguard_strategy",
    "risk_capped_weights",
    "kelly_weights",
    "from_signals_with_risk",
]
