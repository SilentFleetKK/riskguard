"""示例 5:审计追溯——每一次裁决都留下防篡改的记录。

演示:所有风控裁决/熔断/成交写入带哈希链的 JSONL 日志;任何人事后删改中间
一条,verify() 立刻发现。事后复盘"这单当时到底为什么被拒",全靠它。
运行:python examples/05_audit_trail.py
"""

import _bootstrap  # noqa: F401

import os
import tempfile

from riskguard import JsonlAuditSink, Order, PaperBroker, RiskConfig, RiskEngine, Side


def main() -> None:
    path = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
    broker = PaperBroker(cash=100_000, marks={"AAPL": 200.0})

    with JsonlAuditSink(path) as audit:
        engine = RiskEngine(RiskConfig(max_position_pct=0.10), broker=broker, audit=audit)
        engine.check(Order("AAPL", Side.BUY, 1000), broker.get_portfolio())  # 会被缩
        engine.submit(Order("AAPL", Side.BUY, 40), broker.get_portfolio())   # 放行

    print(f"审计日志:  {path}\n")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            print("  " + line.strip()[:110] + " ...")

    print(f"\n哈希链校验:  {'完好 ✅' if JsonlAuditSink.verify(path) else '被篡改 ❌'}")

    # 模拟有人偷改中间一条记录
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    lines[0] = lines[0].replace('"resize"', '"approve"')
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    print(f"篡改后再校验:  {'完好 ✅' if JsonlAuditSink.verify(path) else '被篡改 ❌ ——链断了,证据确凿'}")


if __name__ == "__main__":
    main()
