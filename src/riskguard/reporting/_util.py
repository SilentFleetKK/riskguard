"""digest/stress 两个模块共用的内部工具,不对外导出。"""

from __future__ import annotations

from ..models import Portfolio


def weight_or_inf(portfolio: Portfolio, symbol: str) -> float:
    """某标的占权益的比例;账户已经穿仓(equity <= 0)时给出真实反映风险的值。

    :meth:`~riskguard.models.Portfolio.weight` 在 ``equity <= 0`` 时返回 ``0.0``
    ——那是给风控规则层设计的防御性兜底:规则层在算到这一步之前,早就已经被
    "equity <= 0 直接拒单"的检查拦截了(见 rules/position_limit.py 等),所以这个
    默认值从未在生产路径上代表过"这个仓位是安全的"这层含义,只是防止除零崩溃。

    但 reporting 模块不一样:它会在 equity <= 0 时依然**主动**把这个数字展示给
    人/AI agent 看,不能被这个兜底误导成"仓位很健康"——这正是账户已经资不抵债、
    风险最大的时刻。所以这里改用一致的语义:equity <= 0 时,只要该标的还有持仓
    (非 flat),就返回 ``inf``(无限大的相对暴露),和 ``Portfolio.gross_exposure()``
    /``net_exposure()`` 除以非正 equity 时早已采用的 ``inf`` 约定对齐,不产生
    "总敞口显示 inf、单个持仓却显示 0%"这种自相矛盾的输出。
    """
    if portfolio.equity <= 0:
        return 0.0 if portfolio.position(symbol).is_flat else float("inf")
    return portfolio.weight(symbol)
