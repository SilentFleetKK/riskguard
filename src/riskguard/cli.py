"""RiskGuard 命令行工具(零依赖,标准库 argparse + csv)。

安装后即可用 ``riskguard`` 命令,或 ``python -m riskguard``:

- ``riskguard presets``  列出三档配置预设及关键参数
- ``riskguard check``    用某档预设检查一笔订单(放行/缩量/拒单 + 原因)
- ``riskguard replay``   对一段价格跑"买入持有:套风控 vs 不套"的回撤对比
- ``riskguard digest``   每日体检:高水位/回撤/熔断距离/持仓 vs 上限,一份摘要
- ``riskguard stress``   压力测试:给持仓统一冲击(如 -20%),看会不会触发熔断

退出码(便于脚本判断,各子命令含义略有不同):

- ``check``:   0 放行(原样)· 3 放行但被缩量 · 1 拒单 · 2 用法/输入/IO 错误
- ``digest``:  0 熔断正常 · 1 熔断已触发 · 2 用法/输入/IO 错误
- ``stress``:  0 冲击不会触发熔断/超限 · 3 冲击会触发熔断或超出仓位上限 · 2 用法/输入/IO 错误
- ``replay``/``presets``: 0 正常完成 · 2 用法/输入/IO 错误

⚠️ 同一个数字在不同子命令下含义不同(``check`` 的 1 = 订单被拒,``digest`` 的
1 = 熔断已触发)——脚本按退出码分支时必须连着子命令一起判断,不能只看数字。
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections.abc import Sequence

from . import __version__
from .engine import RiskEngine
from .exceptions import ConfigError, RiskGuardError
from .models import Account, Order, OrderType, Portfolio, Position, Side
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


def _parse_position_specs(specs: Sequence[str]) -> dict[str, Position]:
    """解析可重复的 ``--position SYMBOL:QTY:PRICE`` 参数,供 digest/stress 用。"""
    positions: dict[str, Position] = {}
    for spec in specs:
        parts = spec.split(":")
        if len(parts) != 3:
            raise ConfigError(f"--position 格式应为 SYMBOL:QTY:PRICE,收到 {spec!r}")
        symbol, qty_s, price_s = (p.strip() for p in parts)
        if not symbol:
            raise ConfigError(f"--position 标的代码不能为空:{spec!r}")
        try:
            qty = float(qty_s)
            price = float(price_s)
        except ValueError as exc:
            raise ConfigError(f"--position 数量/价格必须是数字:{spec!r}") from exc
        if not math.isfinite(qty) or qty == 0.0:
            raise ConfigError(f"--position 数量必须是非零有限数:{spec!r}")
        if not math.isfinite(price) or price <= 0.0:
            raise ConfigError(f"--position 价格必须是正的有限数:{spec!r}")
        if symbol in positions:
            raise ConfigError(f"--position 标的重复出现:{symbol!r}")
        positions[symbol] = Position(symbol, qty, price)
    return positions


def _build_portfolio_multi(equity: float, positions: dict[str, Position]) -> Portfolio:
    """由多笔 ``--position`` 拼一个组合快照,现金 = 权益 − 全部持仓市值之和。"""
    invested = sum(pos.quantity * pos.avg_price for pos in positions.values())
    cash = equity - invested
    marks = {sym: pos.avg_price for sym, pos in positions.items()}
    return Portfolio(Account(equity=equity, cash=cash), positions, marks)


def _open_state_store(args: argparse.Namespace) -> SqliteStateStore | None:
    return SqliteStateStore(args.state_db, key=args.state_key) if args.state_db else None


def _warn_persist_error(exc: BaseException) -> None:
    print(f"warning: 状态持久化写入失败,重启保护本次未生效: {exc!r}", file=sys.stderr)


def _load_state_readonly(args: argparse.Namespace):
    """给压力测试专用的只读读取:不存在的文件绝不碰,不留下任何新文件/空表。

    ``SqliteStateStore`` 的构造函数本身会 ``CREATE TABLE IF NOT EXISTS``——对一个
    还不存在的文件,这一步就会在磁盘上凭空建出一个空库。压力测试是"如果……会怎样"
    的一次性假设提问,连"留下一个空文件"都不该发生,所以这里先判断文件是否已经
    存在,不存在就直接当成"没有历史记录",完全不碰文件系统。

    注:存在与否的判断和随后打开文件之间有一个理论上的竞态窗口(比如另一个进程
    在这两步之间刚好写入了存档)——影响仅限于"读到一份稍旧的快照",压力测试本身
    绝不写入,不会因此损坏任何数据。本库的既定约定是"一个存档/key 只能有一个活跃
    写者"(见 SqliteStateStore 的乐观锁),已排除严格并发一致性的场景。
    """
    if not args.state_db or not os.path.exists(args.state_db):
        return None
    store = SqliteStateStore(args.state_db, key=args.state_key)
    try:
        return store.load()
    finally:
        store.close()


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
        ("日内亏损线", "max_daily_loss_pct"),
        ("价格带(±)", "max_price_band_pct"),
        ("每分钟订单", "max_orders_per_minute"),
        ("每小时订单", "max_orders_per_hour"),
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
    store = _open_state_store(args)
    try:
        engine = RiskEngine(
            config,
            state_store=store,
            on_persist_error=_warn_persist_error if store is not None else None,
        )
        portfolio = _build_portfolio(args.equity, args.symbol, args.price, args.position)
        if args.limit_price is not None:
            order = Order(
                symbol=args.symbol,
                side=Side(args.side),
                quantity=args.qty,
                order_type=OrderType.LIMIT,
                limit_price=args.limit_price,
                reduce_only=args.reduce_only,
            )
        else:
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


def cmd_reset_breaker(args: argparse.Namespace) -> int:
    """人工复位熔断——先打印当初为什么熔断,要求确认,再动手。

    退出码:0 = 已复位;1 = 无可复位 / 操作者取消;2 = 错误(存档不存在等)。
    复位刻意只做成需要人在终端上执行的动作——"不把原因想明白,不许重启"
    不应该有自动化版本。
    """
    if not os.path.exists(args.state_db):
        print(f"error: 存档不存在:{args.state_db}", file=sys.stderr)
        return 2

    store = SqliteStateStore(args.state_db, key=args.state_key)
    try:
        engine = RiskEngine(state_store=store)
        state = engine.state
        resettable = state.breaker_tripped or (args.include_daily and state.daily_tripped)
        if not resettable:
            if state.daily_tripped:
                print(
                    "总回撤熔断未触发;日内亏损熔断处于激活状态,但未传 --include-daily。\n"
                    f"  日内熔断原因: {state.daily_trip_reason}\n"
                    "日内线会在换日后自动复位;确要今天继续,请加 --include-daily 显式覆盖。"
                )
            else:
                print("没有处于激活状态的熔断,无事可做。")
            return 1

        if state.breaker_tripped:
            when = state.tripped_at.isoformat() if state.tripped_at else "unknown"
            print(f"总回撤熔断:  {state.trip_reason}  (触发于 {when})")
        if state.daily_tripped and args.include_daily:
            when = state.daily_tripped_at.isoformat() if state.daily_tripped_at else "unknown"
            print(f"日内熔断:    {state.daily_trip_reason}  (触发于 {when})")

        if not args.yes:
            print("\n复位前请确认已完成人工复盘——不把原因想明白,不许重启。")
            try:
                answer = input("确认复位?[y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("已取消,熔断保持原状。")
                return 1

        engine.reset_breaker(include_daily=args.include_daily)
        print("已复位。高水位已归位到当前权益" +
              (";日内锚定已重置。" if args.include_daily else "。"))
        return 0
    finally:
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


def cmd_digest(args: argparse.Namespace) -> int:
    """每日体检:观测一次当前持仓(推进高水位历史),打印一份状态摘要。

    与 ``check``/``update_equity`` 一样,这一步**会**观测权益、可能触发熔断、
    并写入 ``--state-db``(如果配了)——digest 命令本身是"今天的一次记录点",
    不是纯只读预览。库函数 :func:`riskguard.reporting.build_digest` 才是零副作用的
    纯读取,供已经在别处观测过权益的调用方(比如接了 RiskMonitor 的 AI agent)
    直接取快照用。
    """
    from .reporting import build_digest, render_digest_text

    _warn_if_aggressive(args.preset)
    config = get_preset(args.preset)
    store = _open_state_store(args)
    try:
        engine = RiskEngine(
            config,
            state_store=store,
            on_persist_error=_warn_persist_error if store is not None else None,
        )
        positions = _parse_position_specs(args.position)
        portfolio = _build_portfolio_multi(args.equity, positions)
        engine.update_equity(portfolio)  # 记录今天这一次观测,推进高水位历史
        report = build_digest(engine, portfolio)
        print(render_digest_text(report))
        # 退出码向后兼容:1 的含义保持"总回撤熔断已触发"不变;仅日内亏损线
        # 激活(总线安好)用新退出码 4,老脚本对 1 的既有理解不受影响。
        if report.breaker_tripped:
            return 1
        if report.daily_tripped:
            return 4
        return 0
    finally:
        if store is not None:
            store.close()


def cmd_stress(args: argparse.Namespace) -> int:
    """压力测试:给当前持仓一个统一冲击,推演结果。绝不改变引擎的真实状态
    ——不观测权益、不触发熔断、不写持久化、不在磁盘上留下任何新文件(见
    :mod:`riskguard.reporting.stress` 模块文档 与 :func:`_load_state_readonly`),
    纯粹是"如果……会怎样"的一次性推演。
    """
    from .reporting import render_stress_text, run_stress_test

    _warn_if_aggressive(args.preset)
    config = get_preset(args.preset)
    restored_state = _load_state_readonly(args)
    engine = RiskEngine(config, state=restored_state)  # 不传 state_store:全程只读
    positions = _parse_position_specs(args.position)
    portfolio = _build_portfolio_multi(args.equity, positions)
    result = run_stress_test(engine, portfolio, args.shock)
    print(render_stress_text(result))
    return 3 if (result.would_trip_breaker or result.position_breaches) else 0


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
        description="RiskGuard —— 开源交易风控层。",
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
    sc.add_argument("--price", type=_positive_float, required=True,
                    help="参考价(mark);市价单据此估算敞口,限价单据此校验价格带")
    sc.add_argument("--limit-price", type=_positive_float,
                    help="传入则构造限价单:触发 fat-finger 价格带校验(偏离 --price 超带宽即拒)")
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

    srb = sub.add_parser(
        "reset-breaker",
        help="人工复盘后复位熔断(打印触发原因并要求确认)。",
    )
    srb.add_argument("--state-db", required=True, help="存放熔断状态的 SQLite 文件路径")
    srb.add_argument("--state-key", default="default", help="见 check 子命令的说明")
    srb.add_argument(
        "--include-daily",
        action="store_true",
        help="同时清除日内亏损熔断(默认只清总回撤线;日内线通常应随换日自动复位)",
    )
    srb.add_argument("--yes", action="store_true", help="跳过交互确认(脚本用)")
    srb.set_defaults(func=cmd_reset_breaker)

    sr = sub.add_parser("replay", help="买入持有:套风控 vs 不套 的回撤对比")
    sr.add_argument("--preset", default="balanced", choices=sorted(PRESETS))
    src = sr.add_mutually_exclusive_group()  # --prices 与 --csv 互斥
    src.add_argument("--prices", help="逗号分隔的价格,如 100,96,90,82")
    src.add_argument("--csv", help="CSV 文件(默认取每行最后一列)")
    sr.add_argument("--csv-column", help="CSV 价格列名或列索引(默认最后一列)")
    sr.add_argument("--cash", type=_positive_float, default=100_000.0)
    sr.set_defaults(func=cmd_replay)

    sd = sub.add_parser(
        "digest", help="每日体检:高水位/回撤/熔断距离/持仓 vs 上限,一份摘要"
    )
    sd.add_argument("--preset", default="balanced", choices=sorted(PRESETS))
    sd.add_argument("--equity", type=_finite_float, required=True, help="账户总权益")
    sd.add_argument(
        "--position",
        action="append",
        default=[],
        metavar="SYMBOL:QTY:PRICE",
        help="一笔持仓,可重复传多次(如 --position AAPL:100:190 --position TSLA:-20:250)",
    )
    sd.add_argument("--state-db", help="持久化高水位/熔断状态的 SQLite 文件路径")
    sd.add_argument("--state-key", default="default", help="见 check 子命令的说明")
    sd.set_defaults(func=cmd_digest)

    ss = sub.add_parser(
        "stress", help="压力测试:给持仓统一冲击(如 -20%%),看会不会触发熔断"
    )
    ss.add_argument("--preset", default="balanced", choices=sorted(PRESETS))
    ss.add_argument("--equity", type=_finite_float, required=True, help="账户总权益")
    ss.add_argument(
        "--position",
        action="append",
        default=[],
        metavar="SYMBOL:QTY:PRICE",
        help="一笔持仓,可重复传多次(如 --position AAPL:100:190 --position TSLA:-20:250)",
    )
    ss.add_argument(
        "--shock",
        type=_finite_float,
        required=True,
        help="统一冲击幅度,带符号,如 -0.20 表示所有标的统一下跌 20%%",
    )
    ss.add_argument(
        "--state-db", help="从存档读取高水位/熔断状态(只读,压力测试绝不写回存档)"
    )
    ss.add_argument("--state-key", default="default", help="见 check 子命令的说明")
    ss.set_defaults(func=cmd_stress)

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
