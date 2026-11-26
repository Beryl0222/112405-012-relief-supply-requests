"""物资目录与登记对象的装载。

目录把原文里的中英文名称、包装口径、套装构成固定下来，
解析与占格计算都只认目录，避免“通用数量”掩盖型号差异。
"""
from __future__ import annotations

import json
from pathlib import Path

from .model import Material, Site, Vehicle

# 业务口径固定目录（合成演练数据，见 docs/materials.md）。
DEFAULT_CATALOG: tuple[Material, ...] = (
    Material(
        code="water",
        name_zh="水",
        name_en="bottled water",
        unit="箱",
        aliases=("饮水", "瓶装水", "饮用水", "water"),
        # 瓶是最小饮用单位；24 瓶 = 1 箱，箱占 2 格，散瓶不单独运输
        packages=(("瓶", 24, 2.0),),
        loose_slot=2.0,
    ),
    Material(
        code="F2",
        name_zh="F2滤芯",
        name_en="F2 cartridge",
        unit="个",
        aliases=("滤芯", "F2", "F2滤芯", "cartridge", "F2 cartridge"),
        # 整箱 6 个密封包装占 1 格；散件每个 1 格（与 RS-G 实装口径一致）
        packages=(("箱", 6, 1.0),),
        loose_slot=1.0,
        compatible_models=("F2",),
    ),
    Material(
        code="F1",
        name_zh="F1滤芯",
        name_en="F1 cartridge",
        unit="个",
        aliases=("F1", "F1滤芯", "F1 cartridge"),
        packages=(("箱", 6, 1.0),),
        loose_slot=1.0,
        compatible_models=("F1",),
    ),
    Material(
        code="pump",
        name_zh="泵",
        name_en="pump",
        unit="台",
        aliases=("水泵", "泵机", "pump"),
        loose_slot=2.0,
    ),
    Material(
        code="pump_filter_set",
        name_zh="泵滤套装",
        name_en="pump-filter set",
        unit="套",
        aliases=("泵滤设备", "套装", "泵滤", "set", "pump-filter"),
        # 整套含泵 1 台 + 滤芯 2 个，占 4 格
        packages=(),
        loose_slot=4.0,
        is_set=True,
        set_components=(("pump", 1), ("F2", 2)),
    ),
)

# 中文数字（资料文本只出现个位数，仍保留常见组合）。
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


class Registry:
    """物资、地点、车辆的登记查询。"""

    def __init__(
        self,
        materials: tuple[Material, ...] = DEFAULT_CATALOG,
        sites: tuple[Site, ...] = (),
        vehicles: tuple[Vehicle, ...] = (),
    ):
        self.materials = materials
        self.sites = sites
        self.vehicles = vehicles
        self._material_index: dict[str, Material] = {}
        for material in materials:
            for key in (
                material.code,
                material.name_zh,
                material.name_en,
                *material.aliases,
            ):
                self._material_index[key.lower()] = material
        self._site_index: dict[str, Site] = {}
        for site in sites:
            self._site_index[site.code.lower()] = site
            self._site_index[site.name.lower()] = site
            for alias in site.aliases:
                self._site_index[alias.lower()] = site

    def material(self, token: str) -> Material | None:
        return self._material_index.get(token.strip().lower())

    def require_material(self, token: str):
        material = self.material(token)
        if material is None:
            from .errors import UnknownMaterial

            raise UnknownMaterial(f"目录中没有物资：{token}")
        return material

    def site(self, token: str) -> Site | None:
        return self._site_index.get(token.strip().lower())

    def require_site(self, token: str):
        site = self.site(token)
        if site is None:
            from .errors import UnknownSite

            raise UnknownSite(f"登记中没有地点：{token}")
        return site

    def vehicle(self, code: str) -> Vehicle | None:
        for vehicle in self.vehicles:
            if vehicle.code == code:
                return vehicle
        return None

    def with_case_objects(self, objects: dict) -> "Registry":
        """从 case.json 的 objects 段补充地点与车辆（物资目录保持业务口径）。"""
        sites = tuple(
            Site(
                code=item["id"],
                name=item["name"],
                aliases=tuple(item.get("aliases", ())),
            )
            for item in objects.get("sites", [])
        )
        vehicles = tuple(
            Vehicle(
                code=item["id"],
                capacity=float(item["capacity"]),
                window_start=parse_clock(item["window"].split("-")[0])
                if "window" in item
                else None,
                window_end=parse_clock(item["window"].split("-")[1])
                if "window" in item
                else None,
                reachable=tuple(item["sites"]),
            )
            for item in objects.get("vehicles", [])
        )
        return Registry(self.materials, sites, vehicles)


def parse_clock(text: str) -> int | None:
    """“09:30” -> 570（距当日 00:00 分钟数）。"""
    text = text.strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def format_clock(minutes: int | None) -> str:
    if minutes is None:
        return ""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def cn_number(token: str) -> int | None:
    """解析“两/十/二十四”等资料中出现的中文数词。"""
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if token.startswith("十") and len(token) == 2:
        rest = _CN_DIGITS.get(token[1])
        return 10 + rest if rest is not None else None
    if token.endswith("十") and len(token) == 2:
        first = _CN_DIGITS.get(token[0])
        return first * 10 if first is not None else None
    if "十" in token and len(token) == 3:
        first, _, rest = token
        if first in _CN_DIGITS and rest in _CN_DIGITS:
            return _CN_DIGITS[first] * 10 + _CN_DIGITS[rest]
    return None


def load_case(path: str | Path) -> dict:
    """以只读方式读取资料包。调用方应在隔离目录中用 python -I 运行。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))
