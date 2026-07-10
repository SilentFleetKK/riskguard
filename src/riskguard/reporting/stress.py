"""压力测试:"如果我的持仓明天集体跌 20%,我扛得住吗"。

不是预测,是推演——在坏事真的发生之前,先在纸面上把最坏的情况算一遍。给当前
持仓一个统一的价格冲击(比如 -20%),看权益、回撤、离熔断线的距离,以及哪些仓位
会因此超出你自己设的上限。

⚠️ **绝对只读**:本模块不修改引擎的任何状态(不调用 :meth:`~riskguard.engine.
RiskEngine.check`/:meth:`~riskguard.engine.RiskEngine.update_equity`,不触发熔断、
不写审计、不碰持久化)。它只读 ``engine.config`` 和 ``engine.state`` 的当前快照,
在内存里推演一个假设情景——绝不能让"如果……会怎样"这种假设性提问,污染真实的
风控状态。这也是它和 :mod:`riskguard.backtest` 的区别:backtest 是拿真实历史价格
路径重放、会真的推进引擎状态;stress test 是纯粹的"假设推演",一次性、无副作用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..models import Account, Portfolio
from ._util import weight_or_inf

if TYPE_CHECKING:
    from ..engine import RiskEngine


@dataclass(frozen=True, slots=True)
class PositionBreach:
    """冲击后超出仓位上限的一个持仓。"""

    symbol: str
    shocked_weight: float  # 冲击后占权益的比例(无符号)
    cap: float  # 配置的单笔仓位上限

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "shocked_weight": self.shocked_weight, "cap": self.cap}


@dataclass(frozen=True, slots=True)
class StressResult:
    """一次压力测试的结果。"""

    shock_pct: float  # 施加的冲击幅度,如 -0.20 表示统一下跌 20%
    current_equity: float
    shocked_equity: float
    current_drawdown: float  # 相对高水位的当前回撤
    shocked_drawdown: float  # 冲击后相对(不变的)高水位的回撤
    high_water_mark: float
    would_trip_breaker: bool  # 假如现在就发生这个冲击,熔断会不会被触发
    already_tripped: bool  # 熔断当前是否已经触发(冲击前)
    gross_exposure_ratio: float  # 冲击后总敞口/权益
    net_exposure_ratio: float  # 冲击后净敞口/权益
    position_breaches: tuple[PositionBreach, ...] = field(default_factory=tuple)

    @property
    def equity_change_pct(self) -> float:
        # current_equity <= 0(而不只是 == 0):分母为负时 shocked/current-1 的
        # 符号会反转——账户从 -3000 恶化到 -5000,公式会算出 +66.7% 这种"上涨"
        # 假象,反而掩盖了正在变得更糟这件事。非正权益下百分比变化本就没有
        # 稳定语义,统一给 0.0,不制造误导性的方向感。
        if self.current_equity <= 0:
            return 0.0
        return self.shocked_equity / self.current_equity - 1.0

    def to_dict(self) -> dict:
        """结构化输出,供 AI agent/脚本消费(而不是给人读的文本)。"""
        return {
            "shock_pct": self.shock_pct,
            "current_equity": self.current_equity,
            "shocked_equity": self.shocked_equity,
            "equity_change_pct": self.equity_change_pct,
            "current_drawdown": self.current_drawdown,
            "shocked_drawdown": self.shocked_drawdown,
            "high_water_mark": self.high_water_mark,
            "would_trip_breaker": self.would_trip_breaker,
            "already_tripped": self.already_tripped,
            "gross_exposure_ratio": self.gross_exposure_ratio,
            "net_exposure_ratio": self.net_exposure_ratio,
            "position_breaches": [b.to_dict() for b in self.position_breaches],
        }


def run_stress_test(
    engine: RiskEngine, portfolio: Portfolio, shock_pct: float
) -> StressResult:
    """对 ``portfolio`` 施加统一冲击 ``shock_pct``,推演结果。纯函数,零副作用。

    参数
    ----
    engine:
        只读取其 ``config``(阈值)与 ``state``(高水位/当前熔断状态)快照,
        **绝不调用任何会改变引擎状态的方法**。
    portfolio:
        当前持仓快照(调用方提供,本库不拉取行情——一贯的 fail-closed 原则:
        拿不到价格宁可不算,也不猜)。
    shock_pct:
        统一冲击幅度,带符号:-0.20 表示所有标的的标记价统一下跌 20%,
        +0.20 表示统一上涨 20%。必须是有限数且 > -1.0(跌不能超过 100%)。
    """
    if not math.isfinite(shock_pct):
        raise ValueError(f"shock_pct must be a finite number, got {shock_pct!r}")
    if shock_pct <= -1.0:
        raise ValueError(
            f"shock_pct must be > -1.0 (can't lose more than 100% of value), got {shock_pct}"
        )

    current_equity = portfolio.equity  # 权威值,尊重调用方的断言(如来自真实券商回执)
    cash = portfolio.account.cash

    shocked_marks: dict[str, float] = {}
    mtm_delta = 0.0  # 冲击导致的持仓市值变动量
    for symbol, pos in portfolio.positions.items():
        base = portfolio.mark_for(symbol)
        if base is None:
            continue
        shocked_mark = base * (1.0 + shock_pct)
        shocked_marks[symbol] = shocked_mark
        mtm_delta += pos.market_value(shocked_mark) - pos.market_value(base)

    # 冲击后权益 = 权威的当前权益 + 冲击导致的市值变动量——而不是拿 cash+持仓
    # 从零重算一遍。原因:如果调用方传入的 Account.equity 和"cash+持仓市值"本就
    # 有些出入(现实很常见,比如权益来自真实券商回执、包含未建模的项目),
    # "从零重算"出来的冲击后权益就和 current_equity 不在同一基线上,算出来的
    # 变动幅度会失真;用市值变动量做加法,能保证这个数字只反映"这次冲击"本身
    # 造成的影响,不掺入其它口径差异。
    shocked_equity = current_equity + mtm_delta

    shocked_portfolio = Portfolio(
        account=Account(
            equity=shocked_equity, cash=cash, buying_power=portfolio.account.buying_power
        ),
        positions=portfolio.positions,
        marks=shocked_marks,
    )

    config = engine.config
    state = engine.state  # 只读快照,不触发任何观测/持久化
    hwm = state.high_water_mark
    # 当前回撤和冲击后回撤共用同一个基线:调用方刚传进来的 current_equity,
    # 而不是 state.drawdown(那是"上次观测到"的权益,如果调用方这次传的
    # --equity 和存档里的不一致,用 state.drawdown 展示的"当前回撤"就会是错的、
    # 对不上调用方眼下正在问的这个持仓)。高水位仍然是历史事实,不能被这次
    # 假设性提问悄悄改写。
    current_drawdown = max(0.0, 1.0 - current_equity / hwm) if hwm > 0 else 0.0
    shocked_drawdown = max(0.0, 1.0 - shocked_equity / hwm) if hwm > 0 else 0.0
    would_trip = (
        not state.breaker_tripped and hwm > 0 and shocked_drawdown >= config.max_drawdown_pct
    )

    # 用 weight_or_inf 而不是 shocked_portfolio.weight():后者在冲击后权益 <= 0
    # (账户已经被这个冲击打穿仓)时会静默返回 0.0,导致"总敞口显示 inf(疯狂
    # 报警)、每个持仓却显示 0% 超限(看起来毫无风险)"这种自相矛盾的假阴性
    # ——账户资不抵债恰恰是压力测试最该揪出来的场景,绝不能因为这个防御性默认值
    # 而漏报。
    breaches = tuple(
        PositionBreach(
            symbol=sym,
            shocked_weight=weight_or_inf(shocked_portfolio, sym),
            cap=config.max_position_pct,
        )
        for sym in portfolio.positions
        if weight_or_inf(shocked_portfolio, sym) > config.max_position_pct
    )

    gross_ratio = (
        shocked_portfolio.gross_exposure() / shocked_equity if shocked_equity > 0 else float("inf")
    )
    net_ratio = (
        shocked_portfolio.net_exposure() / shocked_equity if shocked_equity > 0 else float("inf")
    )

    return StressResult(
        shock_pct=shock_pct,
        current_equity=current_equity,
        shocked_equity=shocked_equity,
        current_drawdown=current_drawdown,
        shocked_drawdown=shocked_drawdown,
        high_water_mark=hwm,
        would_trip_breaker=would_trip,
        already_tripped=state.breaker_tripped,
        gross_exposure_ratio=gross_ratio,
        net_exposure_ratio=net_ratio,
        position_breaches=breaches,
    )


def render_text(result: StressResult) -> str:
    """把 :class:`StressResult` 渲染成人类可读的文本报告。"""
    lines = [
        f"压力测试:统一冲击 {result.shock_pct:+.1%}",
        "",
        f"  当前权益:  {result.current_equity:,.0f}",
        f"  冲击后权益:{result.shocked_equity:,.0f}  ({result.equity_change_pct:+.1%})",
        f"  当前回撤:  {result.current_drawdown:.1%}",
        f"  冲击后回撤:{result.shocked_drawdown:.1%}  (高水位 {result.high_water_mark:,.0f})",
        "",
    ]
    if result.already_tripped:
        lines.append("  熔断状态:  已触发 🔴(冲击前就已经在熔断中)")
    elif result.would_trip_breaker:
        lines.append("  熔断状态:  ⚠️ 这个冲击会触发熔断")
    else:
        lines.append("  熔断状态:  正常 🟢(这个冲击不会触发熔断)")

    lines.append(f"  总敞口/权益:{result.gross_exposure_ratio:.1%}")
    lines.append(f"  净敞口/权益:{result.net_exposure_ratio:.1%}")

    if result.position_breaches:
        lines.append("")
        lines.append("  超出单笔仓位上限的持仓:")
        for b in result.position_breaches:
            lines.append(f"    · {b.symbol}: {b.shocked_weight:.1%} > 上限 {b.cap:.1%}")

    return "\n".join(lines)
