"""服务编排：原文接入 -> 解析 -> 澄清 -> 引擎办理 -> 事件落盘 -> 读模型。

同一条原文重复接入时，命令幂等键命中，不会产生重复业务效果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .allocation import AllocationPlan, Allocator
from .catalog import Registry
from .commands import (
    AdvanceClock,
    AnswerClarification,
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
from .engine import Engine
from .errors import Ambiguous, DuplicateCommand, ReliefError
from .model import to_dict
from .parser import ParseResult, parse_record
from .readmodel import ReadModel
from .state import State
from .store import EventStore


@dataclass
class IngestionReport:
    record_id: str
    kind: str
    applied_event_ids: list[str] = field(default_factory=list)
    clarification_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    skipped_duplicate: bool = False

    def to_dict(self) -> dict:
        return to_dict(self)


class ReliefService:
    def __init__(
        self,
        registry: Registry,
        initial_stock: dict[str, int],
        store: EventStore | None = None,
    ):
        self.registry = registry
        self.store = store or EventStore(None)
        self.state = State(registry, initial_stock)
        self.engine = Engine(self.state)
        self.read_model = ReadModel(self.state)
        # 回放既有日志，支持重复办理与读取结果
        self.state.replay(self.store.load_all())

    # ------------------------------------------------------------ 接入

    def ingest_record(self, record: dict) -> IngestionReport:
        report = IngestionReport(record_id=record["id"], kind=record["kind"])
        try:
            result = parse_record(record, self.registry)
        except Ambiguous as exc:
            # 语义不明：登记具体澄清，不猜测办理
            report.ambiguous.append(exc.question)
            clar = OpenClarification(
                command_id=f"cmd:clar:auto:{record['id']}",
                clarification_id="",
                site=None,
                question=exc.question,
                about_source=record["id"],
                sources=(record["id"],),
            )
            self._dispatch(clar, report)
            return report
        report.notes.extend(result.notes)
        for command in result.commands:
            self._dispatch(command, report)
        for clar in result.clarifications:
            self._dispatch(clar, report)
        return report

    def ingest_case(self, case: dict) -> list[IngestionReport]:
        return [self.ingest_record(record) for record in case["records"]]

    def _dispatch(self, command: Command, report: IngestionReport | None = None):
        try:
            ids = self.engine.handle(command)
        except DuplicateCommand as exc:
            if report is not None:
                report.skipped_duplicate = True
                report.applied_event_ids.extend(exc.event_ids)
            return []
        for event_id in ids:
            event = next(ev for ev in self.state.events if ev.event_id == event_id)
            self.store.append(event)
        if report is not None:
            report.applied_event_ids.extend(ids)
            from .events import ClarificationOpened

            for event_id in ids:
                event = next(ev for ev in self.state.events if ev.event_id == event_id)
                if isinstance(event, ClarificationOpened):
                    report.clarification_ids.append(event.clarification_id)
        return ids

    # ------------------------------------------------------------ 显式办理

    def answer_clarification(
        self, clarification_id: str, answer_source: str, answer_text: str
    ) -> list[str]:
        command = AnswerClarification(
            command_id=f"cmd:answer:{clarification_id}:{answer_source}",
            clarification_id=clarification_id,
            answer_source=answer_source,
            answer_text=answer_text,
            sources=(answer_source,),
        )
        return self._dispatch(command, None) or []

    def verify_count(
        self,
        count_id: str,
        verifier: str,
        valid_from: int | None = None,
        valid_until: int | None = None,
    ) -> list[str]:
        command = VerifyCount(
            command_id=f"cmd:verify:{count_id}:{verifier}",
            count_id=count_id,
            valid_from=valid_from,
            valid_until=valid_until,
            verifier=verifier,
        )
        return self._dispatch(command, None) or []

    def create_reservation(
        self, site: str, lines: list[tuple[str, int, str, str | None]]
    ) -> list[str]:
        command = CreateReservation(
            command_id=f"cmd:res:manual:{site}:{len(self.state.events)}",
            site=site,
            lines=lines,
        )
        return self._dispatch(command, None) or []

    def confirm_load(
        self,
        vehicle: str,
        site_lines: list[tuple[str, str, int, str]],
        at: int | None = None,
    ) -> list[str]:
        command = ConfirmLoad(
            command_id=f"cmd:load:manual:{vehicle}:{len(self.state.events)}",
            vehicle=vehicle,
            lines=[(m, q, u, None) for _s, m, q, u in site_lines],
            at=at,
        )
        command.__dict__["site_lines"] = site_lines
        return self._dispatch(command, None) or []

    def plan_reroute(
        self, vehicle: str, from_site: str, to_site: str, material: str,
        qty: int, note: str = "", at: int | None = None,
    ) -> list[str]:
        command = PlanReroute(
            command_id=f"cmd:reroute:manual:{vehicle}:{from_site}:{to_site}:{material}",
            vehicle=vehicle,
            from_site=from_site,
            to_site=to_site,
            material=material,
            qty=qty,
            note=note,
            at=at,
        )
        return self._dispatch(command, None) or []

    def record_handover(
        self,
        vehicle: str,
        site: str,
        material: str,
        requested_qty: int,
        delivered_qty: int,
        rerouted_from: str | None = None,
        at: int | None = None,
    ) -> list[str]:
        command = RecordHandover(
            command_id=(
                f"cmd:handover:manual:{vehicle}:{site}:{material}:"
                f"{delivered_qty}:{len(self.state.events)}"
            ),
            vehicle=vehicle,
            site=site,
            material=material,
            requested_qty=requested_qty,
            delivered_qty=delivered_qty,
            rerouted_from=rerouted_from,
            at=at,
        )
        return self._dispatch(command, None) or []

    def decide_conflict(
        self, conflict_id: str, decision: str, officer: str, note: str = ""
    ) -> list[str]:
        command = DecideConflict(
            command_id=f"cmd:conflict:{conflict_id}:{decision}",
            conflict_id=conflict_id,
            decision=decision,
            officer=officer,
            note=note,
        )
        return self._dispatch(command, None) or []

    def return_stock(
        self, order_id: str, line_id: str, material: str, qty: int,
        officer: str, reason: str,
    ) -> list[str]:
        command = ReturnStock(
            command_id=f"cmd:return:{order_id}:{line_id}:{qty}",
            order_id=order_id,
            line_id=line_id,
            material=material,
            qty=qty,
            reason=reason,
            officer=officer,
        )
        return self._dispatch(command, None) or []

    def advance_clock(self, at: int) -> list[str]:
        command = AdvanceClock(
            command_id=f"cmd:tick:{at}",
            at=at,
        )
        return self._dispatch(command, None) or []

    # ------------------------------------------------------------ 读取

    def gap_board(self):
        return self.read_model.gap_board()

    def query_site(self, site: str) -> dict:
        return self.read_model.query_site(site)

    def allocate(self) -> AllocationPlan:
        return Allocator(self.state).plan()

    def apply_plan(self, plan: AllocationPlan, at: int) -> list[str]:
        """把分配计划按同一调拨链路落成装车命令（再次经过全部不变量校验）。"""
        ids: list[str] = []
        for vehicle_plan in plan.plans:
            if not vehicle_plan.lines:
                continue
            site_lines = [
                (ln.site, ln.material, ln.qty, ln.unit) for ln in vehicle_plan.lines
            ]
            ids.extend(self.confirm_load(vehicle_plan.vehicle, site_lines, at=at))
        return ids
