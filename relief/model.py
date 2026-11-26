"""领域模型：值对象、枚举与不可变结构。

口径（见 docs/materials.md）：
- 瓶装水每箱 24 瓶，每箱占 2 体积格；
- 滤芯每箱 6 个，整箱占 1 格（整箱密封包装），散件按 1 个 0.25 格登记；
- 泵滤套装含泵 1 台、滤芯 2 个，占 4 格。
数量以原子单位的整数表达（水以“箱”为原子单位，滤芯以“个”为原子单位）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


# ---------------------------------------------------------------- 枚举

class LineMode(str, Enum):
    """需求数量的口径。"""

    ADD = "add"          # “再要/再送”：在现有安排之外追加
    TOTAL = "total"      # “总共要”：以本次数量为总量口径
    UNKNOWN = "unknown"  # 口径不明，需要澄清


class RequestStatus(str, Enum):
    DRAFT = "draft"                      # 代报，等待现场确认
    ACTIVE = "active"                    # 有效缺项
    SUPERSEDED = "superseded"            # 被同口径后续请求取代
    CANCELLED = "cancelled"              # 被对应撤销关闭
    FALLBACK_OPEN = "fallback_open"      # 条件替代未到触发时刻，条件悬而未决
    FALLBACK_TRIGGERED = "fallback_triggered"  # 条件未满足，转为后备需求
    FALLBACK_WAIVED = "fallback_waived"        # 条件满足，后备需求放弃


class SourceKind(str, Enum):
    REQUEST = "request"
    RELAY = "relay"                  # 转述，不构成独立观察
    CLARIFICATION = "clarification"
    COUNT = "count"                  # 盘点
    RESERVATION = "reservation"
    LOADING = "loading"
    REROUTE = "reroute"
    DELIVERY = "delivery"
    CANCELLATION = "cancellation"


class OrderState(str, Enum):
    RESERVED = "reserved"      # 备货：库存被占用，未装车
    LOADED = "loaded"          # 已装车：车位被占用
    PARTIAL = "partial"        # 实际短装后交接
    DELIVERED = "delivered"    # 已交接
    CANCELLED = "cancelled"    # 未装车部分被撤销
    FROZEN = "frozen"          # 新盘点影响该单，等待值班员裁决


class LineState(str, Enum):
    OPEN = "open"
    RESERVED = "reserved"
    LOADED = "loaded"
    DELIVERED = "delivered"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FROZEN = "frozen"


class ClarificationState(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"


# ---------------------------------------------------------------- 值对象

@dataclass(frozen=True)
class Quantity:
    """带单位的数量。原子单位见模块文档。"""

    qty: int
    unit: str

    def scaled(self, factor: float) -> "Quantity":
        return Quantity(round(self.qty * factor), self.unit)


@dataclass(frozen=True)
class Material:
    code: str
    name_zh: str
    name_en: str
    unit: str                       # 原子单位（箱/个/台/套）
    aliases: tuple[str, ...] = ()
    # 整包换算：{包装单位: (原子单位数量, 占格)}
    packages: tuple[tuple[str, int, float], ...] = ()
    # 散件占格（每个原子单位）
    loose_slot: float = 0.0
    compatible_models: tuple[str, ...] = ()   # 可替代的滤芯型号
    is_set: bool = False
    set_components: tuple[tuple[str, int], ...] = ()  # (物资编码, 数量)

    def package_slots(self, qty: int, unit: str) -> float:
        """按登记的包装口径计算体积格。未知包装按散件计。"""
        if unit == self.unit:
            return round(qty * self.loose_slot, 4)
        for pkg_unit, pack_size, pack_slots in self.packages:
            if unit == pkg_unit:
                whole, remainder = divmod(qty, pack_size)
                return round(
                    whole * pack_slots + remainder * self.loose_slot, 4
                )
        return round(qty * self.loose_slot, 4)


@dataclass(frozen=True)
class Site:
    code: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Vehicle:
    code: str
    capacity: float
    window_start: int | None       # 距当日 00:00 的分钟数
    window_end: int | None
    reachable: tuple[str, ...]


# ---------------------------------------------------------------- 请求与盘点

@dataclass
class RequestLine:
    """一个地点一条物资的独立缺项。一个请求可拆成多条。"""

    id: str
    site: str
    material: str
    qty: int
    unit: str
    mode: LineMode
    source: str
    sources: list[str] = field(default_factory=list)
    status: RequestStatus = RequestStatus.ACTIVE
    proxy: bool = False                       # 代报，需现场确认
    deadline: int | None = None               # 消耗/到达期限（分钟）
    # 条件替代：material 是首选；条件不满足时产生后备物资需求
    fallback_material: str | None = None
    fallback_qty: int | None = None
    fallback_unit: str | None = None
    condition_note: str = ""
    supersedes: str | None = None             # 取代的前序行
    answer_to: str | None = None              # 回答的澄清编号
    notes: str = ""
    # 办理量（按原子单位）
    reserved_qty: int = 0
    loaded_qty: int = 0
    delivered_qty: int = 0
    cancelled_qty: int = 0

    @property
    def shortfall(self) -> int:
        """尚未通过任何办理覆盖的数量。"""
        covered = self.delivered_qty + self.loaded_qty + self.reserved_qty
        return max(0, self.qty - covered - self.cancelled_qty)

    @property
    def unmet(self) -> int:
        """已确认未满足（不含在途与备货）。"""
        return max(0, self.qty - self.delivered_qty - self.cancelled_qty)


@dataclass
class InventoryCount:
    """一次现场盘点（时刻存量，不是新增订货）。"""

    id: str
    site: str
    material: str
    qty: int
    unit: str
    at: int | None
    source: str
    verified: bool = False
    valid_from: int | None = None
    valid_until: int | None = None
    consumption_per_day: float | None = None   # 每人每日口径推算的日耗
    note: str = ""


@dataclass
class Clarification:
    id: str
    source: str
    site: str | None
    question: str
    state: ClarificationState = ClarificationState.OPEN
    answer_source: str | None = None
    answer_text: str = ""
    answer_to: str | None = None


# ---------------------------------------------------------------- 调拨链路

@dataclass
class OrderLine:
    id: str
    order_id: str
    site: str
    material: str
    unit: str
    qty: int                       # 承诺数量（原子单位）
    request_line: str | None       # 对应的缺口行；库存/改送产生的行可为空
    state: LineState = LineState.OPEN
    reserved_qty: int = 0
    loaded_qty: int = 0
    delivered_qty: int = 0
    short_shipped: int = 0
    sources: list[str] = field(default_factory=list)
    note: str = ""
    rerouted_from: str | None = None   # 改送情形下的原定地点


@dataclass
class Order:
    """一张有效单：备货单或装车单。同一库存/车位不能被两张有效单占用。"""

    id: str
    kind: str                      # reservation | loading
    state: OrderState
    vehicle: str | None
    site: str | None
    created_at: int | None
    sources: list[str] = field(default_factory=list)
    lines: list[OrderLine] = field(default_factory=list)

    def line(self, line_id: str) -> OrderLine:
        for line in self.lines:
            if line.id == line_id:
                return line
        raise KeyError(line_id)

    def find_line(self, site: str, material: str) -> OrderLine | None:
        for line in self.lines:
            if line.site == site and line.material == material:
                return line
        return None


@dataclass
class DeliveryRecord:
    id: str
    order_id: str
    line_id: str
    vehicle: str | None
    site: str
    material: str
    unit: str
    requested_qty: int
    delivered_qty: int
    at: int | None
    source: str
    rerouted_from: str | None = None   # 改送情形下的原定地点


@dataclass
class Conflict:
    """新盘点撞上备货/装车单时登记的冲突，等待值班员确认。"""

    id: str
    at: int | None
    count_id: str
    order_id: str
    site: str
    material: str
    over_qty: int                  # 按新盘点可能超送的数量
    decision: str | None = None    # keep | trim | 其他值班员决定
    decided_by: str = ""
    note: str = ""


@dataclass
class IngestedRecord:
    id: str
    kind: str
    text: str
    at: int | None


def to_dict(obj: Any) -> Any:
    """把领域对象转成可读 JSON 结构（枚举取值，dataclass 递归）。"""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {
            k: to_dict(getattr(obj, k))
            for k in obj.__dataclass_fields__
        }
    return obj
