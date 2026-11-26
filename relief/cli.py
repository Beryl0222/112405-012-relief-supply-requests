"""命令行入口。

示例：
  python -I -m relief.cli ingest data/case.json --events work/events.jsonl
  python -I -m relief.cli board --events work/events.jsonl --case data/case.json
  python -I -m relief.cli allocate --events work/events.jsonl --case data/case.json
  python -I -m relief.cli query S --events work/events.jsonl --case data/case.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import Registry, load_case
from .model import to_dict
from .service import ReliefService
from .store import EventStore


def _build_service(case_path: str, events_path: str | None) -> ReliefService:
    case = load_case(case_path)
    registry = Registry().with_case_objects(case["objects"])
    initial_stock = {
        item["item"]: item["qty"] for item in case["objects"]["stock"]
    }
    store = EventStore(events_path) if events_path else EventStore(None)
    return ReliefService(registry, initial_stock, store)


def _print(payload) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=to_dict)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公共卫生物资调拨办理链路")
    parser.add_argument("--case", default="data/case.json", help="资料包路径")
    parser.add_argument("--events", help="事件日志 JSONL 路径（持久化办理过程）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest", help="接入资料包中的全部原文并办理")
    sub.add_parser("board", help="输出缺口看板、库存、承诺与澄清")
    sub.add_parser("allocate", help="生成可兑现的装车分配计划")
    query = sub.add_parser("query", help="查询单个地点的缺口与在途承诺")
    query.add_argument("site")
    sub.add_parser("records", help="列出已接入原文")
    sub.add_parser("clarifications", help="列出未解决澄清")

    args = parser.parse_args(argv)
    service = _build_service(args.case, args.events)

    if args.command == "ingest":
        case = load_case(args.case)
        reports = service.ingest_case(case)
        _print([report.to_dict() for report in reports])
    elif args.command == "board":
        _print(service.gap_board().to_dict())
    elif args.command == "allocate":
        _print(service.allocate().to_dict())
    elif args.command == "query":
        _print(service.query_site(args.site))
    elif args.command == "records":
        _print([to_dict(record) for record in service.state.records.values()])
    elif args.command == "clarifications":
        _print([to_dict(c) for c in service.state.clarifications.values()])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
