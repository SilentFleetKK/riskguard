"""轻量价格重放器——用来**看见/测试** RiskGuard 在回测里的行为。

它逐 bar 重放一段价格,把一个"目标权重"策略喂给 :class:`RiskOverlay` + 纸面模拟盘,
产出权益曲线、最大回撤与风控统计。同一策略还能一键跑"套风控 vs 不套风控"的对比。

**定位声明**:这**不是**通用回测框架——策略研究请用 backtesting.py / vectorbt(见
本包的适配器)。这个重放器只做一件事:让你直观看到风控叠加层拦下了什么。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ..brokers.paper import PaperBroker
from ..config import RiskConfig
from ..engine import RiskEngine
from ..models import Order, Side
from .overlay import RiskOverlay

_EPS = 1e-9

# 策略签名:(bar 序号, 当前价, 全部价格) -> 目标权重(带符号,+ 多 / − 空 / 0 平)
TargetWeightFn = Callable[[int, float, Sequence[float]], float]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """一次重放的结果。

    ``equity_curve[0]`` 即起始资本(起点基线);其后每点为逐 bar 的期末权益。
    ``max_drawdown`` 与 ``total_return`` 都以这个起始资本为共同基线,彼此一致。
    """

    equity_curve: tuple[float, ...]
    max_drawdown: float
    final_equity: float
    trades: int
    stats: Mapping[str, int] = field(default_factory=dict)

    @property
    def total_return(self) -> float:
        if not self.equity_curve:
            return 0.0
        start = self.equity_curve[0]  # = 起始资本
        return (self.final_equity / start - 1.0) if start else 0.0


def replay(
    prices: Sequence[float],
    strategy: TargetWeightFn,
    *,
    symbol: str = "ASSET",
    cash: float = 100_000.0,
    config: RiskConfig | None = None,
    slippage_bps: float = 0.0,
    commission_bps: float = 0.0,
    risk_managed: bool = True,
) -> ReplayResult:
    """逐 bar 重放 ``prices``,执行 ``strategy`` 给出的目标权重。

    ``risk_managed=True`` 时订单过 RiskGuard 风控叠加层;``False`` 时裸执行
    (只受现金约束),用于 A/B 对比。返回 :class:`ReplayResult`。
    """
    if not prices:
        raise ValueError("prices must be non-empty")

    broker = PaperBroker(
        cash,
        marks={symbol: prices[0]},
        slippage_bps=slippage_bps,
        commission_bps=commission_bps,
    )
    overlay: RiskOverlay | None = None
    if risk_managed:
        engine = RiskEngine(config or RiskConfig(), broker=broker)
        overlay = RiskOverlay(engine=engine, symbol=symbol)

    # 曲线以起始资本为第 0 点,回撤与收益共用同一基线(起始资本),彼此一致。
    curve: list[float] = [float(cash)]
    peak = float(cash)
    max_dd = 0.0
    trades = 0

    for i, price in enumerate(prices):
        # 坏 tick(价格非正)对两条路径一律跳过:不更新标记价、不下单,权益按上一有效价重估。
        if price > 0:
            broker.set_mark(symbol, price)
            target_weight = strategy(i, price, prices)
            if overlay is not None:
                order = overlay.target_weight_to_order(
                    target_weight, price, broker.get_portfolio()
                ).order
            else:
                order = _naive_order(symbol, target_weight, price, broker)
            if order is not None:
                broker.submit_order(order)
                trades += 1

        equity = broker.get_account().equity
        curve.append(equity)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, 1.0 - equity / peak)

    return ReplayResult(
        equity_curve=tuple(curve),
        max_drawdown=max_dd,
        final_equity=curve[-1],
        trades=trades,
        stats=dict(overlay.stats) if overlay is not None else {},
    )


def compare(
    prices: Sequence[float], strategy: TargetWeightFn, **kwargs: object
) -> dict[str, ReplayResult]:
    """同一策略跑"套风控 vs 不套风控",返回 ``{"guarded": ..., "naive": ...}``。"""
    guarded = replay(prices, strategy, risk_managed=True, **kwargs)  # type: ignore[arg-type]
    naive = replay(prices, strategy, risk_managed=False, **kwargs)  # type: ignore[arg-type]
    return {"guarded": guarded, "naive": naive}


def _naive_order(
    symbol: str, target_weight: float, price: float, broker: PaperBroker
) -> Order | None:
    """裸执行(无风控)基线:目标权重 → 差额订单,**仅受现金约束**(不加杠杆)。

    注意:买单按可用现金封顶,因此当已有未实现盈亏、权益偏离现金时,满仓目标会被
    现金上限"欠配"一点点——这是刻意的"不借钱加仓"基线,不是 bug。
    """
    account = broker.get_account()
    equity = account.equity
    if price <= 0 or equity <= 0:
        return None
    pos = broker.get_positions().get(symbol)
    cur_qty = pos.quantity if pos else 0.0
    target_qty = target_weight * equity / price
    delta = target_qty - cur_qty
    if abs(delta) < _EPS:
        return None
    if delta > 0:
        affordable = max(0.0, account.cash / price)
        delta = min(delta, affordable)
        if delta < _EPS:
            return None
        return Order(symbol, Side.BUY, delta, strategy_id="naive")
    reduce_only = abs(target_qty) <= abs(cur_qty) and (
        target_qty == 0.0 or (target_qty > 0) == (cur_qty > 0)
    )
    return Order(symbol, Side.SELL, abs(delta), reduce_only=reduce_only, strategy_id="naive")
