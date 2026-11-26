"""事件存储：只追加的 JSONL 日志与回放。

不把任何翻译后的优先级表作为事实来源；重放日志即可完整重建调拨链路。
"""
from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path

from . import events as event_module
from .events import Event
from .model import to_dict


class EventStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: Event) -> None:
        if self.path is None:
            return
        payload = {"type": type(event).__name__, "data": to_dict(event)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def load_all(self) -> list[Event]:
        if self.path is None or not self.path.exists():
            return []
        out: list[Event] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            event_type = getattr(event_module, payload["type"])
            out.append(_hydrate(event_type, payload["data"]))
        return out


def _hydrate(event_type, data: dict) -> Event:
    """按事件 dataclass 字段重建；未知字段（旧日志兼容）忽略。"""
    names = {f.name for f in fields(event_type)}
    kwargs = {key: value for key, value in data.items() if key in names}
    return event_type(**kwargs)


def event_to_json(event: Event) -> str:
    return json.dumps(
        {"type": type(event).__name__, "data": to_dict(event)},
        ensure_ascii=False,
    )
