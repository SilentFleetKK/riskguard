"""示例 12:压力测试——"如果我的持仓明天集体跌 20%,我扛得住吗"。

不是预测危机,是推演。在坏事真的发生之前,先在纸面上把最坏的情况算一遍:
给当前持仓一个统一的价格冲击,看权益、回撤、离熔断线的距离,以及哪些仓位会
因此超出你自己设的上限。

⚠️ 绝对只读:压力测试不会碰引擎的真实状态——不触发熔断、不写审计、不碰持久化。
它只是一次性的"假设推演",算完就完,不会因为你问了一句"如果会怎样"就把这个
假设情景误当成真实发生过的事写进历史记录。

运行:python examples/12_stress_test.py
"""

import _bootstrap  # noqa: F401

from riskguard import Account, Portfolio, Position, RiskConfig, RiskEngine
from riskguard.reporting import render_stress_text, run_stress_test


def main() -> None:
    config = RiskConfig(max_position_pct=0.15, max_drawdown_pct=0.15)
    engine = RiskEngine(config)
    engine.update_equity(Portfolio(Account(equity=100_000)))  # 确立高水位 100k

    portfolio = Portfolio(
        Account(equity=100_000, cash=25_000),
        positions={
            "AAPL": Position("AAPL", 250, 200.0),  # 多头,权重 50%
            "TSLA": Position("TSLA", -100, 250.0),  # 空头对冲,权重 25%
        },
        marks={"AAPL": 200.0, "TSLA": 250.0},
    )

    for shock in (-0.10, -0.20, -0.35):
        result = run_stress_test(engine, portfolio, shock)
        print(render_stress_text(result))
        print()

    # 确认压力测试全程没有碰过引擎的真实状态
    print(f"引擎真实高水位(应仍是 100,000,没被任何一次假设推演改动):"
          f" {engine.state.high_water_mark:,.0f}")
    print(f"引擎真实熔断状态(应仍是正常,没被任何一次假设推演触发):"
          f" {'已触发' if engine.breaker_tripped else '正常'}")


if __name__ == "__main__":
    main()
