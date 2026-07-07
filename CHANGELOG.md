# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-07-06

新增**回测接线**模块 `riskguard.backtest`,连通量化五层积木的「回测 → 风控」两层。

### 新增
- **`RiskOverlay`** —— 框架无关的风险叠加层:把"目标持仓/权重"翻译成风控批准(或缩量)
  的下一步订单;`approved_target_weight()` 直接给按权重再平衡的框架用;累计缩单/拒单/
  熔断/拦截统计。
- **`replay` / `compare`** —— 轻量价格重放器,一键跑"套风控 vs 不套风控"对比(非通用
  回测框架,仅用于看见/测试风控行为)。
- **`make_riskguard_strategy`** —— backtesting.py 适配器:子类只写 `signal()` 返回目标
  权重,下单自动过风控(可选依赖 `riskguard[backtesting]`,懒加载)。
- **`risk_capped_weights` / `kelly_weights`** —— vectorbt 用的纯函数仓位辅助(无需装
  vectorbt);**`from_signals_with_risk`** —— 封顶后跑 `vbt.Portfolio.from_signals`。
- 新增 `examples/07_backtest_overlay.py`:双均线策略 + 风控叠加,展示崩盘中缩单/熔断/
  拦截追高三道防线同时出手。
- 测试增至 675 项(新增回测模块测试 + 回归测试)。

### 修复(回测模块发布前对抗式审查)
- **[critical] backtesting.py 适配器每 bar 叠单**:旧的 `_rebalance_to` 把"应持有的目标
  权重"当成每 bar 的下单量,导致同向持仓不断加仓、失控杠杆——正是本库要防的过度集中。
  改为按方向 long/flat(short/flat)建平仓:空仓才开到目标、同向不重复加仓。
- **[high] 坏 tick 崩溃**:价格 ≤ 0 的坏 tick 曾让"套风控"重放抛 `BrokerError`(而裸执行
  安然跳过),`compare` 直接中断。现两条路径对称跳过坏 tick,叠加层也不再把坏价误当清仓。
- **[high] `approved_target_weight` 副作用**:是一次完整的(有副作用的)预交易检查;
  `OverlayResult` 新增 `approved_weight` 字段,单次调用即可同时拿到订单与权重,并加显著
  文档提醒它是"每 bar 唯一 overlay 调用"。
- **[medium] 回撤/收益基线不一致**:曲线以起始资本为第 0 点,`max_drawdown` 与
  `total_return` 共用同一基线,回撤可从返回曲线本身复算。
- **[medium] 适配器现金双算**:`_portfolio_from` 现金改为 `权益 − 持仓市值`。
- **[low] vectorbt `from_signals_with_risk`**:调用方自带 `size` 时也会被封顶,不再静默透传。

## [1.0.1] - 2026-07-06

第二轮独立对抗式审查发现并修复的 fail-open 边界(对安全关键的风控库尤为重要)。

### 修复
- **[高] NaN/inf 权益污染熔断**:`RiskState.observe_equity` 现在忽略非有限权益读数
  (feed 抖动/除零),不再让一个 NaN 把 `drawdown` 恒算成 NaN、导致回撤熔断永不触发。
- **[高] equity≤0 时误拒减仓**:仓位/隔离/敞口三条规则把"减仓/`reduce_only` 永远放行"
  提到 `equity<=0` 判断之前——爆仓时通过 `submit()` 手动平仓的减仓单不再被误拒。
- **[中] `max_net_exposure_pct` 死配置**:新增并接入 `NetExposureLimit` 规则,净敞口上限
  真正生效(默认 `None` 时为无害空操作),不再是"设了却没人读"。
- **[中] 审计异常中断风控**:引擎侧审计写入统一走 `_safe_audit` 兜底 + 可选
  `on_audit_error` 回调;磁盘满等 IO 异常绝不再中断 allow/deny 主判决。

### 说明
- `reduce_only` 现由 Broker 契约兜底执行(只减不增,内置 `PaperBroker` 已夹取),
  风控规则据此统一放行减仓单。

## [1.0.0] - 2026-07-06

首个正式版本。补上"量化五层积木"里唯一没有成熟开源标准件的风控层。核心零依赖,
654 项测试全绿,并经过一轮多智能体对抗式审查 + 逐条修复。

### 修复(发布前对抗式审查)
- **[critical] 反手绕过所有规则**:持仓反手到等量/更小幅度时被误判为减仓,曾绕过
  全部仓位上限规则**和熔断**;现在反手一律计为放大敞口,按上限缩单、熔断中拒单。
- **[critical] `reduce_only` 未被执行**:`PaperBroker` 现在把减仓单夹到平仓为止,
  绝不反向开出更大仓位(kill-switch 的最后一道保险)。
- **[critical] 审计防篡改边界**:新增可选 `hmac_key`(HMAC-SHA256 防伪)与
  `verify(expected_count=...)`(检测尾部截断);`verify` 遇坏行返回 False 而非崩溃;
  文档如实说明纯哈希链的边界。
- **[high] 并发**:`RiskMonitor._tick` 串行化(杜绝双重平仓/漏平);`RiskEngine.submit`
  在同一把锁内完成检查与下单(消除"检查时没熔断、下单时已熔断"的窗口)。
- **[high] 非正标记价**:`Portfolio` 构造时丢弃负价/零价/NaN,防止敞口检查 fail-open。
- **[medium] 幻影单**:仓位算法权重为 0 时返回 `None`(明确不下注),不再伪造 1e-9 微单。

### 新增
- **风控引擎** `RiskEngine`:券商无关的预交易闸门 + 熔断状态机,线程安全。
- **四条内置规则**:
  - `MaxPositionLimit` —— 单笔仓位上限(文章铁律一)。
  - `DrawdownCircuitBreaker` —— 总亏损熔断,减仓单永远放行(文章铁律二)。
  - `StrategyQuarantine` —— 新策略隔离观察期(文章铁律三)。
  - `GrossExposureLimit` —— 组合层总名义敞口上限。
- **三种仓位算法**:固定比例、分数 Kelly、波动率目标。
- **券商抽象层** `Broker`:内置零依赖 `PaperBroker`(带滑点/手续费模型);
  `AlpacaBroker` 可选适配器。
- **审计追溯** `JsonlAuditSink`(哈希链防篡改)与 `SqliteAuditSink`。
- **实时监控守护** `RiskMonitor`:周期性观测权益、触发熔断、可自动平仓。
- 全部数据模型不可变(冻结 dataclass);核心零第三方依赖。
