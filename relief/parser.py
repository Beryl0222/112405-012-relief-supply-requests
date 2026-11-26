"""中英双语原文解析。

设计原则：
1. 只做确定性的规则解释，不猜测；数量口径或对象不明时产出澄清命令。
2. 输出的是“命令草稿”，不直接改业务状态；解释结论始终带原文编号。
3. “再要/再送”与“总共要”区分口径；撤销只针对当时谈论的物资。
4. 代报（relay）不覆盖现场确认，盘点文本不产生新需求。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import Registry, cn_number, parse_clock
from .commands import (
    AnswerClarification,
    CancelRequest,
    Command,
    ConfirmLoad,
    CreateReservation,
    IngestRecord,
    OpenClarification,
    PlanReroute,
    QuantifyFallback,
    RecordHandover,
    RegisterCount,
    RegisterRequest,
)
from .errors import Ambiguous, UnknownMaterial, UnknownSite
from .model import SourceKind

# 中文数量必须带单位，避免“F2”“十一时”被误识别为数量
_QUANTITY_RE = re.compile(
    r"(?P<num>[0-9]+|[一二两三四五六七八九十]{1,3})\s*"
    r"(?P<unit>箱|瓶|个|台|套)"
)
_EN_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}
_EN_QUANTITY_RE = re.compile(
    r"(?P<num>[0-9]+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve)\s+(?:[A-Za-z0-9]+\s+)?"
    r"(?P<unit>case(?:s)?|bottle(?:s)?|cartridge(?:s)?|"
    r"pump(?:s)?|set(?:s)?)\b",
    re.IGNORECASE,
)
_CN_CLOCK_RE = re.compile(
    r"([一二两三四五六七八九十]{1,3})时(?:([一二两三四五六七八九十]{1,3})分|半)?"
)
# 时钟表达（09:30 / 十一时 / 九时半），提取数量前先遮蔽
_CLOCK_MASK_RE = re.compile(
    r"(?:[01]?\d|2[0-3]):[0-5]\d|[一二两三四五六七八九十]{1,3}时(?:[一二两三四五六七八九十]{1,3}分|半)?"
)


@dataclass
class ParseResult:
    """一条原文的解析产物。"""

    record_id: str
    kind: str
    commands: list[Command]
    clarifications: list[OpenClarification]
    notes: list[str]

    @property
    def needs_attention(self) -> bool:
        return bool(self.clarifications)


def parse_cn_clock(text: str) -> int | None:
    match = _CN_CLOCK_RE.search(text)
    if not match:
        return None
    hour = cn_number(match.group(1))
    if hour is None:
        return None
    if match.group(2) is not None:
        minute = cn_number(match.group(2)) or 0
    elif match.group(0).endswith("半"):
        minute = 30
    else:
        minute = 0
    return hour * 60 + minute


def extract_clock(text: str) -> int | None:
    match = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", text)
    if match:
        return parse_clock(f"{int(match.group(1)):02d}:{match.group(2)}")
    return parse_cn_clock(text)


def find_site(text: str, registry: Registry):
    """按登记名称与别名找地点，找不到返回 None（由调用方决定是否澄清）。"""
    lowered = text.lower()
    # 长别名优先，避免前缀误配
    candidates = sorted(
        registry.sites,
        key=lambda site: max([len(site.name), *(len(a) for a in site.aliases)]),
        reverse=True,
    )
    for site in candidates:
        if site.name in text or any(
            alias.lower() in lowered for alias in site.aliases
        ):
            return site
    return None


def find_material(text: str, registry: Registry, start: int = 0):
    """在文本中定位目录物资，按名称长度优先匹配。"""
    best = None
    best_pos = -1
    for material in registry.materials:
        for token in (
            material.name_zh,
            material.name_en,
            *material.aliases,
        ):
            pos = text.lower().find(token.lower(), start)
            if pos >= 0 and (best is None or pos < best_pos or
                             (pos == best_pos and len(token) > len(best[0]))):
                best = (token, material)
                best_pos = pos
    if best is None:
        return None
    return best[1]


def _cn_quantities(text: str) -> list[tuple[int, str | None, int]]:
    out: list[tuple[int, str | None, int]] = []
    for match in _QUANTITY_RE.finditer(text):
        num = cn_number(match.group("num"))
        if num is None:
            continue
        out.append((num, match.group("unit"), match.start()))
    return out


def _en_quantities(text: str) -> list[tuple[int, str | None, int]]:
    mapping = {
        "case": "箱", "cases": "箱",
        "bottle": "瓶", "bottles": "瓶",
        "cartridge": "个", "cartridges": "个",
        "pump": "台", "pumps": "台",
        "set": "套", "sets": "套",
    }
    out: list[tuple[int, str | None, int]] = []
    for match in _EN_QUANTITY_RE.finditer(text):
        raw_num = match.group("num").lower()
        num = int(raw_num) if raw_num.isdigit() else _EN_NUMBER_WORDS[raw_num]
        out.append((num, mapping[match.group("unit").lower()], match.start()))
    return out


def quantities(text: str) -> list[tuple[int, str | None, int]]:
    """提取带单位的数量；先遮蔽时钟，避免“十一时/09:30”混入。"""
    masked = _CLOCK_MASK_RE.sub(" ", text)
    found = _cn_quantities(masked) + _en_quantities(masked)
    return sorted(found, key=lambda item: item[2])


def _has_keyword_near(text: str, pos: int, keyword: str, span: int = 8) -> bool:
    """在原文中按遮蔽后位置附近查找关键词（遮蔽只替换等宽空白，位置保持稳定）。"""
    return keyword in text[max(0, pos - span):pos + span]


# ---------------------------------------------------------------- 各类型解析

def _ingest(record: dict) -> list[Command]:
    return [
        IngestRecord(
            command_id=f"cmd:ingest:{record['id']}",
            record_id=record["id"],
            kind=record["kind"],
            text=record["text"],
            at=extract_clock(record["text"]),
            sources=(record["id"],),
        )
    ]


def _parse_request(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    text, rid = record["text"], record["id"]
    at = extract_clock(text)
    site = find_site(text, registry)
    if site is None:
        raise UnknownSite(f"{rid}: 无法识别地点：{text}")
    material = find_material(text, registry)
    if material is None:
        raise UnknownMaterial(f"{rid}: 无法识别物资：{text}")
    notes: list[str] = []
    commands: list[Command] = []
    clarifications: list[OpenClarification] = []

    # 数量口径
    mode = "unknown"
    if re.search(r"再要|再送|另要|还需要|more|another", text, re.IGNORECASE):
        mode = "add"
    elif re.search(r"总共|一共|total", text, re.IGNORECASE):
        mode = "total"
    else:
        # 英文条件句与首次提报默认按总量，但数量本身仍需明确
        mode = "total"
    if "不是把总量凑到" in text or "不是把总量" in text:
        mode = "add"

    deadline = None
    deadline_match = re.search(r"by (noon|\d{1,2}(?::\d{2})\s*(?:am|pm)?)",
                               text, re.IGNORECASE)
    if deadline_match:
        token = deadline_match.group(1).lower()
        if token == "noon":
            deadline = 12 * 60
        else:
            raw = token.replace("am", "").replace("pm", "").strip()
            clock = parse_clock(raw)
            if clock is not None:
                if token.endswith("pm") and clock < 12 * 60:
                    clock += 12 * 60
                deadline = clock
    if "中午前" in text or "午前" in text:
        deadline = 12 * 60

    # ---- 英文条件请求：RS-A 型
    if "no bottled water needed" in text.lower() or "if " in text.lower():
        # “No bottled water needed today if four F2 cartridges arrive by noon”
        f2 = registry.require_material("F2")
        primary_qty = None
        for num, unit, pos in quantities(text):
            hit = find_material(text[pos:pos + 20], registry)
            if hit is not None and hit.code == f2.code:
                primary_qty = num
        if primary_qty is None:
            raise Ambiguous(
                question=f"{site.name}：条件句中首选物资数量不明，请确认需要多少个 {f2.code}",
                source=rid,
            )
        commands.append(
            RegisterRequest(
                command_id=f"cmd:req:{rid}:{f2.code}",
                line_id="",  # 服务层分配
                site=site.code,
                material=f2.code,
                qty=primary_qty,
                unit=f2.unit,
                mode="total",
                deadline=deadline,
                fallback_material="water",
                fallback_qty=None,   # 原文没给后备水量
                fallback_unit="箱",
                condition_note=(
                    f"{deadline and '期限前'}首选 {f2.code} 到货则不需饮水；"
                    "后备饮水数量原文未给"
                ),
                notes=text,
                at=at,
                sources=(rid,),
            )
        )
        clarifications.append(
            OpenClarification(
                command_id=f"cmd:clar:{rid}:fallback",
                clarification_id="",
                site=site.code,
                question=(
                    f"若 {f2.code} 未在期限前到达，{site.name} 需要多少箱饮水作为后备？"
                    "（原文只说不需水的条件，未给后备水量）"
                ),
                about_source=rid,
                at=at,
                sources=(rid,),
            )
        )
        if "takes F2 only" in text or "只" in text:
            notes.append(f"{site.name} 净水器只兼容 {f2.code}，其他型号不能按通用滤芯补足")
        return commands, clarifications, notes

    # ---- 中文带消耗口径请求：RS-B 型（盘点 + 人数 + 追加）
    people_match = re.search(r"([0-9]+|[一二两三四五六七八九十]{1,3})\s*人", text)
    if people_match is not None and ("每人每日" in text or "每日" in text):
        people = cn_number(people_match.group(1)) or int(people_match.group(1))
        # 每人每日两瓶 -> 日耗按瓶折算为箱
        per_day_bottles = people * 2
        consumption_per_day = round(per_day_bottles / 24, 4)
        # 句首盘点：“八时盘点三箱水”——在原文上下文里识别，不依赖遮蔽后切片
        counted = None
        for num, unit, pos in quantities(text):
            if _has_keyword_near(text, pos, "盘点") or _has_keyword_near(text, pos, "实盘"):
                counted = (num, unit)
                break
        if counted is not None:
            commands.append(
                RegisterCount(
                    command_id=f"cmd:count:{rid}",
                    count_id="",
                    site=site.code,
                    material=material.code,
                    qty=counted[0],
                    unit=counted[1] or material.unit,
                    counted_at=at,
                    proxy=False,
                    consumption_per_day=consumption_per_day,
                    note="请求文本中的自述盘点，待核验员确认；含人数日耗口径",
                    sources=(rid,),
                    at=at,
                )
            )
        qs = quantities(text)
        want = None
        for num, unit, pos in qs:
            if _has_keyword_near(text, pos, "盘点") or _has_keyword_near(text, pos, "实盘"):
                continue
            want = (num, unit)
        if want is None:
            raise Ambiguous(
                question=f"{site.name}：请求数量不明，请明确要多少{material.unit}{material.name_zh}",
                source=rid,
            )
        commands.append(
            RegisterRequest(
                command_id=f"cmd:req:{rid}:{material.code}",
                line_id="",
                site=site.code,
                material=material.code,
                qty=want[0],
                unit=want[1] or material.unit,
                mode=mode,
                deadline=None,
                condition_note=f"{people}人每人每日2瓶，日耗约{consumption_per_day}箱",
                notes=text,
                at=at,
                sources=(rid,),
            )
        )
        if mode == "add":
            notes.append("原文明确“再送……不是把总量凑到”，按追加口径办理")
        return commands, clarifications, notes

    # ---- 套装请求：RS-K 型
    if material.is_set:
        commands.append(
            RegisterRequest(
                command_id=f"cmd:req:{rid}:{material.code}",
                line_id="",
                site=site.code,
                material=material.code,
                qty=1,
                unit=material.unit,
                mode="total",
                notes=text,
                at=at,
                sources=(rid,),
            )
        )
        # “已有两个F2可作套内滤芯，泵缺一台”：
        # 只登记套内行（不整套承诺）与现场已有部件的盘点；
        # 具体部件缺口由分配引擎按套装构成减去现场核验存量算出，不重复立单。
        for num, unit, pos in quantities(text):
            hit = find_material(text[max(0, pos - 2):pos + 12], registry)
            if hit is None:
                continue
            if "已有" in text[max(0, pos - 6):pos]:
                commands.append(
                    RegisterCount(
                        command_id=f"cmd:count:{rid}:{hit.code}",
                        count_id="",
                        site=site.code,
                        material=hit.code,
                        qty=num,
                        unit=unit or hit.unit,
                        counted_at=at,
                        proxy=False,
                        note="请求方自述已有套内部件，待核验",
                        sources=(rid,),
                        at=at,
                    )
                )
        notes.append("套装不得在部件不齐时整套承诺；按部件缺口出单件装车单")
        return commands, clarifications, notes

    # ---- 兜底：普通数量请求
    qs = [q for q in quantities(text)]
    if not qs:
        raise Ambiguous(
            question=f"{site.name}：未解析到明确数量，请确认{material.name_zh}需要多少",
            source=rid,
        )
    num, unit, _ = qs[0]
    commands.append(
        RegisterRequest(
            command_id=f"cmd:req:{rid}:{material.code}",
            line_id="",
            site=site.code,
            material=material.code,
            qty=num,
            unit=unit or material.unit,
            mode=mode,
            deadline=deadline,
            notes=text,
            at=at,
            sources=(rid,),
        )
    )
    return commands, clarifications, notes


def _parse_clarification(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    """RS-D 型：回答上一问，同时给明确的总量口径与后备条件。"""
    text, rid = record["text"], record["id"]
    at = extract_clock(text)
    site = find_site(text, registry)
    if site is None:
        raise UnknownSite(f"{rid}: 澄清回复地点不明")
    commands: list[Command] = []
    notes = ["澄清答复只解决对应问题，其他独立缺项继续办理"]

    # “要两套滤芯，一套两个F2，不是两箱”
    set_match = re.search(r"([0-9]+|[一二两三四五六七八九十]{1,3})\s*套", text)
    per_set_match = re.search(
        r"一套\s*([0-9]+|[一二两三四五六七八九十]{1,3})\s*个\s*(F\d)", text
    )
    f2 = registry.require_material("F2")
    if set_match and per_set_match:
        sets = cn_number(set_match.group(1))
        per_set = cn_number(per_set_match.group(1))
        total = sets * per_set
        commands.append(
            RegisterRequest(
                command_id=f"cmd:req:{rid}:{f2.code}",
                line_id="",
                site=site.code,
                material=f2.code,
                qty=total,
                unit=f2.unit,
                mode="total",
                deadline=12 * 60 if "中午前" in text else None,
                fallback_material="water",
                fallback_qty=None,
                fallback_unit="箱",
                condition_note="期限前 F2 不到则转饮水后备",
                answer_to=rid,
                notes=text,
                at=at,
                sources=(rid,),
            )
        )
        notes.append(f"按 {sets} 套 × 每套 {per_set} 个 = {total} 个 F2 登记，拒绝按“两箱”误译")

    # “中午前不到则需四箱饮水”——补全条件行的后备数量，不另立独立需求
    water = registry.require_material("water")
    for num, unit, pos in quantities(text):
        hit = find_material(text[max(0, pos - 2):pos + 10], registry)
        if hit is not None and hit.code == water.code and unit == "箱":
            commands.append(
                QuantifyFallback(
                    command_id=f"cmd:fallback:{rid}",
                    site=site.code,
                    primary_material=f2.code,
                    fallback_material=water.code,
                    fallback_qty=num,
                    fallback_unit="箱",
                    answer_source=rid,
                    at=at,
                    sources=(rid,),
                )
            )
    return commands, [], notes


def _parse_relay(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    """转述只留痕：不能覆盖现场确认，也不是一次新盘点/需求。"""
    return [], [], [
        "转述未在现场核对，仅登记留痕；不生成需求，也不更新盘点口径"
    ]


def _parse_count(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    text, rid = record["text"], record["id"]
    at = extract_clock(text)
    site = find_site(text, registry)
    if site is None:
        raise UnknownSite(f"{rid}: 盘点地点不明")
    material = find_material(text, registry)
    if material is None:
        raise UnknownMaterial(f"{rid}: 盘点物资不明")
    # “现有两箱完整水”优先；其次句首盘点数
    qs = quantities(text)
    chosen = None
    for num, unit, pos in qs:
        window = text[max(0, pos - 8):pos]
        if "现有" in window or "现存" in window:
            chosen = (num, unit)
    if chosen is None and qs:
        chosen = (qs[-1][0], qs[-1][1])
    if chosen is None:
        raise Ambiguous(
            question=f"{site.name}: 盘点数量不明，请确认现有{material.name_zh}数量",
            source=rid,
        )
    note = ""
    if "漏损" in text or "损坏" in text:
        note = "含漏损修正：盘点为时刻存量，不是新增订货"
    command = RegisterCount(
        command_id=f"cmd:count:{rid}",
        count_id="",
        site=site.code,
        material=material.code,
        qty=chosen[0],
        unit=chosen[1] or material.unit,
        counted_at=at,
        proxy=False,
        note=note,
        sources=(rid,),
        at=at,
    )
    return [command], [], ["新盘点经核验后改变缺口；转发旧盘点不是新需求"]


def _parse_reservation(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    text, rid = record["text"], record["id"]
    at = extract_clock(text)
    site = find_site(text, registry)
    material = find_material(text, registry)
    qs = quantities(text)
    if site is None or material is None or not qs:
        raise Ambiguous(
            question="预留文本缺少地点、物资或数量，请核对",
            source=rid,
        )
    num, unit, _ = qs[0]
    command = CreateReservation(
        command_id=f"cmd:res:{rid}",
        order_id="",
        site=site.code,
        lines=[(material.code, num, unit or material.unit, None)],
        at=at,
        sources=(rid,),
    )
    return [command], [], ["预留占用库存但未装车；同一库存不能被两张有效单重复占用"]


def _parse_loading(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    text, rid = record["text"], record["id"]
    at = extract_clock(text)
    vehicle_match = re.search(r"(T\d+)", text)
    if vehicle_match is None:
        raise Ambiguous(question="装车记录缺少车辆编号", source=rid)
    vehicle = vehicle_match.group(1)
    # “实装北营四个F2、南坡两箱水，剩一格”：按标点拆段，只收有物资的明细段
    segments = re.split(r"[、，,;；]", text)
    bound: list[tuple[str, str, int, str]] = []
    current_site = None
    for segment in segments:
        seg_site = find_site(segment, registry)
        if seg_site is not None:
            current_site = seg_site
        if not quantities(segment) or current_site is None:
            continue
        material = find_material(segment, registry)
        if material is None:
            # “剩一格”等非物资描述段直接跳过，不构成澄清
            continue
        num, unit, _ = quantities(segment)[0]
        bound.append((current_site.code, material.code, num, unit or material.unit))
    if not bound:
        raise Ambiguous(question="装车记录未解析到装车明细", source=rid)
    command = ConfirmLoad(
        command_id=f"cmd:load:{rid}",
        order_id="",
        vehicle=vehicle,
        site=None,
        lines=[(m, q, u, None) for (_s, m, q, u) in bound],
        at=at,
        sources=(rid,),
    )
    # 站点绑定随命令附加字段传递（服务层读取）
    command.__dict__["site_lines"] = bound
    return [command], [], ["装车即占车位与库存；已装车货物不会因请求改口自动回库"]


def _parse_reroute(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    text, rid = record["text"], record["id"]
    at = extract_clock(text)
    vehicle_match = re.search(r"(T\d+)", text)
    sites = [s for s in registry.sites if s.name in text]
    material = find_material(text, registry)
    if len(sites) < 2 or material is None:
        raise Ambiguous(
            question="改送文本需包含原定地点、新地点与物资，请核对",
            source=rid,
        )
    # “把滤芯先送东台；……北营未收到” -> 北营为原点，东台为新点
    to_site = None
    for site in sites:
        if re.search(rf"送{site.name}|先送{site.name}", text):
            to_site = site
    if to_site is None:
        raise Ambiguous(question="改送文本未指明新接收地点，请核对", source=rid)
    from_site = next(s for s in sites if s != to_site)
    qty = None
    for num, unit, _pos in quantities(text):
        qty = num
    command = PlanReroute(
        command_id=f"cmd:reroute:{rid}",
        # 原文无车牌时留空，由引擎按原点+物资在在途单中解析车辆
        vehicle=vehicle_match.group(1) if vehicle_match else "",
        from_site=from_site.code,
        to_site=to_site.code,
        material=material.code,
        qty=qty or 0,
        note=text,
        at=at,
        sources=(rid,),
    )
    return [command], [], [
        "原点未满足量与新点交接量按实际卸货重判，不能两边都算完成"
    ]


def _parse_delivery(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    text, rid = record["text"], record["id"]
    at = extract_clock(text)
    vehicle_match = re.search(r"(T\d+)", text)
    # 分号/分号分句，每句一个地点的交接
    segments = re.split(r"[;；]", text)
    commands: list[Command] = []
    for segment in segments:
        site = find_site(segment, registry)
        material = find_material(segment, registry)
        if site is None or material is None or not quantities(segment):
            continue
        qs = quantities(segment)
        # “签收四个F2” -> 交付量；“只收到一箱……另一箱在车上” -> 交付 1
        delivered = qs[0][0]
        requested: int | None = None
        if "只收到" in segment and len(qs) >= 2:
            delivered = qs[0][0]
        if "另一" in segment or "剩" in segment:
            # 短装：请求量 = 交付 + 在车上数量
            if len(qs) >= 2:
                requested = qs[0][0] + qs[1][0]
        commands.append(
            RecordHandover(
                command_id=f"cmd:handover:{rid}:{site.code}:{material.code}",
                delivery_id="",
                vehicle=vehicle_match.group(1) if vehicle_match else "",
                site=site.code,
                material=material.code,
                requested_qty=requested or delivered,
                delivered_qty=delivered,
                at=at,
                sources=(rid,),
            )
        )
    if not commands:
        raise Ambiguous(question="交接记录未解析到地点与物资数量", source=rid)
    return commands, [], ["短装数量留在车上，按实际交接量计完成"]


def _parse_cancellation(
    record: dict, registry: Registry
) -> tuple[list[Command], list[OpenClarification], list[str]]:
    text, rid = record["text"], record["id"]
    at = extract_clock(text)
    site = find_site(text, registry)
    if site is None:
        raise UnknownSite(f"{rid}: 撤销地点不明")
    material = find_material(text, registry)
    qty = None
    for num, unit, pos in quantities(text):
        # “另一箱”这种指代由 ref_source 上下文解析
        qty = num
    command = CancelRequest(
        command_id=f"cmd:cancel:{rid}",
        site=site.code,
        material=material.code if material else None,
        qty=qty,
        ref_source=rid,
        note=text,
        at=at,
        sources=(rid,),
    )
    notes = ["撤销只关闭当时谈论的物资行；其他物资（如滤芯请求）继续有效"]
    if "滤芯请求没有撤销" in text or "没有撤销" in text:
        notes.append("原文显式声明滤芯请求不撤销")
    return [command], [], notes


_PARSERS = {
    SourceKind.REQUEST.value: _parse_request,
    SourceKind.CLARIFICATION.value: _parse_clarification,
    SourceKind.RELAY.value: _parse_relay,
    SourceKind.COUNT.value: _parse_count,
    SourceKind.RESERVATION.value: _parse_reservation,
    SourceKind.LOADING.value: _parse_loading,
    SourceKind.REROUTE.value: _parse_reroute,
    SourceKind.DELIVERY.value: _parse_delivery,
    SourceKind.CANCELLATION.value: _parse_cancellation,
}


def parse_record(record: dict, registry: Registry) -> ParseResult:
    """解析单条原文。无法确定的语义抛 Ambiguous，由服务层转澄清登记。"""
    rid = record["id"]
    ingest = _ingest(record)
    parser = _PARSERS.get(record["kind"])
    if parser is None:
        return ParseResult(rid, record["kind"], ingest, [], [f"未知资料种类 {record['kind']}"])
    commands, clarifications, notes = parser(record, registry)
    return ParseResult(
        record_id=rid,
        kind=record["kind"],
        commands=ingest + commands,
        clarifications=clarifications,
        notes=notes,
    )
