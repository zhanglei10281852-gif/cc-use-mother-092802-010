"""领域错误。API 层依据本模块的异常类型映射 HTTP 状态码。"""


class PolicyDomainError(Exception):
    """所有领域规则错误的基类。"""

    code = "domain_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(PolicyDomainError):
    code = "not_found"


class DuplicateError(PolicyDomainError):
    code = "duplicate"


class ValidationError(PolicyDomainError):
    code = "validation_error"


class CycleError(PolicyDomainError):
    code = "prerequisite_cycle"


class PrerequisiteBlockedError(PolicyDomainError):
    """审批时前置措施在审批日不全部有效。"""

    code = "prerequisites_blocked"


class MilestoneLockedError(PolicyDomainError):
    """变更会重排或裁剪已完成里程碑——历史事实不可静默改写。"""

    code = "milestone_locked"


class DuplicateCountingError(PolicyDomainError):
    """同一凭据出现第二个全额主计方，除非显式确认重复统计。"""

    code = "duplicate_counting"


class ShareOverlapError(PolicyDomainError):
    """凭据分摊比例之和超过 1，除非显式确认重叠。"""

    code = "share_overlap"


class CaliberMismatchError(PolicyDomainError):
    """凭据口径与目标口径不一致，除非显式确认差异。"""

    code = "caliber_mismatch"


class WorkflowStateError(PolicyDomainError):
    """对象当前状态不允许该操作（如重复审批、撤销已撤销措施）。"""

    code = "workflow_state"
