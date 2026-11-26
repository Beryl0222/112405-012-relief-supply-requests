"""读模型：把事件溯源状态投影成协调员与请求方要看的结论。

- 缺口看板：已确认缺口、在途承诺、备货占用、未解决部分、消耗期限；
- 单位/包装/替代条件/相互引用全部保留原文口径；
- 分配解释：为什么可行、哪些澄清仍会改变安排。
读模型不产生新业务事实，只做投影与计算。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .catalog import format_clock
from .model import (
    ClarificationState,
    LineState,
    OrderState,
    RequestStatus,
    to_dict,
)
from .state import State


@dataclass
class GapRow:
    line_id: str
    site: str
    material: str
    material_name: str
    requested_qty: int
    unit: str
    mode: str
    status: str
    delivered_qty: int
    loaded_qty: int          # 在途
    reserved_qty: int        # 备货未装车
    cancelled_qty: int
    shortfall: int           # 仍未被任何办理覆盖
    run_out_at: str | None
    deadline: str | None
    fallback: dict | None
    sources: list[str]
    notes: str
    proxy: bool


@dataclass
class StockRow:
    material: str
    material_name: str
    initial: int
    reserved: int
    loaded: int
    delivered: int
    available: int
    unit: str


@dataclass
class CommitmentRow:
    order_id: str
    kind: str
    state: str
    vehicle: str | None
    site: str
    material: str
    qty: int
    unit: str
    delivered_qty: int
    short_shipped: int
    line_state: str
    sources: list[str]


@dataclass
class ClarificationRow:
    id: str
    source: str
    site: str | None
    question: str
    state: str
    answer_source: str | None
    answer_text: str


@dataclass
class GapBoard:
    as_of: str | None
    gaps: list[GapRow] = field(default_factory=list)
    stock: list[StockRow] = field(default_factory=list)
    commitments: list[CommitmentRow] = field(default_factory=list)
    clarifications: list[ClarificationRow] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    site_stock: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return to_dict(self)


class ReadModel:
    def __init__(self, state: State):
        self.state = state

    # ---------------------------------------------------------- 缺口

    def gap_board(self) -> GapBoard:
        state = self.state
        gaps: list[GapRow] = []
        for line in state.request_lines.values():
            if line.status in (
                RequestStatus.SUPERSEDED,
                RequestStatus.CANCELLED,
                RequestStatus.FALLBACK_WAIVED,
                RequestStatus.DRAFT,
            ):
                continue
            material = state.registry.require_material(line.material)
            fallback = None
            if line.fallback_material:
                fallback = {
                    "material": line.fallback_material,
                    "qty": line.fallback_qty,
                    "unit": line.fallback_unit,
                    "deadline": format_clock(line.deadline),
                    "condition": line.condition_note,
                    "pending_qty_clarification": line.fallback_qty is None,
                }
            gaps.append(
                GapRow(
                    line_id=line.id,
                    site=line.site,
                    material=line.material,
                    material_name=material.name_zh,
                    requested_qty=line.qty,
                    unit=line.unit,
                    mode=line.mode.value,
                    status=line.status.value,
                    delivered_qty=line.delivered_qty,
                    loaded_qty=line.loaded_qty,
                    reserved_qty=line.reserved_qty,
                    cancelled_qty=line.cancelled_qty,
                    shortfall=line.shortfall,
                    run_out_at=self._run_out(line),
                    deadline=format_clock(line.deadline)
                    if line.deadline is not None
                    else None,
                    fallback=fallback,
                    sources=list(line.sources),
                    notes=line.notes,
                    proxy=line.proxy,
                )
            )
        return GapBoard(
            as_of=format_clock(state.now) if state.now is not None else None,
            gaps=gaps,
            stock=self._stock_rows(),
            commitments=self._commitment_rows(),
            clarifications=self._clarification_rows(),
            conflicts=[asdict(c) for c in state.conflicts.values()],
            site_stock=self._site_stock_rows(),
        )

    def _run_out(self, line) -> str | None:
        """以最新核验盘点与日耗推算耗尽时刻；无核验或无日耗则不给结论。"""
        state = self.state
        count = state.latest_verified_count(line.site, line.material)
        if count is None:
            return None
        rate = count.consumption_per_day
        if not rate or rate <= 0:
            return None
        base = count.at if count.at is not None else (state.now or 0)
        minutes_left = count.qty / rate * 24 * 60
        return format_clock(int(base + minutes_left))

    def _stock_rows(self) -> list[StockRow]:
        state = self.state
        rows = []
        materials = {m.code: m for m in state.registry.materials}
        for code, initial in sorted(state.initial_stock.items()):
            reserved = loaded = delivered = 0
            for order in state.orders.values():
                for line in order.lines:
                    if line.material != code:
                        continue
                    if line.state == LineState.RESERVED:
                        reserved += line.qty
                    elif line.state in (LineState.LOADED, LineState.PARTIAL):
                        loaded += line.qty - line.delivered_qty
            for (site, material), qty in state.material_delivered.items():
                if material == code:
                    delivered += qty
            rows.append(
                StockRow(
                    material=code,
                    material_name=materials[code].name_zh if code in materials else code,
                    initial=initial,
                    reserved=reserved,
                    loaded=loaded,
                    delivered=delivered,
                    available=state.available_stock(code),
                    unit=materials[code].unit if code in materials else "",
                )
            )
        return rows

    def _commitment_rows(self) -> list[CommitmentRow]:
        state = self.state
        rows = []
        for order in state.orders.values():
            if order.state == OrderState.CANCELLED and not order.lines:
                continue
            for line in order.lines:
                rows.append(
                    CommitmentRow(
                        order_id=order.id,
                        kind=order.kind,
                        state=order.state.value,
                        vehicle=order.vehicle,
                        site=line.site,
                        material=line.material,
                        qty=line.qty,
                        unit=line.unit,
                        delivered_qty=line.delivered_qty,
                        short_shipped=line.short_shipped,
                        line_state=line.state.value,
                        sources=list(line.sources),
                    )
                )
        return rows

    def _clarification_rows(self) -> list[ClarificationRow]:
        return [
            ClarificationRow(
                id=clar.id,
                source=clar.source,
                site=clar.site,
                question=clar.question,
                state=clar.state.value,
                answer_source=clar.answer_source,
                answer_text=clar.answer_text,
            )
            for clar in self.state.clarifications.values()
        ]

    def _site_stock_rows(self) -> list[dict]:
        state = self.state
        rows = []
        for count in state.counts.values():
            if not count.verified:
                continue
            rows.append(
                {
                    "count_id": count.id,
                    "site": count.site,
                    "material": count.material,
                    "qty": count.qty,
                    "unit": count.unit,
                    "counted_at": format_clock(count.at)
                    if count.at is not None
                    else None,
                    "valid_from": format_clock(count.valid_from)
                    if count.valid_from is not None
                    else None,
                    "valid_until": format_clock(count.valid_until)
                    if count.valid_until is not None
                    else None,
                    "consumption_per_day": count.consumption_per_day,
                    "source": count.source,
                    "note": count.note,
                }
            )
        rows.sort(key=lambda row: (row["site"], row["material"], row["counted_at"]))
        return rows

    # ---------------------------------------------------------- 查询

    def query_site(self, site: str) -> dict:
        """请求方查询：已确认缺口、在途承诺、未解决部分。"""
        board = self.gap_board()
        return {
            "site": site,
            "confirmed_gaps": [
                asdict(row) for row in board.gaps if row.site == site
            ],
            "in_transit": [
                asdict(row)
                for row in board.commitments
                if row.site == site and row.line_state in ("loaded", "partial")
            ],
            "unresolved_clarifications": [
                asdict(row)
                for row in board.clarifications
                if row.site == site and row.state == ClarificationState.OPEN.value
            ],
            "verified_stock": [
                row for row in board.site_stock if row["site"] == site
            ],
        }

    def pending_clarifications(self) -> list[ClarificationRow]:
        return [
            row for row in self._clarification_rows()
            if row.state == ClarificationState.OPEN.value
        ]
