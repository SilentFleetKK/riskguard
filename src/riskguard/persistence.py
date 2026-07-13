"""风控状态持久化:堵住"重启即绕过熔断"的后门。

``RiskState`` 默认纯内存——进程一重启,权益高点和熔断标记全部归零,等于给亏红眼
的操作者留了一条"重启一下继续交易"的隐藏后路。这与"纪律不能靠意志力,必须让系统
强制执行"的立库初衷直接矛盾。``StateStore`` 把状态写透到磁盘,引擎启动时自动恢复,
让熔断真正扛得住重启。

设计取舍(与 :mod:`riskguard.audit` 一致,但方向相反,理由见下):

* **加载失败在构造期硬失败**::meth:`StateStore.load` 抛出的异常**不会**被引擎吞掉。
  宁可拒绝启动,也不能在存档读不出来时悄悄当成"一切正常"从零开始——那正是要堵的
  后门本身。这与审计模块"写失败不中断裁决"的哲学相反,因为这里保护的是**风控状态
  本身的真实性**,而不是历史记录的完整性。存档里的 NaN/±inf 权益值同样视为损坏,
  因为它们会让 :attr:`~riskguard.state.RiskState.drawdown` 恒算成非有限值、
  回撤熔断从此永久失效——比读不到存档更隐蔽、更危险。
* **写入失败在运行期不阻断风控裁决**:磁盘抖动不该变成新的拒绝服务面,因此
  :meth:`RiskEngine` 捕获 :meth:`StateStore.save` 的异常并转交可选的
  ``on_persist_error`` 回调(镜像 ``on_audit_error`` 的既有模式)。但这意味着写失败期间
  重启保护暂时失效——生产环境应始终设置 ``on_persist_error`` 并对它告警,不要静默忽略。
  该回调在引擎的锁内同步执行,必须快、不能阻塞、更不能派生新线程回调本引擎(会被同一把
  锁堵住,直到外层调用返回)。
* **并发写入检测(乐观锁)**::class:`SqliteStateStore` 按 ``version`` 字段做
  compare-and-swap:每次写入都必须匹配"上次读到的版本",不匹配就拒绝覆盖并抛出
  :class:`~riskguard.exceptions.PersistenceError`。这是刻意的设计——**一个
  ``(path, key)`` 只能有一个活跃写者**;两个引擎共享同一份存档若不做这层检测,
  后写入的旧快照会静默覆盖前一个引擎已经落盘的熔断状态,让"重启绕过熔断"以
  "另一个并发写者"的形式借尸还魂。CAS 失败时**响亮报错**而不是静默覆盖,调用方
  能立刻发现"这个 key 被两处同时写了"这个配置错误。
* **`reset_breaker()` 是先落盘、后切换内存状态**(其余状态变更都是先切内存、
  再尽力落盘):人工复盘后的重置,是操作者据以决定"可以恢复交易"的判断依据。
  如果落盘失败却假装重置成功,操作者会带着虚假的安全感继续交易,重启后熔断状态
  却又原样复活——这比"重置失败、留在熔断态"危险得多。因此仅这一个操作,持久化
  失败会直接向调用方抛出,不吞进 ``on_persist_error``。
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from .exceptions import PersistenceError
from .state import RiskState


class StateStore(ABC):
    """风控状态持久化后端接口。实现只需 :meth:`load` / :meth:`save` 两个方法。

    ⚠️ **一个 ``(path, key)`` 只能有一个活跃写者**。多个引擎实例共享同一份存档、
    同一个 key,会互相覆盖对方的高水位/熔断状态。多策略/多账户请用不同的 key
    隔离(见 :class:`SqliteStateStore` 的 ``key`` 参数),不要共用默认值。
    """

    @abstractmethod
    def load(self) -> RiskState | None:
        """读取上次持久化的状态;从未写过则返回 ``None``。

        存档存在但已损坏(无法解析、或权益字段非有限数)时**必须抛异常**,绝不能
        悄悄返回 ``None``——那会让一次读档失败被误当成"首次启动",熔断状态就此丢失。
        """

    @abstractmethod
    def save(self, state: RiskState) -> None:
        """持久化一份状态快照(覆盖式存储当前值,不是追加日志)。

        实现应当检测"自己上次读到的版本"与"当前存档版本"是否一致(乐观锁),
        不一致时抛出 :class:`~riskguard.exceptions.PersistenceError`,而不是
        静默覆盖另一个写者刚落盘的状态。
        """

    def close(self) -> None:
        """释放资源(文件句柄、数据库连接)。默认无操作。"""

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SqliteStateStore(StateStore):
    """基于 SQLite 的状态持久化,单行覆盖式存储 + 乐观锁,纯标准库 ``sqlite3``。

    参数
    ----
    path:
        SQLite 数据库文件路径(``":memory:"`` 无意义——那等于没持久化,请用真实文件)。
    key:
        存储键,默认 ``"default"``。同一文件可用不同 key 隔离多个引擎的状态
        (例如多策略、多账户分别持久化)。**两个引擎绝不能共用同一个 key**——本类
        会用版本号检测这种冲突并报错,但报错也意味着其中一个引擎的状态从此写不进去,
        请从配置上就避免共用。
    """

    def __init__(self, path: str, *, key: str = "default") -> None:
        self.path = path
        self.key = key
        self._lock = threading.RLock()
        self._version: int | None = None  # None = 尚未读取过,首次 save() 前会先探测
        try:
            # check_same_thread=False:允许跨线程调用;写操作自身用 RLock 串行化。
            self._conn = sqlite3.connect(path, check_same_thread=False)
            with self._lock:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS risk_state ("
                    "key TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1)"
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            # sqlite3.Error 不是 OSError/ValueError 的子类,不包一层会从 CLI 的
            # 异常兜底里漏出去变成裸 traceback(例如连到一个根本不是 SQLite 库的文件)。
            raise PersistenceError(f"cannot open state store at {path!r}: {exc!r}") from exc

    def load(self) -> RiskState | None:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT payload, version FROM risk_state WHERE key = ?", (self.key,)
                )
                row = cur.fetchone()
        except sqlite3.Error as exc:
            raise PersistenceError(f"failed to read state store: {exc!r}") from exc
        if row is None:
            self._version = 0
            return None
        payload, version = row
        self._version = version
        return _deserialize(payload)  # 解析失败会自然抛出 PersistenceError,不在此吞掉

    def save(self, state: RiskState) -> None:
        payload = _serialize(state)  # 内部会拒绝非有限权益,绝不写出坏值
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._lock:
                if self._version is None:
                    # 从未调用过 load():当作"我以为这是第一次写"处理。如果存档里
                    # 其实已经有别的写者留下的记录,下面的 INSERT 会因主键冲突而
                    # 失败,转成清晰的 PersistenceError——绝不能现查一次"当前最新
                    # 版本"再拿它当基线,那样"冲突检测"永远会在第一次写时失效
                    # (查到的版本必然和查到的版本"一致",等于没检测)。
                    self._version = 0

                if self._version == 0:
                    # 尚无记录 -> 尝试插入首行(version=1)。
                    try:
                        self._conn.execute(
                            "INSERT INTO risk_state (key, payload, updated_at, version) "
                            "VALUES (?, ?, ?, 1)",
                            (self.key, payload, now),
                        )
                        self._conn.commit()
                        self._version = 1
                        return
                    except sqlite3.IntegrityError as exc:
                        # 另一个写者抢先插入了同一 key —— 两个引擎共用了同一个 key。
                        raise PersistenceError(
                            f"concurrent writer already created key {self.key!r}; "
                            "two engines/processes appear to share the same "
                            "(path, key) — each must own an exclusive key"
                        ) from exc

                new_version = self._version + 1
                cur = self._conn.execute(
                    "UPDATE risk_state SET payload = ?, updated_at = ?, version = ? "
                    "WHERE key = ? AND version = ?",
                    (payload, now, new_version, self.key, self._version),
                )
                self._conn.commit()
                if cur.rowcount == 0:
                    # 乐观锁冲突:存档版本已不是我们上次读到的那个,说明有另一个写者
                    # 在此期间写过——绝不覆盖它,响亮报错而不是静默吃掉它的状态。
                    raise PersistenceError(
                        f"concurrent writer updated key {self.key!r} since last read "
                        f"(expected version {self._version}); refusing to overwrite — "
                        "are two engines/processes sharing this store/key?"
                    )
                self._version = new_version
        except sqlite3.Error as exc:
            raise PersistenceError(f"failed to write state store: {exc!r}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()


#: 当前存档格式版本。v1(无 ``schema`` 键)= 1.4 及更早;v2 增加 AI 代理闸门字段
#: (会话锚定/日内熔断/最近订单)。读取端对**更高**的版本 fail-closed 拒读。
#: 已知限制(无廉价解法):旧版本代码读 v2 存档会成功(多余键被忽略),但随后
#: 保存时会静默丢弃新字段——降级部署前请自行备份存档。
_SCHEMA_VERSION = 2


def _serialize(state: RiskState) -> str:
    if not math.isfinite(state.high_water_mark) or not math.isfinite(state.last_equity):
        # 绝不把 NaN/±inf 写出去:json.dumps 会吐出非标准的 NaN/Infinity 字面量,
        # 其它工具(jq、JS JSON.parse)读不了,且一旦被读回来会让 drawdown 恒为
        # 非有限值、回撤熔断永久失效——必须在写入这一刻就拒绝,而不是指望读取时防御。
        raise PersistenceError(
            f"refusing to persist non-finite equity "
            f"(high_water_mark={state.high_water_mark!r}, last_equity={state.last_equity!r})"
        )
    if not math.isfinite(state.session_anchor_equity):
        # 同理:非有限的锚定值会让 daily_loss 的判断永久失真。
        raise PersistenceError(
            "refusing to persist non-finite session anchor "
            f"(session_anchor_equity={state.session_anchor_equity!r})"
        )
    return json.dumps(
        {
            "schema": _SCHEMA_VERSION,
            "high_water_mark": state.high_water_mark,
            "last_equity": state.last_equity,
            "breaker_tripped": state.breaker_tripped,
            "tripped_at": state.tripped_at.isoformat() if state.tripped_at else None,
            "trip_reason": state.trip_reason,
            "strategy_inception": {
                k: v.isoformat() for k, v in state.strategy_inception.items()
            },
            "session_date": state.session_date,
            "session_anchor_equity": state.session_anchor_equity,
            "daily_tripped": state.daily_tripped,
            "daily_tripped_at": (
                state.daily_tripped_at.isoformat() if state.daily_tripped_at else None
            ),
            "daily_trip_reason": state.daily_trip_reason,
            "recent_orders": [
                [ts.isoformat(), reduce_only] for ts, reduce_only in state.recent_orders
            ],
        }
    )


def _deserialize(payload: str) -> RiskState:
    """把存档 JSON 还原成 :class:`RiskState`。任何解析问题(损坏的 JSON、缺字段、
    字段类型不对、权益字段非有限数)都转成 :class:`PersistenceError` 抛出——绝不
    吞掉后静默返回一个"看起来正常"的默认状态,那会让存档损坏和"从未持久化过"
    无法区分。"""
    try:
        data = json.loads(payload)
        schema = int(data.get("schema", 1))
        if schema > _SCHEMA_VERSION:
            # 未来版本的存档:半懂不懂地读进来比读不到更危险(可能丢掉新版本
            # 才有的熔断语义)。fail-closed,让操作者用对应版本的代码来读。
            raise ValueError(
                f"state archive written by a newer riskguard (schema {schema} > "
                f"{_SCHEMA_VERSION}); refusing to half-read it"
            )
        hwm = float(data["high_water_mark"])
        last_equity = float(data["last_equity"])
        if not math.isfinite(hwm) or not math.isfinite(last_equity):
            raise ValueError(
                f"non-finite equity in persisted state (hwm={hwm!r}, "
                f"last_equity={last_equity!r}) — drawdown would never trip again"
            )
        # v2 新字段一律 .get() 带默认值:v1 存档(schema 键都没有)直接可读,
        # 这就是全部的迁移逻辑。
        anchor = float(data.get("session_anchor_equity", 0.0))
        if not math.isfinite(anchor):
            raise ValueError(
                f"non-finite session anchor in persisted state ({anchor!r}) — "
                "daily loss line would be permanently broken"
            )
        daily_tripped_at = data.get("daily_tripped_at")
        return RiskState(
            high_water_mark=hwm,
            last_equity=last_equity,
            breaker_tripped=data["breaker_tripped"],
            tripped_at=(
                datetime.fromisoformat(data["tripped_at"])
                if data["tripped_at"]
                else None
            ),
            trip_reason=data["trip_reason"],
            strategy_inception={
                k: datetime.fromisoformat(v)
                for k, v in data["strategy_inception"].items()
            },
            session_date=data.get("session_date"),
            session_anchor_equity=anchor,
            daily_tripped=bool(data.get("daily_tripped", False)),
            daily_tripped_at=(
                datetime.fromisoformat(daily_tripped_at) if daily_tripped_at else None
            ),
            daily_trip_reason=data.get("daily_trip_reason", ""),
            recent_orders=tuple(
                (datetime.fromisoformat(ts), bool(reduce_only))
                for ts, reduce_only in data.get("recent_orders", [])
            ),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(
            f"state store payload is corrupted or unreadable: {exc!r}"
        ) from exc
