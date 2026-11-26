"""语义解释层测试：口径、条件、澄清、撤销、盘点的区分。"""
from __future__ import annotations

import unittest

from support import build_case, build_registry, build_service, ingest_all

from relief.catalog import cn_number, format_clock, parse_clock
from relief.errors import Ambiguous, BusinessRule
from relief.model import RequestStatus
from relief.parser import extract_clock, parse_record, quantities


class ParsingPrimitivesTest(unittest.TestCase):
    def test_clock_extraction_bilingual_and_half_hour(self):
        self.assertEqual(extract_clock("南坡点八时盘点"), 8 * 60)
        self.assertEqual(extract_clock("九时半出发"), 9 * 60 + 30)
        self.assertEqual(extract_clock("十时三十分"), 10 * 60 + 30)
        self.assertEqual(extract_clock("arrive by 12:00"), 12 * 60)

    def test_quantities_require_unit_and_ignore_clock_and_model_numbers(self):
        # “十一时”不是数量；“F2”的 2 不被当成数量
        qs = quantities("北营通道十一时封闭，四个F2先送东台")
        self.assertEqual([q[0] for q in qs], [4])
        self.assertEqual(qs[0][1], "个")
        # 英文数量词
        self.assertEqual(
            quantities("four F2 cartridges arrive")[0][:2], (4, "个")
        )

    def test_cn_number_words(self):
        self.assertEqual(cn_number("两"), 2)
        self.assertEqual(cn_number("二十四"), 24)
        self.assertEqual(cn_number("十"), 10)

    def test_format_clock_roundtrip(self):
        self.assertEqual(format_clock(parse_clock("09:30")), "09:30")


class InterpretationTest(unittest.TestCase):
    def setUp(self):
        self.service, self.case = build_service()
        self.reports = ingest_all(self.service, self.case)
        self.state = self.service.state

    def test_water_remark_is_not_translated_into_water_shortage(self):
        # RS-A 登记的是 F2 条件请求，不是饮水缺两箱
        req_a = [
            line for line in self.state.request_lines.values()
            if "RS-A" in line.sources
        ]
        self.assertEqual(len(req_a), 1)
        line = req_a[0]
        self.assertEqual(line.material, "F2")
        self.assertEqual(line.status, RequestStatus.SUPERSEDED)

    def test_clarification_replaces_two_boxes_with_two_sets_of_two(self):
        # RS-D：两套×每套两个 = 4 个 F2，不是两箱
        active = [
            line for line in self.state.request_lines.values()
            if line.site == "N" and line.material == "F2"
            and line.status == RequestStatus.FALLBACK_OPEN
        ]
        self.assertEqual(len(active), 1)
        line = active[0]
        self.assertEqual(line.qty, 4)
        self.assertEqual(line.unit, "个")
        # 后备饮水 4 箱通过澄清挂接到同一条件行
        self.assertEqual(line.fallback_material, "water")
        self.assertEqual(line.fallback_qty, 4)

    def test_add_vs_total_mode_distinguished(self):
        line = next(
            line for line in self.state.request_lines.values()
            if "RS-B" in line.sources and line.material == "water"
        )
        self.assertEqual(line.mode.value, "add")
        self.assertEqual(line.qty, 2)  # “再送两箱”，不是把总量凑到两箱

    def test_relay_leaves_no_trace_business_fact(self):
        # RS-C 转述不产生需求、不产生盘点
        self.assertFalse(
            any("RS-C" in line.sources for line in self.state.request_lines.values())
        )
        self.assertFalse(
            any(c.source == "RS-C" for c in self.state.counts.values())
        )

    def test_forwarded_old_count_is_not_a_new_need(self):
        # 盘点记录都不生成请求行
        count_sources = {c.source for c in self.state.counts.values()}
        self.assertIn("RS-E", count_sources)
        for line in self.state.request_lines.values():
            self.assertNotIn("RS-E", line.sources)

    def test_cancellation_only_closes_discussed_material_line(self):
        # RS-J 只撤南坡饮水，东台/北营滤芯相关请求仍然有效
        water_line = next(
            line for line in self.state.request_lines.values()
            if line.site == "S" and line.material == "water"
        )
        self.assertEqual(water_line.status, RequestStatus.CANCELLED)
        live = [
            line for line in self.state.request_lines.values()
            if line.status in (RequestStatus.ACTIVE, RequestStatus.FALLBACK_OPEN)
        ]
        materials = {(line.site, line.material) for line in live}
        self.assertIn(("N", "F2"), materials)
        self.assertIn(("E", "pump_filter_set"), materials)
        self.assertNotIn(("S", "water"), materials)

    def test_set_request_not_promised_as_whole_set(self):
        # RS-K 套装登记，但没有任何整套装车/备货承诺
        for order in self.state.orders.values():
            for line in order.lines:
                self.assertNotEqual(line.material, "pump_filter_set")

    def test_ambiguous_text_opens_specific_clarification(self):
        registry = build_registry()
        weird = {
            "id": "RS-X",
            "kind": "request",
            "text": "南坡点需要一些水，尽快。",
        }
        # 解析层对不明数量直接抛 Ambiguous，不猜测
        with self.assertRaises(Ambiguous) as ctx:
            parse_record(weird, registry)
        self.assertIn("数量", ctx.exception.question)
        # 服务接入时把异常转成具体澄清登记，已明确的其他缺项不受影响
        service, _ = build_service()
        report = service.ingest_record(weird)
        self.assertTrue(report.ambiguous)
        self.assertEqual(len(service.state.clarifications), 1)


if __name__ == "__main__":
    unittest.main()
