"""示例 1:单笔仓位上限——别把身家压在一个想法上。

演示风控如何把一笔"过大"的订单自动缩到权益的 10% 以内。
运行:python examples/01_position_limit.py
"""

import _bootstrap  # noqa: F401  引入 src 到路径

from riskguard import Order, PaperBroker, RiskConfig, RiskEngine, Side


def main() -> None:
    # 10 万本金,AAPL 现价 200
    broker = PaperBroker(cash=100_000, marks={"AAPL": 200.0})
    engine = RiskEngine(RiskConfig(max_position_pct=0.10), broker=broker)

    # 手一抖,想买 1000 股 = 20 万 = 权益的 200%
    order = Order("AAPL", Side.BUY, 1000)
    decision = engine.check(order, broker.get_portfolio())

    print(f"原始下单:  {order.quantity:.0f} 股  (占权益 {1000 * 200 / 100_000:.0%})")
    print(f"风控裁决:  {decision.decision.value.upper()}")
    print(f"放行数量:  {decision.order.quantity:.0f} 股  (占权益 "
          f"{decision.order.quantity * 200 / 100_000:.0%})")
    print(f"原因:      {decision.reasons()}")

    # 真正提交(进模拟盘)
    broker_order = engine.submit(order, broker.get_portfolio())
    print(f"\n已成交:    {broker_order.filled_quantity:.0f} 股 @ "
          f"{broker_order.filled_avg_price:.2f}")
    print(f"当前持仓:  {broker.get_positions()}")


if __name__ == "__main__":
    main()
