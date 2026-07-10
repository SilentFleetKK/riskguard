"""示例 11:每日体检——"让 AI 帮你做每日体检,而不是每分钟盯盘"。

不用你自己整天盯着账户。每天(或每次你想check-in的时候),观测一次当前持仓,
拿到一份结构化摘要:权益、离熔断线还有多远、哪些持仓已经贴近或超出仓位上限、
有没有策略还在隔离观察期。

这份摘要是**事实**,不是判断——数字都能直接复算验证。把它喂给一个 AI agent
(比如 Claude),让它去叙述、去推送提醒,是刻意的分工:AI 负责把数字变成人话,
RiskGuard 只负责保证这些数字是真的。`report.to_dict()` 就是这条分工线——结构化
数据交给 agent,不掺入任何"AI 觉得这次不一样"式的模糊判断。

运行:python examples/11_daily_digest.py
"""

import _bootstrap  # noqa: F401

import json
import os
import tempfile

from riskguard import Account, Portfolio, Position, RiskConfig, RiskEngine, SqliteStateStore
from riskguard.reporting import build_digest, render_digest_text


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "portfolio_state.db")
    config = RiskConfig(max_position_pct=0.10, max_drawdown_pct=0.15)

    # ---- 第 1 天:建仓,观测一次,确立高水位 ----
    store = SqliteStateStore(db_path)
    engine = RiskEngine(config, state_store=store)
    portfolio_day1 = Portfolio(
        Account(equity=100_000, cash=42_000),
        positions={
            "AAPL": Position("AAPL", 100, 190.0),
            "TSLA": Position("TSLA", -30, 250.0),  # 一笔对冲空头
        },
        marks={"AAPL": 190.0, "TSLA": 250.0},
    )
    engine.update_equity(portfolio_day1)
    print("=== 第 1 天体检 ===")
    print(render_digest_text(build_digest(engine, portfolio_day1)))
    store.close()

    # ---- 第 5 天:市场跌了一些,重新观测(全新引擎,模拟另一次 check-in)----
    store2 = SqliteStateStore(db_path)
    engine2 = RiskEngine(config, state_store=store2)
    portfolio_day5 = Portfolio(
        Account(equity=92_000, cash=42_000),
        positions={
            "AAPL": Position("AAPL", 100, 190.0),
            "TSLA": Position("TSLA", -30, 250.0),
        },
        marks={"AAPL": 175.0, "TSLA": 240.0},
    )
    engine2.update_equity(portfolio_day5)
    report = build_digest(engine2, portfolio_day5)
    print("\n=== 第 5 天体检 ===")
    print(render_digest_text(report))

    print("\n=== 交给 AI agent 的结构化数据(to_dict)===")
    print(json.dumps(report.to_dict(), indent=2, default=str)[:400], "...")
    store2.close()


if __name__ == "__main__":
    main()
