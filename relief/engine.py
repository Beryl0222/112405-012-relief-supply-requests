"""命令引擎：校验业务不变量并追加事件。

handle() 是唯一入口；重复命令按幂等键直接返回既有事件，保证“支持重复办理”。
"""
from __future__ import annotations

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
    QuantifyFallback,
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
    ReservationCreated,
    StockReturned,
    Tick,
)
from .model import LineState, OrderState, RequestStatus
from .state import State


class Engine:
    def __init__(self, state: State):
        self.state = state

    def handle(self, command: Command) -> list[str]:
        done = self.state.already_done(command.command_id)
        if done is not None:
            raise DuplicateCommand(command.command_id, done)
        handler = getattr(self, f"_handle_{type(command).__name__}", None)
        if handler is None:
            raise BusinessRule(f"没有该命令的办理逻辑：{type(command).__name__}")
        event_ids = handler(command)
        # 时钟随命令推进（命令自带时刻时）
        if getattr(command, "at", None) is not None:
            self._evaluate_deadlines(command)
        return event_ids

    # ------------------------------------------------------------ 基础

    def _new_event_id(self) -> str:
        return self.state._next_id("EV")

    def _emit(self, command: Command, event_type, **kwargs) -> str:
        event = event_type(
            event_id=self._new_event_id(),
            command_id=command.command_id,
            at=getattr(command, "at", None),
            sources=tuple(getattr(command, "sources", ())),
            **kwargs,
        )
        return self.state._append(command, event)

    def _handle_IngestRecord(self, command: IngestRecord) -> list[str]:
        return [
            self._emit(
                command,
                RecordIngested,
                record_id=command.record_id,
                kind=command.kind,
                text=command.text,
            )
        ]

    def note_only(self, command: Command, record_id: str, reason: str) -> list[str]:
        """relay 等不构成独立观察的资料只留痕。"""
        return [self._emit(command, NoteRecorded, record_id=record_id, reason=reason)]

    def _handle_OpenClarification(self, command: OpenClarification) -> list[str]:
        clarification_id = command.clarification_id or self.state._next_id("CLAR")
        return [
            self._emit(
                command,
                ClarificationOpened,
                clarification_id=clarification_id,
                site=command.site,
                question=command.question,
                about_source=command.about_source,
            )
        ]

    def _handle_AnswerClarification(self, command: AnswerClarification) -> list[str]:
        if command.clarification_id not in self.state.clarifications:
            raise BusinessRule(f"澄清不存在：{command.clarification_id}")
        return [
            self._emit(
                command,
                ClarificationAnswered,
                clarification_id=command.clarification_id,
                answer_source=command.answer_source,
                answer_text=command.answer_text,
            )
        ]

    def _handle_QuantifyFallback(self, command: QuantifyFallback) -> list[str]:
        """澄清答复只补全对应条件行的后备数量，不产生新的独立缺项。"""
        state = self.state
        target = None
        for line in state.request_lines.values():
            if (
                line.site == command.site
                and line.material == command.primary_material
                and line.status == RequestStatus.FALLBACK_OPEN
            ):
                target = line
        if target is None:
            raise BusinessRule(
                f"{command.site} 没有悬而未决的 {command.primary_material} 条件行可挂接后备数量"
            )
        return [
            self._emit(
                command,
                FallbackQuantified,
                line_id=target.id,
                fallback_material=command.fallback_material,
                fallback_qty=command.fallback_qty,
                fallback_unit=command.fallback_unit,
                answer_source=command.answer_source,
            )
        ]

    # ------------------------------------------------------------ 请求

    def _handle_RegisterRequest(self, command: RegisterRequest) -> list[str]:
        state = self.state
        material = state.registry.require_material(command.material)
        state.registry.require_site(command.site)
        if command.qty <= 0:
            raise BusinessRule("需求数量必须为正整数；数量不明应走澄清而不是造数")

        # 型号兼容：F1 不能补 F2 的缺口（在分配侧强制，登记侧记录兼容口径）
        line_id = command.line_id or state._next_id("REQ")

        # 同来源同物资的旧行被新行取代（澄清答复修正口径时）
        supersedes = command.supersedes
        if supersedes is None and command.answer_to:
            prior = [
                line.id
                for line in state.request_lines.values()
                if line.site == command.site
                and line.material == command.material
                and line.status
                in (RequestStatus.ACTIVE, RequestStatus.FALLBACK_OPEN)
                and command.answer_to not in line.sources
            ]
            supersedes = prior[-1] if prior else None

        event_id = self._emit(
            command,
            RequestRegistered,
            line_id=line_id,
            site=command.site,
            material=command.material,
            qty=command.qty,
            unit=command.unit or material.unit,
            mode=command.mode,
            proxy=command.proxy,
            deadline=command.deadline,
            fallback_material=command.fallback_material,
            fallback_qty=command.fallback_qty,
            fallback_unit=command.fallback_unit,
            condition_note=command.condition_note,
            answer_to=command.answer_to,
            supersedes=supersedes,
            notes=command.notes,
        )
        return [event_id]

    def _handle_CancelRequest(self, command: CancelRequest) -> list[str]:
        """撤销按“当时谈论的物资”定位；不撤整个地点的需求。"""
        state = self.state
        candidates = [
            line
            for line in state.request_lines.values()
            if line.site == command.site
            and line.status
            in (RequestStatus.ACTIVE, RequestStatus.FALLBACK_OPEN,
                RequestStatus.FALLBACK_TRIGGERED)
            and (command.material is None or line.material == command.material)
        ]
        if not candidates:
            raise BusinessRule(
                f"{command.site} 没有可撤销的对应物资行；撤销不能扩大到整个地点"
            )
        # “刚才说的另一箱”优先取最近登记、且仍未满足的同物资行
        candidates.sort(key=lambda line: line.id)
        target = candidates[-1]
        qty = command.qty if command.qty is not None else target.shortfall
        qty = min(qty, target.shortfall) if target.shortfall else qty
        if qty <= 0:
            # 已在途/已交付的部分不能凭空撤销；未满足量为 0 时只登记说明
            qty = 0
        ids = [
            self._emit(
                command,
                RequestCancelled,
                line_id=target.id,
                site=target.site,
                material=target.material,
                qty=qty,
                note=command.note,
            )
        ]
        # 若该撤销撞上备货/装车单，登记冲突交值班员；已装车不自动回库
        for order in state.orders.values():
            if order.state not in (OrderState.RESERVED, OrderState.LOADED,
                                   OrderState.PARTIAL):
                continue
            for line in order.lines:
                if (
                    line.site == target.site
                    and line.material == target.material
                    and line.state in (LineState.RESERVED, LineState.LOADED)
                ):
                    conflict_id = state._next_id("CFL")
                    ids.append(
                        self._emit(
                            command,
                            ConflictRaised,
                            conflict_id=conflict_id,
                            count_id=None,
                            order_id=order.id,
                            line_id=line.id,
                            site=line.site,
                            material=line.material,
                            over_qty=min(qty or line.qty, line.qty),
                            reason=(
                                "撤销对应在途/备货物资；已装车货物不能自动回库，"
                                "等待值班员确认受影响安排"
                            ),
                        )
                    )
        return ids

    # ------------------------------------------------------------ 盘点

    def _handle_RegisterCount(self, command: RegisterCount) -> list[str]:
        state = self.state
        state.registry.require_site(command.site)
        state.registry.require_material(command.material)
        count_id = command.count_id or state._next_id("CNT")
        return [
            self._emit(
                command,
                CountRegistered,
                count_id=count_id,
                site=command.site,
                material=command.material,
                qty=command.qty,
                unit=command.unit,
                counted_at=command.counted_at,
                proxy=command.proxy,
                consumption_per_day=command.consumption_per_day,
                note=command.note,
            )
        ]

    def _handle_VerifyCount(self, command: VerifyCount) -> list[str]:
        state = self.state
        if command.count_id not in state.counts:
            raise BusinessRule(f"盘点不存在：{command.count_id}")
        ids = [
            self._emit(
                command,
                CountVerified,
                count_id=command.count_id,
                valid_from=command.valid_from,
                valid_until=command.valid_until,
                verifier=command.verifier,
            )
        ]
        ids.extend(self._raise_count_conflicts(command))
        return ids

    def _raise_count_conflicts(self, command: VerifyCount) -> list[str]:
        """新核验盘点撞上备货/装车单：避免超送，同时保住运输交接。"""
        state = self.state
        count = state.counts[command.count_id]
        ids: list[str] = []
        latest = state.latest_verified_count(count.site, count.material)
        if latest is None or latest.id != count.id:
            return ids
        # 同点同物资的有效缺口（已交付不再追）
        for order in state.orders.values():
            if order.state not in (OrderState.RESERVED, OrderState.LOADED,
                                   OrderState.PARTIAL):
                continue
            for line in order.lines:
                if line.site != count.site or line.material != count.material:
                    continue
                if line.state not in (LineState.RESERVED, LineState.LOADED):
                    continue
                # 现场存量 + 已交付 + 在途/备货 vs 需求总量
                need = self._site_need(line.site, line.material)
                delivered = state.material_delivered.get(
                    (line.site, line.material), 0
                )
                inflight = sum(
                    ln.qty - ln.delivered_qty
                    for od in state.orders.values()
                    if od.state in (OrderState.RESERVED, OrderState.LOADED,
                                    OrderState.PARTIAL)
                    for ln in od.lines
                    if ln.site == line.site
                    and ln.material == line.material
                    and ln.state in (LineState.RESERVED, LineState.LOADED)
                )
                projected = count.qty + delivered + inflight
                over = max(0, projected - need)
                if over <= 0:
                    continue
                conflict_id = state._next_id("CFL")
                ids.append(
                    self._emit(
                        command,
                        ConflictRaised,
                        conflict_id=conflict_id,
                        count_id=count.id,
                        order_id=order.id,
                        line_id=line.id,
                        site=line.site,
                        material=line.material,
                        over_qty=min(over, line.qty),
                        reason=(
                            f"核验盘点 {count.qty}{count.unit} 后预计超送 {over}；"
                            "装车/备货安排冻结待值班员裁决，运输交接不自动撤销"
                        ),
                    )
                )
                ids.append(
                    self._emit(
                        command,
                        OrderLineFrozen,
                        order_id=order.id,
                        line_id=line.id,
                        conflict_id=conflict_id,
                    )
                )
        return ids

    def _site_need(self, site: str, material: str) -> int:
        total = 0
        for line in self.state.request_lines.values():
            if (
                line.site == site
                and line.material == material
                and line.status
                in (RequestStatus.ACTIVE, RequestStatus.FALLBACK_TRIGGERED)
            ):
                total += line.qty - line.cancelled_qty
        return total

    # ------------------------------------------------------------ 备货/装车

    def _check_stock(self, material: str, qty: int) -> None:
        available = self.state.available_stock(material)
        if qty > available:
            raise BusinessRule(
                f"{material} 可用库存不足：需要 {qty}，可用 {available}（"
                "已被有效备货/装车单占用的库存不能重复使用）"
            )

    def _check_model_match(self, site: str, wanted: str, offered: str) -> None:
        if wanted == offered:
            return
        wanted_mat = self.state.registry.require_material(wanted)
        offered_mat = self.state.registry.require_material(offered)
        # 只在滤芯类替代场景做型号校验；水与滤芯之间的替代由后备条件单独管理
        if wanted_mat.compatible_models and offered_mat.compatible_models:
            if offered not in wanted_mat.compatible_models:
                raise BusinessRule(
                    f"{site} 需要 {wanted}，{offered} 型号不兼容，不能按通用数量补足"
                )

    def _handle_CreateReservation(self, command: CreateReservation) -> list[str]:
        state = self.state
        state.registry.require_site(command.site)
        for material, qty, unit, _req in command.lines:
            mat = state.registry.require_material(material)
            if unit != mat.unit and not any(p[0] == unit for p in mat.packages):
                raise BusinessRule(f"{material} 未登记的包装单位：{unit}")
            self._check_stock(material, qty)
        for line in state.request_lines.values():
            if line.site == command.site and line.status in (
                RequestStatus.ACTIVE,
                RequestStatus.FALLBACK_OPEN,
                RequestStatus.FALLBACK_TRIGGERED,
            ):
                self._check_model_match(command.site, line.material, material)
        order_id = command.order_id or state._next_id("ORD")
        line_specs = tuple(
            (state._next_id("OL"), material, qty, unit, req_line)
            for material, qty, unit, req_line in command.lines
        )
        return [
            self._emit(
                command,
                ReservationCreated,
                order_id=order_id,
                site=command.site,
                lines=line_specs,
            )
        ]

    def _handle_ConfirmLoad(self, command: ConfirmLoad) -> list[str]:
        state = self.state
        vehicle = state.registry.vehicle(command.vehicle)
        if vehicle is None:
            raise BusinessRule(f"车辆未登记：{command.vehicle}")
        existing = state.order_for_vehicle(command.vehicle)
        if existing is not None and existing.state in (
            OrderState.LOADED,
            OrderState.PARTIAL,
        ):
            raise BusinessRule(
                f"车位 {command.vehicle} 已被有效装车单 {existing.id} 占用，"
                "同一车位不能挂两张有效单"
            )
        # 解析带站点的行（解析层挂在 site_lines）
        site_lines = command.__dict__.get("site_lines")
        if site_lines is None:
            site_lines = [
                (command.site, material, qty, unit)
                for material, qty, unit, _req in command.lines
            ]
        for site, material, qty, unit in site_lines:
            if site not in vehicle.reachable:
                raise BusinessRule(
                    f"{command.vehicle} 不可到达 {site}（可达：{','.join(vehicle.reachable)}）"
                )
            self._check_stock(material, qty)
        slots = 0.0
        for site, material, qty, unit in site_lines:
            slots += state.registry.require_material(material).package_slots(qty, unit)
        if slots > vehicle.capacity:
            raise BusinessRule(
                f"{command.vehicle} 容量 {vehicle.capacity} 格，本单需 {slots} 格，超出容量"
            )
        if vehicle.window_start is not None and command.at is not None:
            if not (vehicle.window_start <= command.at <= vehicle.window_end):
                raise BusinessRule(
                    f"{command.vehicle} 出发窗口外，不能在该时刻装车"
                )
        order_id = command.order_id or state._next_id("ORD")
        # 找到同点同物资的备货单作为链路来源
        reservation_id = self._match_reservation(site_lines)
        # 行号在命令时确定，事件携带，保证回放结果一致
        if reservation_id:
            old_order = state.orders[reservation_id]
            old_by_key = {(ln.site, ln.material): ln for ln in old_order.lines}
        else:
            old_by_key = {}
        line_specs = tuple(
            (
                old_by_key[(site, material)].id
                if (site, material) in old_by_key
                else state._next_id("OL"),
                site,
                material,
                qty,
                unit,
                self._match_request_line(site, material, qty),
            )
            for site, material, qty, unit in site_lines
        )
        return [
            self._emit(
                command,
                LoadConfirmed,
                order_id=order_id,
                vehicle=command.vehicle,
                site=command.site,
                lines=line_specs,
                reservation_id=reservation_id,
            )
        ]

    def _match_reservation(self, site_lines) -> str | None:
        state = self.state
        for order in state.orders.values():
            if order.state != OrderState.RESERVED:
                continue
            keys = {(ln.site, ln.material, ln.qty) for ln in order.lines}
            wanted = {(s, m, q) for s, m, q, _u in site_lines}
            if wanted & keys:
                return order.id
        return None

    def _match_request_line(
        self, site: str, material: str, qty: int
    ) -> str | None:
        for line in self.state.request_lines.values():
            if (
                line.site == site
                and line.material == material
                and line.status
                in (RequestStatus.ACTIVE, RequestStatus.FALLBACK_TRIGGERED)
                and line.shortfall > 0
            ):
                return line.id
        return None

    # ------------------------------------------------------------ 改送/交接

    def _handle_PlanReroute(self, command: PlanReroute) -> list[str]:
        state = self.state
        vehicle_code = command.vehicle
        found = None
        if vehicle_code:
            vehicle = state.registry.vehicle(vehicle_code)
            if vehicle is None:
                raise BusinessRule(f"车辆未登记：{vehicle_code}")
            found = state.find_loaded_line(
                vehicle_code, command.from_site, command.material
            )
        else:
            # 原文无车牌：按原点+物资在所有在途装车单中定位
            found = self._find_active_line(command.from_site, command.material)
            if found is not None:
                order, line = found
                vehicle_code = order.vehicle
                vehicle = state.registry.vehicle(vehicle_code)
        if found is None:
            raise BusinessRule(
                f"没有 {command.from_site} 的 {command.material} 在途行可改送"
            )
        if command.to_site not in vehicle.reachable:
            raise BusinessRule(
                f"{vehicle_code} 改送不可到达 {command.to_site}"
            )
        order, line = found
        return [
            self._emit(
                command,
                ReroutePlanned,
                order_id=order.id,
                line_id=line.id,
                from_site=command.from_site,
                to_site=command.to_site,
                material=command.material,
                qty=command.qty or line.qty,
                note=command.note,
            )
        ]

    def _handle_RecordHandover(self, command: RecordHandover) -> list[str]:
        state = self.state
        if command.vehicle:
            found = state.find_loaded_line(
                command.vehicle, command.site, command.material
            )
            if found is None:
                # 交接文本可能不重复车牌：退化为按地点+物资找在途行
                found = self._find_active_line(command.site, command.material)
        else:
            found = self._find_active_line(command.site, command.material)
        if found is None:
            raise BusinessRule(
                f"找不到 {command.site} 的 {command.material} 在途行可交接"
            )
        order, line = found
        if command.delivered_qty > line.qty - line.delivered_qty:
            raise BusinessRule("交接量不能超过车上剩余数量")
        rerouted_from = command.rerouted_from or line.rerouted_from
        delivery_id = command.delivery_id or state._next_id("DLV")
        return [
            self._emit(
                command,
                HandoverRecorded,
                delivery_id=delivery_id,
                order_id=order.id,
                line_id=line.id,
                site=command.site,
                material=command.material,
                unit=line.unit,
                requested_qty=command.requested_qty or line.qty,
                delivered_qty=command.delivered_qty,
                rerouted_from=rerouted_from,
            )
        ]

    def _find_active_line(self, site: str, material: str):
        state = self.state
        for order in state.orders.values():
            if order.state not in (OrderState.LOADED, OrderState.PARTIAL):
                continue
            for line in order.lines:
                if (
                    line.site == site
                    and line.material == material
                    and line.state in (LineState.LOADED, LineState.PARTIAL)
                ):
                    return order, line
        return None

    # ------------------------------------------------------------ 冲突裁决/回库

    def _handle_DecideConflict(self, command: DecideConflict) -> list[str]:
        state = self.state
        if command.conflict_id not in state.conflicts:
            raise BusinessRule(f"冲突不存在：{command.conflict_id}")
        conflict = state.conflicts[command.conflict_id]
        ids = [
            self._emit(
                command,
                ConflictDecided,
                conflict_id=command.conflict_id,
                decision=command.decision,
                officer=command.officer,
                note=command.note,
            )
        ]
        if command.decision in ("trim", "return_stock"):
            order = state.orders[conflict.order_id]
            line = order.line(command.conflict_id and self._line_of_conflict(conflict))
            qty = conflict.over_qty
            if command.decision == "return_stock":
                ids.append(
                    self._emit(
                        command,
                        StockReturned,
                        order_id=order.id,
                        line_id=line.id,
                        material=conflict.material,
                        qty=qty,
                        reason=command.note or "值班员裁决回库",
                        officer=command.officer,
                    )
                )
            else:
                ids.append(
                    self._emit(
                        command,
                        OrderLineCancelled,
                        order_id=order.id,
                        line_id=line.id,
                        qty=qty,
                        note=command.note or "值班员裁定削减超送部分",
                    )
                )
        return ids

    def _line_of_conflict(self, conflict) -> str:
        for event in reversed(self.state.events):
            if isinstance(event, ConflictRaised) and event.conflict_id == conflict.id:
                return event.line_id
        raise BusinessRule("冲突缺少行定位")

    def _handle_ReturnStock(self, command: ReturnStock) -> list[str]:
        state = self.state
        if command.order_id not in state.orders:
            raise BusinessRule(f"单据不存在：{command.order_id}")
        order = state.orders[command.order_id]
        line = order.line(command.line_id)
        if line.state != LineState.LOADED and line.state != LineState.PARTIAL:
            raise BusinessRule("只有已装车货物需要显式回库操作")
        if command.qty > line.qty - line.delivered_qty:
            raise BusinessRule("回库量不能超过车上剩余数量")
        return [
            self._emit(
                command,
                StockReturned,
                order_id=order.id,
                line_id=line.id,
                material=command.material or line.material,
                qty=command.qty,
                reason=command.reason,
                officer=command.officer,
            )
        ]

    def _handle_AdvanceClock(self, command: AdvanceClock) -> list[str]:
        return [
            self._emit(
                command,
                Tick,
            )
        ]

    # ------------------------------------------------------------ 条件期限

    def _evaluate_deadlines(self, command: Command) -> list[str]:
        """条件替代到期判定：期限内首选未到货则转后备；已到货则放弃后备。"""
        state = self.state
        now = command.at
        ids: list[str] = []
        for line in list(state.request_lines.values()):
            if line.status != RequestStatus.FALLBACK_OPEN:
                continue
            if line.deadline is None or now is None or now < line.deadline:
                continue
            delivered = state.material_delivered.get(
                (line.site, line.material), 0
            )
            if delivered >= line.qty:
                ids.append(
                    self._emit(
                        command,
                        FallbackWaived,
                        line_id=line.id,
                        reason=f"期限前 {line.material} 已到货，后备需求放弃",
                    )
                )
                continue
            if line.fallback_qty is None:
                # 后备数量尚未澄清：不造数，保持悬置并由读模型提示
                continue
            fallback_line_id = state._next_id("REQ")
            ids.append(
                self._emit(
                    command,
                    FallbackTriggered,
                    line_id=line.id,
                    fallback_line_id=fallback_line_id,
                    fallback_material=line.fallback_material,
                    fallback_qty=line.fallback_qty,
                    fallback_unit=line.fallback_unit,
                )
            )
        return ids


def command_material(command) -> str:
    return getattr(command, "material", "") or ""
