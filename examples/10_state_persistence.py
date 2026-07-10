"""示例 10:状态持久化——堵住"重启绕过熔断"的后门。

RiskState 默认纯内存:进程一重启,高水位和熔断标记全部归零。这等于给亏红眼的
操作者留了一条"重启一下继续交易"的隐藏后路——与"纪律不能靠意志力,必须让系统
强制执行"的立库初衷直接矛盾。

这个例子用两个**完全独立的引擎实例**(模拟"进程 1"跑到熔断、"进程 2"重启后接管)
演示:只要连到同一个 SqliteStateStore,熔断状态就扛得住重启。
运行:python examples/10_state_persistence.py
"""

import _bootstrap  # noqa: F401

import os
import tempfile

from riskguard import Order, PaperBroker, RiskConfig, RiskEngine, Side, SqliteStateStore


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "risk_state.db")
    config = RiskConfig(max_drawdown_pct=0.15, max_position_pct=1.0)

    # ---- "进程 1":正常交易,权益跌破熔断线 ----
    store1 = SqliteStateStore(db_path)
    broker1 = PaperBroker(cash=100_000, marks={"AAPL": 100.0})
    engine1 = RiskEngine(config, broker=broker1, state_store=store1)

    engine1.update_equity(broker1.get_portfolio())  # 权益高点 = 100k
    broker1._cash = 80_000  # 模拟亏到 -20%
    engine1.update_equity(broker1.get_portfolio())
    print(f"[进程 1] 权益 -20% -> 熔断: {'已触发 🔴' if engine1.breaker_tripped else '正常 🟢'}")
    store1.close()
    print("[进程 1] 进程退出(比如崩溃、部署重启、或有人手贱 Ctrl-C 又拉起来)。\n")

    # ---- "进程 2":全新引擎、全新 broker,唯一相同的是连到同一个存档 ----
    store2 = SqliteStateStore(db_path)
    broker2 = PaperBroker(cash=80_000, marks={"AAPL": 100.0})
    engine2 = RiskEngine(config, broker=broker2, state_store=store2)

    print(f"[进程 2 / 重启后] 熔断状态: {'已触发 🔴(扛住了重启)' if engine2.breaker_tripped else '正常 🟢'}")
    print(f"[进程 2 / 重启后] 权益高点: {engine2.state.high_water_mark:,.0f}(从存档恢复,不是从零开始)")

    decision = engine2.check(Order("AAPL", Side.BUY, 1), broker2.get_portfolio())
    print(f"\n重启后尝试新开仓: {decision.decision.value.upper()}  <- {decision.reasons()}")

    reduce = engine2.check(Order("AAPL", Side.SELL, 1, reduce_only=True), broker2.get_portfolio())
    print(f"重启后尝试减仓:   {reduce.decision.value.upper()}(减仓永远放行)")

    store2.close()
    print("\n没有 state_store 的话,「进程 2」会把高水位和熔断全部清零,")
    print("重启就成了绕过纪律最简单的办法。现在它扛住了。")


if __name__ == "__main__":
    main()
