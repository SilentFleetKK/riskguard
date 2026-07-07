"""实时监控守护进程 / 自动刹车(kill-switch)。

文章里的"自动刹车":光把纪律写进配置还不够,还得有人(或线程)时刻盯着盘面。
:class:`RiskMonitor` 就是这条后台线程——它周期性地向券商拉取组合快照,交给
:meth:`RiskEngine.update_equity` 刷新权益高点与回撤熔断;一旦熔断触发,可选择
自动踩下刹车:撤掉所有挂单、把每个非平仓头寸市价平掉。

设计要点:

* **永不静默死掉**:守护线程的一次 tick 抛任何异常,都被兜住并转交
  ``on_error`` 回调(缺省则忽略),线程继续存活到下一个周期。一次网络抖动
  不该让监控哑火。
* **kill-switch 不受风控规则阻挡**:平仓动作**直接**打到 broker,绕过
  :class:`RiskEngine` 的预交易闸门——熔断后风控若拦住平仓单,风险反而无法收敛。
* **幂等触发**:熔断只处理一次(``_handled_trip``),人工 ``reset_breaker``
  后自动重新武装,避免每个周期重复撤单/平仓。
* **线程安全**:生命周期(start/stop)加锁;引擎自身的状态访问已在引擎内加锁,
  broker 适配器需自行保证线程安全。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional

from ..audit.base import AuditEvent
from ..models import Order, Side


class RiskMonitor:
    """周期性观测权益、评估熔断、可自动平仓的后台守护进程。

    参数
    ----
    engine:
        风控引擎;每个周期调用 :meth:`RiskEngine.update_equity` 刷新熔断状态。
    broker:
        执行后端;用于拉取组合快照,以及熔断时直接撤单/平仓。
    interval:
        轮询周期(秒)。线程用 ``Event.wait(interval)`` 休眠,stop 时可即时唤醒。
    auto_liquidate:
        熔断触发时是否自动撤单 + 平仓。默认 False,只回调不动手。
    on_trip:
        熔断触发回调,签名 ``on_trip(state)``,收到更新后的 :class:`RiskState`。
    on_error:
        一次 tick 异常时的回调,签名 ``on_error(exc)``;缺省则静默忽略。
    marks_provider:
        可选的标记价来源,签名 ``() -> Mapping[str, float]``;每个周期调用一次,
        结果传给 ``broker.get_portfolio(marks)``。缺省则由 broker 自行取价。
    """

    def __init__(
        self,
        engine,
        broker,
        *,
        interval: float = 5.0,
        auto_liquidate: bool = False,
        on_trip: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        marks_provider: Optional[Callable[[], Mapping[str, float]]] = None,
    ) -> None:
        self.engine = engine
        self.broker = broker
        self.interval = interval
        self.auto_liquidate = auto_liquidate
        self.on_trip = on_trip
        self.on_error = on_error
        self.marks_provider = marks_provider

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._handled_trip = False
        self._lifecycle_lock = threading.RLock()
        # 串行化 _tick:守护线程与外部手动调用(或重叠的 tick)不得并发进入,
        # 否则 _handled_trip 的"检查-置位"非原子,会导致重复平仓或漏平。
        self._tick_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> "RiskMonitor":
        """启动后台线程(幂等:已在运行则原样返回)。"""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="riskguard-monitor",
                daemon=True,
            )
            self._thread.start()
            return self

    def stop(self, timeout: Optional[float] = None) -> None:
        """请求停止并等待线程退出。

        设置停止事件唤醒正在休眠的线程,再 join(可选超时)。
        """
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    @property
    def is_running(self) -> bool:
        """线程是否存活。"""
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    # ---- 上下文管理器 ----
    def __enter__(self) -> "RiskMonitor":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # 轮询循环
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        """主循环:按周期休眠,醒来跑一次 tick。异常绝不外泄导致线程死亡。"""
        # Event.wait 返回 True 表示 stop 被 set,退出;False 表示超时到点,继续。
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 —— 守护线程必须扛住一切瞬时异常
                if self.on_error is not None:
                    self.on_error(e)
                # 无 on_error 则静默忽略,线程存活到下一周期。

    def _tick(self) -> None:
        """观测一次权益并按需触发刹车。可被外部直接调用,便于确定性测试。

        全程持 ``_tick_lock``,保证 ``_handled_trip`` 的"检查-置位"与它所依据的
        权益快照原子一致:两次重叠的 tick 不会都对同一次熔断动手(双重平仓),
        reset 之后的重新武装也不会与另一次 tick 的置位彼此错位(漏平)。
        """
        with self._tick_lock:
            marks = self.marks_provider() if self.marks_provider is not None else None
            portfolio = self.broker.get_portfolio(marks)
            state = self.engine.update_equity(portfolio)
            if state.breaker_tripped and not self._handled_trip:
                self._handled_trip = True
                if self.on_trip is not None:
                    self.on_trip(state)
                if self.auto_liquidate:
                    self._liquidate(portfolio)
            elif not state.breaker_tripped:
                # 人工 reset_breaker 之后重新武装,下次熔断能再次响应。
                self._handled_trip = False

    # ------------------------------------------------------------------
    # kill-switch:直接打到 broker,绕过风控闸门
    # ------------------------------------------------------------------
    def _liquidate(self, portfolio) -> None:
        """熔断后的自动平仓:先撤挂单,再逐一市价平掉每个非平仓头寸。

        平仓单**直接**提交给 broker(不经引擎),因为熔断状态下引擎会拦新仓,
        而平仓恰恰是收敛风险所必需。每个头寸的处理各自 try/except 隔离,
        一个失败不影响其余头寸。
        """
        now = self._audit_now()

        # 1) 撤掉所有未成交挂单(broker 不支持则跳过)。
        try:
            open_orders = self.broker.get_open_orders()
        except NotImplementedError:
            open_orders = []
        except Exception as e:  # noqa: BLE001
            open_orders = []
            self._record_error("cancel_open_orders", e, now)
        for bo in open_orders:
            try:
                self.broker.cancel_order(bo.broker_order_id)
            except Exception as e:  # noqa: BLE001
                self._record_error("cancel_order", e, now, order_id=bo.broker_order_id)

        # 2) 对每个非平仓头寸,提交一笔减仓市价单把它打平。
        for symbol, pos in portfolio.positions.items():
            qty = pos.quantity
            if qty == 0:
                continue
            try:
                order = Order(
                    symbol=symbol,
                    side=Side.SELL if qty > 0 else Side.BUY,
                    quantity=abs(qty),
                    reduce_only=True,
                    strategy_id="risk_monitor",
                )
                broker_order = self.broker.submit_order(order)
                self._record_event(
                    now,
                    action="liquidate",
                    symbol=symbol,
                    side=order.side.value,
                    quantity=order.quantity,
                    broker_order_id=getattr(broker_order, "broker_order_id", None),
                    status=getattr(broker_order, "status", None),
                )
            except Exception as e:  # noqa: BLE001 —— 单个头寸失败不该中断整轮平仓
                self._record_error("liquidate", e, now, symbol=symbol)

    # ------------------------------------------------------------------
    # 审计辅助
    # ------------------------------------------------------------------
    def _audit_now(self) -> datetime:
        """取审计时间戳:优先复用引擎时钟,回退到 UTC now。"""
        clock = getattr(self.engine, "_clock", None)
        if callable(clock):
            try:
                return clock()
            except Exception:  # noqa: BLE001
                pass
        return datetime.now(timezone.utc)

    def _record_event(self, now: datetime, **payload: object) -> None:
        """向引擎审计后端落一条 monitor 事件(无 audit 则忽略)。"""
        audit = getattr(self.engine, "audit", None)
        if audit is None:
            return
        try:
            audit.record(
                AuditEvent(timestamp=now, event_type="monitor", payload=dict(payload))
            )
        except Exception:  # noqa: BLE001 —— 审计失败不该拖垮刹车动作
            pass

    def _record_error(
        self, action: str, exc: BaseException, now: datetime, **extra: object
    ) -> None:
        """把平仓过程中的错误落审计,同时转交 on_error 回调。"""
        payload = {"action": action, "error": repr(exc), **extra}
        self._record_event(now, **payload)
        if self.on_error is not None:
            try:
                self.on_error(exc)
            except Exception:  # noqa: BLE001
                pass
