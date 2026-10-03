"""政策承诺跟踪核心领域服务。

关键规则（对应业务要求）：

1. 一次审批只能基于*审批日当时有效*的前置措施；审批时对前置状态做快照留痕。
2. 撤销/替代不删除任何记录：旧措施在决策日闭合生效区间，历史报告日仍可见其原貌；
   已完成里程碑（achieved_date）永久锁定，任何会重排它们的变更直接拒绝。
3. 撤销必须产出影响清单（直接/间接依赖措施、受影响目标、被保留的里程碑）。
4. 同一凭据被多部门采用时，主计/分摊/口径差异/比例重叠全部显式标注，
   绝不静默合并或重复计账。
"""

from dataclasses import replace
from datetime import date

from .contracts import (
    ChangeProposal,
    CountingRole,
    Event,
    Evidence,
    EvidenceAdoption,
    MeasureStatus,
    Milestone,
    PolicyMeasure,
    ProposalStatus,
    ProposalType,
    Target,
)
from .errors import (
    CaliberMismatchError,
    CycleError,
    DuplicateCountingError,
    DuplicateError,
    MilestoneLockedError,
    NotFoundError,
    PrerequisiteBlockedError,
    ShareOverlapError,
    ValidationError,
    WorkflowStateError,
)

ACTIVE_STATES = {MeasureStatus.APPROVED, MeasureStatus.ACTIVE}
TERMINAL_STATES = {MeasureStatus.WITHDRAWN, MeasureStatus.SUPERSEDED}


class PolicyTracker:
    def __init__(self, repo):
        self.repo = repo

    # ===================================================================
    # 登记
    # ===================================================================

    def add_body(self, body, registered_on: date | None = None) -> dict:
        if self.repo.contains("bodies", body.body_id):
            raise DuplicateError(f"责任主体已存在: {body.body_id}")
        self.repo.put("bodies", body)
        return self._record("body.registered", registered_on or date.today(),
                            (body.body_id,), {"name": body.name})

    def add_measure(self, measure: PolicyMeasure, registered_on: date | None = None) -> dict:
        if self.repo.contains("measures", measure.measure_id):
            raise DuplicateError(f"措施已存在: {measure.measure_id}")
        if measure.effective_until is not None and measure.effective_until <= measure.effective_from:
            raise ValidationError("生效区间必须满足 effective_from < effective_until")
        for pid in measure.prerequisite_ids:
            if not self.repo.contains("measures", pid):
                raise NotFoundError(f"前置措施不存在: {pid}", {"prerequisite_id": pid})
        self._assert_no_cycle(measure.measure_id, measure.prerequisite_ids)
        self.repo.put("measures", measure)
        return self._record(
            "measure.registered",
            registered_on or measure.effective_from,
            (measure.measure_id,),
            {"owner": measure.owner, "status": str(measure.status), "revision": measure.revision},
        )

    def activate_measure(self, measure_id: str, actor: str, on_date: date) -> dict:
        """登记为 PROPOSED 的措施经审批激活。

        与变更提案同一规则：只能基于激活日当时有效的前置措施，并把前置状态写入事件留痕。
        """
        measure = self._require_measure(measure_id)
        if measure.status != MeasureStatus.PROPOSED:
            raise WorkflowStateError(f"措施当前状态 {measure.status} 不能激活",
                                     {"status": str(measure.status)})
        blockers = self._blockers_for(measure.prerequisite_ids, on_date)
        snapshot = tuple(self._snapshot(pid, on_date) for pid in measure.prerequisite_ids)
        if blockers:
            raise PrerequisiteBlockedError(
                f"激活日 {on_date.isoformat()} 前置措施未全部有效",
                {"measure_id": measure_id, "blockers": blockers},
            )
        self.repo.put("measures", replace(measure, status=MeasureStatus.ACTIVE))
        self._record("measure.activated", on_date, (measure_id,),
                     {"actor": actor, "prereq_snapshot": [dict(s) for s in snapshot]})
        return {"measure_id": measure_id, "status": str(MeasureStatus.ACTIVE),
                "prereq_snapshot": [dict(s) for s in snapshot]}

    def add_target(self, target: Target) -> dict:
        if self.repo.contains("targets", target.target_id):
            raise DuplicateError(f"目标已存在: {target.target_id}")
        self._require_measure(target.measure_id)
        if target.target_value <= target.baseline:
            raise ValidationError("目标值必须高于基线值")
        self.repo.put("targets", target)
        return self._record("target.registered", target.due_date, (target.target_id, target.measure_id),
                            {"metric": target.metric, "caliber_id": target.caliber_id})

    def add_milestone(self, ms: Milestone) -> dict:
        if self.repo.contains("milestones", ms.milestone_id):
            raise DuplicateError(f"里程碑已存在: {ms.milestone_id}")
        target = self.repo.get("targets", ms.target_id)
        if target is None:
            raise NotFoundError(f"目标不存在: {ms.target_id}")
        if ms.achieved_date is not None:
            raise ValidationError("新建里程碑不能带完成日期，请使用 complete_milestone")
        self.repo.put("milestones", ms)
        return self._record("milestone.registered", ms.planned_date,
                            (ms.milestone_id, ms.target_id, target.measure_id),
                            {"label": ms.label, "planned_date": ms.planned_date.isoformat()})

    def reschedule_milestone(self, milestone_id: str, new_planned_date: date, actor: str, on_date: date) -> dict:
        """开放里程碑可以改期（留痕）；已完成里程碑锁定，拒绝重排。"""
        ms = self._require_milestone(milestone_id)
        if ms.achieved_date is not None:
            raise MilestoneLockedError(
                f"里程碑已完成，禁止重排: {milestone_id}",
                {"milestone_id": milestone_id, "achieved_date": ms.achieved_date.isoformat(),
                 "rejected_planned_date": new_planned_date.isoformat()},
            )
        target = self.repo.get("targets", ms.target_id)
        self.repo.put("milestones", replace(ms, planned_date=new_planned_date))
        return self._record("milestone.rescheduled", on_date,
                            (ms.milestone_id, ms.target_id, target.measure_id),
                            {"old_planned_date": ms.planned_date.isoformat(),
                             "new_planned_date": new_planned_date.isoformat(), "actor": actor})

    def complete_milestone(self, milestone_id: str, on_date: date, actor: str,
                           evidence_id: str | None = None) -> dict:
        ms = self._require_milestone(milestone_id)
        if ms.achieved_date is not None:
            raise MilestoneLockedError(
                f"里程碑已完成，日期不可更改: {milestone_id}",
                {"milestone_id": milestone_id, "achieved_date": ms.achieved_date.isoformat()},
            )
        if evidence_id and not self.repo.contains("evidence", evidence_id):
            raise NotFoundError(f"凭据不存在: {evidence_id}")
        target = self.repo.get("targets", ms.target_id)
        self.repo.put("milestones", replace(ms, achieved_date=on_date, evidence_id=evidence_id))
        return self._record("milestone.completed", on_date,
                            (ms.milestone_id, ms.target_id, target.measure_id),
                            {"evidence_id": evidence_id, "actor": actor})

    # ===================================================================
    # 凭据与统计边界
    # ===================================================================

    def add_evidence(self, ev: Evidence) -> dict:
        if self.repo.contains("evidence", ev.evidence_id):
            raise DuplicateError(f"凭据已存在: {ev.evidence_id}")
        self.repo.put("evidence", ev)
        return self._record("evidence.registered", ev.report_date, (ev.evidence_id,),
                            {"source_owner": ev.source_owner, "quantity": ev.quantity,
                             "caliber_id": ev.caliber_id})

    def adopt_evidence(self, adoption: EvidenceAdoption) -> dict:
        ev = self.repo.get("evidence", adoption.evidence_id)
        if ev is None:
            raise NotFoundError(f"凭据不存在: {adoption.evidence_id}")
        target = self.repo.get("targets", adoption.target_id)
        if target is None:
            raise NotFoundError(f"目标不存在: {adoption.target_id}")
        if self.repo.contains("adoptions", adoption.adoption_id):
            raise DuplicateError(f"采用记录已存在: {adoption.adoption_id}")
        if ev.metric != target.metric or ev.unit != target.unit:
            raise ValidationError(
                "凭据指标/单位与目标不一致，不能计账",
                {"evidence_metric": ev.metric, "target_metric": target.metric,
                 "evidence_unit": ev.unit, "target_unit": target.unit},
            )
        for existing in self.repo.all("adoptions"):
            if (existing.evidence_id == adoption.evidence_id
                    and existing.target_id == adoption.target_id
                    and existing.adopted_by == adoption.adopted_by):
                raise DuplicateError("同一部门已就同一目标采用过该凭据",
                                     {"evidence_id": ev.evidence_id, "target_id": target.target_id})

        caliber_mismatch = ev.caliber_id != target.caliber_id
        if caliber_mismatch and not adoption.caliber_mismatch:
            raise CaliberMismatchError(
                "凭据口径与目标口径不一致，采用须显式确认 caliber_mismatch",
                {"evidence_caliber": ev.caliber_id, "target_caliber": target.caliber_id},
            )
        if adoption.role == CountingRole.SHARED and not (0 < adoption.share <= 1):
            raise ValidationError("分摊比例必须在 (0, 1] 区间")

        siblings = [a for a in self.repo.all("adoptions") if a.evidence_id == ev.evidence_id]
        primary_count = sum(1 for a in siblings if a.role == CountingRole.PRIMARY)
        share_sum = sum(a.share for a in siblings if a.role == CountingRole.SHARED)

        if adoption.role == CountingRole.PRIMARY:
            if primary_count >= 1 and not adoption.duplicate_confirmed:
                raise DuplicateCountingError(
                    "该凭据已有全额主计方，再次主计构成重复统计，须显式确认 duplicate_confirmed",
                    {"evidence_id": ev.evidence_id, "existing_primary_count": primary_count},
                )
            projected_coverage = 1.0 + share_sum + (1.0 if primary_count else 0.0)
            if projected_coverage > 1.0 + 1e-9 and not (adoption.overlap_confirmed or adoption.duplicate_confirmed):
                raise ShareOverlapError(
                    "主计与分摊比例之和超过 1，须显式确认 overlap_confirmed",
                    {"coverage": projected_coverage},
                )
        else:
            projected_coverage = (1.0 if primary_count else 0.0) + share_sum + adoption.share
            if projected_coverage > 1.0 + 1e-9 and not adoption.overlap_confirmed:
                raise ShareOverlapError(
                    "凭据分摊比例之和超过 1，须显式确认 overlap_confirmed",
                    {"coverage": projected_coverage},
                )

        stored = replace(adoption, caliber_mismatch=caliber_mismatch)
        self.repo.put("adoptions", stored)
        self._record("evidence.adopted", adoption.on_date,
                     (adoption.adoption_id, ev.evidence_id, target.target_id, target.measure_id),
                     {"adopted_by": adoption.adopted_by, "role": str(adoption.role),
                      "share": adoption.share, "caliber_mismatch": caliber_mismatch,
                      "duplicate_confirmed": adoption.duplicate_confirmed,
                      "overlap_confirmed": adoption.overlap_confirmed})
        return self.evidence_boundary(ev.evidence_id)

    def evidence_boundary(self, evidence_id: str) -> dict:
        ev: Evidence | None = self.repo.get("evidence", evidence_id)
        if ev is None:
            raise NotFoundError(f"凭据不存在: {evidence_id}")
        adoptions = [a for a in self.repo.all("adoptions") if a.evidence_id == evidence_id]
        primary = [a for a in adoptions if a.role == CountingRole.PRIMARY]
        shared = [a for a in adoptions if a.role == CountingRole.SHARED]
        coverage = sum(1.0 for _ in primary) + sum(a.share for a in shared)
        adopters = sorted({a.adopted_by for a in adoptions})
        return {
            "evidence_id": ev.evidence_id,
            "title": ev.title,
            "metric": ev.metric,
            "unit": ev.unit,
            "quantity": ev.quantity,
            "caliber_id": ev.caliber_id,
            "adoptions": [
                {
                    "adoption_id": a.adoption_id,
                    "target_id": a.target_id,
                    "adopted_by": a.adopted_by,
                    "adopter_name": self._body_name(a.adopted_by),
                    "role": str(a.role),
                    "share": a.share,
                    "counted_quantity": round(ev.quantity * a.share, 6),
                    "caliber_mismatch": a.caliber_mismatch,
                    "duplicate_confirmed": a.duplicate_confirmed,
                    "overlap_confirmed": a.overlap_confirmed,
                }
                for a in adoptions
            ],
            "boundary": {
                "shared_across_bodies": len(adopters) > 1,
                "adopter_count": len(adopters),
                "adopters": adopters,
                "primary_count": len(primary),
                "double_counting": len(primary) > 1,
                "caliber_mismatch": any(a.caliber_mismatch for a in adoptions),
                "share_coverage": round(coverage, 6),
                "coverage_overlap": coverage > 1.0 + 1e-9,
            },
        }

    # ===================================================================
    # 变更提案：提交 / 审批 / 拒绝
    # ===================================================================

    def submit_proposal(self, proposal: ChangeProposal) -> dict:
        if self.repo.contains("proposals", proposal.proposal_id):
            raise DuplicateError(f"提案已存在: {proposal.proposal_id}")
        measure = self._require_measure(proposal.measure_id)
        if measure.status in TERMINAL_STATES:
            raise WorkflowStateError(f"措施已终结，不能再提变更: {measure.measure_id}",
                                     {"status": str(measure.status)})

        if proposal.ptype == ProposalType.REPLACE:
            draft = proposal.replacement
            if draft is None:
                raise ValidationError("替代提案必须附带 replacement 草案")
            if self.repo.contains("measures", draft.measure_id):
                raise DuplicateError(f"新措施标识已被占用: {draft.measure_id}")
            for pid in draft.prerequisite_ids:
                if pid == measure.measure_id or not self.repo.contains("measures", pid):
                    raise NotFoundError(f"草案前置措施不存在: {pid}")
            self._assert_no_cycle(draft.measure_id, draft.prerequisite_ids,
                                  extra_edges={measure.measure_id: draft.prerequisite_ids})
            if draft.effective_until and draft.effective_until <= draft.effective_from:
                raise ValidationError("草案生效区间非法")
        elif proposal.ptype == ProposalType.AMEND:
            new_prereqs = proposal.new_prerequisite_ids
            if new_prereqs is not None:
                for pid in new_prereqs:
                    if not self.repo.contains("measures", pid):
                        raise NotFoundError(f"新前置措施不存在: {pid}")
                self._assert_no_cycle(measure.measure_id, new_prereqs)
            nf = proposal.new_effective_from
            nu = proposal.new_effective_until
            if nf and nu and nu <= nf:
                raise ValidationError("新生效区间非法")
        elif proposal.ptype == ProposalType.WITHDRAW:
            pass
        else:  # pragma: no cover - 枚举穷尽
            raise ValidationError(f"未知提案类型: {proposal.ptype}")

        self.repo.put("proposals", proposal)
        self._record("proposal.submitted", proposal.submitted_on,
                     (proposal.proposal_id, measure.measure_id),
                     {"type": str(proposal.ptype), "submitted_by": proposal.submitted_by})
        return {"proposal_id": proposal.proposal_id, "status": str(proposal.status)}

    def approve_proposal(self, proposal_id: str, actor: str, on_date: date) -> dict:
        proposal = self._require_proposal(proposal_id)
        if proposal.status != ProposalStatus.PENDING:
            raise WorkflowStateError(f"提案已处理: {proposal.status}", {"status": str(proposal.status)})
        measure = self._require_measure(proposal.measure_id)

        # —— 规则 1：审批只能基于审批日当时有效的前置措施，并做快照 ——
        prereq_ids = self._proposed_prerequisites(proposal, measure)
        blockers = self._blockers_for(prereq_ids, on_date)
        snapshot = tuple(self._snapshot(pid, on_date) for pid in prereq_ids)
        if blockers:
            raise PrerequisiteBlockedError(
                f"审批日 {on_date.isoformat()} 前置措施未全部有效",
                {"proposal_id": proposal_id, "blockers": blockers},
            )

        decided = replace(proposal, status=ProposalStatus.APPROVED, decided_on=on_date,
                          decided_by=actor, prereq_snapshot=snapshot)
        self.repo.put("proposals", decided)

        if proposal.ptype == ProposalType.AMEND:
            result = self._apply_amend(measure, decided, actor, on_date)
        elif proposal.ptype == ProposalType.REPLACE:
            result = self._apply_replace(measure, decided, actor, on_date)
        else:
            result = self._apply_withdraw(measure, decided, actor, on_date)
        result["prereq_snapshot"] = [dict(s) for s in snapshot]
        return result

    def reject_proposal(self, proposal_id: str, actor: str, on_date: date, reason: str = "") -> dict:
        proposal = self._require_proposal(proposal_id)
        if proposal.status != ProposalStatus.PENDING:
            raise WorkflowStateError(f"提案已处理: {proposal.status}")
        self.repo.put("proposals", replace(proposal, status=ProposalStatus.REJECTED,
                                           decided_on=on_date, decided_by=actor))
        self._record("proposal.rejected", on_date, (proposal_id, proposal.measure_id),
                     {"actor": actor, "reason": reason})
        return {"proposal_id": proposal_id, "status": str(ProposalStatus.REJECTED)}

    def _apply_amend(self, measure: PolicyMeasure, proposal: ChangeProposal, actor: str, on_date: date) -> dict:
        new_from = proposal.new_effective_from or measure.effective_from
        new_until = proposal.new_effective_until if proposal.new_effective_until is not None else measure.effective_until
        new_prereqs = (proposal.new_prerequisite_ids
                       if proposal.new_prerequisite_ids is not None else measure.prerequisite_ids)

        # 禁止回溯：新生效/失效日不得早于审批日，避免追溯改写历史报告日
        if new_from < on_date and new_from != measure.effective_from:
            raise ValidationError(
                "新生效日不得早于审批日（禁止回溯生效）",
                {"new_effective_from": new_from.isoformat(), "decision_date": on_date.isoformat()})
        if new_until is not None and new_until <= on_date \
                and new_until != measure.effective_until:
            raise ValidationError(
                "新失效日必须晚于审批日（即时失效请走撤销提案）",
                {"new_effective_until": new_until.isoformat(), "decision_date": on_date.isoformat()})

        # —— 规则 2：不允许用区间变更把已完成里程碑挤到措施生效之前 ——
        locked = self._milestones_measure(measure.measure_id)
        violating = [m.milestone_id for m in locked
                     if m.achieved_date is not None and m.achieved_date < new_from]
        if violating:
            raise MilestoneLockedError(
                "延期/调整将使已完成里程碑早于新生效日，历史成就不能静默重排；请改用替代提案",
                {"milestone_ids": violating, "new_effective_from": new_from.isoformat()},
            )

        revised = replace(measure, effective_from=new_from, effective_until=new_until,
                          prerequisite_ids=tuple(new_prereqs), revision=measure.revision + 1)
        self.repo.put("measures", revised)
        self._record("measure.amended", on_date, (measure.measure_id, proposal.proposal_id),
                     {"actor": actor, "revision": revised.revision,
                      "old_effective_from": measure.effective_from.isoformat(),
                      "new_effective_from": new_from.isoformat(),
                      "old_effective_until": measure.effective_until.isoformat() if measure.effective_until else None,
                      "new_effective_until": new_until.isoformat() if new_until else None})
        return {"proposal_id": proposal.proposal_id, "action": "amend",
                "measure_id": measure.measure_id, "revision": revised.revision}

    def _apply_replace(self, measure: PolicyMeasure, proposal: ChangeProposal, actor: str, on_date: date) -> dict:
        draft = proposal.replacement
        # 旧措施在决策日闭合——历史报告日仍可见其有效
        old_until = measure.effective_until
        closed_until = on_date if old_until is None or old_until > on_date else old_until
        superseded = replace(measure, status=MeasureStatus.SUPERSEDED, effective_until=closed_until)
        new_measure = PolicyMeasure(
            measure_id=draft.measure_id, title=draft.title, owner=draft.owner,
            effective_from=draft.effective_from, effective_until=draft.effective_until,
            prerequisite_ids=draft.prerequisite_ids, status=MeasureStatus.ACTIVE,
            supersedes=measure.measure_id, revision=measure.revision + 1,
        )
        self.repo.put("measures", superseded)
        self.repo.put("measures", new_measure)

        # 目标迁移到新措施；已完成里程碑原样保留（日期、凭据一律不动）
        carried, preserved = [], []
        for target in self.repo.all("targets"):
            if target.measure_id == measure.measure_id:
                self.repo.put("targets", replace(target, measure_id=new_measure.measure_id))
                carried.append(target.target_id)
                for ms in self.repo.all("milestones"):
                    if ms.target_id == target.target_id and ms.achieved_date is not None:
                        preserved.append({"milestone_id": ms.milestone_id,
                                          "achieved_date": ms.achieved_date.isoformat(),
                                          "evidence_id": ms.evidence_id})
        self._record("measure.replaced", on_date,
                     (measure.measure_id, new_measure.measure_id, proposal.proposal_id),
                     {"actor": actor, "carried_targets": carried,
                      "preserved_milestones": preserved})
        return {"proposal_id": proposal.proposal_id, "action": "replace",
                "old_measure_id": measure.measure_id, "new_measure_id": new_measure.measure_id,
                "carried_targets": carried, "preserved_milestones": preserved}

    def _apply_withdraw(self, measure: PolicyMeasure, proposal: ChangeProposal, actor: str, on_date: date) -> dict:
        old_until = measure.effective_until
        closed_until = on_date if old_until is None or old_until > on_date else old_until
        self.repo.put("measures", replace(measure, status=MeasureStatus.WITHDRAWN,
                                          effective_until=closed_until))
        impact = self._impact_report(measure.measure_id, on_date)
        impacted_ids = tuple(m["measure_id"] for m in impact["affected_measures"])
        self._record("measure.withdrawn", on_date,
                     (measure.measure_id, proposal.proposal_id, *impacted_ids),
                     {"actor": actor, "impact": impact})
        return {"proposal_id": proposal.proposal_id, "action": "withdraw",
                "measure_id": measure.measure_id, "impact": impact}

    # ===================================================================
    # 影响清单（撤销/替代的下游分析）
    # ===================================================================

    def preview_withdrawal_impact(self, measure_id: str, on_date: date) -> dict:
        self._require_measure(measure_id)
        return self._impact_report(measure_id, on_date)

    def _impact_report(self, measure_id: str, on_date: date) -> dict:
        dependents = self._transitive_dependents(measure_id)
        affected_measures = []
        for dep_id, chain in dependents:
            dep = self.repo.get("measures", dep_id)
            dep_targets = [t.target_id for t in self.repo.all("targets") if t.measure_id == dep_id]
            affected_measures.append({
                "measure_id": dep_id,
                "title": dep.title,
                "owner": dep.owner,
                "owner_name": self._body_name(dep.owner),
                "dependency_chain": chain,
                "effective_on_decision_date": self._is_effective(dep, on_date),
                "targets": dep_targets,
                "blocked_reason": "upstream_withdrawn",
            })

        own_targets = [t.target_id for t in self.repo.all("targets") if t.measure_id == measure_id]
        preserved = [
            {"milestone_id": m.milestone_id, "target_id": m.target_id,
             "achieved_date": m.achieved_date.isoformat(), "evidence_id": m.evidence_id}
            for m in self.repo.all("milestones")
            if m.achieved_date is not None and self._milestone_measure_ids(m.milestone_id) & (
                {measure_id} | {d[0] for d in dependents})
        ]
        affected_target_ids = set(own_targets)
        for d in affected_measures:
            affected_target_ids.update(d["targets"])
        affected_adoptions = [
            a.adoption_id for a in self.repo.all("adoptions")
            if (t := self.repo.get("targets", a.target_id)) and t.target_id in affected_target_ids
        ]
        return {
            "withdrawn_measure_id": measure_id,
            "decision_date": on_date.isoformat(),
            "direct_dependent_count": sum(1 for _, chain in dependents if len(chain) == 2),
            "transitive_dependent_count": len(dependents),
            "affected_measures": affected_measures,
            "own_targets": own_targets,
            "preserved_completed_milestones": preserved,
            "affected_evidence_adoptions": sorted(affected_adoptions),
            "note": "已完成里程碑与历史凭据均保留，不做删除或重排；报告日早于决策日的状态不变。",
        }

    def _transitive_dependents(self, measure_id: str) -> list[tuple[str, list[str]]]:
        """返回 (依赖措施id, 引用链) ，引用链形如 [根, 中间, 该措施]。"""
        result = []
        stack = [(measure_id, [measure_id])]
        seen = set()
        while stack:
            current, chain = stack.pop()
            for m in self.repo.all("measures"):
                if current in m.prerequisite_ids and m.measure_id not in seen:
                    seen.add(m.measure_id)
                    new_chain = chain + [m.measure_id]
                    result.append((m.measure_id, new_chain))
                    stack.append((m.measure_id, new_chain))
        result.sort(key=lambda x: (len(x[1]), x[0]))
        return result

    # ===================================================================
    # 报告日查询：状态 / 阻塞 / 进度
    # ===================================================================

    def status_on(self, measure_id: str, report_date: date) -> dict:
        measure = self._require_measure(measure_id)
        blockers = self._blockers_for(measure.prerequisite_ids, report_date)
        return {
            "measure_id": measure.measure_id,
            "title": measure.title,
            "owner": measure.owner,
            "owner_name": self._body_name(measure.owner),
            "status": str(self._state_on(measure, report_date)),
            "effective": self._is_effective(measure, report_date),
            "effective_from": measure.effective_from.isoformat(),
            "effective_until": measure.effective_until.isoformat() if measure.effective_until else None,
            "revision": measure.revision,
            "supersedes": measure.supersedes,
            "prerequisites": list(measure.prerequisite_ids),
            "blockers": blockers,
        }

    def blockers_on(self, measure_id: str, report_date: date) -> list[dict]:
        measure = self._require_measure(measure_id)
        return self._blockers_for(measure.prerequisite_ids, report_date)

    def report(self, report_date: date, owner: str | None = None) -> dict:
        measures = [m for m in self.repo.all("measures") if owner is None or m.owner == owner]
        measures.sort(key=lambda m: (m.owner, m.measure_id))
        rows = []
        for m in measures:
            blockers = self._blockers_for(m.prerequisite_ids, report_date)
            rows.append({
                **self.status_on(m.measure_id, report_date),
                "targets": [self._target_view(t, report_date) for t in self._targets_of(m.measure_id)],
                "blocked": bool(blockers),
            })
        return {
            "report_date": report_date.isoformat(),
            "owner": owner,
            "measure_count": len(rows),
            "measures": rows,
        }

    def _target_view(self, target: Target, report_date: date) -> dict:
        adoptions = [a for a in self.repo.all("adoptions")
                     if a.target_id == target.target_id and a.on_date <= report_date]
        compatible = sum(a.share * self._evidence_qty(a.evidence_id)
                         for a in adoptions if not a.caliber_mismatch)
        mismatched = sum(a.share * self._evidence_qty(a.evidence_id)
                         for a in adoptions if a.caliber_mismatch)
        milestones = []
        for ms in self.repo.all("milestones"):
            if ms.target_id != target.target_id:
                continue
            milestones.append({
                "milestone_id": ms.milestone_id,
                "label": ms.label,
                "planned_date": ms.planned_date.isoformat(),
                "achieved_date": ms.achieved_date.isoformat() if ms.achieved_date else None,
                "evidence_id": ms.evidence_id,
                "state": "achieved" if ms.achieved_date is not None
                else ("overdue" if ms.planned_date <= report_date else "on_track"),
            })
        achieved = sum(1 for x in milestones if x["state"] == "achieved")
        overdue = sum(1 for x in milestones if x["state"] == "overdue")
        return {
            "target_id": target.target_id,
            "metric": target.metric,
            "unit": target.unit,
            "target_value": target.target_value,
            "baseline": target.baseline,
            "due_date": target.due_date.isoformat(),
            "caliber_id": target.caliber_id,
            "progress": {
                "counted_compatible": round(compatible, 6),
                "counted_caliber_mismatch": round(mismatched, 6),
                "attainment_ratio": round(compatible / (target.target_value - target.baseline), 6)
                if target.target_value > target.baseline else None,
            },
            "milestones": milestones,
            "milestone_summary": {"total": len(milestones), "achieved": achieved, "overdue": overdue},
        }

    # ===================================================================
    # 变更历史
    # ===================================================================

    def history(self, measure_id: str) -> dict:
        self._require_measure(measure_id)
        lineage = self._lineage(measure_id)
        events = [e for e in sorted(self.repo.all("events"), key=lambda x: x.seq)
                  if lineage & set(e.entities)]
        proposals = [p for p in self.repo.all("proposals")
                     if p.measure_id in lineage or
                     (p.replacement is not None and p.replacement.measure_id in lineage)]
        return {
            "measure_id": measure_id,
            "lineage": sorted(lineage),
            "events": [self._event_dict(e) for e in events],
            "proposals": [
                {
                    "proposal_id": p.proposal_id,
                    "measure_id": p.measure_id,
                    "type": str(p.ptype),
                    "status": str(p.status),
                    "rationale": p.rationale,
                    "submitted_by": p.submitted_by,
                    "submitted_on": p.submitted_on.isoformat(),
                    "decided_on": p.decided_on.isoformat() if p.decided_on else None,
                    "decided_by": p.decided_by,
                    "prereq_snapshot": [dict(s) for s in p.prereq_snapshot],
                }
                for p in sorted(proposals, key=lambda x: x.submitted_on)
            ],
        }

    def events(self) -> list[dict]:
        return [self._event_dict(e) for e in sorted(self.repo.all("events"), key=lambda x: x.seq)]

    def _lineage(self, measure_id: str) -> set[str]:
        """沿 supersedes 向上找全部修订版本。"""
        result = {measure_id}
        current = self.repo.get("measures", measure_id)
        while current and current.supersedes:
            result.add(current.supersedes)
            current = self.repo.get("measures", current.supersedes)
        return result

    # ===================================================================
    # 点时状态推导
    # ===================================================================

    def _state_on(self, measure: PolicyMeasure, d: date) -> MeasureStatus:
        decision = self._terminal_decision_date(measure.measure_id, measure.status)
        if measure.status in TERMINAL_STATES and decision is not None and d < decision:
            # 决策日之前：按审批情况表现为 active/approved/proposed
            return self._pre_decision_state(measure.measure_id, d)
        if measure.status in TERMINAL_STATES:
            return measure.status
        return self._pre_decision_state(measure.measure_id, d)

    def _pre_decision_state(self, measure_id: str, d: date) -> MeasureStatus:
        activation = self._activation_date(measure_id)
        if activation is not None and d >= activation:
            return MeasureStatus.ACTIVE
        approval = self._approval_date(measure_id)
        if approval is None or d < approval:
            return MeasureStatus.PROPOSED
        return MeasureStatus.ACTIVE if self._registered_status(measure_id) == MeasureStatus.ACTIVE \
            else MeasureStatus.APPROVED

    def _activation_date(self, measure_id: str) -> date | None:
        dates = [e.on_date for e in self.repo.all("events")
                 if e.etype == "measure.activated" and measure_id in e.entities]
        return min(dates) if dates else None

    def _registered_status(self, measure_id: str) -> MeasureStatus:
        """措施获批时的初始状态（用于终态措施回看决策日之前）。"""
        for e in self.repo.all("events"):
            if e.etype == "measure.registered" and measure_id in e.entities:
                return MeasureStatus(e.payload.get("status", "approved"))
            if e.etype == "measure.replaced" and measure_id in e.entities:
                return MeasureStatus.ACTIVE
        current = self.repo.get("measures", measure_id)
        return current.status if current else MeasureStatus.PROPOSED

    def _approval_date(self, measure_id: str) -> date | None:
        dates = []
        for p in self.repo.all("proposals"):
            if p.status != ProposalStatus.APPROVED or p.decided_on is None:
                continue
            if p.ptype == ProposalType.REPLACE and p.replacement is not None \
                    and p.replacement.measure_id == measure_id:
                dates.append(p.decided_on)
        for e in self.repo.all("events"):
            if measure_id not in e.entities:
                continue
            if e.etype == "measure.registered" and e.payload.get("status") in (
                    str(MeasureStatus.ACTIVE), str(MeasureStatus.APPROVED)):
                dates.append(e.on_date)
            elif e.etype == "measure.activated":
                dates.append(e.on_date)
            elif e.etype == "measure.replaced":
                dates.append(e.on_date)
        return min(dates) if dates else None

    def _terminal_decision_date(self, measure_id: str, current_status: MeasureStatus) -> date | None:
        for p in self.repo.all("proposals"):
            if p.status != ProposalStatus.APPROVED or p.decided_on is None or p.measure_id != measure_id:
                continue
            if p.ptype == ProposalType.WITHDRAW and current_status == MeasureStatus.WITHDRAWN:
                return p.decided_on
            if p.ptype == ProposalType.REPLACE and current_status == MeasureStatus.SUPERSEDED:
                return p.decided_on
        return None

    def _is_effective(self, measure: PolicyMeasure, d: date) -> bool:
        if self._state_on(measure, d) not in ACTIVE_STATES:
            return False
        if d < measure.effective_from:
            return False
        if measure.effective_until is not None and d >= measure.effective_until:
            return False
        return True

    def _blockers_for(self, prereq_ids, d: date, _seen: frozenset = frozenset()) -> list[dict]:
        blockers = []
        for pid in prereq_ids:
            if pid in _seen:
                continue
            pre = self.repo.get("measures", pid)
            if pre is None:
                blockers.append({"prerequisite_id": pid, "owner": None, "reason": "missing"})
                continue
            state = self._state_on(pre, d)
            reason = None
            if state == MeasureStatus.WITHDRAWN:
                reason = "withdrawn"
            elif state == MeasureStatus.SUPERSEDED:
                reason = "superseded"
            elif d < pre.effective_from:
                reason = "not_yet_effective"
            elif pre.effective_until is not None and d >= pre.effective_until:
                reason = "expired"
            elif state == MeasureStatus.PROPOSED:
                reason = "not_approved"
            if reason:
                blockers.append({
                    "prerequisite_id": pid, "owner": pre.owner,
                    "owner_name": self._body_name(pre.owner), "reason": reason,
                    "effective_from": pre.effective_from.isoformat(),
                    "effective_until": pre.effective_until.isoformat() if pre.effective_until else None,
                })
            else:
                # 前置本身有效，继续上溯；via 记录从查询方到更深阻塞点之间经过的措施
                for u in self._blockers_for(pre.prerequisite_ids, d, _seen | {pid}):
                    chain = [pid, *u.get("via", [])]
                    blockers.append({**u, "via": chain})
        return blockers

    def _snapshot(self, pid: str, on_date: date) -> dict:
        pre = self.repo.get("measures", pid)
        return {
            "measure_id": pid,
            "owner": pre.owner if pre else None,
            "status": str(self._state_on(pre, on_date)) if pre else "missing",
            "revision": pre.revision if pre else None,
            "effective_from": pre.effective_from.isoformat() if pre else None,
            "effective_until": pre.effective_until.isoformat() if pre and pre.effective_until else None,
            "snapshot_date": on_date.isoformat(),
        }

    # ===================================================================
    # 辅助
    # ===================================================================

    def _proposed_prerequisites(self, proposal: ChangeProposal, measure: PolicyMeasure):
        if proposal.ptype == ProposalType.REPLACE:
            return proposal.replacement.prerequisite_ids
        if proposal.ptype == ProposalType.AMEND and proposal.new_prerequisite_ids is not None:
            return proposal.new_prerequisite_ids
        return measure.prerequisite_ids

    def _assert_no_cycle(self, node_id: str, prereq_ids, extra_edges: dict | None = None) -> None:
        graph = {m.measure_id: tuple(m.prerequisite_ids) for m in self.repo.all("measures")}
        if extra_edges:
            graph.update(extra_edges)
        graph[node_id] = tuple(prereq_ids)

        def visit(node, stack):
            for nxt in graph.get(node, ()):
                if nxt in stack:
                    raise CycleError(f"前置依赖成环: {' -> '.join(stack + [nxt])}",
                                     {"cycle": stack + [nxt]})
                visit(nxt, stack + [nxt])

        visit(node_id, [node_id])

    def _evidence_qty(self, evidence_id: str) -> float:
        ev = self.repo.get("evidence", evidence_id)
        return ev.quantity if ev else 0.0

    def _targets_of(self, measure_id: str) -> list[Target]:
        return [t for t in self.repo.all("targets") if t.measure_id == measure_id]

    def _milestones_measure(self, measure_id: str) -> list[Milestone]:
        target_ids = {t.target_id for t in self._targets_of(measure_id)}
        return [m for m in self.repo.all("milestones") if m.target_id in target_ids]

    def _milestone_measure_ids(self, milestone_id: str) -> set[str]:
        ms = self.repo.get("milestones", milestone_id)
        if ms is None:
            return set()
        target = self.repo.get("targets", ms.target_id)
        return {target.measure_id} if target else set()

    def _body_name(self, body_id: str | None) -> str | None:
        if body_id is None:
            return None
        body = self.repo.get("bodies", body_id)
        return body.name if body else None

    def _require_measure(self, measure_id: str) -> PolicyMeasure:
        m = self.repo.get("measures", measure_id)
        if m is None:
            raise NotFoundError(f"措施不存在: {measure_id}")
        return m

    def _require_milestone(self, milestone_id: str) -> Milestone:
        m = self.repo.get("milestones", milestone_id)
        if m is None:
            raise NotFoundError(f"里程碑不存在: {milestone_id}")
        return m

    def _require_proposal(self, proposal_id: str) -> ChangeProposal:
        p = self.repo.get("proposals", proposal_id)
        if p is None:
            raise NotFoundError(f"提案不存在: {proposal_id}")
        return p

    def _record(self, etype: str, on_date: date, entities, payload: dict) -> dict:
        event = Event(seq=self.repo.next_event_seq(), on_date=on_date, actor="system",
                      etype=etype, entities=tuple(entities), payload=payload)
        self.repo.put("events", event)
        return self._event_dict(event)

    @staticmethod
    def _event_dict(e: Event) -> dict:
        return {"seq": e.seq, "date": e.on_date.isoformat(), "actor": e.actor,
                "type": e.etype, "entities": list(e.entities), "payload": e.payload}
