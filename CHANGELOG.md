# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增(AI 代理闸门三件套)
- 三条新规则,默认配置下**全部关闭**(v1.4 行为与全部既有测试不变),由预设开启
  且三档单调:
  - `DailyLossLimit` —— 日内亏损线(`max_daily_loss_pct`,预设 2%/3%/5%):当日
    相对会话锚定权益的亏损触线即停新仓,减仓永远放行;**当日粘性**(回血不解锁,
    防"回血一点就重新上杠杆"的赌徒循环),换日(`session_boundary_utc`,UTC)
    自动复位并留审计事件。与 HWM 总回撤熔断相互独立,`reset_breaker()` 默认不清它。
  - `PriceBandRule` —— fat-finger 价格保护带(`max_price_band_pct`,预设
    ±5%/±10%/±15%):限价偏离 mark 超带宽即拒。参考价只认 `portfolio.marks`
    (不用 `resolve_price()`——它对限价单回退到限价自身,自己跟自己比恒为零偏离;
    也不用陈旧的 `avg_price`);无 mark 拒单 fail-closed;市价单诚实豁免(没有
    声明价可校验);减仓单豁免。
  - `OrderThrottle` —— 下单频率节流(`max_orders_per_minute`/`_hour`,预设
    6/60、10/120、30/360):滚动窗口对**已批准**订单计数,触顶即拒,失控 AI 循环
    最多烧掉一个窗口的配额。**减仓单默认完全豁免**("减仓永远放行"核心原则
    原样保留);显式设置 `reduce_only_throttle_factor`(≥1)可 opt-in 一个
    单独计桶的放宽上限,给"连减仓循环也要有限"的运营者。
- CLI:`check --limit-price`(构造限价单,触发价格带校验);新子命令
  `reset-breaker`(打印触发原因 → 交互确认(`--yes` 跳过)→ 复位;
  `--include-daily` 显式覆盖日内线并把日内锚定重置到当前权益防止秒复发;
  退出码 0=已复位/1=无可复位或取消/2=错误);`presets` 表格新增四行。
- 日报:`DigestReport` 新增日内亏损区块(锚定/当前/距离/是否熔断);
  `digest` 命令对"仅日内线激活"返回**新退出码 4**(退出码 1 的含义保持
  "总回撤熔断已触发"不变,老脚本不受影响)。

### 变更
- `build_default_rules` 现在返回 8 条规则(新增三条在默认配置下是空操作);
  自定义 `rules=` 参数的调用方不受影响。
- `RiskEngine.check()` 在批准订单后把它记入节流预算——**纯 check 调用也消耗
  配额**(check 本就不是纯查询:它观测权益、可能触发熔断)。高频监控请改用
  零副作用的 `riskguard.reporting.build_digest`。(维护者如倾向只在 submit
  记录,改动集中在 `engine.check()` 一处,欢迎讨论。)
- 存档格式升至 **schema 2**(新增会话锚定/日内熔断/最近订单字段):v1.4 存档可
  无损加载(这就是全部迁移);读到**更新**版本的存档 fail-closed 拒绝。
  ⚠️ 已知限制:旧版本代码读 schema 2 存档能成功,但随后保存会**静默丢弃**新字段
  ——降级部署前请备份存档。
- 加固(实现期间对抗审查发现):`session_key_for` 对 naive datetime 一律按 UTC
  解释(否则注入 `datetime.utcnow()` 时会话边界按宿主机时区漂移,可在换日之外
  静默清除日内熔断);节流配额大到记录缓冲装不下时在引擎构造期抛 `ConfigError`
  (数不到 cap 的节流是悄悄失效的护栏,fail-closed)。

## [1.4.0] - 2026-07-10

回应"AI 帮你 24 小时盯盘"这个刚需场景。逐层比对文章描绘的机构级风控四层框架
(实时监控 / 异常检测 / 硬编码熔断线 / 压力测试)后判定:硬编码熔断线是本库
自 v1.0 起就在做的事;实时监控的核心机制(RiskMonitor)已有,这版补上它的
"交付形态"(每日体检);压力测试原语已在(backtest 模块),这版补上"一键推演
当前持仓"的顺手入口;异常检测**明确排除**在外——那是数据科学/AI 能力,和本库
"确定性、fail-closed、绝不模糊判断"的设计哲学是两种不同的产品,交给外部 AI
agent 去做,RiskGuard 只保证它拿到的事实是真的。

### 新增
- **`riskguard.reporting`**(每日体检 + 压力测试,两个纯函数原语):
  - `build_digest(engine, portfolio, now=None)` —— 把引擎当前状态(高水位/熔断/
    隔离中的策略)和持仓快照组装成结构化 `DigestReport`;`to_dict()` 供 AI agent
    消费,`render_text()` 给人读。零副作用(不观测权益、不触发熔断)。
  - `run_stress_test(engine, portfolio, shock_pct)` —— 给持仓统一冲击(如 -20%),
    推演权益/回撤/是否触发熔断/哪些仓位会超限。**绝对只读**:不调用
    `engine.check`/`update_equity`,不触发熔断、不写审计、不碰持久化——一次性
    的"如果……会怎样",不会污染真实风控状态。
  - 冲击后权益用**市值变动量叠加在权威当前权益上**计算,而不是拿 cash+持仓
    从零重算——避免调用方传入的 `Account.equity` 和 cash+持仓市值本就有出入时,
    算出一个和当前权益不可比、失真的"冲击后权益"。
- CLI `riskguard digest` / `riskguard stress`,支持 `--position SYMBOL:QTY:PRICE`
  多持仓输入(可重复传参)。`stress` 命令即便配了 `--state-db`,文件不存在时也
  完全不碰文件系统——不留下任何空文件。
- `examples/11_daily_digest.py`、`examples/12_stress_test.py`。
- 测试增至 774 项。

### 修复(发布前对抗审查,1 critical + 1 high + 2 low)
- **[critical] 压力测试假阴性**:账户被冲击打穿仓(权益 ≤ 0,恰恰是压力测试
  最该报警的场景)时,`position_breaches` 会因为 `Portfolio.weight()` 的除零
  防御性默认值(返回 0.0)而**漏报所有超限仓位**,同一份结果里的总敞口比例却
  正确显示 `inf`——两个字段自相矛盾,而 CLI 的退出码依赖 `position_breaches`,
  可能导致"账户已经爆仓,退出码却显示一切正常"的静默失败。
- **[high] 日报字段自相矛盾**:同样的根因,`build_digest` 在权益 ≤ 0 时,总敞口
  显示 `inf`(危险),但每个持仓的 weight/headroom 却显示 0.0/正数(健康),
  一份报告里两处数字互相打脸。
  - 两处统一修复:不改动已被多轮审查验证过的 `Portfolio.weight()` 核心方法
    (它在 reporting 模块之外从未被调用,其 `eq≤0→0.0` 是给风控规则层"更早就已
    拒单"场景设计的防御性默认值),而是在 reporting 内部新增
    `weight_or_inf()`:权益 ≤ 0 时,非平仓持仓一律返回 `inf`,和总敞口比例的
    `inf` 约定对齐。顺手加固 `equity_change_pct` 在权益为负时不再算出方向反转
    的误导性"上涨"假象。
- **[low]** `_load_state_readonly` 的文件存在性检查与打开之间有理论上的竞态
  窗口(影响仅限于读到稍旧的快照,不写入不会损坏数据)——补充注释说明。
- **[low]** `check`/`digest` 的退出码 1 含义不同,补充文档提醒脚本不能只看数字。

## [1.3.0] - 2026-07-09

第三方独立体检(架构 + 领域 + 竞品 + 唱衰四路评审)发现的"必修项"修复:名实不符
的文档主张 + 一个与产品使命直接矛盾的结构性缺陷(重启可绕过熔断)。测试增至 730 项
(新增状态持久化模块测试 + 两轮对抗审查的回归测试)。

### 新增
- **状态持久化** `riskguard.persistence`(`StateStore` 抽象 + `SqliteStateStore`
  实现):`RiskEngine` 新增可选 `state_store` 参数,构造时自动从存档恢复高水位/
  熔断/策略入役时间,此后每次状态变更写透。**堵住"重启即绕过熔断"的结构性后门**
  ——这是本轮体检发现的最严重问题:进程重启此前会让高水位/熔断标记全部归零,
  等于给操作者留了一条隐藏后路,与"纪律必须被系统强制执行"的立库初衷直接矛盾。
  - 加载失败在构造期**硬失败**(拒绝以"一切正常"的假象启动);写入失败在运行期
    不阻断风控裁决(转交可选的 `on_persist_error`,镜像既有的 `on_audit_error`)。
  - **乐观锁(版本号 CAS)**:一个 `(path, key)` 只能有一个活跃写者,两个引擎共用
    同一 key 会被检测到并抛 `PersistenceError`,而不是静默互相覆盖对方的熔断状态
    ——这是发布前对抗审查发现的 critical 问题(见下方"发布前对抗审查修复")。
  - `reset_breaker()` 是**先落盘、成功后才切内存状态**(其余方法相反,先切内存、
    再尽力落盘):人工复盘重置是操作者据以恢复交易的决定性动作,落盘失败必须
    直接抛出,不能假装重置成功。
  - 存档拒绝写入/读回非有限权益(NaN/±inf)——否则回撤永久算不出来,熔断永久失效。
  - 新增异常 `PersistenceError`。
- CLI `riskguard check` 新增 `--state-db` + `--state-key`,让脚本化重复调用(如
  cron 定时验单)也能跨调用持久化熔断状态——否则"反复重跑 CLI"本身就是另一种
  重启绕过;多个策略/标的共用同一个数据库文件时用 `--state-key` 隔离。
- `examples/10_state_persistence.py`:两个独立引擎实例模拟"进程重启",演示熔断
  状态如何被存档恢复。
- **GitHub Actions CI**:pytest 矩阵(3.10–3.13)+ ruff,README 徽章从手写静态数字
  换成真实的 CI 状态徽章。
- `examples/09_the_cost_of_discipline.py` + README 新增"纪律不是免费的"章节:
  同一档配置在 +200% 单边牛市里只吃到 20% 收益(裸奔账户吃满 200%)——主动展示
  风控的成本侧,不只展示它救命的那一面。

### 修复
- **README 安装指引名实不符**:`pip install riskguard` 在 PyPI 未发布前是装不上的
  坏指令,改为诚实的 git 安装方式并在路线图标注"发布到 PyPI"待办。
- `pyproject.toml` 的 `project.urls` 指向错误的 GitHub 用户名。
- `SqliteStateStore` 统一把 `sqlite3.Error` 转成 `PersistenceError`(不包一层会从
  CLI 的异常兜底里漏出去变成裸 traceback,例如连到一个根本不是 SQLite 库的文件)。
- `kelly_weights` 的 `zip()` 补 `strict=True`(输入序列长度不一致时报错,而不是
  静默截断到较短序列)。
- ruff 全量清理(120+ 处现代化:`Optional[X]` → `X | None`、移除多余的字符串引用等)。

### 发布前对抗审查修复(状态持久化模块,1 critical + 4 high + 2 medium)
- **[critical] 静默覆盖**:两个独立引擎共用同一存档/key 时,后写入的旧快照会
  静默覆盖前一个引擎刚落盘的熔断状态——"重启绕过熔断"换个马甲(并发写者)借尸
  还魂。修复:`SqliteStateStore` 加版本号乐观锁,冲突时抛 `PersistenceError` 而不是
  覆盖;发现修复过程中还堵了一个自我复查逻辑漏洞(从未 `load()` 过就直接 `save()`
  时,不能现查当前版本当基线——那样冲突检测在第一次写入时必然失效)。
- **[high] NaN/inf 权益毒化存档**:非有限权益一旦被写入,回撤会恒算成非有限值、
  熔断永久失效,且每次落盘都会把这个坏值重新写回去——写入和读取两端都补了校验。
- **[high] `reset_breaker()` 崩溃窗口**:原实现先切内存状态、再落盘,若落盘失败/
  进程在窗口期崩溃,操作者以为重置成功、实际存档仍是熔断态,重启后原样复活——
  比"重置失败继续熔断"更危险。改为先落盘、成功后才切内存,失败直接抛出。
- **[high] `RiskMonitor._handled_trip` 不随重启恢复**:引擎从存档恢复出"已触发"
  的熔断状态后,全新的 `RiskMonitor` 若从 `False` 起步,会在重启后第一个 tick
  重新拉响 `on_trip`/重复自动平仓。改为在构造时从 `engine.breaker_tripped` 播种。
- **[high] 未文档化的延迟回归**:配置 `state_store` 后,`check()`/`update_equity()`
  会在持锁期间做一次同步磁盘 IO,把延迟画像从"纯内存微秒级"变成"阻塞式磁盘 IO、
  串行化所有并发调用者",此前没有任何文档提及——已在相关 docstring 中明确说明。
- **[medium] `on_persist_error` 回调约束未说明**:该回调在引擎锁持有期间同步执行,
  补充文档要求"必须快、不能阻塞、不能派生新线程回调本引擎"。
- **[medium] CLI 无 key 隔离**:`riskguard check --state-db` 原本没有 `--state-key`,
  多个 cron 任务共用同一数据库文件会静默冲突——加了 `--state-key` 并接上
  `on_persist_error` 打印告警。

## [1.2.0] - 2026-07-06

新增**配置预设**与**零依赖命令行工具**。

### 新增
- **配置预设** `riskguard.presets`:`CONSERVATIVE` / `BALANCED` / `AGGRESSIVE` 三档
  开箱模板 + `get_preset(name)` 查询(大小写不敏感)。`BALANCED` 等价库默认。
- **命令行** `riskguard`(亦可 `python -m riskguard`,纯标准库):
  - `riskguard presets` —— 三档参数对照表。
  - `riskguard check` —— 用某档预设检查一笔订单(放行/缩量/拒单 + 原因),退出码
    反映裁决(0 放行 / 1 拒单 / 2 输入错误)。
  - `riskguard replay` —— 对一段价格(`--prices` 或 `--csv`)跑"买入持有:套风控
    vs 不套"的回撤对比。
- 新增 `examples/08_presets.py`;顶层导出 `CONSERVATIVE/BALANCED/AGGRESSIVE/PRESETS/get_preset`。
- 测试增至 698 项。

### 修复(发布前对抗式审查)
- **[critical] CSV 静默篡改价格**:`replay --csv` 曾用朴素 `split(",")` 取最后一列,把
  千分位价 `1,250` 读成 `250`、丢弃带引号字段。改用标准库 `csv` 解析,支持 `--csv-column`
  选列,坏行**显式计数并告警**,拒绝非正/非有限价——绝不静默编造行情。
- **[high] 文件错误崩栈**:`--csv` 指向目录/不可读文件曾抛未捕获异常。`main()` 改捕获
  `OSError`,统一返回退出码 2。
- **[medium] nan/inf 输入**:`--equity/--price/--qty/--cash` 在边界校验有限性(拒绝
  nan/inf),`--price/--qty/--cash` 还须为正;equity≤0 时权重显示 `n/a` 而非误导的 0.0%。
- **[low] 预设一致性**:`--prices` 与 `--csv` 改为互斥;`check` 缩量返回专属退出码 3
  (原样放行仍 0);选 `aggressive` 会在 stderr 给出风险提醒。

### 变更
- **预设参数收敛得更稳健**:`aggressive` 单笔上限 25%→20%、Kelly 0.75→0.50(不越过 config
  自身认可的"实务常用 0.25~0.5");三档均设**净敞口上限**且单调不减(1.0/1.0/1.5),
  最激进档不再是唯一"净敞口不设限"的一档。总敞口明确为组合层天花板(非单笔)。

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
