"""示例 4:实时监控守护——在你情绪失控时替你踩刹车。

演示:后台守护进程周期性观测权益,一旦回撤触及红线,自动熔断并平掉所有持仓
(kill-switch)。这里用极短 interval + 直接推进行情来快速演示。
运行:python examples/04_monitor_daemon.py
"""

import _bootstrap  # noqa: F401

import time

from riskguard import Order, PaperBroker, RiskConfig, RiskEngine, RiskMonitor, Side


def main() -> None:
    broker = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    engine = RiskEngine(
        RiskConfig(max_drawdown_pct=0.15, max_position_pct=1.0), broker=broker
    )

    # 先建一笔多头持仓
    engine.submit(Order("AAPL", Side.BUY, 500), broker.get_portfolio())
    engine.update_equity(broker.get_portfolio())
    print(f"建仓后持仓:  {broker.get_positions()}")

    tripped = {"fired": False}

    def on_trip(state):
        tripped["fired"] = True
        print(f"\n🚨 熔断触发!回撤 {state.drawdown:.1%} -> 自动平仓")

    monitor = RiskMonitor(
        engine, broker, interval=0.05, auto_liquidate=True, on_trip=on_trip
    )
    monitor.start()
    print("监控守护已启动,后台盯盘中...")

    # 行情崩了:AAPL 100 -> 70(-30%)
    for price in (95, 88, 80, 70):
        broker.set_mark("AAPL", price)
        time.sleep(0.1)
        if tripped["fired"]:
            break

    monitor.stop()
    print(f"\n最终持仓:    {broker.get_positions()}  (已被 kill-switch 清空)")
    print(f"熔断状态:    {'已触发' if engine.breaker_tripped else '正常'}")


if __name__ == "__main__":
    main()
