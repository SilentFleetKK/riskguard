# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
