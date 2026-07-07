"""示例 7:把 RiskGuard 接进回测——双均线策略 + 风控叠加层。

同一个双均线(SMA 快线上穿慢线做多、下穿平仓)策略,跑同一段"涨→崩→假反弹→再崩"
的行情。对比"裸奔"和"套上 RiskGuard(单笔 50% 上限 + 15% 回撤熔断)":
看风控如何在崩盘中封顶仓位、并在假反弹时用熔断**阻止追高再入场**。

⚠️ 确定性模拟,数字是纪律的机械结果,不代表任何未来收益。
运行:python examples/07_backtest_overlay.py
"""

import _bootstrap  # noqa: F401

from riskguard import RiskConfig
from riskguard.backtest import compare

# 涨到 130 -> 崩到 90 -> 假反弹到 101 -> 再崩到 64
PRICES = [
    100, 103, 107, 111, 116, 121, 126, 130,        # 上涨
    125, 118, 108, 98, 90,                          # 第一波崩盘
    94, 99, 101,                                    # 假反弹(诱多)
    95, 87, 79, 72, 66, 64,                         # 第二波崩盘
]


def sma(values, n):
    if len(values) < n:
        return sum(values) / len(values)
    return sum(values[-n:]) / n


def ma_cross_strategy(i, price, prices):
    """双均线:快线(3)在慢线(6)之上 -> 满仓做多(权重 1.0),否则清仓(0.0)。"""
    window = list(prices[: i + 1])
    if len(window) < 6:
        return 0.0
    return 1.0 if sma(window, 3) > sma(window, 6) else 0.0


def main() -> None:
    # 故意把单笔上限放宽到 90%,好让三道防线在这一段行情里都出手给你看
    cfg = RiskConfig(max_position_pct=0.90, max_drawdown_pct=0.15)
    res = compare(PRICES, ma_cross_strategy, config=cfg, cash=100_000,
                  slippage_bps=2, commission_bps=1)
    g, n = res["guarded"], res["naive"]

    print("双均线策略,同一段「涨→崩→假反弹→再崩」行情\n")
    print(f"{'':<14}{'裸奔':>12}{'RiskGuard':>14}")
    print(f"{'期末权益':<14}{n.final_equity:>12,.0f}{g.final_equity:>14,.0f}")
    print(f"{'总收益':<15}{n.total_return:>11.1%}{g.total_return:>13.1%}")
    print(f"{'最大回撤':<14}{n.max_drawdown:>11.1%}{g.max_drawdown:>13.1%}")
    print("\nRiskGuard 三道防线全部出手:")
    print(f"  · 缩单 {g.stats['resized']} 次 —— 单笔仓位封顶")
    print(f"  · 熔断 {g.stats['breaker_trips']} 次 —— 回撤触线拉闸")
    print(f"  · 拦下 {g.stats['halted_bars']} 次追高再入场 —— 假反弹时不许你去接下落的飞刀")
    print("\n把风控叠加层接进回测,你的策略研究和爆仓防护第一次在同一条流水线上。")


if __name__ == "__main__":
    main()
