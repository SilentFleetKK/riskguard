"""Alpaca 券商适配器测试。

本模块要在**装了 / 没装** ``alpaca-py`` 两种环境下都能通过,因此分成三层:

1. 依赖无关的**导入契约**:``AlpacaBroker`` 要么为 ``None``(适配器缺失时优雅
   降级),要么就是一个真正的 :class:`~riskguard.brokers.base.Broker` 子类。
   无论哪种,``import riskguard`` 都不该因为缺 ``alpaca`` 而炸。
2. 依赖无关的**构造契约**:当 ``alpaca-py`` 未安装时,构造 ``AlpacaBroker(...)``
   必须抛 :class:`~riskguard.exceptions.BrokerError`(带可执行的安装提示),
   而不是裸 ``ImportError``。SDK 的异常类型不许泄漏到上层。
3. 依赖无关的**纯函数/静态方法**:状态映射表 ``_STATUS_MAP`` 与静态方法
   ``_map_status`` 不碰网络、不需要 ``alpaca``,可直接单测其全部分支。

只有真正需要 SDK 的**请求构造 / 回执映射**用例才用
``pytest.importorskip("alpaca")`` 守卫——这样文件在有无依赖时都能跑,缺依赖时
这些用例被 skip 而非 fail。

约束:纯 pytest、零网络、确定性(涉及时间的走可变列表时钟注入引擎)。所有断言
都直接对照公开 API 的契约,不臆测 SDK 内部实现。
"""

from __future__ import annotations

import importlib

import pytest

import riskguard
import riskguard.brokers as brokers_pkg
import riskguard.brokers.alpaca as alpaca_mod
from riskguard import BrokerError
from riskguard.brokers.alpaca import AlpacaBroker as ModuleAlpacaBroker
from riskguard.brokers.base import Broker


# --------------------------------------------------------------------------- #
# 是否装了 alpaca-py —— 决定构造契约走哪条分支
# --------------------------------------------------------------------------- #
def _alpaca_installed() -> bool:
    """探测可选依赖 ``alpaca-py`` 是否可导入(导入名为 ``alpaca``)。"""
    try:
        importlib.import_module("alpaca")
        return True
    except Exception:
        return False


ALPACA_INSTALLED = _alpaca_installed()


# --------------------------------------------------------------------------- #
# 1. 导入契约(依赖无关)
# --------------------------------------------------------------------------- #
class TestImportContract:
    """无论装没装 alpaca-py,导入层都必须稳定、不炸。"""

    def test_import_riskguard_does_not_require_alpaca(self):
        """核心库零第三方依赖:import riskguard 绝不因缺 alpaca 而失败。"""
        # 能走到这里说明模块级 import 已成功;再显式确认关键符号存在。
        assert hasattr(riskguard, "AlpacaBroker")
        assert hasattr(riskguard, "BrokerError")

    def test_alpaca_module_imports_cleanly(self):
        """alpaca 适配器**模块**本身必须可导入(顶层不许 import alpaca)。"""
        assert alpaca_mod is not None
        # 缺依赖也应能拿到状态映射表这类纯数据。
        assert isinstance(alpaca_mod._STATUS_MAP, dict)

    def test_alpaca_broker_exported_from_top_level(self):
        """三个入口(顶层包 / brokers 子包 / 模块)导出的是同一对象。"""
        assert riskguard.AlpacaBroker is brokers_pkg.AlpacaBroker

    def test_alpaca_broker_in_dunder_all(self):
        """公开 API 契约:AlpacaBroker 与 BrokerError 必须在 __all__ 里。"""
        assert "AlpacaBroker" in riskguard.__all__
        assert "BrokerError" in riskguard.__all__

    def test_alpaca_broker_is_none_or_broker_subclass(self):
        """核心断言:AlpacaBroker 要么为 None(降级),要么是 Broker 子类。"""
        ab = brokers_pkg.AlpacaBroker
        assert ab is None or (isinstance(ab, type) and issubclass(ab, Broker))

    def test_module_class_is_always_a_broker_subclass(self):
        """从模块直接导入的类(绕过降级)恒为 Broker 子类,便于内省测试。"""
        assert issubclass(ModuleAlpacaBroker, Broker)

    def test_broker_error_is_riskguard_error(self):
        """BrokerError 归属于 RiskGuard 异常层次,可被统一兜底。"""
        assert issubclass(BrokerError, riskguard.RiskGuardError)


# --------------------------------------------------------------------------- #
# 2. 构造契约(依赖无关)—— 缺依赖时必须抛 BrokerError
# --------------------------------------------------------------------------- #
class TestConstructionContract:
    """构造 AlpacaBroker 的行为随依赖存在与否而变,两条分支都要覆盖。"""

    def test_none_or_construct_raises_broker_error_without_dep(self):
        """任务核心:AlpacaBroker 为 None,或(缺依赖时)构造抛 BrokerError。

        为了在有无依赖时都稳定:
        - 若适配器降级为 None,直接通过;
        - 若为真实类且**未装** alpaca-py,构造必须抛 BrokerError;
        - 若**装了** alpaca-py,构造不该因缺依赖报错(可能因鉴权/网络另抛,
          但那不属于本用例断言范围,故跳过)。
        """
        ab = brokers_pkg.AlpacaBroker
        if ab is None:
            return  # 降级分支:契约已满足
        if ALPACA_INSTALLED:
            pytest.skip("alpaca-py installed; missing-dep branch not exercisable")
        with pytest.raises(BrokerError):
            ab(api_key="k", secret_key="s")

    def test_construct_module_class_raises_broker_error_without_dep(self):
        """直接对模块类构造:缺依赖 -> BrokerError(而非裸 ImportError)。"""
        if ALPACA_INSTALLED:
            pytest.skip("alpaca-py installed; missing-dep branch not exercisable")
        with pytest.raises(BrokerError):
            ModuleAlpacaBroker("key", "secret")

    def test_missing_dep_error_is_not_bare_import_error(self):
        """契约:SDK 的 ImportError 必须被包成 BrokerError,不许泄漏。"""
        if ALPACA_INSTALLED:
            pytest.skip("alpaca-py installed; missing-dep branch not exercisable")
        with pytest.raises(BrokerError):
            ModuleAlpacaBroker("k", "s")
        # ImportError 不是 RiskGuardError 的子类;若泄漏,上面的 raises 会失败。
        assert not issubclass(BrokerError, ImportError)

    def test_missing_dep_error_message_mentions_install_hint(self):
        """缺依赖时的报错信息要给出可执行的安装提示,便于用户自救。"""
        if ALPACA_INSTALLED:
            pytest.skip("alpaca-py installed; missing-dep branch not exercisable")
        with pytest.raises(BrokerError) as excinfo:
            ModuleAlpacaBroker("k", "s")
        msg = str(excinfo.value).lower()
        assert "alpaca" in msg
        # 提示里应含安装指引关键词(pip / install 之一即可)。
        assert "pip" in msg or "install" in msg

    def test_missing_dep_uses_keyword_paper_argument(self):
        """paper 是 keyword-only 参数;缺依赖时仍应在导入阶段就抛 BrokerError。"""
        if ALPACA_INSTALLED:
            pytest.skip("alpaca-py installed; missing-dep branch not exercisable")
        with pytest.raises(BrokerError):
            ModuleAlpacaBroker("k", "s", paper=False)

    def test_missing_dep_broker_error_catchable_as_riskguard_error(self):
        """调用方用一个 except RiskGuardError 就能兜住构造失败。"""
        if ALPACA_INSTALLED:
            pytest.skip("alpaca-py installed; missing-dep branch not exercisable")
        with pytest.raises(riskguard.RiskGuardError):
            ModuleAlpacaBroker("k", "s")


# --------------------------------------------------------------------------- #
# 3. 状态映射(依赖无关的纯数据 / 静态方法)
# --------------------------------------------------------------------------- #
class TestStatusMap:
    """_STATUS_MAP 与静态 _map_status 都不需要 alpaca,分支可全量单测。"""

    def test_status_map_values_are_canonical(self):
        """归一化后的状态只能落在 BrokerOrder 约定的这几个取值里。"""
        allowed = {"accepted", "filled", "partially_filled", "canceled", "rejected"}
        assert set(alpaca_mod._STATUS_MAP.values()) <= allowed

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("new", "accepted"),
            ("accepted", "accepted"),
            ("pending_new", "accepted"),
            ("accepted_for_bidding", "accepted"),
            ("held", "accepted"),
            ("filled", "filled"),
            ("partially_filled", "partially_filled"),
            ("canceled", "canceled"),
            ("cancelled", "canceled"),  # 英式拼写也要归到同一态
            ("expired", "canceled"),
            ("done_for_day", "canceled"),
            ("replaced", "canceled"),
            ("rejected", "rejected"),
            ("suspended", "rejected"),
            ("stopped", "rejected"),
        ],
    )
    def test_map_status_known_strings(self, raw, expected):
        """每个已知 Alpaca 状态字符串都映射到正确的归一化态。"""
        assert ModuleAlpacaBroker._map_status(raw) == expected

    def test_map_status_unknown_falls_back_to_accepted(self):
        """未知状态一律当作在途 accepted,绝不误判成终态。"""
        assert ModuleAlpacaBroker._map_status("some_new_status") == "accepted"

    def test_map_status_none_falls_back_to_accepted(self):
        """回执缺 status 字段(None)时也退化为 accepted。"""
        assert ModuleAlpacaBroker._map_status(None) == "accepted"

    def test_map_status_is_case_insensitive(self):
        """Alpaca 有时回大写枚举名,映射必须大小写不敏感。"""
        assert ModuleAlpacaBroker._map_status("FILLED") == "filled"
        assert ModuleAlpacaBroker._map_status("Canceled") == "canceled"

    def test_map_status_reads_enum_value_attribute(self):
        """传入带 .value 的枚举样对象时,应读 .value 再映射。"""

        class _FakeEnum:
            value = "partially_filled"

        assert ModuleAlpacaBroker._map_status(_FakeEnum()) == "partially_filled"

    def test_map_status_enum_value_case_insensitive(self):
        """枚举 .value 为大写时同样要归一化。"""

        class _FakeEnum:
            value = "REJECTED"

        assert ModuleAlpacaBroker._map_status(_FakeEnum()) == "rejected"

    def test_map_status_unknown_enum_value_falls_back(self):
        """枚举 .value 命不中映射表时退化为 accepted。"""

        class _FakeEnum:
            value = "brand_new_state"

        assert ModuleAlpacaBroker._map_status(_FakeEnum()) == "accepted"


# --------------------------------------------------------------------------- #
# 4. 元数据契约(依赖无关)
# --------------------------------------------------------------------------- #
class TestBrokerMetadata:
    """适配器的类级元数据,不实例化即可断言。"""

    def test_broker_name_is_alpaca(self):
        """name 用于日志/错误信息里标识后端,必须是 'alpaca'。"""
        assert ModuleAlpacaBroker.name == "alpaca"

    def test_implements_required_broker_methods(self):
        """必须实现 Broker ABC 的四个抽象方法,否则无法实例化。"""
        for method in ("submit_order", "cancel_order", "get_account", "get_positions"):
            assert callable(getattr(ModuleAlpacaBroker, method))

    def test_overrides_optional_broker_hooks(self):
        """覆盖了可选钩子 get_open_orders / get_marks,而非沿用基类默认。"""
        assert ModuleAlpacaBroker.get_open_orders is not Broker.get_open_orders
        assert ModuleAlpacaBroker.get_marks is not Broker.get_marks

    def test_abstract_methods_are_concrete(self):
        """所有抽象方法都已落地,abstractmethods 集合应为空。"""
        assert getattr(ModuleAlpacaBroker, "__abstractmethods__", frozenset()) == frozenset()


# --------------------------------------------------------------------------- #
# 5. 可选:请求构造 / 回执映射(需要真正的 alpaca-py)
# --------------------------------------------------------------------------- #
class TestRequestMappingWithDep:
    """这些用例触碰 SDK 的请求/枚举类型,故整体用 importorskip 守卫。

    仍然零网络:只构造一个 AlpacaBroker 实例并调用其内部翻译方法,不发任何请求
    (提交订单 / 拉账户才会走网络,这里一概不碰)。构造若因鉴权失败,同样跳过。
    """

    @pytest.fixture()
    def broker(self):
        """构造一个不联网的 AlpacaBroker(仅用于内部翻译方法测试)。"""
        pytest.importorskip("alpaca")
        try:
            return ModuleAlpacaBroker("test-key", "test-secret", paper=True)
        except BrokerError as exc:  # pragma: no cover - 装了依赖但构造仍失败
            pytest.skip(f"AlpacaBroker construction failed offline: {exc}")

    def test_build_market_request(self, broker):
        """市价买单应翻译成 MarketOrderRequest,方向/数量/标的正确回填。"""
        from riskguard import Order, OrderType, Side

        order = Order(symbol="AAPL", side=Side.BUY, quantity=10, order_type=OrderType.MARKET)
        req = broker._build_request(order)
        assert getattr(req, "symbol", None) == "AAPL"
        assert float(getattr(req, "qty")) == 10

    def test_build_limit_request_carries_limit_price(self, broker):
        """限价卖单应翻译成 LimitOrderRequest,并带上限价。"""
        from riskguard import Order, OrderType, Side

        order = Order(
            symbol="MSFT",
            side=Side.SELL,
            quantity=5,
            order_type=OrderType.LIMIT,
            limit_price=300.0,
        )
        req = broker._build_request(order)
        assert float(getattr(req, "limit_price")) == 300.0
        assert getattr(req, "symbol", None) == "MSFT"

    def test_build_request_passes_client_order_id(self, broker):
        """带 client_order_id 的订单应把该 id 透传给 SDK 请求。"""
        from riskguard import Order, Side

        order = Order(symbol="TSLA", side=Side.BUY, quantity=1, client_order_id="coid-123")
        req = broker._build_request(order)
        assert getattr(req, "client_order_id", None) == "coid-123"

    def test_to_broker_order_normalizes_fields(self, broker):
        """SDK 原始回执 -> 归一化 BrokerOrder:状态映射 + 成交量/价转 float。"""
        from riskguard import Order, Side

        class _RawOrder:
            id = "abc-1"
            status = "filled"
            filled_qty = "3"
            filled_avg_price = "101.5"
            submitted_at = None

        order = Order(symbol="AAPL", side=Side.BUY, quantity=3)
        bo = broker._to_broker_order(_RawOrder(), order)
        assert bo.broker_order_id == "abc-1"
        assert bo.status == "filled"
        assert bo.is_filled
        assert bo.filled_quantity == 3.0
        assert bo.filled_avg_price == 101.5
        assert bo.order is order

    def test_to_broker_order_handles_missing_fill_fields(self, broker):
        """未成交回执:filled_qty 缺省算 0,filled_avg_price 缺省为 None。"""
        from riskguard import Order, Side

        class _RawOrder:
            id = "abc-2"
            status = "new"  # -> accepted
            filled_qty = None
            filled_avg_price = None

        order = Order(symbol="AAPL", side=Side.BUY, quantity=1)
        bo = broker._to_broker_order(_RawOrder(), order)
        assert bo.status == "accepted"
        assert bo.filled_quantity == 0.0
        assert bo.filled_avg_price is None
        assert not bo.is_terminal

    def test_reconstruct_order_from_raw_limit(self, broker):
        """从 open-order 原始对象反推 RiskGuard Order:方向/类型/限价还原。"""
        from riskguard import OrderType, Side

        class _RawOrder:
            symbol = "NVDA"
            side = "sell"
            order_type = "limit"
            qty = "4"
            limit_price = "500"
            client_order_id = "c-9"

        order = broker._reconstruct_order(_RawOrder())
        assert order.symbol == "NVDA"
        assert order.side is Side.SELL
        assert order.order_type is OrderType.LIMIT
        assert order.limit_price == 500.0
        assert order.quantity == 4.0

    def test_reconstruct_order_defaults_to_market_buy(self, broker):
        """反推时拿不到有效字段:方向默认 BUY、类型默认 MARKET、数量兜底为正。"""
        from riskguard import OrderType, Side

        class _RawOrder:
            symbol = "SPY"
            side = None
            order_type = None
            qty = None
            limit_price = None
            client_order_id = None

        order = broker._reconstruct_order(_RawOrder())
        assert order.side is Side.BUY
        assert order.order_type is OrderType.MARKET
        assert order.limit_price is None
        assert order.quantity > 0  # Order 契约要求 quantity > 0

    def test_get_marks_empty_symbols_returns_empty(self, broker):
        """空标的列表时,get_marks 短路返回空映射,不触网。"""
        assert broker.get_marks([]) == {}
