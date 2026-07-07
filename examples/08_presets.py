"""示例 8:三档配置预设——同一笔冲动,不同的风控口径。

不想逐个参数调?挑一档预设起步:保守/均衡/激进。看同一笔"满仓冲动"的买单,
在三档下分别被缩到多少。
运行:python examples/08_presets.py
"""

import _bootstrap  # noqa: F401

from riskguard import Order, PaperBroker, RiskEngine, Side, get_preset

PRESETS = ["conservative", "balanced", "aggressive"]


def main() -> None:
    print("同一笔:10 万本金,想买 1000 股 @ 200(= 20 万 = 200% 权益)\n")
    print(f"{'预设':<14}{'单笔上限':>10}{'放行数量':>10}{'占权益':>10}")
    print("-" * 44)
    for name in PRESETS:
        cfg = get_preset(name)
        broker = PaperBroker(100_000, marks={"AAPL": 200.0})
        engine = RiskEngine(cfg, broker=broker)
        d = engine.check(Order("AAPL", Side.BUY, 1000), broker.get_portfolio())
        qty = d.order.quantity
        print(f"{name:<14}{cfg.max_position_pct:>9.0%}{qty:>10.0f}{qty * 200 / 100_000:>10.1%}")
    print("\n挑一档起步,再用 config.replace(...) 微调成你自己的口径。")


if __name__ == "__main__":
    main()
