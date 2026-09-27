"""政策措施及跨部门承诺契约。"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class MeasureStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class PolicyMeasure:
    measure_id: str
    owner: str
    effective_from: date
    effective_until: date | None
    prerequisite_ids: tuple[str, ...]
    status: MeasureStatus
