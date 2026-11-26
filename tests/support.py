"""测试公共夹具：从合成资料包构建服务与登记。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from relief.catalog import Registry, load_case  # noqa: E402
from relief.service import ReliefService  # noqa: E402
from relief.store import EventStore  # noqa: E402

CASE_PATH = Path(__file__).parents[1] / "data" / "case.json"


def build_case():
    return load_case(CASE_PATH)


def build_registry(case=None):
    case = case or build_case()
    return Registry().with_case_objects(case["objects"])


def build_service(events_path: str | Path | None = None):
    case = build_case()
    registry = build_registry(case)
    stock = {item["item"]: item["qty"] for item in case["objects"]["stock"]}
    store = EventStore(events_path)
    return ReliefService(registry, stock, store), case


def ingest_all(service: ReliefService, case: dict) -> dict:
    reports = {}
    for record in case["records"]:
        reports[record["id"]] = service.ingest_record(record)
    return reports


class CaseFixtureTest(unittest.TestCase):
    def test_full_case_ingests_without_ambiguity_errors(self):
        service, case = build_service()
        reports = ingest_all(service, case)
        # 只有资料本身语义不明才允许出现澄清；本资料包应全部可解释
        for rid, report in reports.items():
            self.assertEqual(report.ambiguous, [], f"{rid} 出现未预期澄清")
        self.assertEqual(len(service.state.records), len(case["records"]))


if __name__ == "__main__":
    unittest.main()
