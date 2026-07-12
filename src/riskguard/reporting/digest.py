"""每日体检:不用你自己盯着账户,把"实时监控"和"异常检测"这两件事外包给
一份定期生成的简短汇总。

:func:`build_digest` 把引擎当前状态(高水位、熔断、隔离中的策略)和一份持仓快照,
组装成一个结构化的 :class:`DigestReport`——它是**事实**,不是判断:数字从哪来、
离哪条线还有多远,都是可以直接复算验证的,没有任何"AI 觉得这次不一样"式的模糊
判断。把这份结构化事实交给 AI agent 去叙述、去推送提醒,是刻意的分工:AI 负责
把噪音变成人话,RiskGuard 只负责保证这些数字是真的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..models import Portfolio
from ._util import weight_or_inf

if TYPE_CHECKING:
    from ..engine import RiskEngine


@dataclass(frozen=True, slots=True)
class PositionStanding:
    """一个持仓相对仓位上限的当前状态。"""

    symbol: str
    weight: float  # 占权益的比例(无符号)
    cap: float
    headroom: float  # 距离上限还有多少(cap - weight;负数表示已经超限)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "weight": self.weight,
            "cap": self.cap,
            "headroom": self.headroom,
        }


@dataclass(frozen=True, slots=True)
class QuarantineStanding:
    """一个仍在隔离观察期内的策略。"""

    strategy_id: str
    age_days: float
    quarantine_days: int
    days_remaining: float

    def to_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "age_days": self.age_days,
            "quarantine_days": self.quarantine_days,
            "days_remaining": self.days_remaining,
        }


@dataclass(frozen=True, slots=True)
class DigestReport:
    """一次"每日体检"的结构化结果。"""

    generated_at: datetime
    equity: float
    high_water_mark: float
    drawdown: float
    max_drawdown_pct: float
    headroom_to_breaker: float  # max_drawdown_pct - drawdown;负数表示已经熔断
    breaker_tripped: bool
    trip_reason: str
    max_daily_loss_pct: float | None
    session_date: str | None
    session_anchor_equity: float
    daily_loss: float
    daily_tripped: bool
    daily_trip_reason: str
    gross_exposure_ratio: float
    max_gross_exposure_pct: float
    net_exposure_ratio: float
    max_net_exposure_pct: float | None
    positions: tuple[PositionStanding, ...] = field(default_factory=tuple)
    quarantined_strategies: tuple[QuarantineStanding, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        """结构化输出,供 AI agent/脚本消费。"""
        return {
            "generated_at": self.generated_at.isoformat(),
            "equity": self.equity,
            "high_water_mark": self.high_water_mark,
            "drawdown": self.drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "headroom_to_breaker": self.headroom_to_breaker,
            "breaker_tripped": self.breaker_tripped,
            "trip_reason": self.trip_reason,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "session_date": self.session_date,
            "session_anchor_equity": self.session_anchor_equity,
            "daily_loss": self.daily_loss,
            "daily_tripped": self.daily_tripped,
            "daily_trip_reason": self.daily_trip_reason,
            "gross_exposure_ratio": self.gross_exposure_ratio,
            "max_gross_exposure_pct": self.max_gross_exposure_pct,
            "net_exposure_ratio": self.net_exposure_ratio,
            "max_net_exposure_pct": self.max_net_exposure_pct,
            "positions": [p.to_dict() for p in self.positions],
            "quarantined_strategies": [q.to_dict() for q in self.quarantined_strategies],
        }


def build_digest(
    engine: RiskEngine, portfolio: Portfolio, *, now: datetime | None = None
) -> DigestReport:
    """组装一份结构化日报。只读 ``engine.config``/``engine.state``,零副作用
    ——不调用 :meth:`~riskguard.engine.RiskEngine.check`/``update_equity``,
    不会顺带观测权益或触发熔断(那是 :meth:`RiskEngine.update_equity` 的职责,
    日报只是"照一张当前状态的照片")。
    """
    moment = now or datetime.now(timezone.utc)
    config = engine.config
    state = engine.state

    equity = portfolio.equity
    gross_ratio = portfolio.gross_exposure() / equity if equity > 0 else float("inf")
    net_ratio = portfolio.net_exposure() / equity if equity > 0 else float("inf")

    # 用 weight_or_inf 而不是 portfolio.weight():后者在 equity <= 0(账户已经
    # 穿仓)时会静默返回 0.0,导致"总敞口显示 inf(疯狂报警)、每个持仓却显示
    # 0% / 正的 headroom(看起来毫无风险)"这种自相矛盾的输出——账户资不抵债
    # 恰恰是日报最该显眼标出来的时刻。
    positions = tuple(
        PositionStanding(
            symbol=sym,
            weight=weight_or_inf(portfolio, sym),
            cap=config.max_position_pct,
            headroom=config.max_position_pct - weight_or_inf(portfolio, sym),
        )
        for sym in portfolio.positions
        if not portfolio.position(sym).is_flat
    )

    quarantined = []
    for strategy_id in state.strategy_inception:
        age = state.strategy_age_days(strategy_id, moment)
        if age is not None and age < config.quarantine_days:
            quarantined.append(
                QuarantineStanding(
                    strategy_id=strategy_id,
                    age_days=age,
                    quarantine_days=config.quarantine_days,
                    days_remaining=config.quarantine_days - age,
                )
            )

    return DigestReport(
        generated_at=moment,
        equity=equity,
        high_water_mark=state.high_water_mark,
        drawdown=state.drawdown,
        max_drawdown_pct=config.max_drawdown_pct,
        headroom_to_breaker=config.max_drawdown_pct - state.drawdown,
        breaker_tripped=state.breaker_tripped,
        trip_reason=state.trip_reason,
        max_daily_loss_pct=config.max_daily_loss_pct,
        session_date=state.session_date,
        session_anchor_equity=state.session_anchor_equity,
        daily_loss=state.daily_loss,
        daily_tripped=state.daily_tripped,
        daily_trip_reason=state.daily_trip_reason,
        gross_exposure_ratio=gross_ratio,
        max_gross_exposure_pct=config.max_gross_exposure_pct,
        net_exposure_ratio=net_ratio,
        max_net_exposure_pct=config.max_net_exposure_pct,
        positions=positions,
        quarantined_strategies=tuple(quarantined),
    )


def render_text(report: DigestReport) -> str:
    """把 :class:`DigestReport` 渲染成人类可读的文本报告。"""
    lines = [
        f"每日体检  ({report.generated_at.strftime('%Y-%m-%d %H:%M UTC')})",
        "",
        f"  权益:      {report.equity:,.0f}",
        f"  高水位:    {report.high_water_mark:,.0f}",
        f"  当前回撤:  {report.drawdown:.1%}  (熔断线 {report.max_drawdown_pct:.1%},"
        f" 距离 {report.headroom_to_breaker:.1%})",
    ]
    if report.breaker_tripped:
        lines.append(f"  熔断状态:  已触发 🔴  {report.trip_reason}")
    else:
        lines.append("  熔断状态:  正常 🟢")

    if report.max_daily_loss_pct is not None:
        headroom = report.max_daily_loss_pct - report.daily_loss
        lines.append(
            f"  日内亏损:  {report.daily_loss:.1%}  (日内线 "
            f"{report.max_daily_loss_pct:.1%}, 距离 {headroom:.1%}, "
            f"锚定 {report.session_anchor_equity:,.0f})"
        )
        if report.daily_tripped:
            lines.append(f"  日内熔断:  已触发 🔴  {report.daily_trip_reason}")

    lines.append(
        f"  总敞口/权益: {report.gross_exposure_ratio:.1%}  "
        f"(上限 {report.max_gross_exposure_pct:.1%})"
    )
    if report.max_net_exposure_pct is not None:
        lines.append(
            f"  净敞口/权益: {report.net_exposure_ratio:.1%}  "
            f"(上限 {report.max_net_exposure_pct:.1%})"
        )

    if report.positions:
        lines.append("")
        lines.append("  持仓:")
        for p in sorted(report.positions, key=lambda x: -x.weight):
            flag = " ⚠️" if p.headroom < 0 else ""
            lines.append(f"    · {p.symbol}: {p.weight:.1%} / 上限 {p.cap:.1%}{flag}")

    if report.quarantined_strategies:
        lines.append("")
        lines.append("  隔离观察中的策略:")
        for q in report.quarantined_strategies:
            lines.append(
                f"    · {q.strategy_id}: 第 {q.age_days:.0f} 天,"
                f" 还剩 {q.days_remaining:.0f} 天出观察期"
            )

    return "\n".join(lines)
