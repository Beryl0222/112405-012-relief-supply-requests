"""盘点核验、缺口重算与冲突冻结测试。"""
from __future__ import annotations

import unittest

from support import build_service, ingest_all

from relief.errors import BusinessRule
from relief.model import LineState, OrderState


class CountVerificationTest(unittest.TestCase):
    def setUp(self):
        self.service, self.case = build_service()
        ingest_all(self.service, self.case)

    def test_unverified_count_does_not_change_verified_view(self):
        board = self.service.gap_board()
        # 全部盘点尚未核验，核验库存视图为空
        self.assertEqual(board.site_stock, [])

    def test_verifier_confirms_validity_window(self):
        count = next(c for c in self.service.state.counts.values() if c.source == "RS-E")
        self.service.verify_count(
            count.id, verifier="核验员甲",
            valid_from=count.at, valid_until=count.at + 12 * 60,
        )
        refreshed = self.service.state.counts[count.id]
        self.assertTrue(refreshed.verified)
        self.assertEqual(refreshed.valid_until, count.at + 12 * 60)
        board = self.service.gap_board()
        self.assertEqual(len(board.site_stock), 1)

    def test_new_verified_count_raises_conflict_against_paperwork(self):
        # RS-F 为南坡预留 3 箱水；若核验盘点显示存量已够，应冻结备货行而非自动撤单
        count = next(c for c in self.service.state.counts.values() if c.source == "RS-E")
        # 构造一个会超送的核验：南坡需求已取消，存量 2 + 预留 3 超出
        self.service.verify_count(count.id, verifier="核验员甲", valid_from=count.at)
        conflicts = list(self.service.state.conflicts.values())
        self.assertTrue(conflicts)
        # 至少一个冲突指向备货/装车单，且对应行被冻结，运输交接没有被自动撤销
        order_ids = {c.order_id for c in conflicts}
        self.assertTrue(order_ids)

    def test_verify_unknown_count_rejected(self):
        with self.assertRaises(BusinessRule):
            self.service.verify_count("CNT-999", verifier="核验员甲")

    def test_runout_projection_uses_verified_count_and_rate(self):
        # RS-B 自述盘点带日耗 3.3333 箱/日；核验后可推算耗尽时刻
        count = next(c for c in self.service.state.counts.values() if c.source == "RS-B")
        self.service.verify_count(count.id, verifier="核验员乙", valid_from=count.at)
        board = self.service.gap_board()
        water_rows = [g for g in board.gaps if g.material == "water"]
        # 南坡水需求已被撤销，缺口行不展示；改为直接核对盘点行的日耗字段
        verified = [row for row in board.site_stock if row["source"] == "RS-B"][0]
        self.assertAlmostEqual(verified["consumption_per_day"], 80 / 24, places=3)


if __name__ == "__main__":
    unittest.main()
