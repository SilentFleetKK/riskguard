<div align="center">

# 🛡️ RiskGuard

**给你的交易系统,配一个 7×24、永不情绪化的风控官。**

[![CI](https://github.com/SilentFleetKK/riskguard/actions/workflows/ci.yml/badge.svg)](https://github.com/SilentFleetKK/riskguard/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Core deps](https://img.shields.io/badge/core%20deps-0-brightgreen.svg)
![PRs](https://img.shields.io/badge/PRs-welcome-orange.svg)

> "Rule No.1:永远不要亏钱。Rule No.2:永远不要忘记第一条。" —— 沃伦·巴菲特

</div>

量化交易的五层积木——**数据 → 研究 → 回测 → 风控 → 执行**——数据有 OpenBB、研究有 Qlib、回测有 backtesting.py/vectorbt、执行有 Alpaca,**唯独风控层没有一个成熟的开源标准件**。RiskGuard 就是来补这块空白的。

风控的本质不是技术,而是**提前写死的纪律**。RiskGuard 把那几条能挡住绝大多数爆仓惨剧的规则,做成一个券商无关、核心零依赖、可组合的库,让系统自动执行——因为等你真的亏钱上头那一刻,意志力是这世界上最靠不住的东西。

---

## 🔥 看它拦下一次爆仓(30 秒)

同一个"满仓做多"信号,喂给同一段 −45% 的崩盘行情。一个不设风控,一个套上 RiskGuard(单笔 10% 上限 + 15% 回撤熔断)。看**最大回撤**的差距:

| | 裸奔账户 | 🛡️ RiskGuard 账户 |
|---|---:|---:|
| 单笔仓位占比 | 100% | **10%(自动缩单)** |
| 期末权益(起始 10 万) | 55,000 | **94,377** |
| **最大回撤** | **−45.0%** | **−5.6%** |

```text
同一段崩盘,风控把最大回撤从 45% 压到 5.6%。
纪律不是让你多赚,是让你别在最坏的一天把本金亏光。
```

> 这段数字是 [`examples/06_with_vs_without.py`](examples/06_with_vs_without.py) 的真实输出,是"仓位封顶"这条纪律的**机械结果**、可一键复现——不是收益承诺。`git clone` 下来自己跑一遍就知道。

### 但纪律不是免费的

同一档配置,换成 +200% 的单边牛市,风控账户只吃到裸奔涨幅的 **10%**——`120,000` vs `300,000`。仓位上限截断的是分布的**两端**:既拦暴跌,也拦暴涨。这是真实的保费,不是隐藏条款(见 [`examples/09`](examples/09_the_cost_of_discipline.py))。值不值,取决于你更怕错过暴涨,还是更怕活不到下一次机会。

---

## 🤔 为什么不自己写几个 `if` 判断就好?

你当然可以写。但风控真正难的不是"能不能判断",而是**分析质量和决策纪律**——尤其在你亏钱上头、最不该动手的那一刻。

| 你以为够了 | 现实会发生什么 | RiskGuard 怎么做 |
|---|---|---|
| 记在脑子里"别超过 10%" | 上头时,脑子里的规则第一个失效 | 写进配置、系统强制,越界**自动缩单** |
| "跌多了我就手动止损" | 你会犹豫、舍不得、甚至加仓摊平 | 回撤触线**自动熔断**,拉闸停手 |
| 回测年化 30% 很香 | 滑点/手续费一扣,实盘常年亏 | 模拟盘**内建滑点 + 手续费**,先扣摩擦再看 |
| 拿不到价就用上一个价 | 用错价 = 敞口算错 = 风控失效 | 拿不到价**直接拒单**(fail-closed) |
| 平仓单和开仓单一视同仁 | 熔断时把平仓也拦了,风险无法收敛 | **减仓单永远放行** |
| 日志记个 txt | 能被随手改,出事查无对证 | **哈希链留痕**,`verify()` 一键验真伪 |

**这不是"能不能判断"的问题,而是"纪律能不能被系统强制执行"的问题。** RiskGuard 把这六件事全部焊死进代码。

---

## 🧱 一套风控,五组能力

| 能力 | 类 | 一句话 |
|---|---|---|
| 🚫 单笔仓位闸 | `MaxPositionLimit` | 任一标的名义敞口 ≤ 权益的 `max_position_pct`(默认 10%)—— 别把身家压一注 |
| 🧯 回撤熔断 | `DrawdownCircuitBreaker` | 回撤触及 `max_drawdown_pct`(默认 15%)即停新仓,**减仓永远放行** |
| 🐣 新兵隔离区 | `StrategyQuarantine` | 新策略先用小钱蹲观察期(默认 90 天),活过了再加仓 |
| ⚖️ 组合敞口闸 | `GrossExposureLimit` / `NetExposureLimit` | 总敞口管杠杆、净敞口管方向;一堆各自合规的小仓也不许叠成大风险 |
| 🎲 动态下注 | `KellySizer` / `VolatilityTargetSizer` / `FixedFractionalSizer` | 下多大注让公式算,不让情绪算;无正期望时自动不下注 |
| 🚨 实时哨兵 | `RiskMonitor` | 后台线程盯盘,触线自动踩刹车(撤单 + 平仓)——情绪失控时替你拉闸 |
| 📿 防篡改案底 | `JsonlAuditSink` / `SqliteAuditSink` | 每次裁决/熔断/成交都留哈希链记录,可选 HMAC 防伪 |
| 🔌 券商无关 | `Broker` / `PaperBroker` / `AlpacaBroker` | 纸面模拟盘、Alpaca、或你自研后端,实现一个接口即可接入 |

> **两层严格分离**:`Sizer` 只决定"下多大注",`Rule` 决定"能不能下、要不要缩、该不该全停"。AI 可以做研究、写代码、挑毛病,但**每一笔真实指令都必须先过这几条写死的风控规则**。

---

## ⚡ 60 秒上手

> ⚠️ **尚未发布到 PyPI**——`pip install riskguard` 现在装不上,请用下面的 git 安装方式。
> 发布到 PyPI 后这里会第一时间更新(进度见 [路线图](#-路线图))。

```bash
pip install git+https://github.com/SilentFleetKK/riskguard.git              # 核心,零依赖
pip install "riskguard[alpaca] @ git+https://github.com/SilentFleetKK/riskguard.git"  # 需要 Alpaca 适配器时
```

```python
from riskguard import RiskEngine, RiskConfig, Order, Side, PaperBroker

# 1) 一个带滑点 + 手续费的纸面模拟盘(呼应"回测和实盘是两个世界")
broker = PaperBroker(cash=100_000, slippage_bps=2, commission_bps=1,
                     marks={"AAPL": 200.0})

# 2) 把纪律写进配置:单笔 ≤ 10%、回撤 15% 熔断
engine = RiskEngine(RiskConfig(max_position_pct=0.10, max_drawdown_pct=0.15),
                    broker=broker)

# 3) 想买 1000 股 = 20 万 = 占比 200%,风控自动缩到上限
decision = engine.check(Order("AAPL", Side.BUY, 1000), broker.get_portfolio())
print(decision.decision)          # Decision.RESIZE
print(decision.order.quantity)    # 50.0  ← 缩到 10% 权益
print(decision.reasons())
# AAPL capped to 10.00% of equity (qty 1000 -> 50); gross exposure capped ... (qty 1000 -> 500)
# ↑ 多条规则可能同时报告,引擎取最保守的那个(min = 50)

# 4) 放行则真正下单(这里进模拟盘)
engine.submit(Order("AAPL", Side.BUY, 40), broker.get_portfolio())
```

熔断触发后必须**人工复盘**再 `engine.reset_breaker()`——不把原因想明白,不许重启。

---

## 🎲 动态下注:让公式算,不让情绪算

```python
from riskguard import KellySizer, Signal, Side

engine = RiskEngine(RiskConfig(kelly_fraction=0.5), sizer=KellySizer(), broker=broker)

# Kelly 需要胜率与盈亏比;f = kelly_fraction × (p − q/b)
sig = Signal("AAPL", Side.BUY, price=200.0, win_probability=0.55, payoff_ratio=1.5)
engine.size_and_submit(sig, broker.get_portfolio())   # 无正期望时返回 None,自动不下注
```

- `FixedFractionalSizer` —— 固定比例,最难自欺。
- `KellySizer` —— 分数 Kelly,满 Kelly 太颠簸,默认取半。
- `VolatilityTargetSizer` —— 目标波动率 / 实现波动率,给每个头寸等量风险预算。

## 📿 防篡改审计

```python
from riskguard import RiskEngine, JsonlAuditSink

with JsonlAuditSink("audit.jsonl", hmac_key="放在日志之外的密钥") as audit:
    engine = RiskEngine(broker=broker, audit=audit)
    ...

JsonlAuditSink.verify("audit.jsonl", hmac_key="...", expected_count=42)   # 验真伪
```

> **诚实说清边界**:不带密钥的纯哈希链只能防"改/重排中间记录",挡不住尾部截断或整体重写。要真正防伪,传 `hmac_key`(密钥存在日志之外)+ `verify(expected_count=N)` 用外部锚点核对条数。

## 🔒 状态持久化:堵住"重启绕过熔断"的后门(v1.3)

`RiskState` 默认纯内存——进程一重启,高水位和熔断标记全部归零,等于给亏红眼的操作者留了一条"重启一下继续交易"的隐藏后路。这与本库的立库初衷直接矛盾。接一个 `state_store`,熔断就真的扛得住重启([`examples/10`](examples/10_state_persistence.py)):

```python
from riskguard import RiskEngine, SqliteStateStore

store = SqliteStateStore("risk_state.db")
engine = RiskEngine(config, broker=broker, state_store=store)
# 构造时自动从存档恢复高水位/熔断/策略入役时间;此后每次状态变更自动写透。
# 存档读取失败会直接抛出——宁可拒绝启动,也不能悄悄当成"一切正常"重新开始。
```

CLI 同理支持跨调用持久化(否则脚本反复调用本身就是一种"重启绕过"),多个策略/标的
共用一个数据库文件时用 `--state-key` 隔离:

```bash
$ riskguard check --state-db risk_state.db --state-key strategy_a \
    --equity 80000 --side buy --qty 10 --price 100
```

> **诚实说清边界**:这堵住的是"重启进程"这条最常见的绕过路径,不是万能防绕过——
> 一个决心要绕的人仍可以删掉存档文件、改配置、或者干脆改代码。它提高的是绕过成本,
> 不是消灭绕过本身;个人量化的风控终究是尤利西斯之约,不是机构级的权责分离。
>
> **一个 `(存档文件, key)` 只能有一个活跃写者**:两个引擎共用同一个 key 会被乐观锁
> (版本号 CAS)检测到并报错,而不是静默互相覆盖——多策略/多账户请务必用不同 key。

## 🚨 实时哨兵(kill-switch)

```python
from riskguard import RiskMonitor

with RiskMonitor(engine, broker, interval=5.0, auto_liquidate=True):
    ...   # 后台线程:周期观测权益 → 触及回撤红线自动熔断并平仓
```

## 🔌 接进你的回测框架(v1.1)

把 RiskGuard 作为"风险叠加层"接进 backtesting.py / vectorbt,让**策略研究和爆仓防护第一次在同一条流水线上**([`examples/07`](examples/07_backtest_overlay.py))。

```python
# 框架无关的叠加层:目标持仓 → 风控批准的订单;还能一键跑"套风控 vs 不套"对比
from riskguard.backtest import RiskOverlay, compare

res = compare(prices, my_strategy, config=RiskConfig(max_position_pct=0.10))
print(res["naive"].max_drawdown, res["guarded"].max_drawdown)  # 例:−45% vs −5.6%
```

```python
# backtesting.py:子类只写 signal() 返回目标权重,下单自动过风控
from riskguard.backtest import make_riskguard_strategy
class MyStrat(make_riskguard_strategy(RiskConfig(max_position_pct=0.10))):
    def signal(self): return 1.0 if bullish else 0.0
```

```python
# vectorbt:把仓位按上限封顶(纯函数,不装 vectorbt 也能用)
from riskguard.backtest import risk_capped_weights, kelly_weights
size = risk_capped_weights(target_weights, RiskConfig(max_position_pct=0.10))
```

---

## 🎚️ 三档预设 + 命令行(v1.2)

不想逐个调参?挑一档起步——**保守 / 均衡 / 激进**:

```python
from riskguard import get_preset, RiskEngine
engine = RiskEngine(get_preset("conservative"))   # 或 "balanced" / "aggressive"
# 再用 config.replace(...) 微调成你自己的口径
```

装好后还带一个**零依赖命令行**,一键验单、看预设、跑对比:

```bash
$ riskguard check --preset balanced --equity 100000 --side buy --qty 1000 --price 200
裁决:  RESIZE
放行:  BUY 50 ASSET  (占权益 10.0%)

$ riskguard presets                               # 三档参数对照表
$ riskguard replay --prices 100,96,90,82,75,70    # 套风控 vs 不套 的回撤对比
```

## 🗺️ 架构

```
        策略 / 信号
            │
            ▼
      ┌───────────────┐   Sizer:  下多大注
      │  RiskEngine   │   Rules:  能不能下、要不要缩
      │   ─ state ─   │   Breaker: 该不该全停
      └───────┬───────┘
              │ 放行的订单(线程安全、加锁)
              ▼
        Broker 抽象层  ── PaperBroker / AlpacaBroker / 你自研的
              │
              ▼
   📿 审计(JSONL/SQLite) + 🚨 哨兵守护(RiskMonitor)
```

## 🙋 适合谁

- 自己写策略、但从没认真做过风控的
- 已经在用 Claude Code / backtesting.py / vectorbt,想补上"风控"这层的
- 想把"纪律"从脑子里搬进代码、不再靠意志力硬扛的
- 需要一个**券商无关**、能接 Alpaca / 自研后端的风控中间层的
- 想要一份"从零搭起、还有测试和模拟盘实战"的硬核作品的

## 🧭 设计原则

1. **纪律成文、系统强制**:阈值全在 `RiskConfig`,不可变、启动即校验。
2. **绝不静默失败**:拿不到价宁可抛异常、丢弃坏价,也绝不用猜测价放行(fail-closed)。
3. **减仓永远放行**:任何时候都不阻止风险收敛,哪怕熔断中、哪怕反手也会被夹到平仓为止。
4. **一切不可变**:数据对象只读,状态变更返回新快照,历史永远可回放;监控线程可安全共享。
5. **AI 动脑,系统守纪律**:AI 可研究/写码/挑毛病,但每笔真实指令必须先过写死的风控规则。
6. **纪律扛得住重启**:熔断状态可持久化,进程重启不是绕过风控的合法后门。

## ⚠️ 诚实的边界与免责声明

- 本库是**风险控制工具,不是投资建议**,也**不保证盈利或防止亏损**。它约束的是敞口和纪律,不预测行情。
- 任何策略都应**先在模拟盘养满三个月**,真钱只用"亏光也不影响生活、不影响睡眠的闲钱"。
- 上面的对比数字来自**确定性模拟脚本**,是仓位纪律的机械结果,不代表任何未来收益。本项目**不附带、也不宣称任何实盘战绩**。
- 审计防篡改有明确边界(见上),别当成万能防伪。

## 🚧 路线图

- [ ] 发布到 PyPI(`pip install riskguard` 真正能装)
- [ ] Alpaca 适配器实盘打通(下单/持仓/撤单端到端)
- [ ] 更多券商:盈透(IBKR)、加密交易所(ccxt)
- [x] 状态持久化:堵住"重启绕过熔断"的后门(v1.3 ✅)
- [x] CI(pytest 矩阵 + ruff)(v1.3 ✅)
- [x] 配置预设 + CLI:保守/均衡/激进三档 + 命令行(v1.2 ✅)
- [x] 与 backtesting.py / vectorbt 的一键接线(v1.1 ✅)
- [ ] 审计外部锚定(WORM / 公证)
- [ ] AI 代理闸门三件套:日内亏损线、fat-finger 价格保护带、下单频率节流

## 🛠️ 开发

```bash
pip install -e ".[dev]"
pytest            # 730 passed,CI 跑 3.10–3.13
ruff check src     # 干净
mypy src           # strict 模式,当前仍有已知缺口(类型标注在路上,尚未接入 CI 门禁)
```

## 📄 License

MIT。欢迎 issue / PR。

<div align="center">

如果它帮你少爆一次仓,给个 ⭐ 吧。

[![Star History Chart](https://api.star-history.com/svg?repos=SilentFleetKK/riskguard&type=Date)](https://star-history.com/#SilentFleetKK/riskguard&Date)

</div>
