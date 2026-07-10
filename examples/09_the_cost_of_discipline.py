"""示例 9:纪律的代价——风控不是免费的午餐。

examples/06 展示了风控在崩盘里救了你(−45% → −5.6%)。这个例子诚实地展示另一面:
同一档配置,套在单边牛市里,会让你少赚多少。

RiskGuard 的仓位上限把"下多大注"焊死在一个比例上——它拦住的不只是灾难性下跌,
也拦住了你本可以吃满的暴涨。风控是**用潜在上行换生存概率**的权衡,不是白拿的保险。
这个权衡值不值,取决于你更怕"错过一次暴涨"还是更怕"没能活到下一次机会"。

⚠️ 确定性模拟,数字是"仓位封顶"这条纪律的机械结果,不代表任何未来收益。
运行:python examples/09_the_cost_of_discipline.py
"""

import _bootstrap  # noqa: F401

from riskguard import RiskConfig
from riskguard.backtest import compare

# 单边牛市:100 一路涨到 300(+200%),涨幅与 06 示例的崩盘幅度对称
PRICES = [100, 110, 121, 133, 146, 161, 177, 195, 214, 236, 260, 286, 300]


def main() -> None:
    cfg = RiskConfig(max_position_pct=0.10, max_drawdown_pct=0.15)
    res = compare(PRICES, lambda i, p, ps: 1.0, config=cfg, cash=100_000)
    naive, guard = res["naive"], res["guarded"]

    print("场景:同一段 +200% 单边牛市,同一个「满仓做多」信号\n")
    print(f"{'':<16}{'裸奔账户':>14}{'RiskGuard 账户':>18}")
    print(f"{'单笔仓位占比':<14}{'100%':>14}{'10%(自动缩)':>20}")
    print(f"{'期末权益':<16}{naive.final_equity:>14,.0f}{guard.final_equity:>18,.0f}")
    print(f"{'总收益':<17}{naive.total_return:>13.1%}{guard.total_return:>17.1%}")

    captured = guard.total_return / naive.total_return
    print(f"\n风控账户只吃到了裸奔涨幅的 {captured:.0%}。")
    print("这就是 examples/06 里那份「防崩盘」保险的真实保费——")
    print("纪律截断的是整个分布的两端:既拦住暴跌,也拦住暴涨。")
    print("\n值不值,是你自己的判断:更怕错过一次暴涨,还是更怕没能活到下一次机会。")


if __name__ == "__main__":
    main()
