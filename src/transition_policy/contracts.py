"""政策措施及跨部门承诺契约（领域对象）。

所有领域对象均为不可变 dataclass：变更不是原地修改，而是通过
``dataclasses.replace`` 生成新版本并追加事件，保证历史可追溯。
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class MeasureStatus(StrEnum):
    PROPOSED = "proposed"      # 已登记，尚未审批
    APPROVED = "approved"      # 已审批但尚未到生效日
    ACTIVE = "active"          # 报告日处于生效区间内
    EXPIRED = "expired"        # 生效区间自然届满
    WITHDRAWN = "withdrawn"    # 主动撤回
    SUPERSEDED = "superseded"  # 被新措施替代


class ProposalType(StrEnum):
    ADOPT = "adopt"            # 审批通过一项措施
    EXTEND = "extend"          # 延期（延长生效区间 / 调整未完成里程碑）
    REPLACE = "replace"        # 以新措施替代旧措施
    REPOINT = "repoint"        # 重新确认前置引用（增删前置措施）
    WITHDRAW = "withdraw"      # 撤回措施


class ProposalStatus(StrEnum):
    RAISED = "raised"
    APPROVED = "approved"
    REJECTED = "rejected"


class MilestoneStatus(StrEnum):
    PLANNED = "planned"
    COMPLETED = "completed"


class SharingBoundary(StrEnum):
    EXCLUSIVE = "exclusive"        # 单一部门采用
    SHARED = "shared"              # 多部门采用，份额之和不超过 100%
    DOUBLE_COUNT = "double_count"  # 多部门主张份额之和超过 100%，存在重复统计


@dataclass(frozen=True)
class ResponsibleBody:
    """责任主体（部门或机构）。"""

    body_id: str
    name: str
    department: str  # energy / transport / manufacturing ...


@dataclass(frozen=True)
class PrerequisiteSnapshot:
    """审批发生时对前置措施状态的留痕。

    一次审批只能基于当时有效的前置措施，审批后前置措施被撤回，
    不影响本次审批的历史效力，但会在依赖方产生阻塞记录。
    """

    measure_id: str
    status: str
    effective_from: date
    effective_until: date | None
    captured_on: date


@dataclass(frozen=True)
class PolicyMeasure:
    measure_id: str
    owner: str
    effective_from: date
    effective_until: date | None
    prerequisite_ids: tuple[str, ...]
    status: MeasureStatus
    title: str = ""
    approval_date: date | None = None
    withdrawn_on: date | None = None
    withdrawal_reason: str | None = None
    superseded_on: date | None = None
    replaces_id: str | None = None
    approval_basis: tuple[PrerequisiteSnapshot, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Milestone:
    """目标指标下的里程碑。完成后其日期与凭据被冻结，不允许被延期提案静默重排。"""

    milestone_id: str
    target_id: str
    name: str
    due_date: date
    status: MilestoneStatus = MilestoneStatus.PLANNED
    completed_date: date | None = None
    evidence_id: str | None = None
    completed_by: str | None = None


@dataclass(frozen=True)
class Target:
    """目标指标（量化承诺）。"""

    target_id: str
    measure_id: str
    name: str
    unit: str
    target_value: float
    due_date: date
    baseline: float | None = None
    additive_across_departments: bool = True
    milestones: tuple[Milestone, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EvidenceAdoption:
    """一个部门对一份进度凭据的采用记录。

    share 为该部门主张凭据数值的比例（0~1）。不同部门采用同一凭据时，
    由服务层计算共享 / 重复统计边界。
    """

    department: str
    share: float
    scope_note: str
    adopted_on: date


@dataclass(frozen=True)
class ProgressEvidence:
    """进度凭据（凭证、监测数据、验收文件等）。"""

    evidence_id: str
    target_id: str
    title: str
    kind: str
    reference: str
    origin_department: str
    value: float
    unit: str
    period_start: date
    period_end: date
    adoptions: tuple[EvidenceAdoption, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ChangeProposal:
    """变更提案。payload 结构随 proposal_type 不同：

    ADOPT: {}
    EXTEND: {"new_effective_until": ISO, "milestone_reschedules": {mid: ISO}}
    REPLACE: {"new_measure": {measure_id, title, owner, effective_from,
             effective_until, prerequisite_ids}}
    REPOINT: {"add_prerequisite_ids": [...], "remove_prerequisite_ids": [...]}
    WITHDRAW: {"reason": str}
    """

    proposal_id: str
    proposal_type: ProposalType
    measure_id: str
    payload: dict
    status: ProposalStatus
    raised_by: str
    raised_on: date
    rationale: str
    decided_by: str | None = None
    decided_on: date | None = None
    decision_note: str | None = None


@dataclass(frozen=True)
class ImpactItem:
    """撤回 / 替代对单个依赖方的影响。"""

    dependent_measure_id: str
    owner: str
    blocker: str
    open_milestones: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    # 已完成里程碑只做留痕展示，绝不因上游撤回而被重排
    preserved_completed_milestones: tuple[tuple[str, str, str], ...] = field(
        default_factory=tuple
    )


@dataclass(frozen=True)
class ImpactReport:
    source_measure_id: str
    change_type: str
    occurred_on: date
    items: tuple[ImpactItem, ...]
