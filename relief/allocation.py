"""分配引擎：把核验后的缺口与可用库存、车辆约束配对成可兑现装车单。

只产出“计划”，是否落单由服务层再走 ConfirmLoad 命令（同一条调拨链路）。
可行性解释随计划返回；不能满足的行逐条说明原因，不静默丢弃。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import format_clock
from .model import LineState, OrderState, RequestStatus
from .state import State


@dataclass
class PlannedLine:
    site: str
    material: str
    qty: int
    unit: str
    slots: float
    request_line: str | None
    note: str = ""


@dataclass
class VehiclePlan:
    vehicle: str
    capacity: float
    used_slots: float
    window: str
    lines: list[PlannedLine] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)


@dataclass
class AllocationPlan:
    feasible: bool
    plans: list[VehiclePlan] = field(default_factory=list)
    explanations: list[str] = field(default_factory=list)
    unmet: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        from .model import to_dict

        return to_dict(self)


class Allocator:
    def __init__(self, state: State):
        self.state = state

    def plan(self) -> AllocationPlan:
        state = self.state
        plan = AllocationPlan(feasible=True)
        # 先算“受保护份额”：条件替代未满足前，后备物资不能提前让给别人
        protected = self._protected_fallback_shares()
        virtual_stock = {
            code: state.available_stock(code) - protected.get(code, 0)
            for code in state.initial_stock
        }
        for code, qty in protected.items():
            if qty:
                plan.explanations.append(
                    f"{code} 保留 {qty} 作为条件后备份额，"
                    "替代条件未满足前不参与其他点分配"
                )

        gaps = self._collect_gap_lines()
        # 有期限的优先，其次缺口量大的优先（确定性排序）
        gaps.sort(key=lambda g: (
            g.get("deadline") is None,
            g.get("deadline") or 0,
            -g["qty"],
            g["site"],
            g["material"],
        ))

        vehicles = sorted(state.registry.vehicles, key=lambda v: v.code)
        vehicle_plans = {
            v.code: VehiclePlan(
                vehicle=v.code,
                capacity=v.capacity,
                used_slots=0.0,
                window=(
                    f"{format_clock(v.window_start)}-{format_clock(v.window_end)}"
                ),
            )
            for v in vehicles
        }
        remaining = dict(virtual_stock)

        for gap in gaps:
            placed = False
            for vehicle in vehicles:
                vp = vehicle_plans[vehicle.code]
                reason = self._can_place(vehicle, vp, gap, remaining)
                if reason is None:
                    self._place(vp, vehicle, gap, remaining)
                    placed = True
                    break
                gap.setdefault("rejections", []).append(
                    {"vehicle": vehicle.code, "reason": reason}
                )
            if not placed:
                plan.feasible = False
                plan.unmet.append(
                    {
                        "site": gap["site"],
                        "material": gap["material"],
                        "qty": gap["qty"],
                        "unit": gap["unit"],
                        "request_line": gap["request_line"],
                        "rejections": gap.get("rejections", []),
                        "reason": "没有车辆同时满足库存、容量、窗口与可达性",
                    }
                )

        plan.plans = [vp for vp in vehicle_plans.values() if vp.lines or vp.rejected]
        plan.explanations.extend(self._explain(plan))
        return plan

    # ------------------------------------------------------------ 缺口归集

    def _collect_gap_lines(self) -> list[dict]:
        state = self.state
        out: list[dict] = []
        for line in state.request_lines.values():
            if line.status not in (
                RequestStatus.ACTIVE,
                RequestStatus.FALLBACK_TRIGGERED,
            ):
                continue
            if line.shortfall <= 0:
                continue
            material = state.registry.require_material(line.material)
            if material.is_set:
                # 套装不整套承诺：按现场已核验部件拆成单件缺口
                out.extend(self._split_set(line))
                continue
            qty = line.shortfall
            slots = material.package_slots(qty, line.unit)
            out.append(
                {
                    "site": line.site,
                    "material": line.material,
                    "qty": qty,
                    "unit": line.unit,
                    "slots": slots,
                    "request_line": line.id,
                    "deadline": line.deadline,
                    "note": line.condition_note,
                }
            )
        return out

    def _split_set(self, line) -> list[dict]:
        state = self.state
        material = state.registry.require_material(line.material)
        rows = []
        for component_code, per_set in material.set_components:
            needed = per_set * line.qty
            count = state.latest_verified_count(line.site, component_code)
            on_site = count.qty if count else 0
            missing = max(0, needed - on_site)
            if missing <= 0:
                continue
            component = state.registry.require_material(component_code)
            rows.append(
                {
                    "site": line.site,
                    "material": component_code,
                    "qty": missing,
                    "unit": component.unit,
                    "slots": component.package_slots(missing, component.unit),
                    "request_line": line.id,
                    "deadline": line.deadline,
                    "note": (
                        f"套装 {line.material} 拆单件：需 {needed}，"
                        f"现场核验已有 {on_site}，缺 {missing}"
                    ),
                }
            )
        return rows

    def _protected_fallback_shares(self) -> dict[str, int]:
        state = self.state
        protected: dict[str, int] = {}
        for line in state.request_lines.values():
            if (
                line.status == RequestStatus.FALLBACK_OPEN
                and line.fallback_material
                and line.fallback_qty
            ):
                protected[line.fallback_material] = (
                    protected.get(line.fallback_material, 0) + line.fallback_qty
                )
        return protected

    # ------------------------------------------------------------ 配对

    def _can_place(self, vehicle, vp: VehiclePlan, gap: dict, remaining: dict):
        state = self.state
        if gap["site"] not in vehicle.reachable:
            return f"车辆不可到达 {gap['site']}"
        material = state.registry.require_material(gap["material"])
        # 同型号自匹配；滤芯类还要核对兼容型号表
        if gap["material"] != material.code:
            return f"{material.code} 与需求型号不兼容"
        if material.compatible_models and gap["material"] not in material.compatible_models:
            return f"{material.code} 与需求型号不兼容"
        stock_left = remaining.get(gap["material"], 0)
        if stock_left < gap["qty"]:
            return (
                f"可用库存不足：需 {gap['qty']}{gap['unit']}，"
                f"扣除占用与后备份额后剩 {stock_left}"
            )
        if vp.used_slots + gap["slots"] > vehicle.capacity + 1e-9:
            return (
                f"容量不足：本单再需 {gap['slots']} 格，"
                f"车辆余 {round(vehicle.capacity - vp.used_slots, 2)} 格"
            )
        # 窗口：缺口期限不得晚于车辆窗口结束
        if gap.get("deadline") is not None and vehicle.window_end is not None:
            if vehicle.window_end > gap["deadline"] and vehicle.window_start > gap["deadline"]:
                return f"窗口 {format_clock(vehicle.window_start)} 晚于期限 {format_clock(gap['deadline'])}"
        # 该车辆已有在途装车单时不能再挂新单
        existing = state.order_for_vehicle(vehicle.code)
        if existing is not None and existing.state in (
            OrderState.LOADED,
            OrderState.PARTIAL,
        ):
            return f"车位已被有效装车单 {existing.id} 占用"
        return None

    def _place(
        self, vp: VehiclePlan, vehicle, gap: dict, remaining: dict
    ) -> None:
        vp.lines.append(
            PlannedLine(
                site=gap["site"],
                material=gap["material"],
                qty=gap["qty"],
                unit=gap["unit"],
                slots=gap["slots"],
                request_line=gap["request_line"],
                note=gap.get("note", ""),
            )
        )
        vp.used_slots = round(vp.used_slots + gap["slots"], 4)
        remaining[gap["material"]] = remaining.get(gap["material"], 0) - gap["qty"]

    def _explain(self, plan: AllocationPlan) -> list[str]:
        notes = []
        for vp in plan.plans:
            if vp.lines:
                notes.append(
                    f"{vp.vehicle}（{vp.window}，{vp.capacity}格）"
                    f"计划装 {vp.used_slots} 格："
                    + "；".join(
                        f"{ln.site} {ln.material}×{ln.qty}{ln.unit}"
                        for ln in vp.lines
                    )
                )
        for item in plan.unmet:
            notes.append(
                f"未满足：{item['site']} {item['material']}×{item['qty']}"
                f"（{item['reason']}）"
            )
        return notes
