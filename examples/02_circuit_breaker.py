"""示例 2:总亏损熔断——回撤触及红线,系统立刻停手。

演示:权益从高点回撤超过 15% 时自动熔断,之后新开仓被拒、减仓仍放行;
人工复盘后 reset 才能重启。
运行:python examples/02_circuit_breaker.py
"""

import _bootstrap  # noqa: F401

from riskguard import Order, PaperBroker, RiskConfig, RiskEngine, Side


def main() -> None:
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    # 关掉隔离/仓位干扰,聚焦熔断
    engine = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15, max_position_pct=1.0), broker=broker
    )

    engine.update_equity(broker.get_portfolio())
    print(f"权益高点:  {engine.state.high_water_mark:,.0f}")

    # 模拟一路亏到 84k(-16%,击穿 15% 红线)
    broker._cash = 84_000
    state = engine.update_equity(broker.get_portfolio())
    print(f"当前权益:  {state.last_equity:,.0f}  回撤 {state.drawdown:.1%}")
    print(f"熔断状态:  {'已触发 🔴' if state.breaker_tripped else '正常 🟢'}")
    print(f"触发原因:  {state.trip_reason}\n")

    buy = engine.check(Order("AAPL", Side.BUY, 1), broker.get_portfolio())
    print(f"尝试新开仓:  {buy.decision.value.upper()}  <- {buy.reasons()}")

    sell = engine.check(
        Order("AAPL", Side.SELL, 1, reduce_only=True), broker.get_portfolio()
    )
    print(f"尝试减仓:    {sell.decision.value.upper()}  (减仓永远放行)\n")

    # 人工复盘后重启
    engine.reset_breaker()
    print(f"复盘后重置:  熔断={'已触发' if engine.breaker_tripped else '已解除 ✅'}")


if __name__ == "__main__":
    main()
