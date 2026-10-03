"""JSON 文件仓储：纯标准库、离线可用。

写操作先写临时文件再原子替换。仓储不做业务校验，规则全部在 service 层。
序列化采用 dataclasses.asdict + 自定义 date/tuple/enum 编解码。
"""

import json
import os
import tempfile
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .contracts import (
    ChangeProposal,
    CountingRole,
    Evidence,
    EvidenceAdoption,
    Event,
    MeasureStatus,
    Milestone,
    PolicyMeasure,
    ProposalStatus,
    ProposalType,
    ReplacementDraft,
    ResponsibleBody,
    Target,
)

COLLECTIONS = (
    "bodies",
    "measures",
    "targets",
    "milestones",
    "evidence",
    "adoptions",
    "proposals",
    "events",
)

_KEY_FIELDS = {
    "bodies": "body_id",
    "measures": "measure_id",
    "targets": "target_id",
    "milestones": "milestone_id",
    "evidence": "evidence_id",
    "adoptions": "adoption_id",
    "proposals": "proposal_id",
    "events": "seq",
}

_KINDS = {
    "bodies": "ResponsibleBody",
    "measures": "PolicyMeasure",
    "targets": "Target",
    "milestones": "Milestone",
    "evidence": "Evidence",
    "adoptions": "EvidenceAdoption",
    "proposals": "ChangeProposal",
    "events": "Event",
}


def _encode(value):
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, (tuple, list)):
        return {"__tuple__" if isinstance(value, tuple) else "__list__": [_encode(v) for v in value]}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if hasattr(value, "_name_"):
        return str(value)
    return value


def _decode(value):
    if isinstance(value, dict):
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
        if "__tuple__" in value:
            return tuple(_decode(v) for v in value["__tuple__"])
        if "__list__" in value:
            return [_decode(v) for v in value["__list__"]]
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _build(kind: str, data: dict):
    data = dict(data)
    if kind == "PolicyMeasure":
        data["status"] = MeasureStatus(data["status"])
        data["prerequisite_ids"] = tuple(data.get("prerequisite_ids", ()))
    elif kind == "EvidenceAdoption":
        data["role"] = CountingRole(data["role"])
    elif kind == "ChangeProposal":
        data["ptype"] = ProposalType(data["ptype"])
        data["status"] = ProposalStatus(data["status"])
        data["new_prerequisite_ids"] = (
            tuple(data["new_prerequisite_ids"]) if data.get("new_prerequisite_ids") is not None else None
        )
        if data.get("replacement"):
            data["replacement"] = _build("ReplacementDraft", data["replacement"])
        data["prereq_snapshot"] = tuple(dict(s) for s in data.get("prereq_snapshot", ()))
    elif kind == "Event":
        data["entities"] = tuple(data.get("entities", ()))
    return _BUILDERS[kind](**data)


_BUILDERS = {
    "PolicyMeasure": PolicyMeasure,
    "ResponsibleBody": ResponsibleBody,
    "Target": Target,
    "Milestone": Milestone,
    "Evidence": Evidence,
    "EvidenceAdoption": EvidenceAdoption,
    "ReplacementDraft": ReplacementDraft,
    "ChangeProposal": ChangeProposal,
    "Event": Event,
}


class JsonRepository:
    """以单个 JSON 文件保存全部集合，按主键索引。path=None 表示纯内存模式。"""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else None
        self._data: dict[str, dict[str, dict]] = {name: {} for name in COLLECTIONS}
        if self.path and self.path.exists():
            self.load()

    def load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._data = {name: dict(raw.get(name, {})) for name in COLLECTIONS}

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {name: _encode(store) for name, store in self._data.items()}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def put(self, collection: str, entity) -> None:
        key = str(getattr(entity, _KEY_FIELDS[collection]))
        self._data[collection][key] = asdict(entity)

    def all(self, collection: str):
        kind = _KINDS[collection]
        return [_build(kind, _decode(raw)) for raw in self._data[collection].values()]

    def get(self, collection: str, key: str):
        raw = self._data[collection].get(str(key))
        if raw is None:
            return None
        return _build(_KINDS[collection], _decode(dict(raw)))

    def delete(self, collection: str, key: str) -> None:
        self._data[collection].pop(str(key), None)

    def contains(self, collection: str, key: str) -> bool:
        return str(key) in self._data[collection]

    def next_event_seq(self) -> int:
        return len(self._data["events"]) + 1
