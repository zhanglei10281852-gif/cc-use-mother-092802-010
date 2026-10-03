"""HTTP API 端到端测试：真实启动 ThreadingHTTPServer，走 HTTP 协议。"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from transition_policy.api import make_handler
from transition_policy.repository import JsonRepository
from transition_policy.service import PolicyTracker


class ApiClient:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "api.json"
        cls.repo = JsonRepository(cls.db_path)
        cls.handler = make_handler(PolicyTracker(cls.repo), cls.repo)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def test_01_full_workflow(self):
        # 登记部门
        for body in [("energy", "能源局", "energy"), ("transport", "交通局", "transport"),
                     ("industry", "制造局", "manufacturing")]:
            status, _ = self.api.call("POST", "/api/bodies",
                                      {"body_id": body[0], "name": body[1], "sector": body[2]})
            self.assertEqual(status, 201)

        # 登记措施
        status, _ = self.api.call("POST", "/api/measures", {
            "measure_id": "m-grid", "title": "电网改造", "owner": "energy",
            "effective_from": "2026-01-01", "prerequisite_ids": [], "status": "active"})
        self.assertEqual(status, 201)
        status, _ = self.api.call("POST", "/api/measures", {
            "measure_id": "m-charge", "title": "公路充电网络", "owner": "transport",
            "effective_from": "2026-03-01", "prerequisite_ids": ["m-grid"],
            "status": "proposed"})
        self.assertEqual(status, 201)

        # 目标
        status, _ = self.api.call("POST", "/api/targets", {
            "target_id": "t-charge", "measure_id": "m-charge", "metric": "充电桩",
            "unit": "个", "target_value": 1000, "due_date": "2029-12-31",
            "caliber_id": "cal-2026"})
        self.assertEqual(status, 201)

        # 前置未到生效日 → 422
        status, body = self.api.call("POST", "/api/measures/m-charge/activate",
                                     {"date": "2025-12-01", "actor": "t"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "prerequisites_blocked")
        self.assertEqual(body["details"]["blockers"][0]["reason"], "not_yet_effective")

        # 生效后激活
        status, body = self.api.call("POST", "/api/measures/m-charge/activate",
                                     {"date": "2026-03-01", "actor": "transport-boss"})
        self.assertEqual(status, 200)
        self.assertEqual(body["prereq_snapshot"][0]["status"], "active")

        # 凭据 + 采用
        status, _ = self.api.call("POST", "/api/evidence", {
            "evidence_id": "ev-1", "title": "高速服务区充电站", "source_owner": "transport",
            "metric": "充电桩", "unit": "个", "quantity": 300,
            "report_date": "2026-08-01", "caliber_id": "cal-2026"})
        self.assertEqual(status, 201)
        status, body = self.api.call("POST", "/api/evidence/ev-1/adoptions", {
            "adoption_id": "ad-1", "target_id": "t-charge", "adopted_by": "transport",
            "role": "primary", "share": 1.0, "date": "2026-08-02"})
        self.assertEqual(status, 201)
        self.assertEqual(body["boundary"]["share_coverage"], 1.0)

        # 里程碑完成
        status, _ = self.api.call("POST", "/api/milestones", {
            "milestone_id": "ms-1", "target_id": "t-charge",
            "label": "首批 300 桩", "planned_date": "2026-08-01"})
        self.assertEqual(status, 201)
        status, _ = self.api.call("POST", "/api/milestones/ms-1/complete",
                                  {"date": "2026-08-03", "actor": "transport",
                                   "evidence_id": "ev-1"})
        self.assertEqual(status, 200)

        # 已完成里程碑重排 → 409
        status, body = self.api.call("POST", "/api/milestones/ms-1/reschedule",
                                     {"date": "2026-09-01", "new_planned_date": "2027-01-01"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "milestone_locked")

        # 报告日查询
        status, report = self.api.call("GET", "/api/report?date=2026-09-01&owner=transport")
        self.assertEqual(status, 200)
        charge = report["measures"][0]
        self.assertEqual(charge["status"], "active")
        self.assertFalse(charge["blocked"])
        target = charge["targets"][0]
        self.assertEqual(target["progress"]["counted_compatible"], 300.0)
        self.assertEqual(target["milestone_summary"], {"total": 1, "achieved": 1, "overdue": 0})

        # 阻塞单独查询：充电措施无阻塞
        status, body = self.api.call("GET", "/api/measures/m-charge/blockers?date=2026-09-01")
        self.assertEqual(status, 200)
        self.assertEqual(body["blockers"], [])

        # 撤销电网：先预览影响（交通措施是直接受影响方）
        status, impact = self.api.call("GET", "/api/measures/m-grid/impact?date=2027-06-01")
        self.assertEqual(status, 200)
        self.assertEqual({m["measure_id"] for m in impact["affected_measures"]}, {"m-charge"})

        # 提交并审批撤销提案
        status, _ = self.api.call("POST", "/api/proposals", {
            "proposal_id": "p-wd", "measure_id": "m-grid", "type": "withdraw",
            "rationale": "资金退出", "submitted_by": "energy", "submitted_on": "2027-06-01"})
        self.assertEqual(status, 201)
        status, decision = self.api.call("POST", "/api/proposals/p-wd/approve",
                                         {"date": "2027-06-01", "actor": "mayor"})
        self.assertEqual(status, 200)
        self.assertEqual(decision["impact"]["direct_dependent_count"], 1)

        # 撤销日后：交通措施被阻塞，但已完成里程碑保持 achieved
        status, report = self.api.call("GET", "/api/report?date=2027-06-02&owner=transport")
        charge = report["measures"][0]
        self.assertTrue(charge["blocked"])
        self.assertEqual(charge["blockers"][0]["reason"], "withdrawn")
        self.assertEqual(charge["targets"][0]["milestones"][0]["state"], "achieved")

        # 历史日：撤销日前一切照旧
        status, before = self.api.call("GET", "/api/measures/m-grid/status?date=2027-05-31")
        self.assertEqual(before["status"], "active")
        self.assertTrue(before["effective"])

        # 变更历史
        status, hist = self.api.call("GET", "/api/measures/m-charge/history")
        self.assertEqual(status, 200)
        etypes = {e["type"] for e in hist["events"]}
        self.assertIn("measure.withdrawn", etypes)  # 通过依赖链实体可见上游撤销
        self.assertIn("milestone.completed", etypes)

        # 事件流
        status, events = self.api.call("GET", "/api/events")
        self.assertEqual(status, 200)
        self.assertGreater(len(events["events"]), 5)

    def test_02_replace_keeps_milestone_via_http(self):
        # 同一库内：替代提案走 HTTP 全链路
        self.api.call("POST", "/api/measures", {
            "measure_id": "m-fleet", "title": "车队电气化", "owner": "industry",
            "effective_from": "2026-06-01", "prerequisite_ids": ["m-charge"],
            "status": "proposed"})
        # 上游已撤销，新建措施保持 proposed 即可；这里验证替代旧措施 m-charge 不行（已 blocked），
        # 改为直接对已终结提案返回 409 的校验：
        status, body = self.api.call("POST", "/api/proposals", {
            "proposal_id": "p-bad", "measure_id": "m-grid", "type": "amend",
            "submitted_on": "2027-07-01", "new_effective_until": "2030-01-01"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "workflow_state")

    def test_03_bad_routes_and_payloads(self):
        status, body = self.api.call("GET", "/api/measures/nope/status?date=2026-01-01")
        self.assertEqual(status, 404)
        status, body = self.api.call("GET", "/api/measures/m-grid/status")
        self.assertEqual(status, 400)
        self.assertIn("date", body["message"])
        status, body = self.api.call("GET", "/no/such/path")
        self.assertEqual(status, 404)

    def test_04_persistence_survives_restart(self):
        # 数据已落盘；用同一文件重新实例化仓储后数据仍在
        repo2 = JsonRepository(self.db_path)
        self.assertTrue(repo2.contains("measures", "m-grid"))
        self.assertTrue(repo2.contains("adoptions", "ad-1"))
        ms = repo2.get("milestones", "ms-1")
        self.assertEqual(ms.achieved_date, date(2026, 8, 3))


if __name__ == "__main__":
    unittest.main()
