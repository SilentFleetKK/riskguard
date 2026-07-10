"""RiskGuard 命令行工具(零依赖,标准库 argparse + csv)。

安装后即可用 ``riskguard`` 命令,或 ``python -m riskguard``:

- ``riskguard presets``  列出三档配置预设及关键参数
- ``riskguard check``    用某档预设检查一笔订单(放行/缩量/拒单 + 原因)
- ``riskguard replay``   对一段价格跑"买入持有:套风控 vs 不套"的回撤对比

退出码:``0`` 放行(原样)· ``3`` 放行但被缩量 · ``1`` 拒单 · ``2`` 用法/输入/IO 错误。
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections.abc import Sequence

from . import __version__
from .engine import RiskEngine
from .exceptions import ConfigError, RiskGuardError
from .models import Account, Order, Portfolio, Position, Side
from .persistence import SqliteStateStore
from .presets import PRESETS, get_preset


# --------------------------------------------------------------------------- #
# 输入校验(系统边界,快速失败)
# --------------------------------------------------------------------------- #
def _finite_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是合法数字:{text!r}") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"必须是有限数(不接受 nan/inf):{text!r}")
    return value


def _positive_float(text: str) -> float:
    value = _finite_float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"必须 > 0:{text!r}")
    return value


def _build_portfolio(
    equity: float, symbol: str, price: float, position_qty: float
) -> Portfolio:
    """由 CLI 输入拼一个组合快照。约定:``--equity`` 为权威权益值,
    现金 = 权益 − 持仓市值(单标的账本;可为负,即透支/穿仓)。"""
    positions = (
        {symbol: Position(symbol, position_qty, price)} if position_qty != 0.0 else {}
    )
    cash = equity - position_qty * price
    return Portfolio(Account(equity=equity, cash=cash), positions, {symbol: price})


def _fmt_weight(qty: float, price: float, equity: float) -> str:
    if equity > 0 and math.isfinite(equity):
        return f"{qty * price / equity:.1%}"
    return "n/a"


def _warn_if_aggressive(preset: str) -> None:
    if preset == "aggressive":
        print(
            "note: aggressive 档口径明显更松(单笔 20% / 回撤 25% / 1.5× 敞口),"
            "仅适合能承受更大回撤、清楚自己在做什么的人。",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_presets(args: argparse.Namespace) -> int:
    rows = [
        ("单笔仓位上限", "max_position_pct"),
        ("回撤熔断线", "max_drawdown_pct"),
        ("总敞口上限", "max_gross_exposure_pct"),
        ("净敞口上限", "max_net_exposure_pct"),
        ("Kelly 系数", "kelly_fraction"),
        ("隔离天数", "quarantine_days"),
    ]
    names = ["conservative", "balanced", "aggressive"]
    header = f"{'参数':<14}" + "".join(f"{n:>16}" for n in names)
    print(header)
    print("-" * len(header))
    for label, field in rows:
        cells = []
        for n in names:
            val = getattr(PRESETS[n], field)
            cells.append("—" if val is None else f"{val:g}")
        print(f"{label:<14}" + "".join(f"{c:>16}" for c in cells))
    print("\n用法:riskguard check --preset balanced --equity 100000 "
          "--side buy --qty 1000 --price 200")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    _warn_if_aggressive(args.preset)
    config = get_preset(args.preset)

    # --state-db:跨多次 CLI 调用持久化高水位/熔断状态(例如 cron 定时验单)。
    # 没有它,每次调用都是一个全新引擎——脚本反复调用本身就是一种"重启绕过熔断"。
    # --state-key:同一个 db 文件跑多个策略/标的时必须用不同 key,否则会互相
    # 覆盖对方的状态(SqliteStateStore 用乐观锁检测这种冲突并报错,而不是静默覆盖)。
    store = (
        SqliteStateStore(args.state_db, key=args.state_key) if args.state_db else None
    )

    def _warn_persist_error(exc: BaseException) -> None:
        print(f"warning: 状态持久化写入失败,重启保护本次未生效: {exc!r}", file=sys.stderr)

    try:
        engine = RiskEngine(
            config,
            state_store=store,
            on_persist_error=_warn_persist_error if store is not None else None,
        )
        portfolio = _build_portfolio(args.equity, args.symbol, args.price, args.position)
        order = Order(
            symbol=args.symbol,
            side=Side(args.side),
            quantity=args.qty,
            reduce_only=args.reduce_only,
        )
        decision = engine.check(order, portfolio)
        equity = portfolio.equity

        print(f"预设:  {args.preset}")
        if store is not None:
            print(f"状态:  {args.state_db}(跨调用持久化)")
        print(
            f"请求:  {args.side.upper()} {args.qty:g} {args.symbol} @ {args.price:g}  "
            f"(占权益 {_fmt_weight(order.quantity, args.price, equity)})"
        )
        print(f"裁决:  {decision.decision.value.upper()}")
        if decision.approved:
            print(
                f"放行:  {args.side.upper()} {decision.order.quantity:g} {args.symbol}  "
                f"(占权益 {_fmt_weight(decision.order.quantity, args.price, equity)})"
            )
        reasons = decision.reasons()
        if reasons:
            print(f"原因:  {reasons}")

        if decision.rejected:
            return 1
        if decision.resized:
            return 3  # 放行但被缩量(便于脚本区分"原样通过")
        return 0
    finally:
        if store is not None:
            store.close()


def cmd_replay(args: argparse.Namespace) -> int:
    from .backtest import compare  # 延迟导入

    _warn_if_aggressive(args.preset)
    prices = _load_prices(args)
    config = get_preset(args.preset)
    result = compare(prices, lambda i, p, ps: 1.0, config=config, cash=args.cash)
    naive, guard = result["naive"], result["guarded"]
    print(f"预设:  {args.preset}  |  买入持有,{len(prices)} 个 bar,起始 {args.cash:,.0f}\n")
    print(f"{'':<10}{'裸奔':>14}{'RiskGuard':>16}")
    print(f"{'期末权益':<10}{naive.final_equity:>14,.0f}{guard.final_equity:>16,.0f}")
    print(f"{'总收益':<11}{naive.total_return:>13.1%}{guard.total_return:>15.1%}")
    print(f"{'最大回撤':<10}{naive.max_drawdown:>13.1%}{guard.max_drawdown:>15.1%}")
    return 0


# --------------------------------------------------------------------------- #
# 价格解析(用标准库 csv,坏行显式计数并告警,绝不静默篡改)
# --------------------------------------------------------------------------- #
def _coerce_prices(tokens: Sequence[str]) -> tuple[list[float], int]:
    prices: list[float] = []
    skipped = 0
    for tok in tokens:
        tok = (tok or "").strip()
        try:
            value = float(tok)
        except ValueError:
            skipped += 1
            continue
        if not math.isfinite(value) or value <= 0:
            skipped += 1  # 拒绝非正/非有限价,而不是静默接受
            continue
        prices.append(value)
    return prices, skipped


def _load_prices_csv(path: str, column: str | None) -> tuple[list[float], int]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))  # 正确处理引号/内嵌逗号,不再朴素 split
    if not rows:
        return [], 0
    header = rows[0]
    col_idx: int | None = None
    if column is not None:
        if column in header:
            col_idx = header.index(column)
        else:
            try:
                col_idx = int(column)
            except ValueError as exc:
                raise ConfigError(
                    f"CSV 找不到列 {column!r}(表头:{header})"
                ) from exc
    cells: list[str] = []
    for row in rows:
        if not row:
            continue
        idx = col_idx if col_idx is not None else len(row) - 1
        cells.append(row[idx] if 0 <= idx < len(row) else "")
    return _coerce_prices(cells)


def _load_prices(args: argparse.Namespace) -> list[float]:
    if args.prices is not None:
        prices, skipped = _coerce_prices(args.prices.split(","))
        source = "--prices"
    elif args.csv is not None:
        prices, skipped = _load_prices_csv(args.csv, args.csv_column)
        source = args.csv
    else:
        raise ConfigError("replay 需要 --prices 或 --csv 提供价格序列")
    if skipped:
        print(
            f"warning: 从 {source} 跳过了 {skipped} 个无法解析为正数价格的行/值",
            file=sys.stderr,
        )
    if not prices:
        raise ConfigError("未从输入解析到任何有效价格(检查列名/分隔/表头)")
    return prices


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riskguard",
        description="RiskGuard —— 券商无关的开源交易风控层。",
    )
    parser.add_argument(
        "--version", action="version", version=f"riskguard {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    sp = sub.add_parser("presets", help="列出三档配置预设及关键参数")
    sp.set_defaults(func=cmd_presets)

    sc = sub.add_parser("check", help="用某档预设检查一笔订单")
    sc.add_argument("--preset", default="balanced", choices=sorted(PRESETS))
    sc.add_argument("--equity", type=_finite_float, required=True, help="账户总权益(可 ≤0)")
    sc.add_argument("--symbol", default="ASSET")
    sc.add_argument("--side", choices=["buy", "sell"], required=True)
    sc.add_argument("--qty", type=_positive_float, required=True, help="下单数量(幅度 >0)")
    sc.add_argument("--price", type=_positive_float, required=True)
    sc.add_argument("--position", type=_finite_float, default=0.0, help="当前持仓(带符号)")
    sc.add_argument("--reduce-only", action="store_true")
    sc.add_argument(
        "--state-db",
        help="跨多次调用持久化高水位/熔断状态的 SQLite 文件路径(例如 cron 定时验单)。"
        "不传则每次调用都是全新状态,熔断不会跨调用保留。",
    )
    sc.add_argument(
        "--state-key",
        default="default",
        help="同一个 --state-db 文件跑多个策略/标的时,每个必须用不同的 --state-key,"
        "否则会互相覆盖对方的高水位/熔断状态(默认 'default')。",
    )
    sc.set_defaults(func=cmd_check)

    sr = sub.add_parser("replay", help="买入持有:套风控 vs 不套 的回撤对比")
    sr.add_argument("--preset", default="balanced", choices=sorted(PRESETS))
    src = sr.add_mutually_exclusive_group()  # --prices 与 --csv 互斥
    src.add_argument("--prices", help="逗号分隔的价格,如 100,96,90,82")
    src.add_argument("--csv", help="CSV 文件(默认取每行最后一列)")
    sr.add_argument("--csv-column", help="CSV 价格列名或列索引(默认最后一列)")
    sr.add_argument("--cash", type=_positive_float, default=100_000.0)
    sr.set_defaults(func=cmd_replay)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    try:
        return func(args)
    except (RiskGuardError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
