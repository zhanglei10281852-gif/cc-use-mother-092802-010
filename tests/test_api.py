"""API 端到端验证：启动真实 HTTP 服务，走完整请求往返。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from transition_policy.api import create_server  # noqa: E402
from transition_policy.seed import build_seed_registry  # noqa: E402


class ApiServer:
    def __init__(self, seed: bool = False):
        self.registry = build_seed_registry() if seed else None
        self.server = create_server("127.0.0.1", 0, seed=False,
                                    registry=self.registry)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method: str, path: str, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url(path), data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class SeededApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = ApiServer(seed=True).__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.__exit__(None, None, None)

    def test_health(self):
        status, body = self.srv.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_daily_report_before_and_after_seed_events(self):
        # 2026-05-01：m-charge 尚为 proposed，m-grid active
        _, early = self.srv.request("GET", "/api/report?on=2026-05-01")
        by_id = {m["measure_id"]: m for m in early["measures"]}
        self.assertEqual(by_id["m-grid"]["status"], "active")
        self.assertEqual(by_id["m-charge"]["status"], "proposed")

        # 2027-06-30：四条种子措施均已审批，无阻塞（其他用例会追加措施）
        _, later = self.srv.request("GET", "/api/report?on=2027-06-30")
        by_id = {m["measure_id"]: m for m in later["measures"]}
        self.assertGreaterEqual(later["summary"]["total"], 4)
        self.assertFalse(by_id["m-fleet"]["blocked"])

    def test_seed_evidence_boundaries(self):
        _, meter = self.srv.request(
            "GET", "/api/evidence/e-meter/boundary")
        # e-meter 基础份额合计为 1.0（其他用例可能继续追加采用份额）
        self.assertGreaterEqual(meter["claimed_share_total"], 1.0)
        self.assertIn(meter["boundary"], {"shared", "double_count"})

        _, audit = self.srv.request(
            "GET", "/api/evidence/e-audit/boundary")
        self.assertEqual(audit["boundary"], "double_count")
        self.assertEqual(audit["claimed_share_total"], 1.4)

    def test_report_carries_approval_basis(self):
        _, report = self.srv.request(
            "GET", "/api/measures/m-greenmfg?on=2027-01-15")
        basis = {b["measure_id"] for b in report["approval_basis"]}
        self.assertEqual(basis, {"m-grid", "m-charge"})
        self.assertEqual(report["status"], "active")
        # 已完成里程碑 ms-r1 的共享凭据边界被带出
        grid = next(m for m in self._all_reports("2026-08-01")
                    if m["measure_id"] == "m-grid")
        ms = grid["targets"][0]["milestones"][0]
        self.assertIn(ms["evidence_boundary"], {"shared", "double_count"})
        self.assertTrue(ms["frozen"])

    def _all_reports(self, on):
        _, body = self.srv.request("GET", f"/api/report?on={on}")
        return body["measures"]

    def test_change_history_endpoint(self):
        _, history = self.srv.request("GET", "/api/measures/m-grid/history")
        kinds = [e["kind"] for e in history["events"]]
        self.assertIn("proposal_approved", kinds)
        self.assertIn("milestone_completed", kinds)

    def test_bad_approval_over_http_returns_400(self):
        # 新建一项引用 proposed 措施的措施并尝试审批 → 400
        self.srv.request("POST", "/api/measures", {
            "measure_id": "m-x", "owner": "transport",
            "title": "临时", "effective_from": "2026-03-01",
            "prerequisite_ids": [], "raised_by": "b-transport"})
        self.srv.request("POST", "/api/measures", {
            "measure_id": "m-y", "owner": "manufacturing",
            "title": "依赖临时", "effective_from": "2026-09-01",
            "prerequisite_ids": ["m-x"], "raised_by": "b-mfg"})
        self.srv.request("POST", "/api/proposals", {
            "proposal_id": "p-y", "proposal_type": "adopt",
            "measure_id": "m-y", "raised_by": "b-mfg",
            "raised_on": "2026-08-01", "rationale": ""})
        status, body = self.srv.request(
            "POST", "/api/proposals/p-y/decision",
            {"approve": True, "decided_by": "committee",
             "decided_on": "2026-08-01"})
        self.assertEqual(status, 400)
        self.assertIn("not_approved", body["error"])

    def test_withdraw_workflow_and_impact_endpoint(self):
        # 新建能源措施 m-w + 交通依赖措施 m-d，审批后撤回 m-w
        for mid, owner, frm, pre in (
                ("m-w", "energy", "2026-02-01", []),
                ("m-d", "transport", "2026-08-01", ["m-w"])):
            status, _ = self.srv.request("POST", "/api/measures", {
                "measure_id": mid, "owner": owner, "title": mid,
                "effective_from": frm, "prerequisite_ids": pre,
                "raised_by": "b"})
            self.assertEqual(status, 201)
        for pid, mid, on in (("p-w", "m-w", "2026-01-20"),
                             ("p-d", "m-d", "2026-07-20")):
            self.srv.request("POST", "/api/proposals", {
                "proposal_id": pid, "proposal_type": "adopt",
                "measure_id": mid, "raised_by": "b", "raised_on": on,
                "rationale": ""})
            status, body = self.srv.request(
                "POST", f"/api/proposals/{pid}/decision",
                {"approve": True, "decided_by": "committee", "decided_on": on})
            self.assertEqual(status, 200, body)

        self.srv.request("POST", "/api/proposals", {
            "proposal_id": "p-wd", "proposal_type": "withdraw",
            "measure_id": "m-w", "raised_by": "b", "raised_on": "2027-03-01",
            "rationale": "路线撤销", "payload": {"reason": "路线撤销"}})
        status, body = self.srv.request(
            "POST", "/api/proposals/p-wd/decision",
            {"approve": True, "decided_by": "committee",
             "decided_on": "2027-03-02"})
        self.assertEqual(status, 200)
        affected = {i["dependent_measure_id"] for i in body["impact"]["items"]}
        self.assertIn("m-d", affected)

        status, fetched = self.srv.request("GET", "/api/impacts/p-wd")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["source_measure_id"], "m-w")

        # 撤回后日报显示阻塞；撤回日前的报告日不阻塞
        _, blocked = self.srv.request(
            "GET", "/api/measures/m-d?on=2027-03-03")
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["status"], "active",
                         "措施自身仍 active，阻塞来自前置失效")
        _, clean = self.srv.request("GET", "/api/measures/m-d?on=2027-03-01")
        self.assertFalse(clean["blocked"])

    def test_evidence_adoption_updates_boundary(self):
        _, before = self.srv.request(
            "GET", "/api/evidence/e-meter/boundary")
        depts = {a["department"] for a in before["adoptions"]}
        self.assertEqual(depts, {"energy", "manufacturing"})

        # 新部门再以 0.3 份额采用同一凭据 → 合计 1.3，触发重复统计
        status, body = self.srv.request(
            "POST", "/api/evidence/e-meter/adoptions",
            {"department": "transport", "share": 0.3,
             "scope_note": "交通侧追加主张", "adopted_on": "2026-10-01"})
        self.assertEqual(status, 200)
        self.assertEqual(body["boundary"], "double_count")
        self.assertAlmostEqual(body["claimed_share_total"], 1.3)

    def test_unknown_measure_returns_400(self):
        status, body = self.srv.request(
            "GET", "/api/measures/nope?on=2027-01-01")
        self.assertEqual(status, 400)
        self.assertIn("error", body)


class BlankApiTests(unittest.TestCase):
    """空库基础写路径验证。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = ApiServer(seed=False).__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.__exit__(None, None, None)

    def test_full_create_to_report_flow(self):
        status, _ = self.srv.request("POST", "/api/bodies", {
            "body_id": "b1", "name": "能源司", "department": "energy"})
        self.assertEqual(status, 201)

        status, _ = self.srv.request("POST", "/api/measures", {
            "measure_id": "m1", "owner": "energy", "title": "试点",
            "effective_from": "2026-04-01", "effective_until": None,
            "prerequisite_ids": []})
        self.assertEqual(status, 201)

        status, _ = self.srv.request(
            "POST", "/api/measures/m1/targets", {
                "target_id": "t1", "name": "覆盖率", "unit": "%",
                "target_value": 20.0, "due_date": "2028-12-31",
                "milestones": [
                    {"milestone_id": "ms1", "name": "启动",
                     "due_date": "2026-10-01"}]})
        self.assertEqual(status, 201)

        # proposed 措施在生效日报为 proposed
        _, report = self.srv.request(
            "GET", "/api/measures/m1?on=2026-05-01")
        self.assertEqual(report["status"], "proposed")

        # 审批 → active
        self.srv.request("POST", "/api/proposals", {
            "proposal_id": "p1", "proposal_type": "adopt", "measure_id": "m1",
            "raised_by": "b1", "raised_on": "2026-03-01", "rationale": ""})
        status, _ = self.srv.request(
            "POST", "/api/proposals/p1/decision",
            {"approve": True, "decided_by": "committee",
             "decided_on": "2026-03-05"})
        self.assertEqual(status, 200)

        # 登记凭据并完成里程碑
        status, _ = self.srv.request(
            "POST", "/api/targets/t1/evidence", {
                "evidence_id": "e1", "title": "验收单", "reference": "V-1",
                "origin_department": "energy", "value": 5.0, "unit": "%",
                "period_start": "2026-09-01", "period_end": "2026-09-30"})
        self.assertEqual(status, 201)
        status, _ = self.srv.request(
            "POST", "/api/measures/m1/targets/t1/milestones/ms1/complete",
            {"completed_on": "2026-09-28", "completed_by": "b1",
             "evidence_id": "e1"})
        self.assertEqual(status, 200)

        _, report = self.srv.request(
            "GET", "/api/measures/m1?on=2026-10-01")
        self.assertEqual(report["status"], "active")
        ms = report["targets"][0]["milestones"][0]
        self.assertEqual(ms["status"], "completed")
        self.assertTrue(ms["frozen"])

    def test_invalid_json_returns_400(self):
        req = urllib.request.Request(
            self.srv.url("/api/measures"),
            data=b"{not-json", headers={"Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
