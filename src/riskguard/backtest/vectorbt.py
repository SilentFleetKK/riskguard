"""`vectorbt` 适配器 + 纯 Python 的向量化仓位辅助。

vectorbt 是向量化回测框架。RiskGuard 在这里提供两类东西:

1. **纯函数辅助**(无需装 vectorbt):把目标权重按 ``max_position_pct`` 封顶、或由
   胜率/盈亏比逐点算分数 Kelly 权重。返回普通列表,可直接喂给
   ``vbt.Portfolio.from_signals(size=..., size_type="targetpercent")``。
2. **:func:`from_signals_with_risk`**:懒加载 vectorbt,把仓位封顶后跑 from_signals。
   vectorbt 是可选依赖(``pip install "riskguard[vectorbt]"``)。
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..config import RiskConfig


def risk_capped_weights(
    target_weights: Sequence[float], config: Optional[RiskConfig] = None
) -> list[float]:
    """把每个目标权重的**幅度**封顶在 ``max_position_pct`` 以内(保留方向符号)。"""
    cfg = config or RiskConfig()
    cap = cfg.max_position_pct
    return [max(-cap, min(cap, float(w))) for w in target_weights]


def kelly_weights(
    win_prob: Sequence[float],
    payoff_ratio: Sequence[float],
    config: Optional[RiskConfig] = None,
) -> list[float]:
    """由逐点胜率 p 与盈亏比 b 算分数 Kelly 权重 ``f = kelly_fraction·(bp−q)/b``,并封顶。

    公式与 :class:`~riskguard.sizing.kelly.KellySizer` 一致;两处差别是刻意为向量化服务:
    (1) **fail-soft**:非法输入(b≤0 或 p∉[0,1],含 NaN)本函数记为 0 而**不抛异常**——
    向量化流水线里一个坏点不该炸掉整条序列;引擎里的 KellySizer 则对单点严格报错。
    (2) 本函数直接封顶到 ``max_position_pct``(引擎路径靠下游的仓位上限规则封顶)。
    """
    cfg = config or RiskConfig()
    out: list[float] = []
    for p, b in zip(win_prob, payoff_ratio):
        p = float(p)
        b = float(b)
        if b <= 0.0 or not (0.0 <= p <= 1.0):
            out.append(0.0)
            continue
        f_star = (b * p - (1.0 - p)) / b
        f = max(0.0, cfg.kelly_fraction * f_star)
        out.append(min(f, cfg.max_position_pct))
    return out


def _load_vectorbt():
    try:
        import vectorbt  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            'vectorbt not installed; run: pip install "riskguard[vectorbt]"'
        ) from exc
    return vectorbt


def from_signals_with_risk(
    close,
    entries,
    exits,
    *,
    config: Optional[RiskConfig] = None,
    **kwargs: object,
):
    """跑 ``vbt.Portfolio.from_signals``,单笔目标仓位按 ``max_position_pct`` 封顶。

    等价于给 from_signals 传 ``size=max_position_pct, size_type="targetpercent"``,
    把"别把身家压一注"这条纪律直接作用到向量化回测上。``**kwargs`` 透传给 from_signals。
    """
    vbt = _load_vectorbt()
    cfg = config or RiskConfig()
    cap = cfg.max_position_pct
    if "size" in kwargs and kwargs["size"] is not None:
        # 调用方自带 size:也要封顶,否则 "with_risk" 名不副实(变成透传)。
        size = kwargs["size"]
        try:
            kwargs["size"] = risk_capped_weights(size, cfg)  # 序列
        except TypeError:
            kwargs["size"] = max(-cap, min(cap, float(size)))  # 标量
    else:
        kwargs["size"] = cap
    kwargs.setdefault("size_type", "targetpercent")
    return vbt.Portfolio.from_signals(close, entries, exits, **kwargs)
