"""事件定义。

事件只追加、不改写；状态由事件回放得到（见 state.py）。
每个事件都带来源编号，保证解释结论能对应具体原句版本。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    event_id: str
    command_id: str
    at: int | None
    sources: tuple[str, ...]


@dataclass(frozen=True)
class RecordIngested(Event):
    record_id: str
    kind: str
    text: str


@dataclass(frozen=True)
class NoteRecorded(Event):
    """转述等不构成独立观察的资料，只留痕，不产生需求或盘点。"""

    record_id: str
    reason: str


@dataclass(frozen=True)
class ClarificationOpened(Event):
    clarification_id: str
    site: str | None
    question: str
    about_source: str


@dataclass(frozen=True)
class ClarificationAnswered(Event):
    clarification_id: str
    answer_source: str
    answer_text: str


@dataclass(frozen=True)
class RequestRegistered(Event):
    line_id: str
    site: str
    material: str
    qty: int
    unit: str
    mode: str                 # add | total | unknown
    proxy: bool
    deadline: int | None
    fallback_material: str | None
    fallback_qty: int | None
    fallback_unit: str | None
    condition_note: str
    answer_to: str | None
    supersedes: str | None
    notes: str


@dataclass(frozen=True)
class RequestSuperseded(Event):
    line_id: str
    superseded_by: str


@dataclass(frozen=True)
class RequestCancelled(Event):
    line_id: str
    site: str
    material: str
    qty: int
    note: str


@dataclass(frozen=True)
class FallbackTriggered(Event):
    line_id: str
    fallback_line_id: str
    fallback_material: str
    fallback_qty: int
    fallback_unit: str


@dataclass(frozen=True)
class FallbackWaived(Event):
    line_id: str
    reason: str


@dataclass(frozen=True)
class FallbackQuantified(Event):
    """澄清答复补全后备物资数量（答复只解决对应问题）。"""

    line_id: str
    fallback_material: str
    fallback_qty: int
    fallback_unit: str
    answer_source: str


@dataclass(frozen=True)
class CountRegistered(Event):
    count_id: str
    site: str
    material: str
    qty: int
    unit: str
    counted_at: int | None
    proxy: bool
    consumption_per_day: float | None
    note: str


@dataclass(frozen=True)
class CountVerified(Event):
    count_id: str
    valid_from: int | None
    valid_until: int | None
    verifier: str


@dataclass(frozen=True)
class ReservationCreated(Event):
    order_id: str
    site: str
    lines: tuple[tuple[str, str, int, str, str | None], ...]
    # 每行：(行号, 物资, 数量, 单位, 对应缺口行)


@dataclass(frozen=True)
class LoadConfirmed(Event):
    order_id: str
    vehicle: str
    site: str | None
    # 每行：(行号, 地点, 物资, 数量, 单位, 对应缺口行)
    lines: tuple[tuple[str, str, str, int, str, str | None], ...]
    reservation_id: str | None


@dataclass(frozen=True)
class ReroutePlanned(Event):
    order_id: str
    line_id: str
    from_site: str
    to_site: str
    material: str
    qty: int
    note: str


@dataclass(frozen=True)
class HandoverRecorded(Event):
    delivery_id: str
    order_id: str
    line_id: str
    site: str
    material: str
    unit: str
    requested_qty: int
    delivered_qty: int
    rerouted_from: str | None


@dataclass(frozen=True)
class ConflictRaised(Event):
    conflict_id: str
    count_id: str | None
    order_id: str
    line_id: str | None
    site: str
    material: str
    over_qty: int
    reason: str


@dataclass(frozen=True)
class ConflictDecided(Event):
    conflict_id: str
    decision: str           # keep | trim | return_stock
    officer: str
    note: str


@dataclass(frozen=True)
class OrderLineFrozen(Event):
    order_id: str
    line_id: str
    conflict_id: str


@dataclass(frozen=True)
class StockReturned(Event):
    """仓管显式回库；已装车货物不会因请求改口自动回库。"""

    order_id: str
    line_id: str
    material: str
    qty: int
    reason: str
    officer: str


@dataclass(frozen=True)
class OrderLineCancelled(Event):
    order_id: str
    line_id: str
    qty: int
    note: str


@dataclass(frozen=True)
class Tick(Event):
    """登记当前办理时刻，用于驱动条件期限判定。"""
