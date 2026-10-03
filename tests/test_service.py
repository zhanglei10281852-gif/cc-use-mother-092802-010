"""核心领域规则测试：审批快照、阻塞、影响清单、里程碑锁定、凭据统计边界。"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from transition_policy.contracts import (
    ChangeProposal, CountingRole, Evidence, EvidenceAdoption, MeasureStatus,
    Milestone, PolicyMeasure, ProposalStatus, ProposalType, ReplacementDraft,
    ResponsibleBody, Target,
)
from transition_policy.errors import (
    CaliberMismatchError, CycleError, DuplicateCountingError,
    MilestoneLockedError, NotFoundError, PrerequisiteBlockedError,
    ShareOverlapError, ValidationError, WorkflowStateError,
)
from transition_policy.repository import JsonRepository
from transition_policy.service import PolicyTracker


def make_tracker():
    svc = PolicyTracker(JsonRepository(None))
    svc.add_body(ResponsibleBody("energy", "能源局", "energy"))
    svc.add_body(ResponsibleBody("transport", "交通局", "transport"))
    svc.add_body(ResponsibleBody("industry", "制造局", "manufacturing"))

    # 能源部门：电网改造（无前置，直接生效）
    svc.add_measure(PolicyMeasure(
        "m-grid", "energy", date(2026, 1, 1), None, (),
        MeasureStatus.ACTIVE, title="电网改造"))
    svc.add_target(Target("t-grid", "m-grid", "供电能力", "MW", 500.0,
                          date(2028, 12, 31), "cal-2026", 0.0))
    # 能源部门自建桩目标：跨部门共享同一份“充电桩”凭据时使用
    svc.add_target(Target("t-grid-pile", "m-grid", "充电桩", "个", 300.0,
                          date(2028, 12, 31), "cal-2026", 0.0))

    # 交通部门：充电设施，前置为能源部门的电网改造（跨部门引用），先登记后激活
    svc.add_measure(PolicyMeasure(
        "m-charge", "transport", date(2026, 3, 1), None, ("m-grid",),
        MeasureStatus.PROPOSED, title="公路充电网络"))
    svc.add_target(Target("t-charge", "m-charge", "充电桩", "个", 1000.0,
                          date(2029, 12, 31), "cal-2026", 0.0))

    # 制造部门：车队电气化，间接依赖能源部门
    svc.add_measure(PolicyMeasure(
        "m-fleet", "industry", date(2026, 6, 1), None, ("m-charge",),
        MeasureStatus.PROPOSED, title="物流车队电气化"))
    svc.add_target(Target("t-fleet", "m-fleet", "电动车比例", "%", 80.0,
                          date(2030, 12, 31), "cal-2025", 0.0))
    return svc


class EffectiveDateTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_tracker()

    def test_effective_window_is_half_open(self):
        self.assertTrue(self.svc.status_on("m-grid", date(2026, 1, 1))["effective"])
        # 未到生效日：措施虽已审批，但尚未生效
        early = self.svc.status_on("m-charge", date(2026, 2, 28))
        self.assertFalse(early["effective"])
        self.assertEqual(early["status"], "proposed")

    def test_expired_measure_is_not_effective(self):
        self.svc.add_measure(PolicyMeasure(
            "m-temp", "energy", date(2025, 1, 1), date(2026, 1, 1), (),
            MeasureStatus.ACTIVE, title="临时补贴"))
        self.assertFalse(self.svc.status_on("m-temp", date(2026, 1, 1))["effective"])
        self.assertTrue(self.svc.status_on("m-temp", date(2025, 12, 31))["effective"])


class ApprovalPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_tracker()

    def test_activation_succeeds_when_prereq_effective_and_records_snapshot(self):
        result = self.svc.activate_measure("m-charge", "transport-boss", date(2026, 3, 1))
        self.assertEqual(result["status"], "active")
        snap = result["prereq_snapshot"][0]
        self.assertEqual(snap["measure_id"], "m-grid")
        self.assertEqual(snap["status"], "active")
        self.assertEqual(snap["owner"], "energy")

    def test_activation_blocked_before_prereq_effective_date(self):
        # 电网改造 2026-01-01 才生效；2025 年激活充电设施必须被拒
        with self.assertRaises(PrerequisiteBlockedError) as ctx:
            self.svc.activate_measure("m-charge", "transport-boss", date(2025, 12, 31))
        self.assertEqual(ctx.exception.details["blockers"][0]["reason"], "not_yet_effective")

    def test_transitive_blocker_carries_via_chain(self):
        self.svc.activate_measure("m-charge", "transport-boss", date(2026, 3, 1))
        # 撤销电网改造后，制造部门措施在撤销日之后被间接阻塞
        self.svc.submit_proposal(ChangeProposal(
            "p-wd-grid", "m-grid", ProposalType.WITHDRAW, "政策调整", "energy",
            date(2027, 6, 1)))
        self.svc.approve_proposal("p-wd-grid", "energy-boss", date(2027, 6, 1))

        blockers = self.svc.blockers_on("m-fleet", date(2027, 6, 2))
        self.assertTrue(blockers)
        root = blockers[0]
        self.assertEqual(root["prerequisite_id"], "m-grid")
        self.assertEqual(root["reason"], "withdrawn")
        self.assertEqual(root["via"], ["m-charge"])

    def test_approval_only_based_on_then_effective_prerequisites(self):
        self.svc.activate_measure("m-charge", "transport-boss", date(2026, 3, 1))
        # 给电网改造设定失效区间（通过延期/修订提案改为 2027 年截止）
        self.svc.submit_proposal(ChangeProposal(
            "p-amend-grid", "m-grid", ProposalType.AMEND, "阶段性到期", "energy",
            date(2026, 12, 1), new_effective_until=date(2027, 1, 1)))
        self.svc.approve_proposal("p-amend-grid", "energy-boss", date(2026, 12, 15))

        # 2027-06-01 前置链已断，激活制造措施被拒
        with self.assertRaises(PrerequisiteBlockedError):
            self.svc.activate_measure("m-fleet", "industry-boss", date(2027, 6, 1))
        # 但 2026-06-01 前置均有效，可以激活
        self.svc.activate_measure("m-fleet", "industry-boss", date(2026, 6, 1))

    def test_prerequisite_cycle_rejected(self):
        with self.assertRaises(CycleError):
            self.svc.submit_proposal(ChangeProposal(
                "p-cycle", "m-grid", ProposalType.AMEND, "成环", "energy",
                date(2026, 5, 1), new_prerequisite_ids=("m-fleet",)))


class MilestoneLockTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_tracker()
        self.svc.add_evidence(Evidence("ev-grid", "电网验收报告", "energy", "供电能力", "MW",
                                       120.0, date(2026, 5, 1), "cal-2026"))
        self.svc.add_milestone(Milestone("ms-grid-1", "t-grid", "首批变电站",
                                         date(2026, 4, 1)))
        self.svc.complete_milestone("ms-grid-1", date(2026, 5, 1), "energy", "ev-grid")

    def test_completed_milestone_cannot_be_rescheduled(self):
        with self.assertRaises(MilestoneLockedError) as ctx:
            self.svc.reschedule_milestone("ms-grid-1", date(2027, 1, 1),
                                          "energy", date(2026, 8, 1))
        self.assertIn("2026-05-01", ctx.exception.details["achieved_date"])

    def test_completed_milestone_cannot_be_recompleted(self):
        with self.assertRaises(MilestoneLockedError):
            self.svc.complete_milestone("ms-grid-1", date(2027, 1, 1), "energy")

    def test_open_milestone_reschedule_is_recorded(self):
        self.svc.add_milestone(Milestone("ms-grid-2", "t-grid", "二期", date(2027, 1, 1)))
        self.svc.reschedule_milestone("ms-grid-2", date(2027, 6, 1),
                                      "energy", date(2026, 9, 1))
        hist = self.svc.history("m-grid")
        types_ = [e["type"] for e in hist["events"]]
        self.assertIn("milestone.rescheduled", types_)

    def test_amend_cannot_push_effective_from_past_completed_milestone(self):
        self.svc.submit_proposal(ChangeProposal(
            "p-amend", "m-grid", ProposalType.AMEND, "推迟启动", "energy",
            date(2026, 6, 1), new_effective_from=date(2027, 1, 1)))
        with self.assertRaises(MilestoneLockedError) as ctx:
            self.svc.approve_proposal("p-amend", "boss", date(2026, 6, 2))
        self.assertIn("ms-grid-1", ctx.exception.details["milestone_ids"])


    def test_amend_cannot_retroactively_rewrite_history(self):
        # 审批日 2026-09-01，却要把生效日改到审批日之前 → 拒绝
        self.svc.submit_proposal(ChangeProposal(
            "p-back", "m-grid", ProposalType.AMEND, "追溯生效", "energy",
            date(2026, 9, 1), new_effective_from=date(2025, 1, 1)))
        with self.assertRaises(ValidationError):
            self.svc.approve_proposal("p-back", "boss", date(2026, 9, 2))
        # 把失效日改到审批日当天或之前也拒绝（即时失效应走撤销）
        self.svc.submit_proposal(ChangeProposal(
            "p-exp", "m-grid", ProposalType.AMEND, "立即到期", "energy",
            date(2026, 9, 1), new_effective_until=date(2026, 9, 1)))
        with self.assertRaises(ValidationError):
            self.svc.approve_proposal("p-exp", "boss", date(2026, 9, 2))

    def test_expired_prerequisite_blocks_dependent(self):
        # 修订前置使其 2027-01-01 到期；之后下游激活被拒，原因为 expired
        self.svc.activate_measure("m-charge", "transport-boss", date(2026, 3, 1))
        self.svc.submit_proposal(ChangeProposal(
            "p-lim", "m-grid", ProposalType.AMEND, "阶段性", "energy",
            date(2026, 12, 1), new_effective_until=date(2027, 1, 1)))
        self.svc.approve_proposal("p-lim", "boss", date(2026, 12, 2))
        with self.assertRaises(PrerequisiteBlockedError) as ctx:
            self.svc.activate_measure("m-fleet", "boss", date(2027, 2, 1))
        self.assertEqual(ctx.exception.details["blockers"][0]["reason"], "expired")


class ReplaceTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_tracker()
        self.svc.add_evidence(Evidence("ev-ch", "充电站清单", "transport", "充电桩", "个",
                                       200.0, date(2026, 9, 1), "cal-2026"))
        self.svc.add_milestone(Milestone("ms-ch-1", "t-charge", "首批 200 桩",
                                         date(2026, 9, 1)))
        self.svc.activate_measure("m-charge", "transport-boss", date(2026, 3, 1))
        self.svc.complete_milestone("ms-ch-1", date(2026, 9, 1), "transport", "ev-ch")

    def _replace(self):
        self.svc.submit_proposal(ChangeProposal(
            "p-rep", "m-charge", ProposalType.REPLACE, "国标更新，措施重立",
            "transport", date(2027, 3, 1),
            replacement=ReplacementDraft(
                "m-charge-v2", "公路充电网络（新国标）", "transport",
                date(2027, 4, 1), None, ("m-grid",))))
        return self.svc.approve_proposal("p-rep", "transport-boss", date(2027, 3, 15))

    def test_replace_closes_old_measure_and_keeps_history_point_in_time(self):
        result = self._replace()
        self.assertEqual(result["new_measure_id"], "m-charge-v2")
        # 决策日之后：旧措施已被替代、区间闭合
        after = self.svc.status_on("m-charge", date(2027, 3, 16))
        self.assertEqual(after["status"], "superseded")
        self.assertFalse(after["effective"])
        # 决策日之前：旧措施仍然 active——历史报告日不被重写
        before = self.svc.status_on("m-charge", date(2027, 3, 14))
        self.assertEqual(before["status"], "active")
        self.assertTrue(before["effective"])

    def test_targets_migrate_and_completed_milestones_preserved(self):
        result = self._replace()
        self.assertEqual(result["carried_targets"], ["t-charge"])
        preserved = result["preserved_milestones"][0]
        self.assertEqual(preserved["milestone_id"], "ms-ch-1")
        self.assertEqual(preserved["achieved_date"], "2026-09-01")
        self.assertEqual(preserved["evidence_id"], "ev-ch")
        # 目标挂到新措施，里程碑原样可查
        report = self.svc.report(date(2027, 5, 1), owner="transport")
        v2 = next(m for m in report["measures"] if m["measure_id"] == "m-charge-v2")
        target = v2["targets"][0]
        self.assertEqual(target["target_id"], "t-charge")
        ms = target["milestones"][0]
        self.assertEqual(ms["achieved_date"], "2026-09-01")
        self.assertEqual(ms["evidence_id"], "ev-ch")

    def test_lineage_history_covers_both_revisions(self):
        self._replace()
        hist = self.svc.history("m-charge-v2")
        self.assertEqual(set(hist["lineage"]), {"m-charge", "m-charge-v2"})
        types_ = [e["type"] for e in hist["events"]]
        self.assertIn("milestone.completed", types_)
        self.assertIn("measure.replaced", types_)
        self.assertEqual(hist["proposals"][0]["type"], "replace")


class WithdrawImpactTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_tracker()
        self.svc.activate_measure("m-charge", "transport-boss", date(2026, 3, 1))
        self.svc.activate_measure("m-fleet", "industry-boss", date(2026, 6, 1))
        self.svc.add_evidence(Evidence("ev-f", "车队台账", "industry", "电动车比例", "%",
                                       30.0, date(2026, 10, 1), "cal-2025"))
        self.svc.add_milestone(Milestone("ms-f-1", "t-fleet", "30% 电动化",
                                         date(2026, 10, 1)))
        self.svc.complete_milestone("ms-f-1", date(2026, 10, 1), "industry", "ev-f")
        self.svc.submit_proposal(ChangeProposal(
            "p-wd", "m-grid", ProposalType.WITHDRAW, "资金退出", "energy",
            date(2027, 6, 1)))

    def test_preview_lists_direct_and_transitive_dependents(self):
        impact = self.svc.preview_withdrawal_impact("m-grid", date(2027, 6, 1))
        ids = {m["measure_id"]: m for m in impact["affected_measures"]}
        self.assertEqual(impact["direct_dependent_count"], 1)
        self.assertEqual(impact["transitive_dependent_count"], 2)
        self.assertEqual(ids["m-charge"]["dependency_chain"], ["m-grid", "m-charge"])
        self.assertEqual(ids["m-fleet"]["dependency_chain"],
                         ["m-grid", "m-charge", "m-fleet"])
        self.assertEqual(ids["m-fleet"]["owner"], "industry")
        self.assertEqual(ids["m-fleet"]["owner_name"], "制造局")
        # 已完成里程碑被保留列入清单，而非删除
        preserved = {p["milestone_id"] for p in impact["preserved_completed_milestones"]}
        self.assertIn("ms-f-1", preserved)

    def test_withdraw_approval_attaches_impact_and_closes_window(self):
        result = self.svc.approve_proposal("p-wd", "boss", date(2027, 6, 1))
        self.assertIn("impact", result)
        self.assertEqual(self.svc.status_on("m-grid", date(2027, 5, 31))["status"], "active")
        self.assertEqual(self.svc.status_on("m-grid", date(2027, 6, 1))["status"], "withdrawn")
        # 历史日报告不变
        self.assertTrue(self.svc.status_on("m-grid", date(2027, 5, 31))["effective"])

    def test_withdraw_then_dependent_becomes_blocked_but_keeps_completed_milestone(self):
        self.svc.approve_proposal("p-wd", "boss", date(2027, 6, 1))
        report = self.svc.report(date(2027, 6, 2), owner="industry")
        fleet = next(m for m in report["measures"] if m["measure_id"] == "m-fleet")
        self.assertTrue(fleet["blocked"])
        self.assertEqual(fleet["blockers"][0]["reason"], "withdrawn")
        ms = fleet["targets"][0]["milestones"][0]
        self.assertEqual(ms["state"], "achieved")
        self.assertEqual(ms["achieved_date"], "2026-10-01")

    def test_cannot_propose_change_on_withdrawn_measure(self):
        self.svc.approve_proposal("p-wd", "boss", date(2027, 6, 1))
        with self.assertRaises(WorkflowStateError):
            self.svc.submit_proposal(ChangeProposal(
                "p-wd2", "m-grid", ProposalType.AMEND, "再改", "energy",
                date(2027, 7, 1), new_effective_until=date(2028, 1, 1)))


class EvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_tracker()
        self.svc.activate_measure("m-charge", "transport-boss", date(2026, 3, 1))
        # 同一份充电设施凭据，源属能源，交通部门目标也想采用
        self.svc.add_evidence(Evidence(
            "ev-shared", "高速服务区充换电一体站", "energy", "充电桩", "个",
            100.0, date(2026, 7, 1), "cal-2026"))

    def test_primary_then_second_primary_is_double_counting(self):
        self.svc.adopt_evidence(EvidenceAdoption(
            "a-1", "ev-shared", "t-grid-pile", "energy", CountingRole.PRIMARY, 1.0,
            date(2026, 7, 2)))
        with self.assertRaises(DuplicateCountingError):
            self.svc.adopt_evidence(EvidenceAdoption(
                "a-2", "ev-shared", "t-charge", "transport", CountingRole.PRIMARY, 1.0,
                date(2026, 7, 3)))
        # 显式确认后允许，但边界必须标出重复统计
        self.svc.adopt_evidence(EvidenceAdoption(
            "a-2", "ev-shared", "t-charge", "transport", CountingRole.PRIMARY, 1.0,
            date(2026, 7, 3), duplicate_confirmed=True))
        boundary = self.svc.evidence_boundary("ev-shared")
        self.assertTrue(boundary["boundary"]["double_counting"])
        self.assertTrue(boundary["boundary"]["shared_across_bodies"])
        self.assertEqual(boundary["boundary"]["primary_count"], 2)

    def test_shared_adoption_counts_by_share_and_overlap_is_guarded(self):
        self.svc.adopt_evidence(EvidenceAdoption(
            "a-1", "ev-shared", "t-grid-pile", "energy", CountingRole.PRIMARY, 1.0,
            date(2026, 7, 2)))
        # 交通部门先分摊 60%，主+分=1.6 超 1，需显式确认重叠
        with self.assertRaises(ShareOverlapError):
            self.svc.adopt_evidence(EvidenceAdoption(
                "a-2", "ev-shared", "t-charge", "transport", CountingRole.SHARED, 0.6,
                date(2026, 7, 3)))
        self.svc.adopt_evidence(EvidenceAdoption(
            "a-2", "ev-shared", "t-charge", "transport", CountingRole.SHARED, 0.6,
            date(2026, 7, 3), overlap_confirmed=True))
        boundary = self.svc.evidence_boundary("ev-shared")
        transport_row = next(a for a in boundary["adoptions"] if a["adopted_by"] == "transport")
        self.assertEqual(transport_row["counted_quantity"], 60.0)
        self.assertTrue(boundary["boundary"]["coverage_overlap"])
        self.assertEqual(boundary["boundary"]["share_coverage"], 1.6)

    def test_caliber_mismatch_must_be_confirmed_and_flagged(self):
        # t-fleet 使用 cal-2025；制造部门一份 cal-2026 口径的车队台账凭据
        self.svc.activate_measure("m-fleet", "industry-boss", date(2026, 6, 1))
        self.svc.add_evidence(Evidence(
            "ev-fleet-cal26", "车队电动化台账（新口径）", "industry",
            "电动车比例", "%", 16.0, date(2026, 7, 5), "cal-2026"))
        with self.assertRaises(CaliberMismatchError):
            self.svc.adopt_evidence(EvidenceAdoption(
                "a-x", "ev-fleet-cal26", "t-fleet", "industry", CountingRole.SHARED, 0.2,
                date(2026, 7, 5)))
        self.svc.adopt_evidence(EvidenceAdoption(
            "a-x", "ev-fleet-cal26", "t-fleet", "industry", CountingRole.SHARED, 0.2,
            date(2026, 7, 5), caliber_mismatch=True))
        boundary = self.svc.evidence_boundary("ev-fleet-cal26")
        self.assertTrue(boundary["boundary"]["caliber_mismatch"])
        # 报告进度时口径不一致的量单列，不计入兼容达成率
        view = self.svc.report(date(2026, 7, 6), owner="industry")
        target = view["measures"][0]["targets"][0]
        self.assertEqual(target["progress"]["counted_caliber_mismatch"], 16.0 * 0.2)
        self.assertEqual(target["progress"]["counted_compatible"], 0.0)


class ProposalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_tracker()

    def test_rejected_proposal_cannot_be_approved(self):
        self.svc.submit_proposal(ChangeProposal(
            "p1", "m-grid", ProposalType.AMEND, "延期", "energy", date(2026, 5, 1),
            new_effective_until=date(2031, 1, 1)))
        self.svc.reject_proposal("p1", "boss", date(2026, 5, 2), "理由不充分")
        with self.assertRaises(WorkflowStateError):
            self.svc.approve_proposal("p1", "boss", date(2026, 5, 3))

    def test_amend_creates_revision(self):
        self.svc.submit_proposal(ChangeProposal(
            "p2", "m-grid", ProposalType.AMEND, "延期五年", "energy", date(2026, 5, 1),
            new_effective_until=date(2031, 1, 1)))
        result = self.svc.approve_proposal("p2", "boss", date(2026, 5, 2))
        self.assertEqual(result["revision"], 2)
        self.assertEqual(self.svc.status_on("m-grid", date(2026, 5, 2))["revision"], 2)

    def test_approve_unknown_proposal_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.svc.approve_proposal("nope", "boss", date(2026, 5, 2))


class ReportTests(unittest.TestCase):
    def test_report_marks_overdue_and_progress(self):
        svc = make_tracker()
        svc.activate_measure("m-charge", "transport-boss", date(2026, 3, 1))
        svc.add_evidence(Evidence("e", "桩", "transport", "充电桩", "个", 400.0,
                                  date(2026, 4, 1), "cal-2026"))
        svc.adopt_evidence(EvidenceAdoption(
            "a", "e", "t-charge", "transport", CountingRole.PRIMARY, 1.0,
            date(2026, 4, 2)))
        svc.add_milestone(Milestone("ms-open", "t-charge", "早期里程碑",
                                    date(2026, 4, 1)))
        report = svc.report(date(2026, 10, 1), owner="transport")
        charge = next(m for m in report["measures"] if m["measure_id"] == "m-charge")
        self.assertFalse(charge["blocked"])
        target = charge["targets"][0]
        self.assertEqual(target["progress"]["counted_compatible"], 400.0)
        self.assertAlmostEqual(target["progress"]["attainment_ratio"], 0.4)
        ms = next(x for x in target["milestones"] if x["milestone_id"] == "ms-open")
        self.assertEqual(ms["state"], "overdue")


if __name__ == "__main__":
    unittest.main()
