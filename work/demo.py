"""端到端演示脚本（合成演练数据）。

在仓库根目录运行：
    python3 -I work/demo.py

脚本不修改 data/case.json，只在内存中重放完整调拨链路并打印关键结论。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from support import build_service, ingest_all  # noqa: E402
from relief.catalog import parse_clock  # noqa: E402


def main() -> int:
    service, case = build_service()
    reports = ingest_all(service, case)
    print("== 1. 原文接入 ==")
    for rid, report in reports.items():
        flags = []
        if report.skipped_duplicate:
            flags.append("幂等跳过")
        if report.ambiguous:
            flags.append(f"澄清：{report.ambiguous}")
        print(f"  {rid}: {len(report.applied_event_ids)} 个事件 " + " ".join(flags))

    print("\n== 2. 已确认缺口与条件替代 ==")
    board = service.gap_board()
    for gap in board.gaps:
        fb = (
            f"；后备 {gap.fallback['qty']}{gap.fallback['unit']}"
            f"{gap.fallback['material']}（期限 {gap.fallback['deadline']}）"
            if gap.fallback
            else ""
        )
        print(
            f"  {gap.site} 缺 {gap.material} {gap.requested_qty}{gap.unit}"
            f"（口径 {gap.mode}，状态 {gap.status}，未覆盖 {gap.shortfall}）{fb}"
        )

    print("\n== 3. 核验盘点与冲突 ==")
    cnt = next(c for c in service.state.counts.values() if c.source == "RS-E")
    service.verify_count(cnt.id, "核验员甲", valid_from=cnt.at)
    for conflict in service.state.conflicts.values():
        print(
            f"  冲突 {conflict.id}: {conflict.site} {conflict.material} "
            f"预计超送 {conflict.over_qty} -> 冻结待值班员"
        )

    print("\n== 4. 值班员裁决在途短装回库 ==")
    cfl = next(c for c in service.state.conflicts.values() if "撤销" in c.note)
    service.decide_conflict(
        cfl.id, "return_stock", officer="值班员丙", note="南坡水源恢复，1箱回库"
    )
    print(f"  water 当前可用库存：{service.state.available_stock('water')} 箱")

    print("\n== 5. 条件期限判定（12:30 仍未收到 F2） ==")
    service.advance_clock(parse_clock("12:30"))
    for line in service.state.request_lines.values():
        if line.site == "N":
            print(f"  {line.material} {line.qty}{line.unit}: {line.status.value}")

    print("\n== 6. 分配计划（含可行性解释） ==")
    plan = service.allocate()
    for note in plan.explanations:
        print("  " + note)
    for item in plan.unmet:
        print(f"  未满足 {item['site']} {item['material']}×{item['qty']}：{item['reason']}")

    print("\n== 7. 请求方查询（东台） ==")
    print(json.dumps(service.query_site("E"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
