"""针对 :mod:`riskguard.models` 的单元测试。

覆盖:Order 校验与不可变性、Position 计价、Account 买力钳制、Portfolio
组合层查询、resolve_price 价格回退链、Signal 校验,以及 RuleResult /
RiskDecision 的裁决辅助方法。

全部纯本地、无网络、确定性。尽量从公开 API ``riskguard`` 导入;仅
``resolve_price`` 与 ``PriceUnavailable`` 从其所在模块导入(未进 top-level
``__all__``,但属公开契约)。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from riskguard import (
    Account,
    Decision,
    Order,
    OrderType,
    Portfolio,
    Position,
    RiskDecision,
    RuleResult,
    Side,
    Signal,
)
from riskguard.exceptions import PriceUnavailable
from riskguard.models import resolve_price


# ---------------------------------------------------------------------------
# 小工具:构造一条最小可用的裁决/规则结果
# ---------------------------------------------------------------------------
def _order(**kw) -> Order:
    base = dict(symbol="AAPL", side=Side.BUY, quantity=10.0)
    base.update(kw)
    return Order(**base)


def _fixed_now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


# ===========================================================================
# Side / OrderType 枚举
# ===========================================================================
class TestSide:
    def test_sign_buy_is_positive(self):
        assert Side.BUY.sign == 1

    def test_sign_sell_is_negative(self):
        assert Side.SELL.sign == -1

    def test_side_is_str_enum(self):
        # 继承 str 便于直接序列化
        assert Side.BUY == "buy"
        assert Side.SELL.value == "sell"


# ===========================================================================
# Order 校验
# ===========================================================================
class TestOrderValidation:
    def test_valid_market_order(self):
        o = _order()
        assert o.symbol == "AAPL"
        assert o.order_type is OrderType.MARKET
        assert o.quantity == 10.0

    def test_empty_symbol_raises(self):
        with pytest.raises(ValueError, match="symbol"):
            _order(symbol="")

    def test_none_symbol_raises(self):
        # None 也是 falsy,应被 "must be a non-empty string" 拦下
        with pytest.raises(ValueError, match="symbol"):
            _order(symbol=None)

    def test_zero_quantity_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            _order(quantity=0)

    def test_negative_quantity_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            _order(quantity=-5)

    def test_limit_order_without_price_raises(self):
        with pytest.raises(ValueError, match="limit_price is required"):
            _order(order_type=OrderType.LIMIT, limit_price=None)

    def test_limit_order_with_price_ok(self):
        o = _order(order_type=OrderType.LIMIT, limit_price=150.0)
        assert o.limit_price == 150.0

    def test_zero_limit_price_raises(self):
        with pytest.raises(ValueError, match="limit_price must be > 0"):
            _order(order_type=OrderType.LIMIT, limit_price=0)

    def test_negative_limit_price_raises(self):
        # 即使是 MARKET 单,给了非法 limit_price 也要拦
        with pytest.raises(ValueError, match="limit_price must be > 0"):
            _order(limit_price=-1.0)

    def test_market_order_may_omit_limit_price(self):
        o = _order(order_type=OrderType.MARKET)
        assert o.limit_price is None

    def test_string_side_coerced_to_enum(self):
        o = _order(side="buy")
        assert o.side is Side.BUY
        assert isinstance(o.side, Side)

    def test_string_order_type_coerced_to_enum(self):
        o = _order(order_type="limit", limit_price=100.0)
        assert o.order_type is OrderType.LIMIT
        assert isinstance(o.order_type, OrderType)

    def test_invalid_side_string_raises(self):
        with pytest.raises(ValueError):
            _order(side="hold")

    def test_invalid_order_type_string_raises(self):
        with pytest.raises(ValueError):
            _order(order_type="stop")

    def test_defaults(self):
        o = _order()
        assert o.strategy_id == "default"
        assert o.client_order_id is None
        assert o.reduce_only is False

    def test_meta_frozen_to_readonly_mapping(self):
        o = _order(meta={"tag": "x"})
        assert o.meta["tag"] == "x"
        with pytest.raises(TypeError):
            o.meta["tag"] = "y"  # mappingproxy 不可写

    def test_meta_snapshot_isolated_from_source_dict(self):
        # 传入的 dict 之后被外部改动,不应影响已构造的订单
        src = {"a": 1}
        o = _order(meta=src)
        src["a"] = 999
        src["b"] = 2
        assert o.meta["a"] == 1
        assert "b" not in o.meta

    def test_meta_default_is_empty(self):
        o = _order()
        assert dict(o.meta) == {}


# ===========================================================================
# Order 不可变性 & 便捷方法
# ===========================================================================
class TestOrderImmutability:
    def test_setattr_raises(self):
        o = _order()
        with pytest.raises(FrozenInstanceError):
            o.quantity = 20  # type: ignore[misc]

    def test_setattr_symbol_raises(self):
        o = _order()
        with pytest.raises(FrozenInstanceError):
            o.symbol = "MSFT"  # type: ignore[misc]

    def test_signed_quantity_buy(self):
        assert _order(side=Side.BUY, quantity=10).signed_quantity == 10

    def test_signed_quantity_sell(self):
        assert _order(side=Side.SELL, quantity=10).signed_quantity == -10

    def test_with_quantity_returns_new_object(self):
        o = _order(quantity=10)
        o2 = o.with_quantity(4)
        assert o2 is not o
        assert o2.quantity == 4
        # 原对象不变
        assert o.quantity == 10

    def test_with_quantity_preserves_other_fields(self):
        o = _order(quantity=10, side=Side.SELL, strategy_id="s1", reduce_only=True)
        o2 = o.with_quantity(3)
        assert o2.side is Side.SELL
        assert o2.strategy_id == "s1"
        assert o2.reduce_only is True
        assert o2.symbol == o.symbol


# ===========================================================================
# Position
# ===========================================================================
class TestPosition:
    def test_long_flags(self):
        p = Position("AAPL", 100, 150.0)
        assert p.is_long is True
        assert p.is_short is False
        assert p.is_flat is False

    def test_short_flags(self):
        p = Position("AAPL", -100, 150.0)
        assert p.is_long is False
        assert p.is_short is True
        assert p.is_flat is False

    def test_flat_flags(self):
        p = Position("AAPL", 0, 0.0)
        assert p.is_long is False
        assert p.is_short is False
        assert p.is_flat is True

    def test_notional_is_absolute(self):
        assert Position("AAPL", 100, 150.0).notional(200.0) == 20000.0
        # 空头名义敞口取绝对值
        assert Position("AAPL", -100, 150.0).notional(200.0) == 20000.0

    def test_market_value_signed(self):
        assert Position("AAPL", 100, 150.0).market_value(200.0) == 20000.0
        # 空头市值为负
        assert Position("AAPL", -100, 150.0).market_value(200.0) == -20000.0

    def test_position_is_frozen(self):
        p = Position("AAPL", 100, 150.0)
        with pytest.raises(FrozenInstanceError):
            p.quantity = 5  # type: ignore[misc]


# ===========================================================================
# Account 买力钳制
# ===========================================================================
class TestAccount:
    def test_negative_buying_power_clamped_to_zero(self):
        a = Account(equity=1000.0, buying_power=-500.0)
        assert a.buying_power == 0.0

    def test_positive_buying_power_kept(self):
        a = Account(equity=1000.0, buying_power=250.0)
        assert a.buying_power == 250.0

    def test_zero_buying_power_kept(self):
        a = Account(equity=1000.0, buying_power=0.0)
        assert a.buying_power == 0.0

    def test_negative_equity_allowed(self):
        # 权益可为负(穿仓),不拦
        a = Account(equity=-100.0)
        assert a.equity == -100.0

    def test_defaults(self):
        a = Account(equity=1000.0)
        assert a.cash == 0.0
        assert a.buying_power == 0.0

    def test_account_is_frozen(self):
        a = Account(equity=1000.0)
        with pytest.raises(FrozenInstanceError):
            a.equity = 2000.0  # type: ignore[misc]


# ===========================================================================
# Portfolio
# ===========================================================================
class TestPortfolio:
    def _pf(self, positions=None, marks=None, equity=100000.0):
        return Portfolio(
            account=Account(equity=equity),
            positions=positions or {},
            marks=marks or {},
        )

    def test_equity_proxies_account(self):
        pf = self._pf(equity=50000.0)
        assert pf.equity == 50000.0

    def test_position_existing(self):
        p = Position("AAPL", 100, 150.0)
        pf = self._pf(positions={"AAPL": p})
        assert pf.position("AAPL") is p

    def test_position_missing_returns_flat_zero(self):
        pf = self._pf()
        p = pf.position("TSLA")
        assert p.symbol == "TSLA"
        assert p.quantity == 0.0
        assert p.is_flat is True

    def test_mark_for_from_marks(self):
        pf = self._pf(marks={"AAPL": 200.0})
        assert pf.mark_for("AAPL") == 200.0

    def test_mark_for_falls_back_to_avg_price(self):
        pf = self._pf(positions={"AAPL": Position("AAPL", 100, 150.0)})
        assert pf.mark_for("AAPL") == 150.0

    def test_mark_for_prefers_mark_over_avg(self):
        pf = self._pf(
            positions={"AAPL": Position("AAPL", 100, 150.0)},
            marks={"AAPL": 200.0},
        )
        assert pf.mark_for("AAPL") == 200.0

    def test_mark_for_none_when_nothing(self):
        pf = self._pf()
        assert pf.mark_for("AAPL") is None

    def test_mark_for_none_when_avg_price_zero(self):
        # 均价为 0 不能当价用,应回退到 None
        pf = self._pf(positions={"AAPL": Position("AAPL", 100, 0.0)})
        assert pf.mark_for("AAPL") is None

    def test_nonpositive_marks_are_dropped(self):
        # 非正/NaN 标记价在构造时即被丢弃(坏 tick 不许污染敞口计算),
        # 无其它价源可回退时 mark_for 返回 None。
        pf = self._pf(marks={"AAPL": 0.0, "MSFT": -5.0, "TSLA": float("nan")})
        assert pf.mark_for("AAPL") is None
        assert pf.mark_for("MSFT") is None
        assert pf.mark_for("TSLA") is None
        assert dict(pf.marks) == {}

    def test_nonpositive_mark_falls_back_to_avg_price(self):
        # 坏价被丢弃后,若持仓有正的均价,mark_for 回退到均价。
        pf = self._pf(
            positions={"AAPL": Position("AAPL", 100, 150.0)},
            marks={"AAPL": -1.0},
        )
        assert pf.mark_for("AAPL") == 150.0

    def test_position_notional_with_mark(self):
        pf = self._pf(
            positions={"AAPL": Position("AAPL", 100, 150.0)},
            marks={"AAPL": 200.0},
        )
        assert pf.position_notional("AAPL") == 20000.0

    def test_position_notional_uses_avg_fallback(self):
        pf = self._pf(positions={"AAPL": Position("AAPL", 100, 150.0)})
        assert pf.position_notional("AAPL") == 15000.0

    def test_position_notional_zero_when_no_price(self):
        # 无持仓、无标记 -> flat 零仓 + 无价 -> 0.0
        pf = self._pf()
        assert pf.position_notional("AAPL") == 0.0

    def test_weight(self):
        pf = self._pf(
            positions={"AAPL": Position("AAPL", 100, 150.0)},
            marks={"AAPL": 200.0},
            equity=100000.0,
        )
        # 20000 / 100000
        assert pf.weight("AAPL") == pytest.approx(0.2)

    def test_weight_zero_equity_returns_zero(self):
        pf = self._pf(
            positions={"AAPL": Position("AAPL", 100, 150.0)},
            marks={"AAPL": 200.0},
            equity=0.0,
        )
        assert pf.weight("AAPL") == 0.0

    def test_weight_negative_equity_returns_zero(self):
        pf = self._pf(
            positions={"AAPL": Position("AAPL", 100, 150.0)},
            marks={"AAPL": 200.0},
            equity=-1000.0,
        )
        assert pf.weight("AAPL") == 0.0

    def test_gross_exposure_sums_absolute(self):
        pf = self._pf(
            positions={
                "AAPL": Position("AAPL", 100, 150.0),   # long
                "TSLA": Position("TSLA", -50, 300.0),   # short
            },
            marks={"AAPL": 200.0, "TSLA": 400.0},
        )
        # |100*200| + |-50*400| = 20000 + 20000
        assert pf.gross_exposure() == 40000.0

    def test_net_exposure_signed(self):
        pf = self._pf(
            positions={
                "AAPL": Position("AAPL", 100, 150.0),   # +20000
                "TSLA": Position("TSLA", -50, 300.0),   # -20000
            },
            marks={"AAPL": 200.0, "TSLA": 400.0},
        )
        assert pf.net_exposure() == 0.0

    def test_gross_exposure_skips_unpriced(self):
        # NOPRICE 无标记、无均价 -> 从敞口计算中被跳过
        pf = self._pf(
            positions={
                "AAPL": Position("AAPL", 100, 150.0),
                "NOPRICE": Position("NOPRICE", 100, 0.0),
            },
            marks={"AAPL": 200.0},
        )
        assert pf.gross_exposure() == 20000.0

    def test_net_exposure_skips_unpriced(self):
        pf = self._pf(
            positions={
                "AAPL": Position("AAPL", 100, 150.0),
                "NOPRICE": Position("NOPRICE", -100, 0.0),
            },
            marks={"AAPL": 200.0},
        )
        assert pf.net_exposure() == 20000.0

    def test_gross_exposure_uses_avg_fallback(self):
        # 无 marks 时用均价计敞口
        pf = self._pf(positions={"AAPL": Position("AAPL", 100, 150.0)})
        assert pf.gross_exposure() == 15000.0

    def test_exposure_empty_portfolio(self):
        pf = self._pf()
        assert pf.gross_exposure() == 0.0
        assert pf.net_exposure() == 0.0

    def test_positions_frozen_to_readonly(self):
        pf = self._pf(positions={"AAPL": Position("AAPL", 100, 150.0)})
        with pytest.raises(TypeError):
            pf.positions["MSFT"] = Position("MSFT", 1, 1.0)  # type: ignore[index]

    def test_marks_frozen_to_readonly(self):
        pf = self._pf(marks={"AAPL": 200.0})
        with pytest.raises(TypeError):
            pf.marks["AAPL"] = 1.0  # type: ignore[index]

    def test_positions_snapshot_isolated(self):
        src = {"AAPL": Position("AAPL", 100, 150.0)}
        pf = self._pf(positions=src)
        src["MSFT"] = Position("MSFT", 1, 1.0)
        assert "MSFT" not in pf.positions

    def test_portfolio_is_frozen(self):
        pf = self._pf()
        with pytest.raises(FrozenInstanceError):
            pf.account = Account(equity=1.0)  # type: ignore[misc]


# ===========================================================================
# resolve_price 价格回退链
# ===========================================================================
class TestResolvePrice:
    def _pf(self, positions=None, marks=None):
        return Portfolio(
            account=Account(equity=100000.0),
            positions=positions or {},
            marks=marks or {},
        )

    def test_prefers_mark(self):
        pf = self._pf(
            positions={"AAPL": Position("AAPL", 100, 150.0)},
            marks={"AAPL": 200.0},
        )
        order = _order(symbol="AAPL", order_type=OrderType.LIMIT, limit_price=175.0)
        assert resolve_price(pf, order) == 200.0

    def test_falls_back_to_limit(self):
        pf = self._pf(positions={"AAPL": Position("AAPL", 100, 150.0)})
        order = _order(symbol="AAPL", order_type=OrderType.LIMIT, limit_price=175.0)
        # 无 mark -> 用限价(优先于均价)
        assert resolve_price(pf, order) == 175.0

    def test_falls_back_to_avg_price(self):
        pf = self._pf(positions={"AAPL": Position("AAPL", 100, 150.0)})
        order = _order(symbol="AAPL", order_type=OrderType.MARKET)
        # 无 mark、无限价 -> 用均价
        assert resolve_price(pf, order) == 150.0

    def test_mark_zero_skipped_uses_limit(self):
        # mark <= 0 视为无效,跳到限价
        pf = self._pf(marks={"AAPL": 0.0})
        order = _order(symbol="AAPL", order_type=OrderType.LIMIT, limit_price=175.0)
        assert resolve_price(pf, order) == 175.0

    def test_mark_negative_skipped_uses_avg(self):
        pf = self._pf(
            positions={"AAPL": Position("AAPL", 100, 150.0)},
            marks={"AAPL": -5.0},
        )
        order = _order(symbol="AAPL", order_type=OrderType.MARKET)
        assert resolve_price(pf, order) == 150.0

    def test_raises_price_unavailable(self):
        pf = self._pf()
        order = _order(symbol="AAPL", order_type=OrderType.MARKET)
        with pytest.raises(PriceUnavailable, match="AAPL"):
            resolve_price(pf, order)

    def test_raises_when_avg_price_zero_and_no_others(self):
        # 均价 0 不可用,且无 mark/limit
        pf = self._pf(positions={"AAPL": Position("AAPL", 100, 0.0)})
        order = _order(symbol="AAPL", order_type=OrderType.MARKET)
        with pytest.raises(PriceUnavailable):
            resolve_price(pf, order)


# ===========================================================================
# Signal 校验
# ===========================================================================
class TestSignal:
    def test_valid_signal(self):
        s = Signal(symbol="AAPL", side=Side.BUY, price=150.0)
        assert s.price == 150.0
        assert s.strategy_id == "default"

    def test_string_side_coerced(self):
        s = Signal(symbol="AAPL", side="sell", price=150.0)
        assert s.side is Side.SELL
        assert isinstance(s.side, Side)

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            Signal(symbol="AAPL", side="hold", price=150.0)

    def test_zero_price_raises(self):
        with pytest.raises(ValueError, match="price"):
            Signal(symbol="AAPL", side=Side.BUY, price=0)

    def test_negative_price_raises(self):
        with pytest.raises(ValueError, match="price"):
            Signal(symbol="AAPL", side=Side.BUY, price=-1.0)

    def test_optional_fields_default_none(self):
        s = Signal(symbol="AAPL", side=Side.BUY, price=150.0)
        assert s.win_probability is None
        assert s.payoff_ratio is None
        assert s.volatility is None

    def test_meta_frozen(self):
        s = Signal(symbol="AAPL", side=Side.BUY, price=150.0, meta={"k": "v"})
        assert s.meta["k"] == "v"
        with pytest.raises(TypeError):
            s.meta["k"] = "x"

    def test_signal_is_frozen(self):
        s = Signal(symbol="AAPL", side=Side.BUY, price=150.0)
        with pytest.raises(FrozenInstanceError):
            s.price = 1.0  # type: ignore[misc]


# ===========================================================================
# RuleResult
# ===========================================================================
class TestRuleResult:
    def test_basic_fields(self):
        r = RuleResult(rule="max_position", action=Decision.APPROVE, passed=True)
        assert r.rule == "max_position"
        assert r.action is Decision.APPROVE
        assert r.passed is True
        assert r.adjusted_quantity is None
        assert r.message == ""

    def test_detail_frozen(self):
        r = RuleResult(
            rule="x",
            action=Decision.REJECT,
            passed=False,
            detail={"limit": 0.1},
        )
        assert r.detail["limit"] == 0.1
        with pytest.raises(TypeError):
            r.detail["limit"] = 0.2

    def test_ruleresult_is_frozen(self):
        r = RuleResult(rule="x", action=Decision.APPROVE, passed=True)
        with pytest.raises(FrozenInstanceError):
            r.passed = False  # type: ignore[misc]


# ===========================================================================
# RiskDecision 辅助方法
# ===========================================================================
class TestRiskDecision:
    def _decision(self, decision_type, results=()):
        o = _order()
        return RiskDecision(
            decision=decision_type,
            order=o,
            original_order=o,
            results=tuple(results),
            timestamp=_fixed_now(),
        )

    def test_approved_true_for_approve(self):
        d = self._decision(Decision.APPROVE)
        assert d.approved is True
        assert d.rejected is False
        assert d.resized is False

    def test_approved_true_for_resize(self):
        d = self._decision(Decision.RESIZE)
        assert d.approved is True
        assert d.resized is True
        assert d.rejected is False

    def test_rejected(self):
        d = self._decision(Decision.REJECT)
        assert d.rejected is True
        assert d.approved is False
        assert d.resized is False

    def test_rejections_filters_reject_and_failed(self):
        results = [
            RuleResult(rule="ok", action=Decision.APPROVE, passed=True),
            RuleResult(
                rule="bad", action=Decision.REJECT, passed=False, message="too big"
            ),
            # REJECT 但 passed=True(理论上不该拒),不算真正的拒单来源
            RuleResult(rule="weird", action=Decision.REJECT, passed=True),
            # RESIZE 且未通过,不属于 rejections
            RuleResult(
                rule="resize", action=Decision.RESIZE, passed=False, message="shrunk"
            ),
        ]
        d = self._decision(Decision.REJECT, results)
        rej = d.rejections()
        assert len(rej) == 1
        assert rej[0].rule == "bad"

    def test_rejections_empty_when_all_pass(self):
        results = [
            RuleResult(rule="a", action=Decision.APPROVE, passed=True),
            RuleResult(rule="b", action=Decision.APPROVE, passed=True),
        ]
        d = self._decision(Decision.APPROVE, results)
        assert d.rejections() == ()

    def test_reasons_joins_failed_messages(self):
        results = [
            RuleResult(rule="a", action=Decision.APPROVE, passed=True, message="ok"),
            RuleResult(
                rule="b", action=Decision.REJECT, passed=False, message="too big"
            ),
            RuleResult(
                rule="c", action=Decision.RESIZE, passed=False, message="shrunk"
            ),
        ]
        d = self._decision(Decision.REJECT, results)
        # 只拼接未通过规则的 message,分号连接;通过的 "ok" 不出现
        assert d.reasons() == "too big; shrunk"

    def test_reasons_empty_when_all_pass(self):
        results = [RuleResult(rule="a", action=Decision.APPROVE, passed=True)]
        d = self._decision(Decision.APPROVE, results)
        assert d.reasons() == ""

    def test_riskdecision_is_frozen(self):
        d = self._decision(Decision.APPROVE)
        with pytest.raises(FrozenInstanceError):
            d.decision = Decision.REJECT  # type: ignore[misc]

    def test_original_vs_final_order_distinct(self):
        # 缩量场景:final order 数量小于 original
        original = _order(quantity=10)
        final = original.with_quantity(4)
        d = RiskDecision(
            decision=Decision.RESIZE,
            order=final,
            original_order=original,
            results=(),
            timestamp=_fixed_now(),
        )
        assert d.order.quantity == 4
        assert d.original_order.quantity == 10
        assert d.resized is True
