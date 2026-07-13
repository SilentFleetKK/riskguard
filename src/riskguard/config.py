"""风控配置。

``RiskConfig`` 是整个库的"纪律成文法":文章里那三条朴素规则
(单笔仓位上限、总亏损熔断、新策略隔离观察)以及动态仓位参数,全部落在这里,
不可变、启动即校验。把纪律写进配置、由系统强制执行,而不是记在脑子里靠意志力——
这正是普通人做风控最大的意义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .exceptions import ConfigError

# 单笔仓位越界时的处置方式:
#   "resize" —— 缩到上限以内继续下单(默认,交易更顺滑)
#   "reject" —— 直接拒单(更保守)
BreachAction = Literal["resize", "reject"]


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """不可变的风控参数集合,构造时即做区间校验。

    默认值刻意对齐文章里的"新手三条铁律":单笔仓位 <10%、总亏损熔断 15%、
    新策略隔离 90 天。想更激进或更保守,改这里一处即可,全库行为随之改变。
    """

    # --- 规则一:单笔仓位上限 ---
    max_position_pct: float = 0.10
    """任一标的的名义敞口占总权益的上限(0.10 = 10%)。"""

    on_position_breach: BreachAction = "resize"
    """单笔仓位越界时缩单还是拒单。"""

    # --- 规则二:总亏损熔断线 ---
    max_drawdown_pct: float = 0.15
    """相对历史权益高点(high-water mark)的最大回撤,触及即熔断停新仓。"""

    # --- 规则三:新策略隔离观察 ---
    quarantine_days: int = 90
    """新策略的隔离观察期(自然日)。期内仓位受更严格上限约束。"""

    quarantine_max_position_pct: float = 0.01
    """隔离期内单策略单标的的仓位上限(1%)。"""

    auto_register_strategies: bool = False
    """是否在首次见到某策略下单时自动把它登记进隔离观察期。

    默认 False:隔离规则只对**显式** :meth:`RiskEngine.register_strategy` 登记过的
    策略生效——把隔离当成"我在评估一个新策略,盯紧它"的主动动作,而非对所有订单的
    隐形封顶(真正的兜底护栏是仓位上限/回撤熔断/组合敞口三条)。设 True 则一视同仁:
    任何新策略首单即进隔离,追求最高安全但可能出乎意料。"""

    # --- 组合层敞口 ---
    max_gross_exposure_pct: float = 1.0
    """全组合总名义敞口(多空绝对值之和)占权益上限。1.0 = 不加杠杆。"""

    max_net_exposure_pct: float | None = None
    """全组合净敞口占权益上限;None 表示不限制。"""

    # --- 动态仓位(Kelly / 波动率目标)---
    kelly_fraction: float = 0.5
    """分数 Kelly 系数。1.0 为满 Kelly(过于激进),实务常用 0.25~0.5。"""

    vol_target_annual: float = 0.15
    """波动率目标法的年化目标波动率(0.15 = 15%)。"""

    max_sizing_leverage: float = 1.0
    """动态仓位算法允许的单标的最大权重(相对权益),防止公式给出过大仓位。"""

    # --- AI 代理闸门三件套(日内亏损线 / 价格保护带 / 下单节流)---
    # 默认全部关闭(None):v1.4 之前的配置与行为完全不变;预设(presets)会开启它们。
    max_daily_loss_pct: float | None = None
    """日内亏损熔断线:当日权益相对本交易日锚定值的亏损比例上限;None 表示不启用。

    与 ``max_drawdown_pct``(相对历史高点、跨日累计)互补:它抓的是"今天快速失血"
    ——高点可能是几个月前的,当日从平盘急跌 3% 远够不着 15% 的总线,但对一个
    失控的 AI 代理来说,这正是最该拉闸的时刻。触发后停新仓到换日自动复位。"""

    session_boundary_utc: str = "00:00"
    """交易日切换时刻(UTC,``HH:MM``)。日内亏损锚定与熔断复位以此为界。"""

    max_price_band_pct: float | None = None
    """fat-finger 价格保护带:限价单价格偏离参考价(mark)的比例上限;None 不启用。

    只约束限价单——市价单没有声明价可校验(诚实边界)。无参考价时拒单(fail-closed)。"""

    max_orders_per_minute: int | None = None
    """滚动 60 秒窗口内允许通过风控的订单数上限;None 不启用。"""

    max_orders_per_hour: int | None = None
    """滚动 3600 秒窗口内允许通过风控的订单数上限;None 不启用。"""

    reduce_only_throttle_factor: float | None = None
    """减仓单的节流放宽倍数;**默认 None = 减仓单完全豁免节流**("减仓永远放行"
    原则原样保留)。

    设为有限值(≥1)则减仓单单独计桶、上限 = cap × 本系数——这是对该原则的一次
    **显式 opt-in 的偏离**,给"连减仓循环也要有限"的运营者用:无界的减仓循环
    本身也有代价(券商限频、手续费、失控代理反复平仓停不下来)。放宽倍数让它
    比普通单难触发得多,但保证循环终会被封。不确定就保持默认豁免。"""

    # --- 元信息 ---
    trading_days_per_year: int = 252
    """年化换算用的年交易日数。"""

    def __post_init__(self) -> None:
        self._check_fraction("max_position_pct", self.max_position_pct)
        self._check_fraction("max_drawdown_pct", self.max_drawdown_pct)
        self._check_fraction("quarantine_max_position_pct", self.quarantine_max_position_pct)
        self._check_positive("max_gross_exposure_pct", self.max_gross_exposure_pct)
        if self.max_net_exposure_pct is not None:
            self._check_positive("max_net_exposure_pct", self.max_net_exposure_pct)
        self._check_positive("max_sizing_leverage", self.max_sizing_leverage)
        self._check_positive("vol_target_annual", self.vol_target_annual)

        if not (0.0 < self.kelly_fraction <= 1.0):
            raise ConfigError(
                f"kelly_fraction must be in (0, 1], got {self.kelly_fraction}"
            )
        if self.quarantine_days < 0:
            raise ConfigError(f"quarantine_days must be >= 0, got {self.quarantine_days}")
        if self.trading_days_per_year <= 0:
            raise ConfigError(
                f"trading_days_per_year must be > 0, got {self.trading_days_per_year}"
            )
        if self.quarantine_max_position_pct > self.max_position_pct:
            raise ConfigError(
                "quarantine_max_position_pct should not exceed max_position_pct "
                f"({self.quarantine_max_position_pct} > {self.max_position_pct})"
            )
        if self.on_position_breach not in ("resize", "reject"):
            raise ConfigError(
                f"on_position_breach must be 'resize' or 'reject', got {self.on_position_breach!r}"
            )

        # --- AI 代理闸门三件套 ---
        if self.max_daily_loss_pct is not None:
            self._check_fraction("max_daily_loss_pct", self.max_daily_loss_pct)
            if self.max_daily_loss_pct > self.max_drawdown_pct:
                raise ConfigError(
                    "max_daily_loss_pct should not exceed max_drawdown_pct "
                    f"({self.max_daily_loss_pct} > {self.max_drawdown_pct}) — "
                    "the daily line must be tighter than the total line"
                )
        self._check_session_boundary(self.session_boundary_utc)
        if self.max_price_band_pct is not None:
            self._check_fraction("max_price_band_pct", self.max_price_band_pct)
        if self.max_orders_per_minute is not None and self.max_orders_per_minute <= 0:
            raise ConfigError(
                f"max_orders_per_minute must be > 0, got {self.max_orders_per_minute}"
            )
        if self.max_orders_per_hour is not None and self.max_orders_per_hour <= 0:
            raise ConfigError(
                f"max_orders_per_hour must be > 0, got {self.max_orders_per_hour}"
            )
        if (
            self.max_orders_per_minute is not None
            and self.max_orders_per_hour is not None
            and self.max_orders_per_hour < self.max_orders_per_minute
        ):
            raise ConfigError(
                "max_orders_per_hour must be >= max_orders_per_minute "
                f"({self.max_orders_per_hour} < {self.max_orders_per_minute}) — "
                "otherwise the per-minute budget can never be used"
            )
        if (
            self.reduce_only_throttle_factor is not None
            and self.reduce_only_throttle_factor < 1.0
        ):
            raise ConfigError(
                "reduce_only_throttle_factor must be >= 1.0 (or None for full "
                f"exemption), got {self.reduce_only_throttle_factor} — reduce-only "
                "orders must never be throttled harder than risk-increasing ones"
            )

    @staticmethod
    def _check_fraction(name: str, value: float) -> None:
        if not (0.0 < value <= 1.0):
            raise ConfigError(f"{name} must be in (0, 1], got {value}")

    @staticmethod
    def _check_positive(name: str, value: float) -> None:
        if value <= 0.0:
            raise ConfigError(f"{name} must be > 0, got {value}")

    @staticmethod
    def _check_session_boundary(value: str) -> None:
        """校验 ``HH:MM``(24 小时制,两位数字,00:00–23:59)。"""
        parts = value.split(":")
        if (
            len(parts) != 2
            or len(parts[0]) != 2
            or len(parts[1]) != 2
            or not parts[0].isdigit()
            or not parts[1].isdigit()
            or not (0 <= int(parts[0]) <= 23)
            or not (0 <= int(parts[1]) <= 59)
        ):
            raise ConfigError(
                f"session_boundary_utc must be 'HH:MM' (24h UTC), got {value!r}"
            )

    def replace(self, **changes: object) -> RiskConfig:
        """返回一个应用了变更的新配置(不可变模式)。"""
        from dataclasses import replace

        return replace(self, **changes)


#: 开箱即用的保守默认配置,等价于 ``RiskConfig()``。
DEFAULT_CONFIG = RiskConfig()
