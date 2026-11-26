"""调拨链路测试：库存/车位独占、容量窗口、型号兼容、改送短装、后备保护。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import build_service, ingest_all

from relief.errors import BusinessRule, DuplicateCommand
from relief.model import (
    LineState,
    OrderState,
    RequestStatus,
)
from relief.catalog import parse_clock
from relief.commands import (
    ConfirmLoad,
    CreateReservation,
    RegisterRequest,
)


class StockAndSlotRulesTest(unittest.TestCase):
    def _fresh(self):
        service, case = build_service()
        ingest_all(service, case)
        return service

    def test_same_stock_cannot_be_held_by_two_valid_orders(self):
        service = self._fresh()
        # 仓库 F2 初始 12，已交付东台 4，可用 8
        self.assertEqual(service.state.available_stock("F2"), 8)
        service.create_reservation("N", [("F2", 8, "个", None)])
        with self.assertRaises(BusinessRule):
            service.create_reservation("E", [("F2", 1, "个", None)])

    def test_vehicle_capacity_enforced(self):
        service = self._fresh()
        # T2 容量 4 格；泵每台 2 格，装 3 台需要 6 格
        with self.assertRaises(BusinessRule):
            service.confirm_load(
                "T2", [("E", "pump", 3, "台")], at=parse_clock("13:30")
            )

    def test_vehicle_window_enforced(self):
        service = self._fresh()
        with self.assertRaises(BusinessRule):
            service.confirm_load(
                "T2", [("E", "pump", 1, "台")], at=parse_clock("08:00")
            )

    def test_reachability_enforced(self):
        service = self._fresh()
        # T2 只能到 N、E，不能到 S
        with self.assertRaises(BusinessRule):
            service.confirm_load(
                "T2", [("S", "water", 1, "箱")], at=parse_clock("14:00")
            )

    def test_vehicle_slot_cannot_have_two_active_orders(self):
        service = self._fresh()
        service.confirm_load(
            "T2", [("E", "pump", 1, "台")], at=parse_clock("13:30")
        )
        with self.assertRaises(BusinessRule):
            service.confirm_load(
                "T2", [("N", "F2", 1, "个")], at=parse_clock("14:00")
            )

    def test_incompatible_filter_model_cannot_fill_gap(self):
        service = self._fresh()
        # 北营只收 F2；尝试用 F1 备货必须被拒
        with self.assertRaises(BusinessRule) as ctx:
            service.create_reservation("N", [("F1", 2, "个", None)])
        self.assertIn("不兼容", str(ctx.exception))


class RerouteAndHandoverTest(unittest.TestCase):
    def setUp(self):
        self.service, self.case = build_service()
        ingest_all(self.service, self.case)

    def test_reroute_rejudges_origin_unmet_and_new_site_delivery(self):
        state = self.service.state
        # 北营 4 个 F2 改送东台后：北营缺口仍为 4，东台有 4 个交付
        north = next(
            line for line in state.request_lines.values()
            if line.site == "N" and line.material == "F2"
            and line.status == RequestStatus.FALLBACK_OPEN
        )
        self.assertEqual(north.shortfall, 4)
        east_delivery = [
            d for d in state.deliveries
            if d.site == "E" and d.material == "F2"
        ]
        self.assertEqual(sum(d.delivered_qty for d in east_delivery), 4)
        # 同一批货不能两边都算完成：北营没有任何 F2 交付
        self.assertFalse(
            any(d.site == "N" and d.material == "F2" for d in state.deliveries)
        )
        # 交接记录保留原点引用
        self.assertEqual(east_delivery[0].rerouted_from, "N")

    def test_short_handover_records_actual_unload_only(self):
        state = self.service.state
        south = next(
            d for d in state.deliveries
            if d.site == "S" and d.material == "water"
        )
        self.assertEqual(south.requested_qty, 2)
        self.assertEqual(south.delivered_qty, 1)
        order = state.orders[south.order_id]
        line = order.line(south.line_id)
        self.assertEqual(line.short_shipped, 1)

    def test_loaded_goods_do_not_auto_return_on_cancellation(self):
        state = self.service.state
        # RS-J 撤销南坡另一箱水：在车上的 1 箱仍然占着，没有凭空回库
        water_in_vehicle = 0
        for order in state.orders.values():
            for line in order.lines:
                if line.material == "water" and line.state in (
                    LineState.LOADED, LineState.PARTIAL
                ):
                    water_in_vehicle += line.qty - line.delivered_qty
        self.assertEqual(water_in_vehicle, 1)
        # 撤销产生了待值班员确认的冲突
        self.assertTrue(state.conflicts)

    def test_explicit_return_stock_frees_inventory(self):
        service, case = build_service()
        ingest_all(service, case)
        state = service.state
        target = None
        for order in state.orders.values():
            for line in order.lines:
                if (line.site == "S" and line.material == "water"
                        and line.state == LineState.PARTIAL):
                    target = (order.id, line.id)
        order_id, line_id = target
        before = state.available_stock("water")
        service.return_stock(order_id, line_id, "water", 1,
                             officer="仓管乙", reason="值班员裁定短装部分回库")
        self.assertEqual(state.available_stock("water"), before + 1)


class FallbackProtectionTest(unittest.TestCase):
    def test_water_share_protected_until_condition_resolved(self):
        service, case = build_service()
        ingest_all(service, case)
        plan = service.allocate()
        # 北营后备饮水 4 箱被保留，分配解释中必须说明
        self.assertTrue(
            any("保留 4" in note for note in plan.explanations)
        )

    def test_deadline_miss_triggers_fallback_and_releases_share(self):
        service, case = build_service()
        ingest_all(service, case)
        # 时间推进到 12:30，北营 F2 未到货 -> 触发 4 箱饮水后备
        service.advance_clock(parse_clock("12:30"))
        fallback = [
            line for line in service.state.request_lines.values()
            if line.site == "N" and line.material == "water"
            and line.status == RequestStatus.ACTIVE
            and line.condition_note.startswith("由")
        ]
        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0].qty, 4)

    def test_deadline_met_waives_fallback(self):
        service, case = build_service()
        # 只接入到 RS-G 之前，构造北营 F2 已在 11:00 到货
        for record in case["records"]:
            service.ingest_record(record)
            if record["id"] == "RS-G":
                break
        # 北营 F2 已在途；直接做一次 4 个的北营交接
        service.record_handover(
            "T1", "N", "F2", requested_qty=4, delivered_qty=4,
            at=parse_clock("11:00"),
        )
        service.advance_clock(parse_clock("12:30"))
        north_f2 = [
            line for line in service.state.request_lines.values()
            if line.site == "N" and line.material == "F2"
        ]
        self.assertTrue(
            any(line.status == RequestStatus.FALLBACK_WAIVED for line in north_f2)
        )


class AllocationPlanTest(unittest.TestCase):
    def test_plan_splits_set_and_matches_verified_on_site_parts(self):
        service, case = build_service()
        ingest_all(service, case)
        # 核验东台自述已有 2 个 F2
        count = next(c for c in service.state.counts.values() if c.site == "E")
        service.verify_count(count.id, verifier="核验员甲", valid_from=count.at)
        plan = service.allocate()
        self.assertTrue(plan.feasible)
        # 东台只需要补 1 台泵，不再补 F2
        east_lines = [
            ln for vp in plan.plans for ln in vp.lines if ln.site == "E"
        ]
        materials = {ln.material for ln in east_lines}
        self.assertEqual(materials, {"pump"})

    def test_apply_plan_goes_through_same_command_chain(self):
        service, case = build_service()
        ingest_all(service, case)
        count = next(c for c in service.state.counts.values() if c.site == "E")
        service.verify_count(count.id, verifier="核验员甲", valid_from=count.at)
        plan = service.allocate()
        ids = service.apply_plan(plan, at=parse_clock("13:30"))
        self.assertTrue(ids)
        # T2 出现有效装车单
        order = service.state.order_for_vehicle("T2")
        self.assertIsNotNone(order)
        self.assertEqual(order.state, OrderState.LOADED)


class IdempotencyAndPersistenceTest(unittest.TestCase):
    def test_duplicate_ingestion_does_not_double_effects(self):
        service, case = build_service()
        first = ingest_all(service, case)
        event_count = len(service.state.events)
        # 再办理一遍：全部命中幂等键
        again = ingest_all(service, case)
        self.assertEqual(len(service.state.events), event_count)
        self.assertTrue(all(r.skipped_duplicate for r in again.values()))

    def test_event_log_replays_identical_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.jsonl"
            service, case = build_service(log)
            ingest_all(service, case)
            # 用同一日志重建服务
            rebuilt, _ = build_service(log)
            self.assertEqual(
                len(rebuilt.state.events), len(service.state.events)
            )
            self.assertEqual(
                rebuilt.state.available_stock("water"),
                service.state.available_stock("water"),
            )
            self.assertEqual(
                sorted(rebuilt.state.request_lines.keys()),
                sorted(service.state.request_lines.keys()),
            )


if __name__ == "__main__":
    unittest.main()
