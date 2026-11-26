"""事件溯源状态机。

状态只由事件回放得到；命令处理函数先校验业务不变量，再追加事件。
关键不变量：
- 可用库存 = 初始库存 - 有效备货/装车占用，任何时刻不为负；
- 同库存、同车位不能被两张有效单占用；
- 滤芯型号不兼容不能按通用数量补足；
- 套装部件不齐不整套承诺；
- 已装车货物只能凭显式回库事件回库，改口不自动回库；
- 改送后原点按未满足重判，新点按实际卸货计交接。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .catalog import Registry
from .commands import (
    AdvanceClock,
    AnswerClarification,
    CancelRequest,
    Command,
    ConfirmLoad,
    CreateReservation,
    DecideConflict,
    IngestRecord,
    OpenClarification,
    PlanReroute,
    RecordHandover,
    RegisterCount,
    RegisterRequest,
    ReturnStock,
    VerifyCount,
)
from .errors import BusinessRule, DuplicateCommand
from .events import (
    ClarificationAnswered,
    ClarificationOpened,
    ConflictDecided,
    ConflictRaised,
    CountRegistered,
    CountVerified,
    Event,
    FallbackQuantified,
    FallbackTriggered,
    FallbackWaived,
    HandoverRecorded,
    LoadConfirmed,
    NoteRecorded,
    OrderLineCancelled,
    OrderLineFrozen,
    RecordIngested,
    ReroutePlanned,
    RequestCancelled,
    RequestRegistered,
    RequestSuperseded,
    ReservationCreated,
    StockReturned,
    Tick,
)
from .model import (
    Clarification,
    ClarificationState,
    Conflict,
    DeliveryRecord,
    IngestedRecord,
    InventoryCount,
    LineMode,
    LineState,
    Order,
    OrderLine,
    OrderState,
    RequestLine,
    RequestStatus,
)


class State:
    def __init__(self, registry: Registry, initial_stock: dict[str, int]):
        self.registry = registry
        self.initial_stock = dict(initial_stock)
        self.events: list[Event] = []
        self.command_index: dict[str, list[str]] = {}
        self.records: dict[str, IngestedRecord] = {}
        self.request_lines: dict[str, RequestLine] = {}
        self.counts: dict[str, InventoryCount] = {}
        self.clarifications: dict[str, Clarification] = {}
        self.orders: dict[str, Order] = {}
        self.deliveries: list[DeliveryRecord] = []
        self.conflicts: dict[str, Conflict] = {}
        self.vehicle_loads: dict[str, str] = {}       # 车辆 -> 当前装车单
        self.vehicle_used_slots: dict[str, float] = defaultdict(float)
        self.now: int | None = None
        self._counters: dict[str, int] = defaultdict(int)
        # 原文编号 -> 由它登记的缺口行（供撤销/澄清按上下文定位）
        self.source_lines: dict[str, list[str]] = defaultdict(list)
        self.material_delivered: dict[tuple[str, str], int] = defaultdict(int)
        # 已显式回库的数量（已装车货不自动回库，只凭回库事件增加）
        self.material_returned: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------ 工具

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}-{self._counters[prefix]:03d}"

    def _append(self, command: Command, event: Event) -> str:
        self.command_index.setdefault(command.command_id, []).append(event.event_id)
        self.events.append(event)
        self._apply(event)
        return event.event_id

    def already_done(self, command_id: str) -> list[str] | None:
        return self.command_index.get(command_id)

    def available_stock(self, material: str) -> int:
        """初始库存扣除仍被占用（备货/在途）、已交接出库，加回显式回库数量。"""
        held = 0
        for order in self.orders.values():
            if order.state in (OrderState.RESERVED, OrderState.LOADED, OrderState.PARTIAL):
                for line in order.lines:
                    if line.material == material and line.state in (
                        LineState.RESERVED,
                        LineState.LOADED,
                        LineState.OPEN,
                    ):
                        held += line.qty - line.delivered_qty
        delivered = sum(
            qty for (site, mat), qty in self.material_delivered.items()
            if mat == material
        )
        returned = self.material_returned.get(material, 0)
        return self.initial_stock.get(material, 0) - held - delivered + returned

    def active_lines(self, site: str | None = None) -> list[RequestLine]:
        out = []
        for line in self.request_lines.values():
            if line.status in (
                RequestStatus.ACTIVE,
                RequestStatus.FALLBACK_TRIGGERED,
            ):
                if site is None or line.site == site:
                    out.append(line)
        return out

    def latest_verified_count(
        self, site: str, material: str
    ) -> InventoryCount | None:
        latest = None
        for count in self.counts.values():
            if (
                count.site == site
                and count.material == material
                and count.verified
                and (latest is None or (count.at or 0) >= (latest.at or 0))
            ):
                latest = count
        return latest

    def order_for_vehicle(self, vehicle: str) -> Order | None:
        order_id = self.vehicle_loads.get(vehicle)
        return self.orders.get(order_id) if order_id else None

    def find_loaded_line(
        self, vehicle: str, site: str, material: str
    ) -> tuple[Order, OrderLine] | None:
        order = self.order_for_vehicle(vehicle)
        if order is None:
            return None
        for line in order.lines:
            if line.site == site and line.material == material:
                return order, line
        return None

    # ------------------------------------------------------------ 回放

    def replay(self, events: Iterable[Event]) -> None:
        for event in events:
            self.events.append(event)
            self.command_index.setdefault(event.command_id, []).append(event.event_id)
            self._apply(event)

    def _apply(self, event: Event) -> None:
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler is None:
            raise BusinessRule(f"未知事件类型：{type(event).__name__}")
        handler(event)

    # ----- 事件应用（纯状态变更，不做校验；校验在命令处理中） -----

    def _on_RecordIngested(self, event: RecordIngested) -> None:
        self.records[event.record_id] = IngestedRecord(
            id=event.record_id, kind=event.kind, text=event.text, at=event.at
        )

    def _on_NoteRecorded(self, event: NoteRecorded) -> None:
        return

    def _on_ClarificationOpened(self, event: ClarificationOpened) -> None:
        self.clarifications[event.clarification_id] = Clarification(
            id=event.clarification_id,
            source=event.about_source,
            site=event.site,
            question=event.question,
        )

    def _on_ClarificationAnswered(self, event: ClarificationAnswered) -> None:
        clar = self.clarifications[event.clarification_id]
        clar.state = ClarificationState.ANSWERED
        clar.answer_source = event.answer_source
        clar.answer_text = event.answer_text

    def _on_RequestRegistered(self, event: RequestRegistered) -> None:
        line = RequestLine(
            id=event.line_id,
            site=event.site,
            material=event.material,
            qty=event.qty,
            unit=event.unit,
            mode=LineMode(event.mode),
            source=event.sources[0] if event.sources else "",
            sources=list(event.sources),
            status=RequestStatus.DRAFT if event.proxy else RequestStatus.ACTIVE,
            proxy=event.proxy,
            deadline=event.deadline,
            fallback_material=event.fallback_material,
            fallback_qty=event.fallback_qty,
            fallback_unit=event.fallback_unit,
            condition_note=event.condition_note,
            answer_to=event.answer_to,
            supersedes=event.supersedes,
            notes=event.notes,
        )
        # 带条件的首选行在期限判定前处于“条件悬而未决”
        if event.fallback_material and event.deadline is not None:
            line.status = RequestStatus.FALLBACK_OPEN
        self.request_lines[event.line_id] = line
        for source in event.sources:
            self.source_lines[source].append(event.line_id)
        if event.supersedes and event.supersedes in self.request_lines:
            old = self.request_lines[event.supersedes]
            old.status = RequestStatus.SUPERSEDED

    def _on_RequestSuperseded(self, event: RequestSuperseded) -> None:
        self.request_lines[event.line_id].status = RequestStatus.SUPERSEDED

    def _on_RequestCancelled(self, event: RequestCancelled) -> None:
        line = self.request_lines.get(event.line_id)
        if line is not None:
            line.status = RequestStatus.CANCELLED
            line.cancelled_qty += event.qty

    def _on_FallbackTriggered(self, event: FallbackTriggered) -> None:
        primary = self.request_lines[event.line_id]
        primary.status = RequestStatus.FALLBACK_TRIGGERED
        fallback = RequestLine(
            id=event.fallback_line_id,
            site=primary.site,
            material=event.fallback_material,
            qty=event.fallback_qty,
            unit=event.fallback_unit or "箱",
            mode=LineMode.TOTAL,
            source=";".join(primary.sources),
            sources=list(primary.sources),
            status=RequestStatus.ACTIVE,
            condition_note=f"由 {primary.id} 条件未满足转入",
            notes=primary.condition_note,
        )
        self.request_lines[event.fallback_line_id] = fallback

    def _on_FallbackWaived(self, event: FallbackWaived) -> None:
        self.request_lines[event.line_id].status = RequestStatus.FALLBACK_WAIVED

    def _on_FallbackQuantified(self, event) -> None:
        line = self.request_lines[event.line_id]
        line.fallback_material = event.fallback_material
        line.fallback_qty = event.fallback_qty
        line.fallback_unit = event.fallback_unit
        if event.answer_source not in line.sources:
            line.sources.append(event.answer_source)

    def _on_CountRegistered(self, event: CountRegistered) -> None:
        self.counts[event.count_id] = InventoryCount(
            id=event.count_id,
            site=event.site,
            material=event.material,
            qty=event.qty,
            unit=event.unit,
            at=event.counted_at,
            source=event.sources[0] if event.sources else "",
            verified=False,
            consumption_per_day=event.consumption_per_day,
            note=event.note,
        )

    def _on_CountVerified(self, event: CountVerified) -> None:
        count = self.counts[event.count_id]
        count.verified = True
        count.valid_from = event.valid_from
        count.valid_until = event.valid_until

    def _on_ReservationCreated(self, event: ReservationCreated) -> None:
        # 行号由命令时确定并随事件携带，回放不再重新生成
        lines = [
            OrderLine(
                id=line_id,
                order_id=event.order_id,
                site=event.site,
                material=material,
                unit=unit,
                qty=qty,
                request_line=req_line,
                state=LineState.RESERVED,
                reserved_qty=qty,
                sources=list(event.sources),
            )
            for (line_id, material, qty, unit, req_line) in event.lines
        ]
        order = Order(
            id=event.order_id,
            kind="reservation",
            state=OrderState.RESERVED,
            vehicle=None,
            site=event.site,
            created_at=event.at,
            sources=list(event.sources),
            lines=lines,
        )
        self.orders[event.order_id] = order
        for line in lines:
            self._bind_request_progress(line)

    def _on_LoadConfirmed(self, event: LoadConfirmed) -> None:
        if event.reservation_id and event.reservation_id in self.orders:
            # 备货单转为装车单：保留单号链路，按装车明细重建行（允许短装）
            old_order = self.orders[event.reservation_id]
            old_by_key = {
                (ln.site, ln.material): ln for ln in old_order.lines
            }
            lines: list[OrderLine] = []
            for line_id, site, material, qty, unit, req_line in event.lines:
                prev = old_by_key.get((site, material))
                lines.append(
                    OrderLine(
                        id=prev.id if prev else line_id,
                        order_id=event.order_id,
                        site=site,
                        material=material,
                        unit=unit,
                        qty=qty,
                        request_line=req_line or (prev.request_line if prev else None),
                        state=LineState.LOADED,
                        reserved_qty=0,
                        loaded_qty=qty,
                        sources=list(set(
                            (prev.sources if prev else []) + list(event.sources)
                        )),
                    )
                )
            order = Order(
                id=event.order_id,
                kind="loading",
                state=OrderState.LOADED,
                vehicle=event.vehicle,
                site=event.site,
                created_at=event.at,
                sources=list(set(old_order.sources + list(event.sources))),
                lines=lines,
            )
            # 原备货单作废（库存占用转移到装车单），保留对象但标记取消
            old_order.state = OrderState.CANCELLED
            for ln in old_order.lines:
                if ln.state == LineState.RESERVED:
                    req = self.request_lines.get(ln.request_line or "")
                    if req is not None:
                        req.reserved_qty = max(0, req.reserved_qty - ln.qty)
            self.orders[event.order_id] = order
            self.vehicle_loads[event.vehicle] = order.id
            for line in lines:
                self._bind_request_progress(line)
        else:
            lines = [
                OrderLine(
                    id=line_id,
                    order_id=event.order_id,
                    site=site,
                    material=material,
                    unit=unit,
                    qty=qty,
                    request_line=req_line,
                    state=LineState.LOADED,
                    loaded_qty=qty,
                    sources=list(event.sources),
                )
                for (line_id, site, material, qty, unit, req_line) in event.lines
            ]
            order = Order(
                id=event.order_id,
                kind="loading",
                state=OrderState.LOADED,
                vehicle=event.vehicle,
                site=event.site,
                created_at=event.at,
                sources=list(event.sources),
                lines=lines,
            )
            self.orders[event.order_id] = order
            self.vehicle_loads[event.vehicle] = order.id
            for line in lines:
                self._bind_request_progress(line)
        self.vehicle_used_slots[event.vehicle] = self._slots_for(event)

    def _slots_for(self, event: LoadConfirmed) -> float:
        total = 0.0
        for spec in event.lines:
            _line_id, site, material, qty, unit, _req = spec
            total += self.registry.require_material(material).package_slots(qty, unit)
        return round(total, 4)

    def _bind_request_progress(self, line: OrderLine) -> None:
        if not line.request_line:
            return
        req = self.request_lines.get(line.request_line)
        if req is None:
            return
        if line.state == LineState.RESERVED:
            req.reserved_qty += line.qty
        elif line.state == LineState.LOADED:
            req.loaded_qty += line.qty

    def _on_ReroutePlanned(self, event: ReroutePlanned) -> None:
        order = self.orders[event.order_id]
        line = order.line(event.line_id)
        # 改送不改变库存/车位占用，只改交付对象；原点办理量回滚为未满足
        req_line = self.request_lines.get(line.request_line or "")
        if req_line is not None and req_line.site == event.from_site:
            req_line.loaded_qty = max(0, req_line.loaded_qty - line.qty)
        line.rerouted_from = event.from_site
        line.site = event.to_site
        line.note = (line.note + " " if line.note else "") + event.note

    def _on_HandoverRecorded(self, event: HandoverRecorded) -> None:
        order = self.orders[event.order_id]
        line = order.line(event.line_id)
        line.delivered_qty += event.delivered_qty
        if event.delivered_qty < line.qty:
            line.short_shipped += line.qty - event.delivered_qty
            line.state = LineState.PARTIAL
            order.state = OrderState.PARTIAL
        else:
            line.state = LineState.DELIVERED
            if all(
                ln.state in (LineState.DELIVERED, LineState.CANCELLED)
                for ln in order.lines
            ):
                order.state = OrderState.DELIVERED
        if line.request_line and line.request_line in self.request_lines:
            req = self.request_lines[line.request_line]
            # 在途转成交付：loaded 减少，delivered 增加
            req.loaded_qty = max(0, req.loaded_qty - event.requested_qty)
            req.delivered_qty += event.delivered_qty
        self.material_delivered[(event.site, event.material)] += event.delivered_qty
        self.deliveries.append(
            DeliveryRecord(
                id=event.delivery_id,
                order_id=event.order_id,
                line_id=event.line_id,
                vehicle=order.vehicle,
                site=event.site,
                material=event.material,
                unit=event.unit,
                requested_qty=event.requested_qty,
                delivered_qty=event.delivered_qty,
                at=event.at,
                source=event.sources[0] if event.sources else "",
                rerouted_from=event.rerouted_from,
            )
        )

    def _on_ConflictRaised(self, event: ConflictRaised) -> None:
        self.conflicts[event.conflict_id] = Conflict(
            id=event.conflict_id,
            at=event.at,
            count_id=event.count_id,
            order_id=event.order_id,
            site=event.site,
            material=event.material,
            over_qty=event.over_qty,
            note=event.reason,
        )

    def _on_OrderLineFrozen(self, event: OrderLineFrozen) -> None:
        order = self.orders[event.order_id]
        order.line(event.line_id).state = LineState.FROZEN

    def _on_ConflictDecided(self, event: ConflictDecided) -> None:
        conflict = self.conflicts[event.conflict_id]
        conflict.decision = event.decision
        conflict.decided_by = event.officer
        conflict.note = (conflict.note + " " if conflict.note else "") + event.note

    def _on_StockReturned(self, event: StockReturned) -> None:
        order = self.orders[event.order_id]
        line = order.line(event.line_id)
        line.qty -= event.qty
        line.loaded_qty = max(0, line.loaded_qty - event.qty)
        self.material_returned[event.material] += event.qty
        if line.qty <= 0:
            line.state = LineState.CANCELLED
        if line.request_line and line.request_line in self.request_lines:
            req = self.request_lines[line.request_line]
            req.loaded_qty = max(0, req.loaded_qty - event.qty)

    def _on_OrderLineCancelled(self, event: OrderLineCancelled) -> None:
        order = self.orders[event.order_id]
        line = order.line(event.line_id)
        line.state = LineState.CANCELLED
        if line.request_line and line.request_line in self.request_lines:
            req = self.request_lines[line.request_line]
            req.reserved_qty = max(0, req.reserved_qty - event.qty)
            req.cancelled_qty += event.qty

    def _on_Tick(self, event: Tick) -> None:
        self.now = event.at
