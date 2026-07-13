"""``RiskConfig`` 的单元测试。

覆盖:默认值、每个区间校验分支(下界/上界/越界)、参数间的一致性校验、
``on_position_breach`` 白名单、``replace()`` 的不可变语义,以及 ``DEFAULT_CONFIG``。

测试只依赖公共 API(``from riskguard import ...``),纯标准库、无网络、确定性。
"""

from __future__ import annotations

import dataclasses

import pytest

from riskguard import DEFAULT_CONFIG, ConfigError, RiskConfig


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------
def test_default_construction_succeeds():
    """无参构造应当成功(默认值本身必须自洽,能通过所有校验)。"""
    cfg = RiskConfig()
    assert isinstance(cfg, RiskConfig)


def test_default_values_match_the_three_rules():
    """默认值对齐文章里的"新手三条铁律"。"""
    cfg = RiskConfig()
    assert cfg.max_position_pct == 0.10
    assert cfg.max_drawdown_pct == 0.15
    assert cfg.quarantine_days == 90
    assert cfg.quarantine_max_position_pct == 0.01
    assert cfg.on_position_breach == "resize"
    assert cfg.auto_register_strategies is False


def test_default_secondary_values():
    """组合层/动态仓位/元信息的默认值。"""
    cfg = RiskConfig()
    assert cfg.max_gross_exposure_pct == 1.0
    assert cfg.max_net_exposure_pct is None
    assert cfg.kelly_fraction == 0.5
    assert cfg.vol_target_annual == 0.15
    assert cfg.max_sizing_leverage == 1.0
    assert cfg.trading_days_per_year == 252


def test_config_is_frozen():
    """冻结 dataclass:任何原地赋值都应报 FrozenInstanceError。"""
    cfg = RiskConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.max_position_pct = 0.2  # type: ignore[misc]


def test_config_uses_slots():
    """slots=True:不允许挂载任意新属性。"""
    cfg = RiskConfig()
    with pytest.raises((AttributeError, TypeError)):
        cfg.not_a_real_field = 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 分数区间 (0, 1]:max_position_pct / max_drawdown_pct / quarantine_max_position_pct
# ---------------------------------------------------------------------------
def test_max_position_pct_upper_boundary_ok():
    """上界 1.0 含闭区间,应通过。"""
    cfg = RiskConfig(max_position_pct=1.0)
    assert cfg.max_position_pct == 1.0


def test_max_position_pct_zero_rejected():
    """下界 0.0 开区间,不含,应报错。"""
    with pytest.raises(ConfigError, match="max_position_pct"):
        RiskConfig(max_position_pct=0.0)


def test_max_position_pct_above_one_rejected():
    with pytest.raises(ConfigError, match="max_position_pct"):
        RiskConfig(max_position_pct=1.01)


def test_max_position_pct_negative_rejected():
    with pytest.raises(ConfigError, match="max_position_pct"):
        RiskConfig(max_position_pct=-0.1)


def test_max_drawdown_pct_upper_boundary_ok():
    cfg = RiskConfig(max_drawdown_pct=1.0)
    assert cfg.max_drawdown_pct == 1.0


def test_max_drawdown_pct_zero_rejected():
    with pytest.raises(ConfigError, match="max_drawdown_pct"):
        RiskConfig(max_drawdown_pct=0.0)


def test_max_drawdown_pct_above_one_rejected():
    with pytest.raises(ConfigError, match="max_drawdown_pct"):
        RiskConfig(max_drawdown_pct=1.5)


def test_max_drawdown_pct_negative_rejected():
    with pytest.raises(ConfigError, match="max_drawdown_pct"):
        RiskConfig(max_drawdown_pct=-0.01)


def test_quarantine_max_position_pct_zero_rejected():
    """隔离期仓位上限同样是 (0, 1] 分数,0 不合法。"""
    with pytest.raises(ConfigError, match="quarantine_max_position_pct"):
        RiskConfig(quarantine_max_position_pct=0.0)


def test_quarantine_max_position_pct_above_one_rejected():
    with pytest.raises(ConfigError, match="quarantine_max_position_pct"):
        RiskConfig(quarantine_max_position_pct=1.2)


def test_quarantine_max_position_pct_negative_rejected():
    with pytest.raises(ConfigError, match="quarantine_max_position_pct"):
        RiskConfig(quarantine_max_position_pct=-0.5)


@pytest.mark.parametrize("value", [0.0, -0.1, 1.01, 2.0])
def test_fraction_params_reject_out_of_range(value):
    """三个分数参数对越界值一律报 ConfigError(参数化覆盖)。"""
    for field_name in ("max_position_pct", "max_drawdown_pct"):
        with pytest.raises(ConfigError):
            RiskConfig(**{field_name: value})


@pytest.mark.parametrize("value", [0.001, 0.5, 1.0])
def test_fraction_params_accept_in_range(value):
    """区间内(含上界 1.0)应通过。

    注意:同时把 quarantine_max_position_pct 压到 <= value,否则默认的 0.01
    在 value=0.001 时会触发"隔离上限>常规上限"的跨字段约束。这里只想验证
    (0,1] 分数校验本身,故显式解耦跨字段约束。
    """
    cfg = RiskConfig(
        max_position_pct=value,
        max_drawdown_pct=value,
        quarantine_max_position_pct=value,
    )
    assert cfg.max_position_pct == value
    assert cfg.max_drawdown_pct == value
    assert cfg.quarantine_max_position_pct == value


# ---------------------------------------------------------------------------
# 正数校验:max_gross_exposure_pct / max_net_exposure_pct / max_sizing_leverage / vol_target_annual
# ---------------------------------------------------------------------------
def test_gross_exposure_above_one_allowed():
    """gross 走的是"> 0"校验,允许 >1(加杠杆),不套 (0,1] 上界。"""
    cfg = RiskConfig(max_gross_exposure_pct=3.0)
    assert cfg.max_gross_exposure_pct == 3.0


def test_gross_exposure_zero_rejected():
    with pytest.raises(ConfigError, match="max_gross_exposure_pct"):
        RiskConfig(max_gross_exposure_pct=0.0)


def test_gross_exposure_negative_rejected():
    with pytest.raises(ConfigError, match="max_gross_exposure_pct"):
        RiskConfig(max_gross_exposure_pct=-1.0)


def test_net_exposure_none_skips_check():
    """None 表示不限制,应跳过正数校验并成功。"""
    cfg = RiskConfig(max_net_exposure_pct=None)
    assert cfg.max_net_exposure_pct is None


def test_net_exposure_positive_ok():
    cfg = RiskConfig(max_net_exposure_pct=0.8)
    assert cfg.max_net_exposure_pct == 0.8


def test_net_exposure_above_one_allowed():
    cfg = RiskConfig(max_net_exposure_pct=2.5)
    assert cfg.max_net_exposure_pct == 2.5


def test_net_exposure_zero_rejected():
    with pytest.raises(ConfigError, match="max_net_exposure_pct"):
        RiskConfig(max_net_exposure_pct=0.0)


def test_net_exposure_negative_rejected():
    with pytest.raises(ConfigError, match="max_net_exposure_pct"):
        RiskConfig(max_net_exposure_pct=-0.5)


def test_max_sizing_leverage_zero_rejected():
    with pytest.raises(ConfigError, match="max_sizing_leverage"):
        RiskConfig(max_sizing_leverage=0.0)


def test_max_sizing_leverage_negative_rejected():
    with pytest.raises(ConfigError, match="max_sizing_leverage"):
        RiskConfig(max_sizing_leverage=-2.0)


def test_max_sizing_leverage_above_one_allowed():
    cfg = RiskConfig(max_sizing_leverage=4.0)
    assert cfg.max_sizing_leverage == 4.0


def test_vol_target_annual_zero_rejected():
    with pytest.raises(ConfigError, match="vol_target_annual"):
        RiskConfig(vol_target_annual=0.0)


def test_vol_target_annual_negative_rejected():
    with pytest.raises(ConfigError, match="vol_target_annual"):
        RiskConfig(vol_target_annual=-0.15)


def test_vol_target_annual_positive_ok():
    cfg = RiskConfig(vol_target_annual=0.30)
    assert cfg.vol_target_annual == 0.30


# ---------------------------------------------------------------------------
# kelly_fraction:区间 (0, 1]
# ---------------------------------------------------------------------------
def test_kelly_fraction_upper_boundary_ok():
    """满 Kelly = 1.0 合法(上界闭)。"""
    cfg = RiskConfig(kelly_fraction=1.0)
    assert cfg.kelly_fraction == 1.0


def test_kelly_fraction_zero_rejected():
    """0 不合法(下界开)。"""
    with pytest.raises(ConfigError, match="kelly_fraction"):
        RiskConfig(kelly_fraction=0.0)


def test_kelly_fraction_above_one_rejected():
    with pytest.raises(ConfigError, match="kelly_fraction"):
        RiskConfig(kelly_fraction=1.1)


def test_kelly_fraction_negative_rejected():
    with pytest.raises(ConfigError, match="kelly_fraction"):
        RiskConfig(kelly_fraction=-0.25)


def test_kelly_fraction_typical_value_ok():
    cfg = RiskConfig(kelly_fraction=0.25)
    assert cfg.kelly_fraction == 0.25


# ---------------------------------------------------------------------------
# quarantine_days:>= 0
# ---------------------------------------------------------------------------
def test_quarantine_days_zero_ok():
    """0 天合法(下界闭:等价于不设隔离期)。"""
    cfg = RiskConfig(quarantine_days=0)
    assert cfg.quarantine_days == 0


def test_quarantine_days_negative_rejected():
    with pytest.raises(ConfigError, match="quarantine_days"):
        RiskConfig(quarantine_days=-1)


def test_quarantine_days_large_ok():
    cfg = RiskConfig(quarantine_days=365)
    assert cfg.quarantine_days == 365


# ---------------------------------------------------------------------------
# trading_days_per_year:> 0
# ---------------------------------------------------------------------------
def test_trading_days_zero_rejected():
    with pytest.raises(ConfigError, match="trading_days_per_year"):
        RiskConfig(trading_days_per_year=0)


def test_trading_days_negative_rejected():
    with pytest.raises(ConfigError, match="trading_days_per_year"):
        RiskConfig(trading_days_per_year=-252)


def test_trading_days_positive_ok():
    cfg = RiskConfig(trading_days_per_year=365)
    assert cfg.trading_days_per_year == 365


# ---------------------------------------------------------------------------
# 参数间一致性:quarantine_max_position_pct 不得超过 max_position_pct
# ---------------------------------------------------------------------------
def test_quarantine_pct_exceeding_position_pct_rejected():
    """两者各自都在 (0,1] 内合法,但隔离上限 > 常规上限时应报错。"""
    with pytest.raises(ConfigError, match="quarantine_max_position_pct"):
        RiskConfig(max_position_pct=0.10, quarantine_max_position_pct=0.50)


def test_quarantine_pct_equal_position_pct_ok():
    """相等是允许的(<=,不是 <)。"""
    cfg = RiskConfig(max_position_pct=0.10, quarantine_max_position_pct=0.10)
    assert cfg.quarantine_max_position_pct == cfg.max_position_pct


def test_quarantine_pct_below_position_pct_ok():
    cfg = RiskConfig(max_position_pct=0.20, quarantine_max_position_pct=0.05)
    assert cfg.quarantine_max_position_pct < cfg.max_position_pct


def test_quarantine_exceed_message_mentions_both_values():
    """错误信息应同时给出越界值与上限,便于定位。"""
    with pytest.raises(ConfigError) as exc_info:
        RiskConfig(max_position_pct=0.10, quarantine_max_position_pct=0.30)
    msg = str(exc_info.value)
    assert "0.3" in msg and "0.1" in msg


# ---------------------------------------------------------------------------
# on_position_breach 白名单
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["resize", "reject"])
def test_on_position_breach_valid_values(action):
    cfg = RiskConfig(on_position_breach=action)
    assert cfg.on_position_breach == action


def test_on_position_breach_invalid_string_rejected():
    with pytest.raises(ConfigError, match="on_position_breach"):
        RiskConfig(on_position_breach="cancel")  # type: ignore[arg-type]


def test_on_position_breach_empty_string_rejected():
    with pytest.raises(ConfigError, match="on_position_breach"):
        RiskConfig(on_position_breach="")  # type: ignore[arg-type]


def test_on_position_breach_wrong_case_rejected():
    """白名单大小写敏感:'Resize' 不是合法值。"""
    with pytest.raises(ConfigError, match="on_position_breach"):
        RiskConfig(on_position_breach="Resize")  # type: ignore[arg-type]


def test_on_position_breach_none_rejected():
    with pytest.raises(ConfigError, match="on_position_breach"):
        RiskConfig(on_position_breach=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# replace():不可变模式,返回新对象
# ---------------------------------------------------------------------------
def test_replace_returns_new_instance():
    base = RiskConfig()
    updated = base.replace(max_position_pct=0.05)
    assert updated is not base
    assert isinstance(updated, RiskConfig)


def test_replace_does_not_mutate_original():
    base = RiskConfig(max_position_pct=0.10)
    updated = base.replace(max_position_pct=0.05)
    assert base.max_position_pct == 0.10  # 原对象未变
    assert updated.max_position_pct == 0.05


def test_replace_keeps_other_fields():
    base = RiskConfig(max_drawdown_pct=0.20, kelly_fraction=0.25)
    updated = base.replace(max_position_pct=0.05)
    assert updated.max_drawdown_pct == 0.20
    assert updated.kelly_fraction == 0.25
    assert updated.max_position_pct == 0.05


def test_replace_multiple_fields():
    base = RiskConfig()
    updated = base.replace(max_position_pct=0.08, quarantine_days=30)
    assert updated.max_position_pct == 0.08
    assert updated.quarantine_days == 30


def test_replace_revalidates_and_rejects_bad_value():
    """replace 走的是 dataclasses.replace,会重新触发 __post_init__ 校验。"""
    base = RiskConfig()
    with pytest.raises(ConfigError, match="max_position_pct"):
        base.replace(max_position_pct=0.0)


def test_replace_revalidates_cross_field_constraint():
    """replace 出来的组合若违反跨字段约束(隔离>常规),同样应被拦截。"""
    base = RiskConfig(max_position_pct=0.10, quarantine_max_position_pct=0.01)
    with pytest.raises(ConfigError, match="quarantine_max_position_pct"):
        base.replace(quarantine_max_position_pct=0.50)


def test_replace_with_no_changes_returns_equivalent():
    base = RiskConfig(max_position_pct=0.07)
    same = base.replace()
    assert same is not base
    assert same.max_position_pct == base.max_position_pct


def test_replace_result_is_frozen():
    updated = RiskConfig().replace(max_position_pct=0.05)
    with pytest.raises(dataclasses.FrozenInstanceError):
        updated.max_position_pct = 0.09  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG
# ---------------------------------------------------------------------------
def test_default_config_is_riskconfig():
    assert isinstance(DEFAULT_CONFIG, RiskConfig)


def test_default_config_equals_bare_construction():
    """DEFAULT_CONFIG 应等价于 RiskConfig()(冻结 dataclass 支持值相等)。"""
    assert DEFAULT_CONFIG == RiskConfig()


def test_default_config_has_expected_values():
    assert DEFAULT_CONFIG.max_position_pct == 0.10
    assert DEFAULT_CONFIG.max_drawdown_pct == 0.15
    assert DEFAULT_CONFIG.quarantine_days == 90


def test_default_config_replace_leaves_module_constant_untouched():
    """基于 DEFAULT_CONFIG 派生新配置,不能污染共享的模块级常量。"""
    derived = DEFAULT_CONFIG.replace(max_position_pct=0.03)
    assert derived.max_position_pct == 0.03
    assert DEFAULT_CONFIG.max_position_pct == 0.10  # 常量不受影响
    assert DEFAULT_CONFIG == RiskConfig()


def test_default_config_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_CONFIG.max_drawdown_pct = 0.30  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 综合:一个自定义但自洽的配置
# ---------------------------------------------------------------------------
def test_fully_custom_valid_config():
    cfg = RiskConfig(
        max_position_pct=0.25,
        on_position_breach="reject",
        max_drawdown_pct=0.10,
        quarantine_days=60,
        quarantine_max_position_pct=0.02,
        auto_register_strategies=True,
        max_gross_exposure_pct=2.0,
        max_net_exposure_pct=1.0,
        kelly_fraction=0.5,
        vol_target_annual=0.20,
        max_sizing_leverage=2.0,
        trading_days_per_year=250,
    )
    assert cfg.on_position_breach == "reject"
    assert cfg.auto_register_strategies is True
    assert cfg.max_net_exposure_pct == 1.0
    assert cfg.trading_days_per_year == 250


# ---------------------------------------------------------------------------
# AI 代理闸门三件套(日内亏损线 / 价格保护带 / 下单节流)
# ---------------------------------------------------------------------------
def test_ai_gate_fields_default_off():
    """三件套默认全关(None),保证 v1.4 行为不变;由预设开启。"""
    cfg = RiskConfig()
    assert cfg.max_daily_loss_pct is None
    assert cfg.session_boundary_utc == "00:00"
    assert cfg.max_price_band_pct is None
    assert cfg.max_orders_per_minute is None
    assert cfg.max_orders_per_hour is None
    assert cfg.reduce_only_throttle_factor is None  # None = 减仓完全豁免节流


def test_max_daily_loss_pct_bounds():
    assert RiskConfig(max_daily_loss_pct=0.03).max_daily_loss_pct == 0.03
    assert RiskConfig(max_daily_loss_pct=0.15).max_daily_loss_pct == 0.15  # == max_drawdown_pct
    with pytest.raises(ConfigError):
        RiskConfig(max_daily_loss_pct=0.0)
    with pytest.raises(ConfigError):
        RiskConfig(max_daily_loss_pct=-0.05)
    with pytest.raises(ConfigError):
        RiskConfig(max_daily_loss_pct=1.5)


def test_max_daily_loss_pct_must_not_exceed_max_drawdown_pct():
    """日内线必须比总回撤线紧(镜像隔离仓位 vs 单笔仓位的既有先例)。"""
    with pytest.raises(ConfigError):
        RiskConfig(max_daily_loss_pct=0.20, max_drawdown_pct=0.15)
    # 一同调大总线则合法
    cfg = RiskConfig(max_daily_loss_pct=0.20, max_drawdown_pct=0.30)
    assert cfg.max_daily_loss_pct == 0.20


@pytest.mark.parametrize("bad", ["24:00", "12:60", "9:00", "0900", "aa:bb", "", "12:00:00"])
def test_session_boundary_utc_rejects_malformed(bad):
    with pytest.raises(ConfigError):
        RiskConfig(session_boundary_utc=bad)


@pytest.mark.parametrize("good", ["00:00", "17:00", "23:59", "09:30"])
def test_session_boundary_utc_accepts_hh_mm(good):
    assert RiskConfig(session_boundary_utc=good).session_boundary_utc == good


def test_max_price_band_pct_bounds():
    assert RiskConfig(max_price_band_pct=0.10).max_price_band_pct == 0.10
    with pytest.raises(ConfigError):
        RiskConfig(max_price_band_pct=0.0)
    with pytest.raises(ConfigError):
        RiskConfig(max_price_band_pct=1.5)


@pytest.mark.parametrize("field", ["max_orders_per_minute", "max_orders_per_hour"])
def test_throttle_caps_must_be_positive_ints(field):
    assert getattr(RiskConfig(**{field: 10}), field) == 10
    with pytest.raises(ConfigError):
        RiskConfig(**{field: 0})
    with pytest.raises(ConfigError):
        RiskConfig(**{field: -3})


def test_throttle_hour_cap_must_cover_minute_cap():
    """两者都设时 hour >= minute,否则分钟配额永远用不满,必是配置笔误。"""
    with pytest.raises(ConfigError):
        RiskConfig(max_orders_per_minute=10, max_orders_per_hour=5)
    cfg = RiskConfig(max_orders_per_minute=10, max_orders_per_hour=10)
    assert cfg.max_orders_per_hour == 10


def test_reduce_only_throttle_factor_bounds():
    assert RiskConfig(reduce_only_throttle_factor=1.0).reduce_only_throttle_factor == 1.0
    assert RiskConfig(reduce_only_throttle_factor=None).reduce_only_throttle_factor is None
    with pytest.raises(ConfigError):
        RiskConfig(reduce_only_throttle_factor=0.5)
