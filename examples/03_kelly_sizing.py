"""示例 3:动态仓位——用 Kelly 判据决定"下多大注"。

演示:给定胜率和盈亏比,分数 Kelly 算出理论仓位;无正期望时自动不下注。
注意"下多大注"由 sizer 决定,"能不能下、要不要缩"仍由风控规则二次把关。
运行:python examples/03_kelly_sizing.py
"""

import _bootstrap  # noqa: F401

from riskguard import KellySizer, PaperBroker, RiskConfig, RiskEngine, Side, Signal


def main() -> None:
    broker = PaperBroker(cash=100_000, marks={"AAPL": 200.0})
    config = RiskConfig(kelly_fraction=0.5, max_position_pct=0.10)
    engine = RiskEngine(config, sizer=KellySizer(), broker=broker)

    # 有正期望:胜率 60%,盈亏比 2:1
    good = Signal("AAPL", Side.BUY, price=200.0, win_probability=0.60, payoff_ratio=2.0)
    weight = KellySizer().target_weight(good, broker.get_portfolio(), config)
    print(f"胜率 60% / 盈亏比 2.0:")
    print(f"  满 Kelly f* = p - q/b = 0.6 - 0.4/2 = 0.40")
    print(f"  半 Kelly 目标权重 = {weight:.0%}")

    order = engine.sizer.size(good, broker.get_portfolio(), config)
    print(f"  Kelly 建议数量 = {order.quantity:.0f} 股(20% 权益)")

    # 但仓位上限规则只放行 10% —— 两层职责分离在这里体现
    decision = engine.check(order, broker.get_portfolio())
    print(f"  风控二次把关 -> {decision.decision.value.upper()} "
          f"到 {decision.order.quantity:.0f} 股(10% 上限)\n")

    # 无正期望:胜率 40%,盈亏比 1:1 -> 不下注
    bad = Signal("XYZ", Side.BUY, price=50.0, win_probability=0.40, payoff_ratio=1.0)
    w_bad = KellySizer().target_weight(bad, broker.get_portfolio(), config)
    print(f"胜率 40% / 盈亏比 1.0(无优势):目标权重 = {w_bad:.0%} —— 不下注就是最好的下注")


if __name__ == "__main__":
    main()
