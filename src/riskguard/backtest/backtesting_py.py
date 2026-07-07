"""`backtesting.py` 适配器:把 RiskGuard 作为风险叠加层接进 Strategy。

``backtesting.py`` 是可选依赖(``pip install "riskguard[backtesting]"``),这里对它
**懒加载**——本模块不装它也能 import;只有真正调用 :func:`make_riskguard_strategy`
时才需要它。

用法::

    from riskguard import RiskConfig
    from riskguard.backtest import make_riskguard_strategy

    Base = make_riskguard_strategy(RiskConfig(max_position_pct=0.10))

    class MyStrategy(Base):
        def signal(self) -> float:          # 返回目标权重,+1 满仓多 / 0 平 / −1 满仓空
            fast = self.data.Close[-1]
            ...
            return 1.0 if bullish else 0.0

    # bt = Backtest(data, MyStrategy); bt.run()
"""

from __future__ import annotations

from typing import Optional

from ..config import RiskConfig
from ..models import Account, Portfolio, Position
from .overlay import RiskOverlay

_SYMBOL = "ASSET"


def _load_backtesting():
    try:
        import backtesting  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ImportError(
            'backtesting.py not installed; run: pip install "riskguard[backtesting]"'
        ) from exc
    return backtesting


def _portfolio_from(equity: float, cur_qty: float, price: float) -> Portfolio:
    """用 backtesting.py 的当前状态拼出一个 RiskGuard 组合快照。

    现金 = 权益 − 持仓市值(单标的账本),避免把持仓市值和现金重复计入。
    """
    positions = (
        {_SYMBOL: Position(_SYMBOL, cur_qty, price)} if cur_qty != 0.0 else {}
    )
    cash = equity - cur_qty * price
    return Portfolio(Account(equity=equity, cash=cash), positions, {_SYMBOL: price})


def make_riskguard_strategy(config: Optional[RiskConfig] = None):
    """返回一个继承 ``backtesting.Strategy`` 的基类,内置 RiskGuard 风控闸门。

    子类只需实现 :meth:`signal` 返回目标权重([−1, 1]);基类每个 bar 把它过一遍风控
    (仓位上限 / 回撤熔断 / 敞口),然后按批准权重的**方向**做 long/flat(或 short/flat)
    建平仓:空仓时开到目标、方向反了先平、同向不重复加仓。可覆盖 ``rg_init()`` 初始化。

    局限(见 :meth:`_rebalance_to`):这是"方向 + 首次开仓封顶"的便利起点,不持续把已有
    持仓精调到变化的目标权重——需要动态调仓请按目标股数自行下 delta 单。
    """
    backtesting = _load_backtesting()
    _config = config or RiskConfig()

    class RiskGuardStrategy(backtesting.Strategy):  # type: ignore[misc, name-defined]
        risk_config = _config

        def init(self) -> None:
            self._overlay = RiskOverlay(config=self.risk_config, symbol=_SYMBOL)
            self.rg_init()

        # ---- 子类可覆盖 ----
        def rg_init(self) -> None:
            """可选初始化钩子。"""

        def signal(self) -> float:
            raise NotImplementedError("override signal() -> target weight in [-1, 1]")

        # ---- 内部 ----
        def next(self) -> None:
            price = float(self.data.Close[-1])
            equity = float(self.equity)
            cur_qty = float(self.position.size)
            portfolio = _portfolio_from(equity, cur_qty, price)
            approved = self._overlay.approved_target_weight(
                self.signal(), price, portfolio
            )
            self._rebalance_to(approved)

        def _rebalance_to(self, weight: float) -> None:
            """按批准权重的**方向**建/平仓(long/flat 或 short/flat 模式)。

            只在"空仓 → 开到目标"时下单——此时账户几乎全为现金,backtesting.py 的
            ``size=fraction``(按可用资金的比例)恰好等于目标权重;方向反了就先平仓。
            **不会**每个 bar 对同向持仓重复加仓(那会不断叠单、失控杠杆)。局限:它不
            持续把已有持仓精调到变化的目标权重;要动态调仓请自行按目标股数下 delta 单。
            """
            want_long = weight > 1e-6
            want_short = weight < -1e-6

            # 方向不符(或要求清仓)→ 先平掉当前持仓
            if self.position and (
                (self.position.is_long and not want_long)
                or (self.position.is_short and not want_short)
            ):
                self.position.close()

            # 仅在空仓时开到目标;已同向持仓则不重复加仓
            if want_long and not self.position.is_long:
                self.buy(size=min(0.999, weight))
            elif want_short and not self.position.is_short:
                self.sell(size=min(0.999, abs(weight)))

    return RiskGuardStrategy
