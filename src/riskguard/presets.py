"""开箱即用的配置预设:保守 / 均衡 / 激进 三档。

不想逐个参数调?挑一档预设起步,再用 :meth:`RiskConfig.replace` 微调。三档都是
不可变的 :class:`~riskguard.config.RiskConfig` 实例,可安全共享。

三档在**每一项风险维度上都单调不减**(越激进越松),且激进档仍收敛在本库自己认可的
稳健范围内(见 config 里对 ``kelly_fraction`` 的说明:实务常用 0.25~0.5)。

| 维度 | conservative | balanced | aggressive |
| --- | --- | --- | --- |
| 单笔仓位上限 | 5% | 10% | 20% |
| 回撤熔断线 | 10% | 15% | 25% |
| 总敞口上限(组合层天花板) | 1.0× | 1.0× | 1.5× |
| 净敞口上限(方向性) | 1.0× | 1.0× | 1.5× |
| Kelly 系数 | 0.25 | 0.50 | 0.50 |
| 日内亏损线 | 2% | 3% | 5% |
| fat-finger 价格带 | ±5% | ±10% | ±15% |
| 每分钟订单上限 | 6 | 10 | 30 |
| 每小时订单上限 | 60 | 120 | 360 |

说明:``max_gross_exposure_pct`` / ``max_net_exposure_pct`` 是**组合层天花板**,约束的是
多个头寸叠加后的总/净敞口;内置 sizer 给单个标的下注时仍受 ``max_position_pct`` 约束,
不会因为总敞口放宽到 1.5× 就给单一标的下更大的注。激进档同时把隔离观察期从 90 天缩到
30 天(仅对**显式登记**的策略生效,见 ``auto_register_strategies``)。

⚠️ **aggressive 口径明显更松,只适合能承受更大回撤、且清楚自己在做什么的人**;命令行选它
会额外给一句提醒。
"""

from __future__ import annotations

from .config import RiskConfig
from .exceptions import ConfigError

#: 保守档:本金优先,尽量少爆仓。
CONSERVATIVE = RiskConfig(
    max_position_pct=0.05,
    on_position_breach="resize",
    max_drawdown_pct=0.10,
    max_gross_exposure_pct=1.0,
    max_net_exposure_pct=1.0,
    quarantine_days=90,
    quarantine_max_position_pct=0.005,
    kelly_fraction=0.25,
    vol_target_annual=0.10,
    max_daily_loss_pct=0.02,
    max_price_band_pct=0.05,
    max_orders_per_minute=6,
    max_orders_per_hour=60,
)

#: 均衡档:对齐文章"新手三条铁律"(单笔 10% / 回撤 15% / 半 Kelly),并加了净敞口护栏。
BALANCED = RiskConfig(
    max_position_pct=0.10,
    max_drawdown_pct=0.15,
    max_gross_exposure_pct=1.0,
    max_net_exposure_pct=1.0,
    quarantine_days=90,
    quarantine_max_position_pct=0.01,
    kelly_fraction=0.50,
    vol_target_annual=0.15,
    max_daily_loss_pct=0.03,
    max_price_band_pct=0.10,
    max_orders_per_minute=10,
    max_orders_per_hour=120,
)

#: 激进档:更大单笔与敞口、允许组合层适度加杠杆;回撤容忍度更高,但净敞口仍设上限。
AGGRESSIVE = RiskConfig(
    max_position_pct=0.20,
    max_drawdown_pct=0.25,
    max_gross_exposure_pct=1.5,
    max_net_exposure_pct=1.5,
    quarantine_days=30,
    quarantine_max_position_pct=0.02,
    kelly_fraction=0.50,
    vol_target_annual=0.25,
    max_daily_loss_pct=0.05,
    max_price_band_pct=0.15,
    max_orders_per_minute=30,
    max_orders_per_hour=360,
)

#: 预设名 → 配置。名字大小写不敏感。
PRESETS: dict[str, RiskConfig] = {
    "conservative": CONSERVATIVE,
    "balanced": BALANCED,
    "aggressive": AGGRESSIVE,
}


def get_preset(name: str) -> RiskConfig:
    """按名字取预设(大小写不敏感);未知名字抛 :class:`ConfigError`。"""
    key = name.strip().lower()
    if key not in PRESETS:
        raise ConfigError(
            f"unknown preset {name!r}; choose from {sorted(PRESETS)}"
        )
    return PRESETS[key]
