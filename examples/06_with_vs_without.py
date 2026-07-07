"""示例 6:裸奔 vs 套上 RiskGuard —— 同一个满仓信号,同一段崩盘行情。

这不是"能不能赚钱"的演示,而是"能不能不爆仓"的演示。两个账户拿一模一样的
"满仓做多"冲动,喂给同一段 -45% 的崩盘行情:一个不设任何风控,一个套上 RiskGuard
(单笔 10% 上限 + 15% 回撤熔断)。看最大回撤的差距。

⚠️ 这是一段确定性的模拟脚本,数字是"仓位上限"这条纪律的机械结果,不代表任何未来收益。
运行:python examples/06_with_vs_without.py
"""

import _bootstrap  # noqa: F401

from riskguard import Order, PaperBroker, RiskConfig, RiskEngine, Side

SYMBOL = "ACME"
# 一段崩盘:100 一路跌到 55(-45%)
PRICE_PATH = [100, 96, 90, 82, 75, 70, 64, 60, 57, 55]
START_CASH = 100_000


def run(guarded: bool) -> tuple[float, float]:
    """跑一遍行情,返回 (期末权益, 最大回撤)。"""
    broker = PaperBroker(START_CASH, marks={SYMBOL: PRICE_PATH[0]})
    engine = (
        RiskEngine(RiskConfig(max_position_pct=0.10, max_drawdown_pct=0.15), broker=broker)
        if guarded
        else None
    )

    peak = START_CASH
    max_dd = 0.0
    for price in PRICE_PATH:
        broker.set_mark(SYMBOL, price)
        # "满仓做多"的冲动:想用当前全部权益买入
        equity = broker.get_account().equity
        want = int(equity / price)
        if want > 0:
            order = Order(SYMBOL, Side.BUY, want)
            if guarded:
                engine.update_equity(broker.get_portfolio())  # 刷新熔断
                engine.submit(order, broker.get_portfolio())   # 风控放行/缩单/拒单
            else:
                # 裸奔:只要有现金就照单全收
                affordable = int(broker.get_account().cash / price)
                if affordable > 0:
                    broker.submit_order(Order(SYMBOL, Side.BUY, affordable))

        equity = broker.get_account().equity
        peak = max(peak, equity)
        max_dd = max(max_dd, 1.0 - equity / peak)

    return broker.get_account().equity, max_dd


def main() -> None:
    naive_eq, naive_dd = run(guarded=False)
    guard_eq, guard_dd = run(guarded=True)

    print("场景:ACME 从 100 一路崩到 55(-45%),两个账户用同一个「满仓做多」信号\n")
    print(f"{'':<16}{'裸奔账户':>14}{'RiskGuard 账户':>18}")
    print(f"{'单笔仓位占比':<14}{'100%':>14}{'10%(自动缩)':>20}")
    print(f"{'期末权益':<16}{naive_eq:>14,.0f}{guard_eq:>18,.0f}")
    print(f"{'最大回撤':<16}{naive_dd:>13.1%}{guard_dd:>17.1%}")
    print(f"\n同一段崩盘,风控把最大回撤从 {naive_dd:.0%} 压到 {guard_dd:.1%}。")
    print("纪律不是让你多赚,是让你别在最坏的一天把本金亏光。")


if __name__ == "__main__":
    main()
