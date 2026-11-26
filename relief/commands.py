"""命令（办理意图）。

解析层输出“命令草稿”，服务层补齐编号后交给状态机执行。
命令带幂等键：同一条原文重复办理不会产生重复事件。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Command:
    command_id: str
    at: int | None = None
    sources: tuple[str, ...] = ()


@dataclass
class IngestRecord(Command):
    record_id: str = ""
    kind: str = ""
    text: str = ""


@dataclass
class OpenClarification(Command):
    clarification_id: str = ""
    site: str | None = None
    question: str = ""
    about_source: str = ""


@dataclass
class AnswerClarification(Command):
    clarification_id: str = ""
    answer_source: str = ""
    answer_text: str = ""


@dataclass
class QuantifyFallback(Command):
    """澄清答复补全某条件行的后备物资数量，不另立独立需求。"""

    site: str = ""
    primary_material: str = ""
    fallback_material: str = ""
    fallback_qty: int = 0
    fallback_unit: str = ""
    answer_source: str = ""


@dataclass
class RegisterRequest(Command):
    line_id: str = ""
    site: str = ""
    material: str = ""
    qty: int = 0
    unit: str = ""
    mode: str = "unknown"          # add | total | unknown
    proxy: bool = False
    deadline: int | None = None
    fallback_material: str | None = None
    fallback_qty: int | None = None
    fallback_unit: str | None = None
    condition_note: str = ""
    answer_to: str | None = None
    supersedes: str | None = None
    notes: str = ""


@dataclass
class CancelRequest(Command):
    """撤销只关闭当时谈论的物资行，不撤掉整个地点的需求。"""

    site: str = ""
    material: str | None = None
    qty: int | None = None
    ref_source: str | None = None   # “刚才说的那批”所指的上下文来源
    note: str = ""


@dataclass
class RegisterCount(Command):
    count_id: str = ""
    site: str = ""
    material: str = ""
    qty: int = 0
    unit: str = ""
    counted_at: int | None = None
    proxy: bool = False
    consumption_per_day: float | None = None
    note: str = ""


@dataclass
class VerifyCount(Command):
    """核验员确认盘点及其有效时段。"""

    count_id: str = ""
    valid_from: int | None = None
    valid_until: int | None = None
    verifier: str = ""


@dataclass
class CreateReservation(Command):
    order_id: str = ""
    site: str = ""
    # (物资, 数量, 单位, 对应缺口行)
    lines: list[tuple[str, int, str, str | None]] = field(default_factory=list)


@dataclass
class ConfirmLoad(Command):
    order_id: str = ""
    vehicle: str = ""
    site: str | None = None
    lines: list[tuple[str, int, str, str | None]] = field(default_factory=list)
    reservation_id: str | None = None


@dataclass
class PlanReroute(Command):
    # 解析层只掌握业务键，order_id/line_id 由引擎按车辆+地点+物资解析
    vehicle: str = ""
    from_site: str = ""
    to_site: str = ""
    material: str = ""
    qty: int = 0
    order_id: str = ""
    line_id: str = ""
    note: str = ""


@dataclass
class RecordHandover(Command):
    delivery_id: str = ""
    # 业务键定位装车行；改送时 rerouted_from 记原定地点
    vehicle: str = ""
    site: str = ""
    material: str = ""
    requested_qty: int = 0
    delivered_qty: int = 0
    order_id: str = ""
    line_id: str = ""
    rerouted_from: str | None = None


@dataclass
class DecideConflict(Command):
    conflict_id: str = ""
    decision: str = ""             # keep | trim | return_stock
    officer: str = ""
    note: str = ""


@dataclass
class ReturnStock(Command):
    order_id: str = ""
    line_id: str = ""
    material: str = ""
    qty: int = 0
    reason: str = ""
    officer: str = ""


@dataclass
class AdvanceClock(Command):
    pass
