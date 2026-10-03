"""仓储序列化往返测试。"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from transition_policy.contracts import (
    ChangeProposal, CountingRole, Evidence, EvidenceAdoption, Event,
    MeasureStatus, Milestone, PolicyMeasure, ProposalStatus, ProposalType,
    ReplacementDraft, ResponsibleBody, Target,
)
from transition_policy.repository import JsonRepository


class RepositoryRoundTripTests(unittest.TestCase):
    def test_all_entity_kinds_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.json"
            repo = JsonRepository(path)
            repo.put("bodies", ResponsibleBody("b1", "能源局", "energy"))
            repo.put("measures", PolicyMeasure(
                "m1", "b1", date(2026, 1, 1), date(2027, 1, 1), ("m0",),
                MeasureStatus.ACTIVE, title="煤电退役", revision=3))
            repo.put("targets", Target("t1", "m1", "减排量", "万吨", 100.0, date(2028, 1, 1), "cal-v2", 10.0))
            repo.put("milestones", Milestone("s1", "t1", "一期", date(2026, 6, 1), date(2026, 5, 1), "e1"))
            repo.put("evidence", Evidence("e1", "验收单", "b1", "减排量", "万吨", 20.0,
                                          date(2026, 5, 1), "cal-v2"))
            repo.put("adoptions", EvidenceAdoption("a1", "e1", "t1", "b1", CountingRole.SHARED,
                                                   0.4, date(2026, 5, 2), caliber_mismatch=True))
            repo.put("proposals", ChangeProposal(
                "p1", "m1", ProposalType.REPLACE, "替代", "b1", date(2026, 3, 1),
                status=ProposalStatus.APPROVED, decided_on=date(2026, 4, 1), decided_by="boss",
                replacement=ReplacementDraft("m2", "新措施", "b1", date(2027, 1, 1), None, ("m0",)),
                prereq_snapshot=({"measure_id": "m0", "status": "active"},)))
            repo.put("events", Event(1, date(2026, 1, 1), "system", "x", ("m1",), {"k": "v"}))
            repo.save()

            reloaded = JsonRepository(path)
            m = reloaded.get("measures", "m1")
            self.assertEqual(m.status, MeasureStatus.ACTIVE)
            self.assertEqual(m.prerequisite_ids, ("m0",))
            self.assertEqual(m.revision, 3)
            ms = reloaded.get("milestones", "s1")
            self.assertEqual(ms.achieved_date, date(2026, 5, 1))
            a = reloaded.get("adoptions", "a1")
            self.assertEqual(a.role, CountingRole.SHARED)
            self.assertTrue(a.caliber_mismatch)
            p = reloaded.get("proposals", "p1")
            self.assertEqual(p.ptype, ProposalType.REPLACE)
            self.assertEqual(p.replacement.measure_id, "m2")
            self.assertEqual(p.prereq_snapshot[0]["status"], "active")
            self.assertEqual(len(reloaded.all("events")), 1)

    def test_in_memory_repo_save_is_noop(self):
        repo = JsonRepository(None)
        repo.put("bodies", ResponsibleBody("b9", "X", "x"))
        repo.save()  # 不应抛错
        self.assertEqual(repo.get("bodies", "b9").name, "X")


if __name__ == "__main__":
    unittest.main()
