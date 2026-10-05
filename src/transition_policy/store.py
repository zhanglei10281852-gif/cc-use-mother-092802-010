"""政策承诺注册表与业务规则。

设计要点：
- 所有变更以不可变对象替换 + 事件追加方式记录，保留完整变更历史；
- 状态（active/expired）在查询时按报告日推导，历史快照不会被后续变更覆盖；
- 审批、延期、替代、重指前置、撤回均通过提案工作流完成；
- 撤回 / 替代生成影响清单（ImpactReport），已完成里程碑冻结留痕。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date

from .contracts import (
    ChangeProposal,
    EvidenceAdoption,
    ImpactItem,
    ImpactReport,
    MeasureStatus,
    Milestone,
    MilestoneStatus,
    PolicyMeasure,
    ProgressEvidence,
    ProposalStatus,
    ProposalType,
    ResponsibleBody,
    SharingBoundary,
    Target,
)


class PolicyError(ValueError):
    """业务规则冲突（映射为 API 400/409）。"""


class Registry:
    def __init__(self) -> None:
        self.bodies: dict[str, ResponsibleBody] = {}
        self.measures: dict[str, PolicyMeasure] = {}
        self.targets: dict[str, Target] = {}
        self.evidence: dict[str, ProgressEvidence] = {}
        self.proposals: dict[str, ChangeProposal] = {}
        self.impacts: dict[str, ImpactReport] = {}  # key = proposal_id
        self.events: list[dict] = []
        self._counters: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------ IDs
    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}-{self._counters[prefix]}"

    def _log(self, on: date, kind: str, actor: str, measure_id: str | None,
             detail: dict | None = None) -> None:
        self.events.append(
            {
                "seq": len(self.events) + 1,
                "on": on.isoformat(),
                "kind": kind,
                "actor": actor,
                "measure_id": measure_id,
                "detail": detail or {},
            }
        )

    # -------------------------------------------------------- 责任主体登记
    def add_body(self, body_id: str, name: str, department: str) -> ResponsibleBody:
        if body_id in self.bodies:
            raise PolicyError(f"责任主体已存在: {body_id}")
        body = ResponsibleBody(body_id, name, department)
        self.bodies[body_id] = body
        return body

    # -------------------------------------------------------- 依赖环检测
    def _would_create_cycle(self, measure_id: str,
                            prerequisite_ids: tuple[str, ...]) -> bool:
        """若 measure_id 经 prereq 链可达自身，则构成环。"""
        stack = list(prerequisite_ids)
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current == measure_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            pre = self.measures.get(current)
            if pre is not None:
                stack.extend(pre.prerequisite_ids)
        return False

    # ----------------------------------------------------------- 措施草拟
    def draft_measure(
        self,
        owner: str,
        effective_from: date,
        effective_until: date | None,
        prerequisite_ids: tuple[str, ...] = (),
        title: str = "",
        measure_id: str | None = None,
        raised_by: str | None = None,
        on: date | None = None,
    ) -> PolicyMeasure:
        """登记一项措施（初始为 PROPOSED），需经 ADOPT 提案审批后生效。"""
        mid = measure_id or self._next_id("m")
        if mid in self.measures:
            raise PolicyError(f"措施编号已存在: {mid}")
        for pid in prerequisite_ids:
            if pid not in self.measures:
                raise PolicyError(f"前置措施不存在: {pid}")
        if self._would_create_cycle(mid, prerequisite_ids):
            raise PolicyError(f"措施 {mid} 的前置集合将形成依赖环")
        if effective_until is not None and effective_until < effective_from:
            raise PolicyError("生效截止日不得早于生效起始日")
        measure = PolicyMeasure(
            measure_id=mid,
            owner=owner,
            effective_from=effective_from,
            effective_until=effective_until,
            prerequisite_ids=tuple(prerequisite_ids),
            status=MeasureStatus.PROPOSED,
            title=title,
        )
        self.measures[mid] = measure
        self._log(on or date.today(), "measure_drafted", raised_by or owner, mid,
                  {"title": title})
        return measure

    # ----------------------------------------------------------- 目标指标
    def add_target(
        self,
        measure_id: str,
        name: str,
        unit: str,
        target_value: float,
        due_date: date,
        baseline: float | None = None,
        additive_across_departments: bool = True,
        target_id: str | None = None,
        milestones: tuple[Milestone, ...] = (),
    ) -> Target:
        if measure_id not in self.measures:
            raise PolicyError(f"措施不存在: {measure_id}")
        tid = target_id or self._next_id("t")
        coerced = []
        for ms in milestones:
            if ms.target_id not in ("", tid):
                raise PolicyError(f"里程碑 {ms.milestone_id} 归属目标不匹配")
            coerced.append(replace(ms, target_id=tid))
        target = Target(
            target_id=tid,
            measure_id=measure_id,
            name=name,
            unit=unit,
            target_value=target_value,
            due_date=due_date,
            baseline=baseline,
            additive_across_departments=additive_across_departments,
            milestones=tuple(coerced),
        )
        self.targets[tid] = target
        return target

    # ----------------------------------------------------------- 进度凭据
    def register_evidence(
        self,
        target_id: str,
        title: str,
        kind: str,
        reference: str,
        origin_department: str,
        value: float,
        unit: str,
        period_start: date,
        period_end: date,
        evidence_id: str | None = None,
        adoptions: tuple[EvidenceAdoption, ...] = (),
    ) -> ProgressEvidence:
        if target_id not in self.targets:
            raise PolicyError(f"目标不存在: {target_id}")
        eid = evidence_id or self._next_id("e")
        if eid in self.evidence:
            raise PolicyError(f"凭据编号已存在: {eid}")
        depts = [a.department for a in adoptions]
        if len(depts) != len(set(depts)):
            raise PolicyError("同一部门对同一凭据只能采用一次")
        for a in adoptions:
            if not 0 < a.share <= 1:
                raise PolicyError("凭据采用份额须在 (0, 1] 区间")
        record = ProgressEvidence(
            evidence_id=eid,
            target_id=target_id,
            title=title,
            kind=kind,
            reference=reference,
            origin_department=origin_department,
            value=value,
            unit=unit,
            period_start=period_start,
            period_end=period_end,
            adoptions=tuple(adoptions),
        )
        self.evidence[eid] = record
        return record

    def adopt_evidence(self, evidence_id: str, department: str, share: float,
                       scope_note: str, on: date) -> ProgressEvidence:
        record = self.evidence.get(evidence_id)
        if record is None:
            raise PolicyError(f"凭据不存在: {evidence_id}")
        if not 0 < share <= 1:
            raise PolicyError("凭据采用份额须在 (0, 1] 区间")
        if any(a.department == department for a in record.adoptions):
            raise PolicyError(f"部门 {department} 已采用该凭据")
        adoption = EvidenceAdoption(department, share, scope_note, on)
        record = replace(record, adoptions=record.adoptions + (adoption,))
        self.evidence[evidence_id] = record
        self._log(on, "evidence_adopted", department,
                  self.targets[record.target_id].measure_id,
                  {"evidence_id": evidence_id, "share": share})
        return record

    def evidence_boundary(self, evidence_id: str) -> dict:
        """计算同一凭据被多部门采用时的共享 / 重复统计边界。"""
        record = self.evidence[evidence_id]
        total = sum(a.share for a in record.adoptions)
        depts = len(record.adoptions)
        if depts <= 1:
            boundary = SharingBoundary.EXCLUSIVE
        elif total <= 1 + 1e-9:
            boundary = SharingBoundary.SHARED
        else:
            boundary = SharingBoundary.DOUBLE_COUNT
        return {
            "evidence_id": evidence_id,
            "target_id": record.target_id,
            "origin_department": record.origin_department,
            "adoptions": [
                {
                    "department": a.department,
                    "share": a.share,
                    "scope_note": a.scope_note,
                    "adopted_on": a.adopted_on.isoformat(),
                }
                for a in record.adoptions
            ],
            "claimed_share_total": round(total, 6),
            "boundary": boundary.value,
            "double_counted": boundary == SharingBoundary.DOUBLE_COUNT,
        }

    # ----------------------------------------------------------- 里程碑
    def complete_milestone(self, target_id: str, milestone_id: str,
                           completed_on: date, completed_by: str,
                           evidence_id: str | None = None) -> Milestone:
        target = self.targets.get(target_id)
        if target is None:
            raise PolicyError(f"目标不存在: {target_id}")
        measure = self.measures[target.measure_id]
        if measure.status in (MeasureStatus.WITHDRAWN, MeasureStatus.SUPERSEDED):
            raise PolicyError("措施已撤回 / 被替代，不能再完成里程碑")
        if measure.status == MeasureStatus.PROPOSED:
            raise PolicyError("措施尚未审批通过，不能完成里程碑")
        if evidence_id is not None:
            ev = self.evidence.get(evidence_id)
            if ev is None:
                raise PolicyError(f"凭据不存在: {evidence_id}")
            if ev.target_id != target_id:
                raise PolicyError("凭据与目标不匹配，不能用于核销该里程碑")
        for ms in target.milestones:
            if ms.milestone_id == milestone_id:
                if ms.status == MilestoneStatus.COMPLETED:
                    raise PolicyError("里程碑已完成，完成记录不可更改")
                done = replace(
                    ms,
                    status=MilestoneStatus.COMPLETED,
                    completed_date=completed_on,
                    evidence_id=evidence_id,
                    completed_by=completed_by,
                )
                new_milestones = tuple(
                    done if m.milestone_id == milestone_id else m
                    for m in target.milestones
                )
                self.targets[target_id] = replace(target, milestones=new_milestones)
                self._log(completed_on, "milestone_completed", completed_by,
                          target.measure_id,
                          {"target_id": target_id, "milestone_id": milestone_id,
                           "evidence_id": evidence_id})
                return done
        raise PolicyError(f"里程碑不存在: {milestone_id}")

    # ======================================================== 提案工作流
    def raise_proposal(self, proposal_type: ProposalType, measure_id: str,
                       raised_by: str, raised_on: date, rationale: str,
                       payload: dict | None = None,
                       proposal_id: str | None = None) -> ChangeProposal:
        if measure_id not in self.measures:
            raise PolicyError(f"措施不存在: {measure_id}")
        pid = proposal_id or self._next_id("p")
        if pid in self.proposals:
            raise PolicyError(f"提案编号已存在: {pid}")
        proposal = ChangeProposal(
            proposal_id=pid,
            proposal_type=ProposalType(proposal_type),
            measure_id=measure_id,
            payload=payload or {},
            status=ProposalStatus.RAISED,
            raised_by=raised_by,
            raised_on=raised_on,
            rationale=rationale,
        )
        # 立项时做一次结构性校验，给出早失败
        self._validate_proposal_payload(proposal)
        self.proposals[pid] = proposal
        self._log(raised_on, "proposal_raised", raised_by, measure_id,
                  {"proposal_id": pid, "type": proposal.proposal_type.value,
                   "rationale": rationale})
        return proposal

    def _validate_proposal_payload(self, p: ChangeProposal) -> None:
        m = self.measures[p.measure_id]
        if p.proposal_type == ProposalType.ADOPT:
            if m.status != MeasureStatus.PROPOSED:
                raise PolicyError("仅 PROPOSED 措施可提交审批")
        elif p.proposal_type == ProposalType.EXTEND:
            if m.status in (MeasureStatus.WITHDRAWN, MeasureStatus.SUPERSEDED):
                raise PolicyError("措施已终止，不能延期")
            reschedules = p.payload.get("milestone_reschedules", {})
            for mid, new_due in reschedules.items():
                ms = self._find_milestone(mid)
                if ms is None:
                    raise PolicyError(f"里程碑不存在: {mid}")
                if ms.status == MilestoneStatus.COMPLETED:
                    raise PolicyError(
                        f"已完成里程碑 {mid} 已冻结，不允许通过延期提案重排"
                    )
        elif p.proposal_type == ProposalType.WITHDRAW:
            if m.status == MeasureStatus.PROPOSED:
                raise PolicyError("尚未审批的措施应直接驳回审批提案，而非撤回")
            if m.status in (MeasureStatus.WITHDRAWN, MeasureStatus.SUPERSEDED):
                raise PolicyError("措施已终止，不能再次撤回")
        elif p.proposal_type == ProposalType.REPLACE:
            nm = p.payload.get("new_measure")
            if not nm or "measure_id" not in nm:
                raise PolicyError("替代提案须给出新措施定义 new_measure")
            if nm["measure_id"] in self.measures:
                raise PolicyError(f"新措施编号已存在: {nm['measure_id']}")
            for pid in nm.get("prerequisite_ids", ()):
                if pid not in self.measures:
                    raise PolicyError(f"前置措施不存在: {pid}")
        elif p.proposal_type == ProposalType.REPOINT:
            adds = tuple(p.payload.get("add_prerequisite_ids", ()))
            for pid in adds:
                if pid not in self.measures:
                    raise PolicyError(f"前置措施不存在: {pid}")
            if p.measure_id in adds:
                raise PolicyError("措施不能以前置方式引用自身")
            merged = tuple(dict.fromkeys((*m.prerequisite_ids, *adds)))
            if self._would_create_cycle(p.measure_id, merged):
                raise PolicyError("新的前置集合将形成依赖环")

    def _find_milestone(self, milestone_id: str) -> Milestone | None:
        for t in self.targets.values():
            for ms in t.milestones:
                if ms.milestone_id == milestone_id:
                    return ms
        return None

    def _find_milestone_target(self, milestone_id: str) -> Target | None:
        for t in self.targets.values():
            if any(ms.milestone_id == milestone_id for ms in t.milestones):
                return t
        return None

    # --------------------------------------------- 前置有效性（审批依据）
    def _is_effective_on(self, m: PolicyMeasure, on: date) -> bool:
        if m.status == MeasureStatus.PROPOSED:
            return False
        if m.status == MeasureStatus.WITHDRAWN:
            return m.withdrawn_on is not None and on < m.withdrawn_on
        if m.status == MeasureStatus.SUPERSEDED:
            return m.superseded_on is not None and on < m.superseded_on
        return m.effective_from <= on and (
            m.effective_until is None or on <= m.effective_until
        )

    def _invalid_prerequisites(self, measure: PolicyMeasure, on: date) -> list[dict]:
        """返回报告日当天无效的前置措施及原因（时间敏感：撤回 / 替代生效前仍有效）。"""
        result = []
        for pid in measure.prerequisite_ids:
            pre = self.measures.get(pid)
            if pre is None:
                result.append({"prerequisite_id": pid, "reason": "missing"})
                continue
            if pre.status == MeasureStatus.PROPOSED:
                result.append({"prerequisite_id": pid, "reason": "not_approved"})
            elif pre.status == MeasureStatus.WITHDRAWN:
                if pre.withdrawn_on is None or on >= pre.withdrawn_on:
                    result.append({"prerequisite_id": pid, "reason": "withdrawn",
                                   "since": pre.withdrawn_on.isoformat()
                                   if pre.withdrawn_on else None})
            elif pre.status == MeasureStatus.SUPERSEDED:
                if pre.superseded_on is None or on >= pre.superseded_on:
                    result.append({"prerequisite_id": pid, "reason": "superseded",
                                   "since": pre.superseded_on.isoformat()
                                   if pre.superseded_on else None})
            elif pre.effective_from > on:
                result.append({"prerequisite_id": pid, "reason": "future_effective",
                               "effective_from": pre.effective_from.isoformat()})
            elif pre.effective_until is not None and pre.effective_until < on:
                result.append({"prerequisite_id": pid, "reason": "expired",
                               "effective_until": pre.effective_until.isoformat()})
        return result

    def decide_proposal(self, proposal_id: str, approve: bool, decided_by: str,
                        decided_on: date, note: str | None = None) -> dict:
        p = self.proposals.get(proposal_id)
        if p is None:
            raise PolicyError(f"提案不存在: {proposal_id}")
        if p.status != ProposalStatus.RAISED:
            raise PolicyError(f"提案已决断: {p.status}")

        if not approve:
            self.proposals[proposal_id] = replace(
                p, status=ProposalStatus.REJECTED, decided_by=decided_by,
                decided_on=decided_on, decision_note=note,
            )
            self._log(decided_on, "proposal_rejected", decided_by, p.measure_id,
                      {"proposal_id": proposal_id, "note": note})
            return {"proposal_id": proposal_id, "status": ProposalStatus.REJECTED.value}

        # 审批通过：核心规则——只能基于当时有效的前置措施
        self._check_prerequisites_at_decision(p, decided_on)

        handler = {
            ProposalType.ADOPT: self._apply_adopt,
            ProposalType.EXTEND: self._apply_extend,
            ProposalType.REPLACE: self._apply_replace,
            ProposalType.REPOINT: self._apply_repoint,
            ProposalType.WITHDRAW: self._apply_withdraw,
        }[p.proposal_type]
        impact = handler(p, decided_by, decided_on)

        self.proposals[proposal_id] = replace(
            p, status=ProposalStatus.APPROVED, decided_by=decided_by,
            decided_on=decided_on, decision_note=note,
        )
        # 事件因果顺序：提案批准 → 措施效果 → 影响清单
        self._log(decided_on, "proposal_approved", decided_by, p.measure_id,
                  {"proposal_id": proposal_id,
                   "type": p.proposal_type.value})
        self._log_effect(p, decided_on, decided_by, impact)
        if impact is not None:
            self.impacts[proposal_id] = impact
            self._log(decided_on, "impact_registered", decided_by,
                      impact.source_measure_id,
                      {"proposal_id": proposal_id,
                       "affected": [i.dependent_measure_id for i in impact.items]})
        return {
            "proposal_id": proposal_id,
            "status": ProposalStatus.APPROVED.value,
            "impact": self._impact_to_prim(impact) if impact else None,
        }

    def _log_effect(self, p: ChangeProposal, on: date, by: str,
                    impact: ImpactReport | None) -> None:
        if p.proposal_type == ProposalType.EXTEND:
            self._log(on, "measure_extended", by, p.measure_id,
                      {"new_effective_until": p.payload.get("new_effective_until"),
                       "reschedules": p.payload.get("milestone_reschedules", {})})
        elif p.proposal_type == ProposalType.REPLACE and impact is not None:
            new_id = next(
                (mid for mid, mm in self.measures.items()
                 if mm.replaces_id == p.measure_id), None)
            self._log(on, "measure_replaced", by, p.measure_id,
                      {"new_measure_id": new_id})
        elif p.proposal_type == ProposalType.WITHDRAW:
            self._log(on, "measure_withdrawn", by, p.measure_id,
                      {"reason": p.payload.get("reason", p.rationale),
                       "affected": [i.dependent_measure_id
                                    for i in impact.items]
                       if impact else []})
        elif p.proposal_type == ProposalType.REPOINT:
            self._log(on, "prerequisites_repointed", by, p.measure_id,
                      {"added": list(p.payload.get("add_prerequisite_ids", ())),
                       "removed": list(p.payload.get("remove_prerequisite_ids", ()))})
        elif p.proposal_type == ProposalType.ADOPT:
            self._log(on, "measure_adopted", by, p.measure_id,
                      {"approval_date": on.isoformat()})

    def _check_prerequisites_at_decision(self, p: ChangeProposal,
                                         on: date) -> None:
        """审批日前置必须有效。

        - ADOPT / EXTEND：措施当前前置集合；
        - REPOINT：增删之后的最终前置集合（允许通过重指摘除失效前置）；
        - REPLACE：新措施的前置集合；
        - WITHDRAW：不设此前置门控（撤回往往正是因为前置失效）。
        """
        if p.proposal_type == ProposalType.WITHDRAW:
            return
        if p.proposal_type == ProposalType.REPLACE:
            spec = p.payload["new_measure"]
            prereq_ids = tuple(spec.get("prerequisite_ids", ()))
            probe = PolicyMeasure(
                measure_id=spec["measure_id"], owner="",
                effective_from=on, effective_until=None,
                prerequisite_ids=prereq_ids, status=MeasureStatus.APPROVED,
            )
        elif p.proposal_type == ProposalType.REPOINT:
            m = self.measures[p.measure_id]
            remove = set(p.payload.get("remove_prerequisite_ids", ()))
            prereq_ids = tuple(
                x for x in dict.fromkeys(
                    (*m.prerequisite_ids, *p.payload.get("add_prerequisite_ids", ()))
                ) if x not in remove
            )
            probe = replace(m, prerequisite_ids=prereq_ids)
        else:
            probe = self.measures[p.measure_id]

        invalid = self._invalid_prerequisites(probe, on)
        if invalid:
            raise PolicyError(
                "审批依据不成立：以下前置措施在审批日非有效: "
                + "; ".join(f"{i['prerequisite_id']}({i['reason']})" for i in invalid)
            )

    # ---- 提案生效处理 --------------------------------------------------
    def _approval_basis(self, measure: PolicyMeasure, on: date):
        from .contracts import PrerequisiteSnapshot

        return tuple(
            PrerequisiteSnapshot(
                measure_id=pid,
                status=self.measures[pid].status.value,
                effective_from=self.measures[pid].effective_from,
                effective_until=self.measures[pid].effective_until,
                captured_on=on,
            )
            for pid in measure.prerequisite_ids
        )

    def _apply_adopt(self, p: ChangeProposal, by: str, on: date) -> None:
        m = self.measures[p.measure_id]
        self.measures[p.measure_id] = replace(
            m,
            status=MeasureStatus.APPROVED,
            approval_date=on,
            approval_basis=self._approval_basis(m, on),
        )
        return None

    def _apply_extend(self, p: ChangeProposal, by: str, on: date) -> None:
        m = self.measures[p.measure_id]
        new_until = _parse_date(p.payload.get("new_effective_until"))
        if new_until is not None:
            if m.effective_until is not None and new_until < m.effective_until:
                raise PolicyError("延期不得缩短原生效区间")
            if new_until < on:
                raise PolicyError("新生效截止日不能早于审批日")
            self.measures[p.measure_id] = replace(m, effective_until=new_until)
            m = self.measures[p.measure_id]
        for mid, due_iso in p.payload.get("milestone_reschedules", {}).items():
            target = self._find_milestone_target(mid)
            ms = next(x for x in target.milestones if x.milestone_id == mid)
            new_due = _parse_date(due_iso)
            if new_due < ms.due_date:
                raise PolicyError(f"里程碑 {mid} 只能延后，不能提前")
            self.targets[target.target_id] = replace(
                target,
                milestones=tuple(
                    replace(x, due_date=new_due) if x.milestone_id == mid else x
                    for x in target.milestones
                ),
            )
        return None

    def _apply_replace(self, p: ChangeProposal, by: str,
                       on: date) -> ImpactReport:
        old = self.measures[p.measure_id]
        spec = p.payload["new_measure"]
        new = PolicyMeasure(
            measure_id=spec["measure_id"],
            owner=spec.get("owner", old.owner),
            effective_from=_parse_date(spec["effective_from"]),
            effective_until=_parse_date(spec.get("effective_until")),
            prerequisite_ids=tuple(spec.get("prerequisite_ids", ())),
            status=MeasureStatus.APPROVED,
            title=spec.get("title", old.title),
            approval_date=on,
            replaces_id=old.measure_id,
            approval_basis=self._approval_basis(
                PolicyMeasure(
                    measure_id=spec["measure_id"], owner="",
                    effective_from=on, effective_until=None,
                    prerequisite_ids=tuple(spec.get("prerequisite_ids", ())),
                    status=MeasureStatus.APPROVED,
                ),
                on,
            ),
        )
        if new.effective_until is not None and \
                new.effective_until < new.effective_from:
            raise PolicyError("新措施生效截止日不得早于起始日")
        self.measures[new.measure_id] = new
        # 旧措施标记为被替代；若仍在生效，替代自审批日起生效，原计划区间保留留痕
        self.measures[old.measure_id] = replace(
            old,
            status=MeasureStatus.SUPERSEDED,
            superseded_on=on if self._is_effective_on(old, on)
            else old.superseded_on,
        )
        self._log(on, "measure_created", by, new.measure_id,
                  {"replaces": old.measure_id, "via_proposal": p.proposal_id})
        return self._build_impact(old.measure_id, ProposalType.REPLACE.value, on)

    def _apply_repoint(self, p: ChangeProposal, by: str, on: date) -> None:
        m = self.measures[p.measure_id]
        add = tuple(p.payload.get("add_prerequisite_ids", ()))
        remove = set(p.payload.get("remove_prerequisite_ids", ()))
        merged = tuple(x for x in dict.fromkeys((*m.prerequisite_ids, *add))
                       if x not in remove)
        if p.measure_id in merged:
            raise PolicyError("措施不能引用自身")
        self.measures[p.measure_id] = replace(m, prerequisite_ids=merged)
        return None

    def _apply_withdraw(self, p: ChangeProposal, by: str,
                        on: date) -> ImpactReport:
        m = self.measures[p.measure_id]
        if m.status in (MeasureStatus.WITHDRAWN, MeasureStatus.SUPERSEDED):
            raise PolicyError("措施已终止，不能撤回")
        if m.status == MeasureStatus.PROPOSED:
            raise PolicyError("尚未审批的措施应直接驳回，而非撤回")
        impact = self._build_impact(m.measure_id, ProposalType.WITHDRAW.value, on)
        self.measures[m.measure_id] = replace(
            m,
            status=MeasureStatus.WITHDRAWN,
            withdrawn_on=on,
            withdrawal_reason=p.payload.get("reason", p.rationale),
        )
        return impact

    # ------------------------------------------------------- 影响清单
    def _dependents(self, measure_id: str) -> list[str]:
        """直接 + 传递依赖方（按当前引用关系），环检测保护。"""
        seen: set[str] = set()
        stack = [measure_id]
        while stack:
            current = stack.pop()
            for mid, m in self.measures.items():
                if current in m.prerequisite_ids and mid not in seen:
                    seen.add(mid)
                    stack.append(mid)
        return sorted(seen)

    def _build_impact(self, source_id: str, change_type: str,
                      on: date) -> ImpactReport:
        items = []
        for dep_id in self._dependents(source_id):
            dep = self.measures[dep_id]
            open_ms: list[tuple[str, str, str]] = []
            done_ms: list[tuple[str, str, str]] = []
            for t in self.targets.values():
                if t.measure_id != dep_id:
                    continue
                for ms in t.milestones:
                    if ms.status == MilestoneStatus.COMPLETED:
                        done_ms.append(
                            (ms.milestone_id, ms.name,
                             ms.completed_date.isoformat())
                        )
                    else:
                        open_ms.append(
                            (ms.milestone_id, ms.name, ms.due_date.isoformat())
                        )
            items.append(
                ImpactItem(
                    dependent_measure_id=dep_id,
                    owner=dep.owner,
                    blocker=(
                        f"前置措施 {source_id} 已于 {on.isoformat()} "
                        f"{'撤回' if change_type == 'withdraw' else '被替代'}，"
                        "需重新确认前置引用或提交变更提案"
                    ),
                    open_milestones=tuple(open_ms),
                    preserved_completed_milestones=tuple(done_ms),
                )
            )
        return ImpactReport(
            source_measure_id=source_id,
            change_type=change_type,
            occurred_on=on,
            items=tuple(items),
        )

    # ======================================================== 查询 / 报告
    def status_on(self, measure_id: str, on: date) -> MeasureStatus:
        """按报告日推导措施状态（历史快照不被后续撤回/替代覆盖）。"""
        m = self.measures[measure_id]
        # 审批日之前的报告日，措施在当时仍是 PROPOSED
        if m.approval_date is not None and on < m.approval_date:
            return MeasureStatus.PROPOSED
        if m.status == MeasureStatus.WITHDRAWN:
            if m.withdrawn_on is not None and on >= m.withdrawn_on:
                return MeasureStatus.WITHDRAWN
        elif m.status == MeasureStatus.SUPERSEDED:
            if m.superseded_on is not None and on >= m.superseded_on:
                return MeasureStatus.SUPERSEDED
        if m.status == MeasureStatus.PROPOSED:
            return MeasureStatus.PROPOSED
        if on < m.effective_from:
            return MeasureStatus.APPROVED
        if m.effective_until is not None and on > m.effective_until:
            if m.status in (MeasureStatus.WITHDRAWN, MeasureStatus.SUPERSEDED):
                return m.status
            return MeasureStatus.EXPIRED
        return MeasureStatus.ACTIVE

    def blockers_on(self, measure_id: str, on: date,
                    _stack: tuple[str, ...] = ()) -> dict:
        """报告日阻塞原因：前置无效、传递阻塞、依赖环、措施自身终止。"""
        m = self.measures[measure_id]
        reasons: list[dict] = []
        status = self.status_on(measure_id, on)

        if measure_id in _stack:
            return {"measure_id": measure_id, "blocked": True,
                    "reasons": [{"kind": "dependency_cycle"}]}
        if status == MeasureStatus.WITHDRAWN:
            reasons.append({"kind": "measure_withdrawn",
                            "since": m.withdrawn_on.isoformat()
                            if m.withdrawn_on else None})
        elif status == MeasureStatus.SUPERSEDED:
            reasons.append({"kind": "measure_superseded"})

        for invalid in self._invalid_prerequisites(m, on):
            reasons.append({"kind": "prerequisite_invalid", **invalid})
        for pid in m.prerequisite_ids:
            if pid not in self.measures:
                continue
            pre = self.measures[pid]
            if pre.status == MeasureStatus.PROPOSED:
                continue
            sub = self.blockers_on(pid, on, _stack + (measure_id,))
            if sub["blocked"]:
                reasons.append({"kind": "transitive_blocked",
                                "prerequisite_id": pid,
                                "upstream_reasons": sub["reasons"]})
        return {"measure_id": measure_id, "blocked": bool(reasons),
                "reasons": reasons}

    def _milestone_view(self, ms: Milestone, on: date) -> dict:
        boundary = None
        if ms.evidence_id:
            boundary = self.evidence_boundary(ms.evidence_id)["boundary"]
        return {
            "milestone_id": ms.milestone_id,
            "name": ms.name,
            "due_date": ms.due_date.isoformat(),
            "status": ms.status.value,
            "completed_date": ms.completed_date.isoformat()
            if ms.completed_date else None,
            "evidence_id": ms.evidence_id,
            "overdue": ms.status == MilestoneStatus.PLANNED
            and ms.due_date < on,
            "frozen": ms.status == MilestoneStatus.COMPLETED,
            "evidence_boundary": boundary,
        }

    def measure_report(self, measure_id: str, on: date) -> dict:
        m = self.measures[measure_id]
        blockers = self.blockers_on(measure_id, on)
        targets = []
        evidence_warnings = []
        for t in self.targets.values():
            if t.measure_id != measure_id:
                continue
            ms_views = [self._milestone_view(ms, on) for ms in t.milestones]
            targets.append({
                "target_id": t.target_id,
                "name": t.name,
                "unit": t.unit,
                "target_value": t.target_value,
                "due_date": t.due_date.isoformat(),
                "additive_across_departments": t.additive_across_departments,
                "completed_milestones": sum(
                    1 for x in ms_views if x["status"] == "completed"),
                "milestones": ms_views,
            })
            for ms in t.milestones:
                if ms.evidence_id:
                    boundary = self.evidence_boundary(ms.evidence_id)
                    if boundary["double_counted"]:
                        evidence_warnings.append({
                            "milestone_id": ms.milestone_id,
                            "evidence_id": ms.evidence_id,
                            "claimed_share_total":
                                boundary["claimed_share_total"],
                            "warning": "同一凭据被多部门重复统计，份额之和超过 100%",
                        })
        return {
            "measure_id": measure_id,
            "title": m.title,
            "owner": m.owner,
            "status": self.status_on(measure_id, on).value,
            "effective_from": m.effective_from.isoformat(),
            "effective_until": m.effective_until.isoformat()
            if m.effective_until else None,
            "prerequisite_ids": list(m.prerequisite_ids),
            "approval_date": m.approval_date.isoformat() if m.approval_date else None,
            "withdrawn_on": m.withdrawn_on.isoformat() if m.withdrawn_on else None,
            "superseded_on": m.superseded_on.isoformat() if m.superseded_on else None,
            "replaces_id": m.replaces_id,
            "approval_basis": [
                {
                    "measure_id": b.measure_id,
                    "status": b.status,
                    "effective_from": b.effective_from.isoformat(),
                    "effective_until": b.effective_until.isoformat()
                    if b.effective_until else None,
                    "captured_on": b.captured_on.isoformat(),
                }
                for b in m.approval_basis
            ],
            "blocked": blockers["blocked"],
            "blockers": blockers["reasons"],
            "targets": targets,
            "evidence_warnings": evidence_warnings,
        }

    def daily_report(self, on: date, department: str | None = None) -> dict:
        rows = []
        for mid, m in sorted(self.measures.items()):
            if department and m.owner != department:
                continue
            rows.append(self.measure_report(mid, on))
        return {
            "report_date": on.isoformat(),
            "department": department,
            "measures": rows,
            "summary": {
                "total": len(rows),
                "blocked": sum(1 for r in rows if r["blocked"]),
                "active": sum(1 for r in rows if r["status"] == "active"),
                "withdrawn": sum(1 for r in rows
                                 if r["status"] == "withdrawn"),
                "superseded": sum(1 for r in rows
                                  if r["status"] == "superseded"),
                "double_count_warnings": sum(
                    len(r["evidence_warnings"]) for r in rows),
            },
        }

    def change_history(self, measure_id: str) -> dict:
        """措施变更历史：自身事件 + 上游影响记录 + 凭据采用事件。"""
        if measure_id not in self.measures:
            raise PolicyError(f"措施不存在: {measure_id}")
        related_targets = {t.target_id for t in self.targets.values()
                           if t.measure_id == measure_id}
        related_evidence = {eid for eid, e in self.evidence.items()
                            if e.target_id in related_targets}
        events = [
            e for e in self.events
            if e["measure_id"] == measure_id
            or e["detail"].get("evidence_id") in related_evidence
        ]
        impacts = []
        for pid, report in self.impacts.items():
            if report.source_measure_id == measure_id:
                impacts.append({"direction": "outgoing",
                                **self._impact_to_prim(report)})
            elif any(i.dependent_measure_id == measure_id for i in report.items):
                impacts.append({"direction": "incoming",
                                **self._impact_to_prim(report)})
        return {"measure_id": measure_id, "events": events, "impacts": impacts}

    @staticmethod
    def _impact_to_prim(report: ImpactReport) -> dict:
        return {
            "source_measure_id": report.source_measure_id,
            "change_type": report.change_type,
            "occurred_on": report.occurred_on.isoformat(),
            "items": [
                {
                    "dependent_measure_id": i.dependent_measure_id,
                    "owner": i.owner,
                    "blocker": i.blocker,
                    "open_milestones": [list(x) for x in i.open_milestones],
                    "preserved_completed_milestones": [
                        list(x) for x in i.preserved_completed_milestones
                    ],
                }
                for i in report.items
            ],
        }


def _parse_date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)
