"""政策措施及跨部门承诺契约。

本模块只定义领域数据结构与枚举，不含业务规则。所有结构均为不可变值对象，
状态演进由 ``transition_policy.service.PolicyTracker`` 以“整体替换 + 追加事件”的
方式完成，从而保证已完成里程碑等历史事实不被静默改写。
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class MeasureStatus(StrEnum):
    """措施的登记状态。某报告日是否“生效”还要结合生效区间推导。"""

    PROPOSED = "proposed"
    APPROVED = "approved"
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class CountingRole(StrEnum):
    """凭据被部门采用时的统计角色。

    PRIMARY：该凭据的实物量由本方全额计账；同一凭据至多一个主计方。
    SHARED：按 ``share`` 分摊比例计账，用于跨部门共享凭据。
    """

    PRIMARY = "primary"
    SHARED = "shared"


class ProposalType(StrEnum):
    AMEND = "amend"        # 延期 / 调整区间或前置
    REPLACE = "replace"    # 以新措施替代旧措施
    WITHDRAW = "withdraw"  # 撤销


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PolicyMeasure:
    """一项政策措施。

    ``owner`` 为责任主体标识（部门或机构）。``prerequisite_ids`` 可跨部门引用。
    生效区间为半开区间 ``[effective_from, effective_until)``；``None`` 表示长期有效。
    """

    measure_id: str
    owner: str
    effective_from: date
    effective_until: date | None
    prerequisite_ids: tuple[str, ...]
    status: MeasureStatus
    title: str = ""
    supersedes: str | None = None
    revision: int = 1


@dataclass(frozen=True)
class ResponsibleBody:
    """责任主体（能源、交通、制造等部门或其下属机构）。"""

    body_id: str
    name: str
    sector: str


@dataclass(frozen=True)
class Target:
    """措施承载的目标指标。

    ``caliber_id`` 为统计口径标识（计入范围、核算方法版本等）。只有口径相同或
    显式确认兼容的凭据才能计账，否则在凭据边界报告中单独标出。
    """

    target_id: str
    measure_id: str
    metric: str
    unit: str
    target_value: float
    due_date: date
    caliber_id: str
    baseline: float = 0.0


@dataclass(frozen=True)
class Milestone:
    """目标下的里程碑。``achieved_date`` 一旦写入即锁定，不允许再修改。"""

    milestone_id: str
    target_id: str
    label: str
    planned_date: date
    achieved_date: date | None = None
    evidence_id: str | None = None


@dataclass(frozen=True)
class Evidence:
    """进度凭据（不可变事实）。

    一份凭据描述某个报告日形成的实物量（如新建充电设施对应的减排量），
    可被多个部门通过 ``EvidenceAdoption`` 采用。
    """

    evidence_id: str
    title: str
    source_owner: str
    metric: str
    unit: str
    quantity: float
    report_date: date
    caliber_id: str


@dataclass(frozen=True)
class EvidenceAdoption:
    """部门对凭据的采用关系，即“共享 / 重复统计边界”的载体。

    ``counted_quantity`` 恒等于 ``Evidence.quantity * share``。
    口径不一致、多主计方、分摊比例之和超 1 等情况必须显式确认并留痕，
    在边界报告中以标志位标出，而不是被系统静默合并。
    """

    adoption_id: str
    evidence_id: str
    target_id: str
    adopted_by: str
    role: CountingRole
    share: float
    on_date: date
    caliber_mismatch: bool = False
    duplicate_confirmed: bool = False
    overlap_confirmed: bool = False


@dataclass(frozen=True)
class ReplacementDraft:
    """替代类提案中新措施的草案。"""

    measure_id: str
    title: str
    owner: str
    effective_from: date
    effective_until: date | None
    prerequisite_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeProposal:
    """变更提案。审批时对当时的前置措施有效性做快照。"""

    proposal_id: str
    measure_id: str
    ptype: ProposalType
    rationale: str
    submitted_by: str
    submitted_on: date
    status: ProposalStatus = ProposalStatus.PENDING
    new_effective_from: date | None = None
    new_effective_until: date | None = None
    new_prerequisite_ids: tuple[str, ...] | None = None
    replacement: ReplacementDraft | None = None
    decided_on: date | None = None
    decided_by: str | None = None
    prereq_snapshot: tuple[dict, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Event:
    """追加式领域事件，构成完整变更历史。"""

    seq: int
    on_date: date
    actor: str
    etype: str
    entities: tuple[str, ...]
    payload: dict
