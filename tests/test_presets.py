"""配置预设测试。"""

from __future__ import annotations

import pytest

from riskguard import AGGRESSIVE, BALANCED, CONSERVATIVE, PRESETS, RiskConfig, get_preset
from riskguard.exceptions import ConfigError


def test_presets_are_valid_configs():
    for cfg in PRESETS.values():
        assert isinstance(cfg, RiskConfig)
    assert set(PRESETS) == {"conservative", "balanced", "aggressive"}


def test_get_preset_case_insensitive():
    assert get_preset("Balanced") is BALANCED
    assert get_preset("  AGGRESSIVE ") is AGGRESSIVE
    assert get_preset("conservative") is CONSERVATIVE


def test_get_preset_unknown_raises():
    with pytest.raises(ConfigError):
        get_preset("yolo")


def test_risk_monotonic_across_tiers():
    # 单笔仓位、回撤容忍、Kelly 系数都应随激进度单调升高
    assert (
        CONSERVATIVE.max_position_pct
        < BALANCED.max_position_pct
        < AGGRESSIVE.max_position_pct
    )
    assert (
        CONSERVATIVE.max_drawdown_pct
        < BALANCED.max_drawdown_pct
        < AGGRESSIVE.max_drawdown_pct
    )
    # Kelly 非严格单调:激进档刻意封顶在库自身认可的稳健上限 0.5(不超过它)
    assert (
        CONSERVATIVE.kelly_fraction
        < BALANCED.kelly_fraction
        <= AGGRESSIVE.kelly_fraction
    )
    assert AGGRESSIVE.kelly_fraction <= 0.5  # 不越过 config 说的"实务常用 0.25~0.5"


def test_net_exposure_cap_present_and_monotone():
    # 净敞口:三档都设了上限(最激进档不能是唯一"不限制"的),且单调不减
    nets = [c.max_net_exposure_pct for c in (CONSERVATIVE, BALANCED, AGGRESSIVE)]
    assert all(n is not None for n in nets)
    assert nets[0] <= nets[1] <= nets[2]


def test_balanced_equals_library_defaults():
    d = RiskConfig()
    assert BALANCED.max_position_pct == d.max_position_pct
    assert BALANCED.max_drawdown_pct == d.max_drawdown_pct


def test_quarantine_cap_within_position_cap():
    for cfg in PRESETS.values():
        assert cfg.quarantine_max_position_pct <= cfg.max_position_pct


# ---------------------------------------------------------------------------
# AI 代理闸门三件套:预设中默认开启,且三档单调
# ---------------------------------------------------------------------------
def test_ai_gate_enabled_in_all_presets():
    """默认 RiskConfig() 里三件套关着;预设是它们"开箱即开"的地方。"""
    for name in ("conservative", "balanced", "aggressive"):
        cfg = get_preset(name)
        assert cfg.max_daily_loss_pct is not None
        assert cfg.max_price_band_pct is not None
        assert cfg.max_orders_per_minute is not None
        assert cfg.max_orders_per_hour is not None


def test_ai_gate_values_monotone_across_tiers():
    c, b, a = (get_preset(n) for n in ("conservative", "balanced", "aggressive"))
    assert c.max_daily_loss_pct <= b.max_daily_loss_pct <= a.max_daily_loss_pct
    assert c.max_price_band_pct <= b.max_price_band_pct <= a.max_price_band_pct
    assert c.max_orders_per_minute <= b.max_orders_per_minute <= a.max_orders_per_minute
    assert c.max_orders_per_hour <= b.max_orders_per_hour <= a.max_orders_per_hour


def test_balanced_ai_gate_values():
    b = get_preset("balanced")
    assert b.max_daily_loss_pct == 0.03
    assert b.max_price_band_pct == 0.10
    assert b.max_orders_per_minute == 10
    assert b.max_orders_per_hour == 120
