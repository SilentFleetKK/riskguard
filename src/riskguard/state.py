"""风控运行时状态。

``RiskState`` 是不可变快照:记录权益高点(high-water mark)、熔断开关、
各策略的"入役时间"(用于隔离观察期)。所有"变更"都返回一个新的 RiskState,
绝不原地修改——历史状态因此永远可追溯、可回放。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType


def _freeze(m: Mapping[str, datetime]) -> Mapping[str, datetime]:
    return MappingProxyType(dict(m))


def session_key_for(now: datetime, boundary_hhmm: str) -> str:
    """计算 ``now`` 所属交易日会话的键(ISO 日期字符串)。

    会话以每天 UTC ``boundary_hhmm`` 为界:边界时刻(含)之后属于"今天"开始的
    会话,之前属于"昨天"开始的会话。例如边界 ``"17:00"`` 时,3 月 5 日 16:59
    仍属 ``2024-03-04`` 会话,17:00 起属 ``2024-03-05`` 会话。

    ``boundary_hhmm`` 的格式校验在 :class:`~riskguard.config.RiskConfig` 构造时
    已经做过,这里按已校验输入处理。

    **naive datetime 一律按 UTC 解释**(与库内其余时间运算一致)。绝不能交给
    ``astimezone`` 去按宿主机本地时区猜——注入 ``datetime.utcnow()`` 这类 naive
    时钟时,本地时区解释会让会话边界漂移,把已触发的日内熔断在换日之外静默清掉
    (fail-open),而这里是全库唯一一处依赖 tzinfo 的地方,最容易踩。
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    hh, mm = boundary_hhmm.split(":")
    offset = timedelta(hours=int(hh), minutes=int(mm))
    return (now.astimezone(timezone.utc) - offset).date().isoformat()


@dataclass(frozen=True, slots=True)
class RiskState:
    """引擎在某一时刻的风控状态快照(不可变)。"""

    high_water_mark: float = 0.0
    """观测到的权益历史最高点,回撤的基准。"""

    last_equity: float = 0.0
    """最近一次观测到的权益。"""

    breaker_tripped: bool = False
    """总亏损熔断是否已触发。"""

    tripped_at: datetime | None = None
    """熔断触发时间。"""

    trip_reason: str = ""
    """熔断触发原因的可读描述。"""

    strategy_inception: Mapping[str, datetime] = field(default_factory=dict)
    """各策略首次被登记的时间,隔离观察期由此计算。"""

    session_date: str | None = None
    """当前交易日会话键(由 :func:`session_key_for` 计算);None 表示尚未锚定。"""

    session_anchor_equity: float = 0.0
    """本会话首次观测到的权益,日内亏损的基准。"""

    daily_tripped: bool = False
    """日内亏损熔断是否已触发(只随换日或人工显式覆盖解除)。"""

    daily_tripped_at: datetime | None = None
    """日内熔断触发时间。"""

    daily_trip_reason: str = ""
    """日内熔断触发原因的可读描述。"""

    recent_orders: tuple[tuple[datetime, bool], ...] = ()
    """最近被风控批准的订单 ``(批准时间, 是否减仓单)``,节流规则的计数依据。

    由引擎在每次批准时通过 :meth:`record_order` 追加并按窗口/长度上限修剪,
    保证有界。"""

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_inception", _freeze(self.strategy_inception))
        object.__setattr__(self, "recent_orders", tuple(self.recent_orders))

    # ---- 派生量 ----
    @property
    def drawdown(self) -> float:
        """当前相对高点的回撤(0.0 表示在高点,0.2 表示回撤 20%)。"""
        if self.high_water_mark <= 0.0:
            return 0.0
        return max(0.0, 1.0 - self.last_equity / self.high_water_mark)

    @property
    def daily_loss(self) -> float:
        """当日亏损比例(相对会话锚定权益;盈利日为 0.0,未锚定为 0.0)。

        与 :attr:`drawdown` 同样做非有限数防御:锚定值非正或任一输入非有限时
        返回 0.0,绝不让 NaN 污染熔断判断。
        """
        if self.session_anchor_equity <= 0.0:
            return 0.0
        if not math.isfinite(self.session_anchor_equity) or not math.isfinite(self.last_equity):
            return 0.0
        return max(0.0, 1.0 - self.last_equity / self.session_anchor_equity)

    # ---- 不可变更新 ----
    def observe_equity(self, equity: float, now: datetime) -> RiskState:
        """观测一笔新的权益值,返回更新了 last_equity / 高点的新状态。

        **坏读数防御**:NaN / ±inf 的权益(feed 抖动、除零、坏 tick)会被直接**忽略**,
        返回原状态不变。绝不能让一个 NaN 污染 last_equity——那会让 drawdown 恒算成
        NaN、回撤熔断从此永不触发(fail-open)。忽略坏读数后,熔断继续按最后一个有效
        权益工作(fail-safe)。
        """
        if not math.isfinite(equity):
            return self
        hwm = max(self.high_water_mark, equity)
        return replace(self, high_water_mark=hwm, last_equity=equity)

    def trip(self, reason: str, now: datetime) -> RiskState:
        """触发熔断,返回新状态(幂等:已触发则原样返回)。"""
        if self.breaker_tripped:
            return self
        return replace(self, breaker_tripped=True, tripped_at=now, trip_reason=reason)

    def reset_breaker(self, now: datetime) -> RiskState:
        """人工复盘后重置熔断,并把高点重置到当前权益,避免立刻二次触发。"""
        return replace(
            self,
            breaker_tripped=False,
            tripped_at=None,
            trip_reason="",
            high_water_mark=self.last_equity,
        )

    def roll_session(self, session_key: str, equity: float) -> RiskState:
        """切换到新的交易日会话:重锚定日内基准、清除日内熔断。

        锚定值取换日后**首次观测**到的权益;非有限的权益不用于锚定(调用方——
        引擎——负责在这种情况下跳过换日,与 :meth:`observe_equity` 的坏 tick
        防御同一哲学)。
        """
        return replace(
            self,
            session_date=session_key,
            session_anchor_equity=equity,
            daily_tripped=False,
            daily_tripped_at=None,
            daily_trip_reason="",
        )

    def trip_daily(self, reason: str, now: datetime) -> RiskState:
        """触发日内亏损熔断,返回新状态(幂等:已触发则原样返回)。"""
        if self.daily_tripped:
            return self
        return replace(
            self, daily_tripped=True, daily_tripped_at=now, daily_trip_reason=reason
        )

    def clear_daily(self) -> RiskState:
        """人工显式清除日内熔断,并把日内锚定重置到当前权益。

        镜像 :meth:`reset_breaker` 对高水位的归位处理:不重置锚定的话,权益仍低于
        旧锚定超过限值,下一次观测就会立刻二次触发,人工覆盖形同虚设。正常情况下
        日内线应当随换日(:meth:`roll_session`)自动复位,本方法只服务于"操作者
        复盘后决定今天继续"的显式覆盖路径(CLI ``reset-breaker --include-daily``)。
        """
        return replace(
            self,
            daily_tripped=False,
            daily_tripped_at=None,
            daily_trip_reason="",
            session_anchor_equity=self.last_equity,
        )

    def record_order(
        self,
        now: datetime,
        reduce_only: bool,
        *,
        keep_window: timedelta,
        max_len: int,
    ) -> RiskState:
        """记录一笔已被批准的订单,并修剪掉窗口外/超长的旧记录。

        ``keep_window``/``max_len`` 由引擎按配置注入(本类保持不依赖 config),
        共同保证 ``recent_orders`` 有界——即便配置了病态的大额度,状态和持久化
        payload 也不会无限膨胀。
        """
        cutoff = now - keep_window
        kept = tuple(
            (ts, ro) for ts, ro in self.recent_orders if ts > cutoff
        ) + ((now, reduce_only),)
        if len(kept) > max_len:
            kept = kept[-max_len:]
        return replace(self, recent_orders=kept)

    def orders_in_window(
        self,
        now: datetime,
        window: timedelta,
        *,
        reduce_only: bool | None = None,
    ) -> int:
        """统计 ``(now - window, now]`` 内已批准的订单数。

        ``reduce_only``:None 统计全部;True/False 只统计对应桶。
        """
        cutoff = now - window
        return sum(
            1
            for ts, ro in self.recent_orders
            if ts > cutoff and (reduce_only is None or ro == reduce_only)
        )

    def register_strategy(self, strategy_id: str, now: datetime) -> RiskState:
        """登记一个策略的入役时间;已存在则不覆盖(保留最早时间)。"""
        if strategy_id in self.strategy_inception:
            return self
        merged = dict(self.strategy_inception)
        merged[strategy_id] = now
        return replace(self, strategy_inception=merged)

    def strategy_age_days(self, strategy_id: str, now: datetime) -> float | None:
        """策略入役至今的天数;未登记返回 None。"""
        inception = self.strategy_inception.get(strategy_id)
        if inception is None:
            return None
        return (now - inception).total_seconds() / 86400.0

    @classmethod
    def initial(cls, equity: float = 0.0, now: datetime | None = None) -> RiskState:
        """用初始权益构造起始状态,高点即为初始权益。"""
        return cls(high_water_mark=equity, last_equity=equity)
