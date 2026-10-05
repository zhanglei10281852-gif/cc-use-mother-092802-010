"""核心业务规则验证。"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from transition_policy.contracts import (  # noqa: E402
    MeasureStatus,
    Milestone,
    MilestoneStatus,
    ProposalType,
    SharingBoundary,
)
from transition_policy.store import PolicyError, Registry  # noqa: E402

D = date.fromisoformat


class ApprovalBasisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = Registry()
        self.reg.add_body("b1", "能源司", "energy")
        self.reg.add_body("b2", "交通司", "transport")
        self.reg.draft_measure("energy", D("2026-01-01"), None,
                               measure_id="m1", title="电网")
        self.reg.draft_measure("transport", D("2026-06-01"), None,
                               prerequisite_ids=("m1",),
                               measure_id="m2", title="充电")

    def _adopt(self, mid, on, pid):
        self.reg.raise_proposal(ProposalType.ADOPT, mid, "b1", on, "",
                                proposal_id=pid)
        return self.reg.decide_proposal(pid, True, "committee", on)

    def test_adopt_rejected_when_prerequisite_not_approved(self):
        # m1 仍为 PROPOSED，m2 不能审批
        self.reg.raise_proposal(ProposalType.ADOPT, "m2", "b2",
                                D("2026-05-01"), "", proposal_id="p1")
        with self.assertRaises(PolicyError) as ctx:
            self.reg.decide_proposal("p1", True, "committee", D("2026-05-01"))
        self.assertIn("not_approved", str(ctx.exception))
        self.assertEqual(
            self.reg.proposals["p1"].status.value, "raised",
            "失败的审批不能改变提案状态",
        )

    def test_adopt_rejected_when_prerequisite_expired_at_decision(self):
        # m1 生效区间 2026-01-01 ~ 2026-03-31
        self.reg.measures["m1"] = self.reg.measures["m1"].__class__(
            "m1", "energy", D("2026-01-01"), D("2026-03-31"), (),
            MeasureStatus.PROPOSED, "电网")
        self._adopt("m1", D("2026-01-05"), "pa")
        self.reg.raise_proposal(ProposalType.ADOPT, "m2", "b2",
                                D("2026-04-01"), "", proposal_id="p2")
        with self.assertRaises(PolicyError) as ctx:
            self.reg.decide_proposal("p2", True, "committee", D("2026-04-01"))
        self.assertIn("expired", str(ctx.exception))

    def test_adopt_rejected_when_prerequisite_future_effective(self):
        # m1 审批通过但生效起始日在未来
        self.reg.measures["m1"] = self.reg.measures["m1"].__class__(
            "m1", "energy", D("2027-01-01"), None, (),
            MeasureStatus.PROPOSED, "电网")
        self._adopt("m1", D("2026-05-01"), "pa")
        self.reg.raise_proposal(ProposalType.ADOPT, "m2", "b2",
                                D("2026-05-02"), "", proposal_id="p2")
        with self.assertRaises(PolicyError) as ctx:
            self.reg.decide_proposal("p2", True, "committee", D("2026-05-02"))
        self.assertIn("future_effective", str(ctx.exception))

    def test_approval_basis_snapshot_is_captured(self):
        self._adopt("m1", D("2025-12-20"), "p1")
        self._adopt("m2", D("2026-05-20"), "p2")
        m2 = self.reg.measures["m2"]
        self.assertEqual(m2.approval_date, D("2026-05-20"))
        self.assertEqual(len(m2.approval_basis), 1)
        basis = m2.approval_basis[0]
        self.assertEqual(basis.measure_id, "m1")
        self.assertEqual(basis.status, "approved")
        self.assertEqual(basis.captured_on, D("2026-05-20"))

    def test_reject_proposal_leaves_measure_proposed(self):
        self._adopt("m1", D("2025-12-20"), "p1")
        self.reg.raise_proposal(ProposalType.ADOPT, "m2", "b2",
                                D("2026-05-01"), "依据不足", proposal_id="p2")
        result = self.reg.decide_proposal("p2", False, "committee",
                                          D("2026-05-05"), "暂缓")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.reg.measures["m2"].status, MeasureStatus.PROPOSED)


class WithdrawalImpactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = Registry()
        self.reg.add_body("b1", "能源司", "energy")
        self.reg.add_body("b2", "交通司", "transport")
        self.reg.add_body("b3", "工业司", "manufacturing")
        self.reg.draft_measure("energy", D("2026-01-01"), None,
                               measure_id="m1", title="电网")
        self.reg.draft_measure("transport", D("2026-06-01"), None,
                               prerequisite_ids=("m1",), measure_id="m2",
                               title="充电")
        self.reg.draft_measure("manufacturing", D("2027-01-01"), None,
                               prerequisite_ids=("m1", "m2"), measure_id="m3",
                               title="绿电制造")
        for mid, on, pid in (("m1", D("2025-12-20"), "p1"),
                             ("m2", D("2026-05-20"), "p2"),
                             ("m3", D("2026-12-20"), "p3")):
            self.reg.raise_proposal(ProposalType.ADOPT, mid, "b", on, "",
                                    proposal_id=pid)
            self.reg.decide_proposal(pid, True, "committee", on)

        self.reg.add_target(
            "m3", "绿电比例", "%", 35.0, D("2030-12-31"), target_id="t3",
            milestones=(
                Milestone("ms-open", "t3", "园区签约", D("2027-03-31")),
                Milestone("ms-done", "t3", "首批验收", D("2027-06-30")),
            ),
        )
        self.reg.complete_milestone("t3", "ms-done", D("2027-06-28"), "b3")

    def _withdraw_m1(self, on=D("2027-09-01")):
        self.reg.raise_proposal(ProposalType.WITHDRAW, "m1", "b1", on,
                                "资金调整", payload={"reason": "资金调整"},
                                proposal_id="pw")
        return self.reg.decide_proposal("pw", True, "committee", on)

    def test_withdraw_builds_transitive_impact_list(self):
        result = self._withdraw_m1()
        impact = result["impact"]
        affected = {i["dependent_measure_id"] for i in impact["items"]}
        self.assertEqual(affected, {"m2", "m3"})  # 直接 + 传递依赖

    def test_impact_preserves_completed_milestones_and_lists_open_ones(self):
        result = self._withdraw_m1()
        item_m3 = next(i for i in result["impact"]["items"]
                       if i["dependent_measure_id"] == "m3")
        open_ids = [row[0] for row in item_m3["open_milestones"]]
        done_ids = [row[0]
                    for row in item_m3["preserved_completed_milestones"]]
        self.assertEqual(open_ids, ["ms-open"])
        self.assertEqual(done_ids, ["ms-done"])
        # 已完成里程碑在存储中仍然冻结为完成状态、完成日不变
        target = self.reg.targets["t3"]
        ms_done = next(m for m in target.milestones if m.milestone_id == "ms-done")
        self.assertEqual(ms_done.status, MilestoneStatus.COMPLETED)
        self.assertEqual(ms_done.completed_date, D("2027-06-28"))

    def test_past_snapshot_remains_active_after_withdrawal(self):
        # 撤回前的历史报告日，状态不得被后续撤回覆盖
        self.assertEqual(
            self.reg.status_on("m1", D("2027-08-31")), MeasureStatus.ACTIVE)
        self._withdraw_m1()
        self.assertEqual(
            self.reg.status_on("m1", D("2027-08-31")), MeasureStatus.ACTIVE,
            "历史快照不能被撤回静默改写",
        )
        self.assertEqual(
            self.reg.status_on("m1", D("2027-09-01")), MeasureStatus.WITHDRAWN)
        self.assertEqual(
            self.reg.status_on("m1", D("2028-01-01")), MeasureStatus.WITHDRAWN)

    def test_dependents_blocked_after_withdrawal(self):
        self._withdraw_m1()
        blockers = self.reg.blockers_on("m3", D("2027-09-02"))
        self.assertTrue(blockers["blocked"])
        kinds = {r["kind"] for r in blockers["reasons"]}
        self.assertIn("prerequisite_invalid", kinds)
        self.assertIn("transitive_blocked", kinds)

    def test_dependents_not_blocked_before_withdrawal_date(self):
        self._withdraw_m1(D("2027-09-01"))
        blockers = self.reg.blockers_on("m3", D("2027-08-31"))
        self.assertFalse(blockers["blocked"])

    def test_double_withdraw_is_rejected(self):
        self._withdraw_m1()
        with self.assertRaises(PolicyError):
            self.reg.raise_proposal(ProposalType.WITHDRAW, "m1", "b1",
                                    D("2027-10-01"), "再次撤回",
                                    proposal_id="pw2")


class ExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = Registry()
        self.reg.add_body("b1", "能源司", "energy")
        self.reg.draft_measure("energy", D("2026-01-01"), D("2027-12-31"),
                               measure_id="m1", title="电网")
        self.reg.add_target(
            "m1", "装机占比", "%", 50.0, D("2027-12-31"), target_id="t1",
            milestones=(
                Milestone("ms1", "t1", "一期", D("2026-06-30")),
                Milestone("ms2", "t1", "二期", D("2027-06-30")),
            ),
        )
        self.reg.raise_proposal(ProposalType.ADOPT, "m1", "b1",
                                D("2025-12-20"), "", proposal_id="pa")
        self.reg.decide_proposal("pa", True, "committee", D("2025-12-20"))
        self.reg.complete_milestone("t1", "ms1", D("2026-06-25"), "b1")

    def test_extend_cannot_reschedule_completed_milestone(self):
        with self.assertRaises(PolicyError) as ctx:
            self.reg.raise_proposal(
                ProposalType.EXTEND, "m1", "b1", D("2027-01-10"), "整体延期",
                payload={"milestone_reschedules":
                         {"ms1": "2026-12-31"}},
                proposal_id="pe-bad")
        self.assertIn("已冻结", str(ctx.exception))

    def test_extend_moves_open_milestone_and_interval(self):
        self.reg.raise_proposal(
            ProposalType.EXTEND, "m1", "b1", D("2027-01-10"), "设备到货延迟",
            payload={"new_effective_until": "2028-12-31",
                     "milestone_reschedules": {"ms2": "2027-12-31"}},
            proposal_id="pe")
        result = self.reg.decide_proposal("pe", True, "committee",
                                          D("2027-01-12"))
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.reg.measures["m1"].effective_until,
                         D("2028-12-31"))
        ms2 = next(m for m in self.reg.targets["t1"].milestones
                   if m.milestone_id == "ms2")
        self.assertEqual(ms2.due_date, D("2027-12-31"))
        # 已完成里程碑完全未被触碰
        ms1 = next(m for m in self.reg.targets["t1"].milestones
                   if m.milestone_id == "ms1")
        self.assertEqual(ms1.completed_date, D("2026-06-25"))

    def test_extend_cannot_shorten_interval_or_bring_milestone_forward(self):
        self.reg.raise_proposal(
            ProposalType.EXTEND, "m1", "b1", D("2027-01-10"), "误填",
            payload={"new_effective_until": "2027-06-30"}, proposal_id="pe1")
        with self.assertRaises(PolicyError):
            self.reg.decide_proposal("pe1", True, "committee", D("2027-01-12"))
        self.reg.raise_proposal(
            ProposalType.EXTEND, "m1", "b1", D("2027-01-10"), "误填",
            payload={"milestone_reschedules": {"ms2": "2027-01-01"}},
            proposal_id="pe2")
        with self.assertRaises(PolicyError):
            self.reg.decide_proposal("pe2", True, "committee", D("2027-01-12"))


class ReplaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = Registry()
        self.reg.add_body("b1", "能源司", "energy")
        self.reg.draft_measure("energy", D("2026-01-01"), None,
                               measure_id="m1", title="旧电网方案")
        self.reg.draft_measure("transport", D("2026-06-01"), None,
                               prerequisite_ids=("m1",), measure_id="m2",
                               title="充电")
        for mid, on, pid in (("m1", D("2025-12-20"), "pa1"),
                             ("m2", D("2026-05-20"), "pa2")):
            self.reg.raise_proposal(ProposalType.ADOPT, mid, "b1", on, "",
                                    proposal_id=pid)
            self.reg.decide_proposal(pid, True, "committee", on)

    def test_replace_creates_new_measure_and_impacts_dependents(self):
        self.reg.raise_proposal(
            ProposalType.REPLACE, "m1", "b1", D("2027-02-01"),
            "技术路线调整，以新型电网方案替代",
            payload={"new_measure": {
                "measure_id": "m1b",
                "title": "新型电网方案",
                "owner": "energy",
                "effective_from": "2027-03-01",
                "effective_until": None,
                "prerequisite_ids": [],
            }},
            proposal_id="pr")
        result = self.reg.decide_proposal("pr", True, "committee",
                                          D("2027-02-15"))
        self.assertEqual(self.reg.measures["m1"].status,
                         MeasureStatus.SUPERSEDED)
        self.assertEqual(self.reg.measures["m1"].superseded_on, D("2027-02-15"))
        self.assertEqual(self.reg.measures["m1b"].replaces_id, "m1")
        self.assertEqual(self.reg.measures["m1b"].status,
                         MeasureStatus.APPROVED)
        affected = {i["dependent_measure_id"]
                    for i in result["impact"]["items"]}
        self.assertIn("m2", affected)

    def test_replace_requires_effective_prereqs_for_new_measure(self):
        # 新增一项已过期的措施 m0，作为新措施前置
        self.reg.draft_measure("energy", D("2026-01-01"), D("2026-12-31"),
                               measure_id="m0", title="过期措施")
        self.reg.raise_proposal(ProposalType.ADOPT, "m0", "b1",
                                D("2026-01-05"), "", proposal_id="pa0")
        self.reg.decide_proposal("pa0", True, "committee", D("2026-01-05"))
        self.reg.raise_proposal(
            ProposalType.REPLACE, "m1", "b1", D("2027-02-01"), "替代",
            payload={"new_measure": {
                "measure_id": "m1b", "title": "新方案", "owner": "energy",
                "effective_from": "2027-03-01",
                "prerequisite_ids": ["m0"]}},
            proposal_id="pr")
        with self.assertRaises(PolicyError) as ctx:
            self.reg.decide_proposal("pr", True, "committee", D("2027-02-15"))
        self.assertIn("expired", str(ctx.exception))
        # 审批失败：旧措施仍未被标记替代、新措施未登记
        self.assertNotEqual(self.reg.measures["m1"].status,
                            MeasureStatus.SUPERSEDED)
        self.assertNotIn("m1b", self.reg.measures)


class RepointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = Registry()
        self.reg.add_body("b1", "能源司", "energy")
        for mid, owner, pre in (("m1", "energy", ()), ("m2", "transport", ("m1",)),
                                ("m3", "energy", ())):
            self.reg.draft_measure(owner, D("2026-01-01"), None,
                                   prerequisite_ids=pre, measure_id=mid,
                                   title=mid)
        for mid, on, pid in (("m1", D("2025-12-20"), "p1"),
                             ("m3", D("2025-12-20"), "p3"),
                             ("m2", D("2026-05-20"), "p2")):
            self.reg.raise_proposal(ProposalType.ADOPT, mid, "b1", on, "",
                                    proposal_id=pid)
            self.reg.decide_proposal(pid, True, "committee", on)

    def test_repoint_to_valid_prerequisite_removes_block_after_withdrawal(self):
        # m1 撤回，m2 被阻塞
        self.reg.raise_proposal(ProposalType.WITHDRAW, "m1", "b1",
                                D("2027-01-01"), "废止",
                                payload={"reason": "废止"}, proposal_id="pw")
        self.reg.decide_proposal("pw", True, "committee", D("2027-01-01"))
        self.assertTrue(self.reg.blockers_on("m2", D("2027-01-02"))["blocked"])

        # 通过 REPOINT 摘除 m1、改指 m3（审批日 m3 有效）
        self.reg.raise_proposal(
            ProposalType.REPOINT, "m2", "b1", D("2027-01-05"),
            "前置改由 m3 承接",
            payload={"add_prerequisite_ids": ["m3"],
                     "remove_prerequisite_ids": ["m1"]},
            proposal_id="pr")
        self.reg.decide_proposal("pr", True, "committee", D("2027-01-10"))
        self.assertEqual(self.reg.measures["m2"].prerequisite_ids, ("m3",))
        self.assertFalse(
            self.reg.blockers_on("m2", D("2027-01-11"))["blocked"])

    def test_repoint_rejects_cycle(self):
        with self.assertRaises(PolicyError):
            self.reg.raise_proposal(
                ProposalType.REPOINT, "m1", "b1", D("2027-01-05"), "成环",
                payload={"add_prerequisite_ids": ["m2"]},
                proposal_id="prc")


class EvidenceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = Registry()
        self.reg.add_body("b1", "能源司", "energy")
        self.reg.draft_measure("energy", D("2026-01-01"), None,
                               measure_id="m1", title="电网")
        self.reg.add_target("m1", "装机", "%", 50.0, D("2028-12-31"),
                            target_id="t1")

    def test_exclusive_when_single_adoption(self):
        self.reg.register_evidence(
            "t1", "计量单", "metering", "DOC-1", "energy", 40.0, "%",
            D("2026-01-01"), D("2026-06-30"), evidence_id="e1")
        boundary = self.reg.evidence_boundary("e1")
        self.assertEqual(boundary["boundary"], SharingBoundary.EXCLUSIVE.value)
        self.assertFalse(boundary["double_counted"])

    def test_shared_when_shares_sum_to_one(self):
        self.reg.register_evidence(
            "t1", "计量单", "metering", "DOC-1", "energy", 40.0, "%",
            D("2026-01-01"), D("2026-06-30"), evidence_id="e1")
        from transition_policy.contracts import EvidenceAdoption

        self.reg.evidence["e1"] = self.reg.evidence["e1"].__class__(
            **{**self.reg.evidence["e1"].__dict__,
               "adoptions": (
                   EvidenceAdoption("energy", 0.8, "a", D("2026-07-01")),
                   EvidenceAdoption("manufacturing", 0.2, "b",
                                    D("2026-07-02")),
               )})
        boundary = self.reg.evidence_boundary("e1")
        self.assertEqual(boundary["boundary"], SharingBoundary.SHARED.value)
        self.assertEqual(boundary["claimed_share_total"], 1.0)

    def test_double_count_flagged_when_shares_exceed_one(self):
        from transition_policy.contracts import EvidenceAdoption

        self.reg.register_evidence(
            "t1", "核查报告", "audit", "DOC-2", "transport", 1000.0, "座",
            D("2026-01-01"), D("2026-06-30"), evidence_id="e2",
            adoptions=(
                EvidenceAdoption("transport", 0.7, "全额", D("2026-07-01")),
                EvidenceAdoption("manufacturing", 0.7, "未扣减",
                                 D("2026-07-02")),
            ))
        boundary = self.reg.evidence_boundary("e2")
        self.assertEqual(boundary["boundary"],
                         SharingBoundary.DOUBLE_COUNT.value)
        self.assertTrue(boundary["double_counted"])

    def test_same_department_cannot_adopt_twice(self):
        self.reg.register_evidence(
            "t1", "计量单", "metering", "DOC-1", "energy", 40.0, "%",
            D("2026-01-01"), D("2026-06-30"), evidence_id="e1")
        # 首次采用成功
        self.reg.adopt_evidence("e1", "energy", 0.5, "首次", D("2026-07-01"))
        # 同一部门第二次采用必须被拒绝
        with self.assertRaises(PolicyError):
            self.reg.adopt_evidence("e1", "energy", 0.3, "再次",
                                    D("2026-07-02"))

    def test_report_carries_double_count_warning(self):
        from transition_policy.contracts import (
            EvidenceAdoption,
            Milestone,
            ProposalType,
        )

        self.reg.raise_proposal(ProposalType.ADOPT, "m1", "b1",
                                D("2025-12-20"), "", proposal_id="pa")
        self.reg.decide_proposal("pa", True, "committee", D("2025-12-20"))
        self.reg.add_target(
            "m1", "装机", "%", 50.0, D("2028-12-31"), target_id="t2",
            milestones=(Milestone("ms1", "t2", "一期", D("2026-09-30")),))
        self.reg.register_evidence(
            "t2", "核查", "audit", "DOC-3", "transport", 1.0, "%",
            D("2026-01-01"), D("2026-09-30"), evidence_id="e3",
            adoptions=(
                EvidenceAdoption("transport", 0.6, "", D("2026-10-01")),
                EvidenceAdoption("manufacturing", 0.6, "", D("2026-10-02")),
            ))
        self.reg.complete_milestone("t2", "ms1", D("2026-10-05"), "b1",
                                    evidence_id="e3")
        report = self.reg.measure_report("m1", D("2026-10-06"))
        self.assertEqual(len(report["evidence_warnings"]), 1)
        target_view = next(t for t in report["targets"]
                           if t["target_id"] == "t2")
        ms_view = target_view["milestones"][0]
        self.assertTrue(ms_view["frozen"])
        self.assertEqual(ms_view["evidence_boundary"], "double_count")


class StatusDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = Registry()
        self.reg.add_body("b1", "能源司", "energy")
        self.reg.draft_measure("energy", D("2026-06-01"), D("2026-12-31"),
                               measure_id="m1", title="季节性措施")
        self.reg.raise_proposal(ProposalType.ADOPT, "m1", "b1",
                                D("2026-01-10"), "", proposal_id="pa")
        self.reg.decide_proposal("pa", True, "committee", D("2026-01-10"))

    def test_lifecycle_statuses_by_report_date(self):
        self.assertEqual(self.reg.status_on("m1", D("2026-01-11")),
                         MeasureStatus.APPROVED)
        self.assertEqual(self.reg.status_on("m1", D("2026-06-01")),
                         MeasureStatus.ACTIVE)
        self.assertEqual(self.reg.status_on("m1", D("2026-12-31")),
                         MeasureStatus.ACTIVE)
        self.assertEqual(self.reg.status_on("m1", D("2027-01-01")),
                         MeasureStatus.EXPIRED)

    def test_overdue_open_milestone_flag(self):
        self.reg.add_target(
            "m1", "指标", "%", 10.0, D("2026-12-31"), target_id="t1",
            milestones=(Milestone("ms1", "t1", "节点", D("2026-08-01")),))
        report = self.reg.measure_report("m1", D("2026-09-01"))
        ms = report["targets"][0]["milestones"][0]
        self.assertTrue(ms["overdue"])
        self.assertEqual(ms["status"], "planned")


class HistoryTests(unittest.TestCase):
    def test_change_history_records_full_timeline(self):
        reg = Registry()
        reg.add_body("b1", "能源司", "energy")
        reg.draft_measure("energy", D("2026-01-01"), None,
                          measure_id="m1", title="电网")
        reg.raise_proposal(ProposalType.ADOPT, "m1", "b1", D("2025-12-20"),
                           "会议承诺", proposal_id="pa")
        reg.decide_proposal("pa", True, "committee", D("2025-12-25"))
        reg.raise_proposal(ProposalType.WITHDRAW, "m1", "b1", D("2027-05-01"),
                           "撤销", payload={"reason": "撤销"}, proposal_id="pw")
        reg.decide_proposal("pw", True, "committee", D("2027-05-02"))
        history = reg.change_history("m1")
        kinds = [e["kind"] for e in history["events"]]
        self.assertEqual(
            kinds,
            ["measure_drafted", "proposal_raised", "proposal_approved",
             "measure_adopted", "proposal_raised", "proposal_approved",
             "measure_withdrawn", "impact_registered"],
        )
        self.assertEqual(history["impacts"][0]["direction"], "outgoing")
        self.assertEqual(history["impacts"][0]["change_type"], "withdraw")


if __name__ == "__main__":
    unittest.main()
