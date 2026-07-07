"""针对 :class:`riskguard.brokers.paper.PaperBroker` 的单元测试。

覆盖内置纸面模拟盘的核心撮合语义:

* 市价单成交价 = 标记价 × (1 ± 滑点),买加卖减;
* ``commission_per_share`` 与 ``commission_bps`` 两种佣金从现金扣除;
* 加权成本价:加仓取加权平均、减仓保持均价、反手取成交价、清仓归零;
* 现金记账:买入减现金、卖出加现金,佣金始终为支出;
* ``get_account`` 权益 = 现金 + Σ(标记价计的市值),标记价缺失回退持仓均价;
* ``get_portfolio`` 带上全部已知标记价(含尚无持仓的标的);
* ``submit_order`` 在无标记价且无有效限价时抛 :class:`BrokerError`;
* ``cancel_order`` 撤未知单号抛 :class:`BrokerError`,撤已成交单为幂等空操作。

全部纯本地、无网络、确定性。尽量从公开 API ``riskguard`` 导入。文件产物用
``tmp_path``,但纸面盘为纯内存,无需落盘。
"""

from __future__ import annotations

import math

import pytest

from riskguard import (
    Account,
    BrokerError,
    BrokerOrder,
    Order,
    OrderType,
    PaperBroker,
    Portfolio,
    Position,
    RiskConfig,
    RiskEngine,
    Side,
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _order(symbol: str = "AAPL", side: Side = Side.BUY, quantity: float = 100.0, **kw) -> Order:
    return Order(symbol=symbol, side=side, quantity=quantity, **kw)


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


def _permissive_config() -> RiskConfig:
    """尽量放行的配置:单笔上限拉满、组合敞口上限拉高,避免规则缩量/拒单。"""
    return RiskConfig(max_position_pct=1.0, max_gross_exposure_pct=100.0)


# ===========================================================================
# 构造与基本约束
# ===========================================================================
class TestConstruction:
    def test_negative_cash_rejected(self):
        with pytest.raises(ValueError):
            PaperBroker(cash=-1.0)

    def test_zero_cash_allowed(self):
        b = PaperBroker(cash=0.0)
        assert b.get_account().cash == 0.0

    def test_name_is_paper(self):
        assert PaperBroker(cash=1000.0).name == "paper"

    def test_initial_marks_copied_not_aliased(self):
        # 外部持有的 marks dict 被偷改不应影响券商内部状态
        external = {"AAPL": 200.0}
        b = PaperBroker(cash=1000.0, marks=external)
        external["AAPL"] = 999.0
        assert b.get_marks(["AAPL"]) == {"AAPL": 200.0}

    def test_no_positions_initially(self):
        b = PaperBroker(cash=1000.0, marks={"AAPL": 200.0})
        assert b.get_positions() == {}

    def test_no_open_orders_initially(self):
        b = PaperBroker(cash=1000.0)
        assert list(b.get_open_orders()) == []


# ===========================================================================
# 成交价:标记价 ± 滑点
# ===========================================================================
class TestFillPrice:
    def test_buy_fills_at_mark_when_no_slippage(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(side=Side.BUY, quantity=10.0))
        assert _approx(rcpt.filled_avg_price, 200.0)

    def test_sell_fills_at_mark_when_no_slippage(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(side=Side.SELL, quantity=10.0))
        assert _approx(rcpt.filled_avg_price, 200.0)

    def test_buy_pays_up_by_slippage(self):
        # 100 bps = 1% 滑点
        b = PaperBroker(cash=1_000_000.0, slippage_bps=100.0, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(side=Side.BUY, quantity=10.0))
        assert _approx(rcpt.filled_avg_price, 200.0 * 1.01)

    def test_sell_gets_hit_down_by_slippage(self):
        b = PaperBroker(cash=1_000_000.0, slippage_bps=100.0, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(side=Side.SELL, quantity=10.0))
        assert _approx(rcpt.filled_avg_price, 200.0 * 0.99)

    def test_slippage_is_symmetric_around_mark(self):
        b = PaperBroker(cash=1_000_000.0, slippage_bps=50.0, marks={"AAPL": 100.0})
        buy = b.submit_order(_order(side=Side.BUY, quantity=1.0))
        sell = b.submit_order(_order(side=Side.SELL, quantity=1.0))
        # 买价与卖价关于标记价对称
        assert _approx((buy.filled_avg_price + sell.filled_avg_price) / 2.0, 100.0)

    def test_limit_price_used_when_no_mark(self):
        # 无标记价时回退到限价撮合
        b = PaperBroker(cash=1_000_000.0)
        order = _order(order_type=OrderType.LIMIT, limit_price=150.0, quantity=10.0)
        rcpt = b.submit_order(order)
        assert _approx(rcpt.filled_avg_price, 150.0)

    def test_mark_preferred_over_limit_price(self):
        # 有标记价时限价被忽略,以标记价撮合
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        order = _order(order_type=OrderType.LIMIT, limit_price=150.0, quantity=10.0)
        rcpt = b.submit_order(order)
        assert _approx(rcpt.filled_avg_price, 200.0)


# ===========================================================================
# 回执内容
# ===========================================================================
class TestReceipt:
    def test_receipt_status_filled(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(quantity=10.0))
        assert rcpt.status == "filled"
        assert rcpt.is_filled is True
        assert rcpt.is_terminal is True

    def test_receipt_filled_quantity_is_absolute(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(side=Side.SELL, quantity=7.0))
        assert rcpt.filled_quantity == 7.0

    def test_receipt_order_is_echoed(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        order = _order(quantity=10.0)
        rcpt = b.submit_order(order)
        assert rcpt.order is order

    def test_receipt_has_submitted_at(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(quantity=10.0))
        assert rcpt.submitted_at is not None

    def test_broker_order_ids_are_unique_and_sequential(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        r1 = b.submit_order(_order(quantity=1.0))
        r2 = b.submit_order(_order(quantity=1.0))
        assert r1.broker_order_id != r2.broker_order_id
        assert r1.broker_order_id == "paper-1"
        assert r2.broker_order_id == "paper-2"

    def test_raw_carries_commission_and_mark(self):
        b = PaperBroker(cash=1_000_000.0, commission_per_share=0.01, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(quantity=10.0))
        assert isinstance(rcpt.raw, dict)
        assert _approx(rcpt.raw["mark"], 200.0)
        assert _approx(rcpt.raw["commission"], 10.0 * 0.01)


# ===========================================================================
# 佣金:每股 + 按成交额
# ===========================================================================
class TestCommission:
    def test_no_commission_by_default(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=10.0))
        # 现金只减去 10 * 200 = 2000
        assert _approx(b.get_account().cash, 100_000.0 - 2000.0)

    def test_commission_per_share_deducted_on_buy(self):
        b = PaperBroker(cash=100_000.0, commission_per_share=0.005, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        commission = 100.0 * 0.005
        assert _approx(b.get_account().cash, 100_000.0 - 100.0 * 200.0 - commission)

    def test_commission_bps_deducted_on_buy(self):
        # 10 bps = 0.1% 按成交额
        b = PaperBroker(cash=100_000.0, commission_bps=10.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        notional = 100.0 * 200.0
        commission = notional * 0.001
        assert _approx(b.get_account().cash, 100_000.0 - notional - commission)

    def test_both_commissions_stack(self):
        b = PaperBroker(
            cash=100_000.0,
            commission_per_share=0.01,
            commission_bps=5.0,
            marks={"AAPL": 200.0},
        )
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        notional = 100.0 * 200.0
        commission = 100.0 * 0.01 + notional * 0.0005
        assert _approx(b.get_account().cash, 100_000.0 - notional - commission)

    def test_commission_bps_uses_fill_price_not_mark(self):
        # 佣金按含滑点的成交额计,而非标记价
        b = PaperBroker(
            cash=1_000_000.0, slippage_bps=100.0, commission_bps=10.0, marks={"AAPL": 200.0}
        )
        rcpt = b.submit_order(_order(side=Side.BUY, quantity=100.0))
        fill_price = 200.0 * 1.01
        expected_commission = abs(100.0 * fill_price) * 0.001
        assert _approx(rcpt.raw["commission"], expected_commission)

    def test_commission_is_a_cost_on_sell_too(self):
        # 卖出得到现金,但佣金仍是支出(净得少于成交额)
        b = PaperBroker(cash=100_000.0, commission_per_share=0.02, marks={"AAPL": 200.0})
        # 先建多头 100 股
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        cash_after_buy = b.get_account().cash
        b.submit_order(_order(side=Side.SELL, quantity=50.0))
        proceeds = 50.0 * 200.0
        commission = 50.0 * 0.02
        assert _approx(b.get_account().cash, cash_after_buy + proceeds - commission)


# ===========================================================================
# 现金记账:买减卖加
# ===========================================================================
class TestCashAccounting:
    def test_buy_decreases_cash(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        assert b.get_account().cash < 100_000.0
        assert _approx(b.get_account().cash, 100_000.0 - 20_000.0)

    def test_sell_increases_cash(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        # 直接卖出(建空头),现金增加
        b.submit_order(_order(side=Side.SELL, quantity=100.0))
        assert _approx(b.get_account().cash, 100_000.0 + 20_000.0)

    def test_round_trip_flat_loses_only_slippage_and_commission(self):
        # 无滑点无佣金时,买了再全额卖回,现金复原
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        b.submit_order(_order(side=Side.SELL, quantity=100.0))
        assert _approx(b.get_account().cash, 100_000.0)
        assert b.get_positions() == {}

    def test_round_trip_costs_slippage(self):
        # 有滑点时来回一趟净亏 = 2 × 滑点 × 成交额
        b = PaperBroker(cash=100_000.0, slippage_bps=50.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        b.submit_order(_order(side=Side.SELL, quantity=100.0))
        buy_cost = 100.0 * 200.0 * 1.005
        sell_proceeds = 100.0 * 200.0 * 0.995
        assert _approx(b.get_account().cash, 100_000.0 - buy_cost + sell_proceeds)

    def test_cash_can_go_negative_on_leverage(self):
        # 纸面盘不硬性拦截超买;买入额超过现金 → 现金为负
        b = PaperBroker(cash=1_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))  # 需 20_000
        assert b.get_account().cash < 0.0
        # 买力被钳制在非负
        assert b.get_account().buying_power == 0.0


# ===========================================================================
# 加权成本价:加仓 / 减仓 / 反手 / 清仓
# ===========================================================================
class TestAveragePrice:
    def test_open_long_sets_avg_to_fill(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        pos = b.get_positions()["AAPL"]
        assert pos.quantity == 100.0
        assert _approx(pos.avg_price, 200.0)

    def test_open_short_sets_avg_to_fill(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.SELL, quantity=100.0))
        pos = b.get_positions()["AAPL"]
        assert pos.quantity == -100.0
        assert _approx(pos.avg_price, 200.0)

    def test_add_to_long_uses_weighted_average(self):
        b = PaperBroker(cash=10_000_000.0)
        b.set_mark("AAPL", 100.0)
        b.submit_order(_order(side=Side.BUY, quantity=100.0))  # 100 @ 100
        b.set_mark("AAPL", 200.0)
        b.submit_order(_order(side=Side.BUY, quantity=100.0))  # +100 @ 200
        pos = b.get_positions()["AAPL"]
        assert pos.quantity == 200.0
        # (100*100 + 100*200) / 200 = 150
        assert _approx(pos.avg_price, 150.0)

    def test_add_to_short_uses_weighted_average(self):
        b = PaperBroker(cash=10_000_000.0)
        b.set_mark("AAPL", 100.0)
        b.submit_order(_order(side=Side.SELL, quantity=100.0))  # -100 @ 100
        b.set_mark("AAPL", 200.0)
        b.submit_order(_order(side=Side.SELL, quantity=100.0))  # -100 @ 200
        pos = b.get_positions()["AAPL"]
        assert pos.quantity == -200.0
        assert _approx(pos.avg_price, 150.0)

    def test_reduce_long_keeps_average(self):
        b = PaperBroker(cash=10_000_000.0)
        b.set_mark("AAPL", 100.0)
        b.submit_order(_order(side=Side.BUY, quantity=100.0))  # 100 @ 100
        b.set_mark("AAPL", 500.0)  # 价格大涨
        b.submit_order(_order(side=Side.SELL, quantity=40.0))  # 减仓 40
        pos = b.get_positions()["AAPL"]
        assert pos.quantity == 60.0
        # 减仓不改成本价
        assert _approx(pos.avg_price, 100.0)

    def test_reduce_short_keeps_average(self):
        b = PaperBroker(cash=10_000_000.0)
        b.set_mark("AAPL", 100.0)
        b.submit_order(_order(side=Side.SELL, quantity=100.0))  # -100 @ 100
        b.set_mark("AAPL", 50.0)
        b.submit_order(_order(side=Side.BUY, quantity=40.0))  # 回补 40
        pos = b.get_positions()["AAPL"]
        assert pos.quantity == -60.0
        assert _approx(pos.avg_price, 100.0)

    def test_flip_long_to_short_sets_avg_to_fill(self):
        b = PaperBroker(cash=10_000_000.0)
        b.set_mark("AAPL", 100.0)
        b.submit_order(_order(side=Side.BUY, quantity=100.0))  # +100 @ 100
        b.set_mark("AAPL", 300.0)
        b.submit_order(_order(side=Side.SELL, quantity=150.0))  # 卖 150 → 反手成 -50
        pos = b.get_positions()["AAPL"]
        assert pos.quantity == -50.0
        # 反手后新方向成本 = 成交价,而非旧均价
        assert _approx(pos.avg_price, 300.0)

    def test_flip_short_to_long_sets_avg_to_fill(self):
        b = PaperBroker(cash=10_000_000.0)
        b.set_mark("AAPL", 100.0)
        b.submit_order(_order(side=Side.SELL, quantity=100.0))  # -100 @ 100
        b.set_mark("AAPL", 40.0)
        b.submit_order(_order(side=Side.BUY, quantity=150.0))  # 买 150 → 反手成 +50
        pos = b.get_positions()["AAPL"]
        assert pos.quantity == 50.0
        assert _approx(pos.avg_price, 40.0)

    def test_close_out_removes_position(self):
        b = PaperBroker(cash=10_000_000.0, marks={"AAPL": 100.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        b.submit_order(_order(side=Side.SELL, quantity=100.0))
        # 平仓后 get_positions 过滤掉 flat 仓位
        assert "AAPL" not in b.get_positions()

    def test_avg_price_unaffected_by_slippage_on_reduce(self):
        # 减仓时即便成交价含滑点,成本价仍保持原值
        b = PaperBroker(cash=10_000_000.0, slippage_bps=100.0, marks={"AAPL": 100.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        pos_after_open = b.get_positions()["AAPL"]
        opened_avg = pos_after_open.avg_price
        b.submit_order(_order(side=Side.SELL, quantity=30.0))
        assert _approx(b.get_positions()["AAPL"].avg_price, opened_avg)


# ===========================================================================
# get_account:权益 = 现金 + Σ 市值
# ===========================================================================
class TestGetAccount:
    def test_equity_equals_cash_when_flat(self):
        b = PaperBroker(cash=50_000.0, marks={"AAPL": 200.0})
        acct = b.get_account()
        assert _approx(acct.equity, 50_000.0)
        assert _approx(acct.cash, 50_000.0)

    def test_equity_includes_long_market_value(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))  # 花 20_000,持 100 股
        b.set_mark("AAPL", 250.0)  # 价格涨到 250
        acct = b.get_account()
        # cash = 80_000,市值 = 100 * 250 = 25_000,权益 = 105_000
        assert _approx(acct.cash, 80_000.0)
        assert _approx(acct.equity, 105_000.0)

    def test_equity_includes_short_market_value(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.SELL, quantity=100.0))  # 收 20_000,持 -100 股
        b.set_mark("AAPL", 150.0)  # 价格跌到 150,空头浮盈
        acct = b.get_account()
        # cash = 120_000,空头市值 = -100 * 150 = -15_000,权益 = 105_000
        assert _approx(acct.cash, 120_000.0)
        assert _approx(acct.equity, 105_000.0)

    def test_equity_falls_back_to_avg_price_without_mark(self):
        # 建仓后抹掉标记价 → 市值按持仓均价计,等价于账面持平
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        b._marks.pop("AAPL")  # 直接抹掉行情,触发回退分支
        acct = b.get_account()
        # 均价 200,市值 = 100*200 = 20_000,cash = 80_000,权益 = 100_000
        assert _approx(acct.equity, 100_000.0)

    def test_equity_sums_multiple_positions(self):
        b = PaperBroker(cash=200_000.0, marks={"AAPL": 100.0, "MSFT": 400.0})
        b.submit_order(_order(symbol="AAPL", side=Side.BUY, quantity=100.0))  # -10_000
        b.submit_order(_order(symbol="MSFT", side=Side.BUY, quantity=50.0))  # -20_000
        acct = b.get_account()
        # cash = 170_000,市值 = 100*100 + 50*400 = 30_000,权益 = 200_000
        assert _approx(acct.cash, 170_000.0)
        assert _approx(acct.equity, 200_000.0)

    def test_buying_power_never_negative(self):
        b = PaperBroker(cash=1_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))  # 现金变负
        assert b.get_account().buying_power == 0.0


# ===========================================================================
# get_portfolio / get_positions / get_marks
# ===========================================================================
class TestGetPortfolio:
    def test_portfolio_includes_marks_for_symbols_without_position(self):
        # 契约要点:即便某标的尚无持仓,其已知标记价也应出现在组合里
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0, "TSLA": 700.0})
        b.submit_order(_order(symbol="AAPL", side=Side.BUY, quantity=10.0))
        pf = b.get_portfolio()
        assert "AAPL" in pf.marks
        assert "TSLA" in pf.marks  # 无持仓但有标记价
        assert "TSLA" not in pf.positions
        assert _approx(pf.marks["TSLA"], 700.0)

    def test_portfolio_positions_exclude_flat(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        b.submit_order(_order(side=Side.SELL, quantity=100.0))  # 平掉
        pf = b.get_portfolio()
        assert "AAPL" not in pf.positions

    def test_portfolio_extra_marks_override(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        pf = b.get_portfolio(marks={"AAPL": 250.0, "NVDA": 900.0})
        assert _approx(pf.marks["AAPL"], 250.0)  # 传入的覆盖内置的
        assert _approx(pf.marks["NVDA"], 900.0)  # 新增的也带上

    def test_portfolio_account_matches_get_account(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        pf = b.get_portfolio()
        assert _approx(pf.account.equity, b.get_account().equity)

    def test_portfolio_is_frozen_snapshot(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(side=Side.BUY, quantity=100.0))
        pf = b.get_portfolio()
        # Portfolio 内部映射为只读,后续券商状态变化不回写到旧快照
        prev_qty = pf.positions["AAPL"].quantity
        b.submit_order(_order(side=Side.BUY, quantity=50.0))
        assert pf.positions["AAPL"].quantity == prev_qty

    def test_get_marks_only_returns_known_symbols(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        got = b.get_marks(["AAPL", "UNKNOWN"])
        assert got == {"AAPL": 200.0}

    def test_set_marks_bulk_update(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        b.set_marks({"AAPL": 210.0, "MSFT": 400.0})
        assert b.get_marks(["AAPL", "MSFT"]) == {"AAPL": 210.0, "MSFT": 400.0}


# ===========================================================================
# 错误路径:无价、撤单
# ===========================================================================
class TestErrorPaths:
    def test_submit_without_mark_raises(self):
        b = PaperBroker(cash=100_000.0)  # 没有任何标记价
        with pytest.raises(BrokerError):
            b.submit_order(_order(quantity=10.0))

    def test_submit_market_order_for_unknown_symbol_raises(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        with pytest.raises(BrokerError):
            b.submit_order(_order(symbol="TSLA", quantity=10.0))

    def test_submit_with_nonpositive_effective_price_raises(self):
        # 标记价为 0 时无法撮合(mark <= 0 分支)
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 0.0})
        with pytest.raises(BrokerError):
            b.submit_order(_order(symbol="AAPL", quantity=10.0))

    def test_cancel_unknown_id_raises(self):
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        with pytest.raises(BrokerError):
            b.cancel_order("does-not-exist")

    def test_cancel_filled_order_is_noop(self):
        # 市价单即时成交,撤已终态单不报错、也不改状态
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        rcpt = b.submit_order(_order(quantity=10.0))
        b.cancel_order(rcpt.broker_order_id)  # 不应抛
        # 状态仍为 filled,且不出现在未成交列表里
        assert list(b.get_open_orders()) == []

    def test_no_mark_error_message_mentions_symbol(self):
        b = PaperBroker(cash=100_000.0)
        with pytest.raises(BrokerError) as ei:
            b.submit_order(_order(symbol="ZZZZ", quantity=1.0))
        assert "ZZZZ" in str(ei.value)


# ===========================================================================
# 与 RiskEngine 集成:确定性时钟注入
# ===========================================================================
class TestEngineIntegration:
    def test_engine_submit_routes_to_paper_broker(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        # 用极宽松配置,确保订单被放行而非缩量
        engine = RiskEngine(_permissive_config(), broker=b)
        pf = b.get_portfolio()
        rcpt = engine.submit(_order(symbol="AAPL", side=Side.BUY, quantity=10.0), pf)
        assert rcpt is not None
        assert isinstance(rcpt, BrokerOrder)
        assert rcpt.is_filled
        # 券商侧确实建了仓
        assert b.get_positions()["AAPL"].quantity == 10.0

    def test_engine_with_injected_list_clock_is_deterministic(self):
        # 用可变列表时钟注入,断言裁决时间戳取自注入时钟
        from datetime import datetime, timezone

        ticks = [datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)]

        def clock():
            return ticks[0]

        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        engine = RiskEngine(_permissive_config(), broker=b, clock=clock)
        decision = engine.check(_order(symbol="AAPL", quantity=1.0), b.get_portfolio())
        assert decision.timestamp == ticks[0]
        # 推进时钟,下一次裁决取到新值
        ticks[0] = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
        decision2 = engine.check(_order(symbol="AAPL", quantity=1.0), b.get_portfolio())
        assert decision2.timestamp == ticks[0]

    def test_engine_rejected_order_leaves_broker_untouched(self):
        # 用极严配置迫使拒单(越界直接拒而非缩量),券商侧不应产生任何持仓/订单
        b = PaperBroker(cash=100_000.0, marks={"AAPL": 200.0})
        engine = RiskEngine(
            RiskConfig(max_position_pct=0.01, on_position_breach="reject"), broker=b
        )
        pf = b.get_portfolio()
        # 想买 1000 股 = 20 万,占 100_000 权益的 200%,远超 1% 上限 → 拒
        rcpt = engine.submit(_order(symbol="AAPL", side=Side.BUY, quantity=1000.0), pf)
        # 拒单返回 None(默认不抛),券商无成交
        assert rcpt is None
        assert b.get_positions() == {}
        assert list(b.get_open_orders()) == []


# ===========================================================================
# 类型/契约:PaperBroker 是 Broker、返回类型正确
# ===========================================================================
class TestContract:
    def test_paper_broker_is_a_broker(self):
        from riskguard import Broker

        assert isinstance(PaperBroker(cash=1.0), Broker)

    def test_get_account_returns_account(self):
        b = PaperBroker(cash=1_000.0)
        assert isinstance(b.get_account(), Account)

    def test_get_positions_returns_dict_of_positions(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        b.submit_order(_order(quantity=1.0))
        positions = b.get_positions()
        assert isinstance(positions, dict)
        assert all(isinstance(p, Position) for p in positions.values())

    def test_submit_returns_broker_order(self):
        b = PaperBroker(cash=1_000_000.0, marks={"AAPL": 200.0})
        assert isinstance(b.submit_order(_order(quantity=1.0)), BrokerOrder)

    def test_get_portfolio_returns_portfolio(self):
        b = PaperBroker(cash=1_000.0, marks={"AAPL": 200.0})
        assert isinstance(b.get_portfolio(), Portfolio)
