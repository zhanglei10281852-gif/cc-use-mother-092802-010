"""离线 HTTP API（仅依赖 Python 标准库）。

启动：
    python -m transition_policy.api --db data/policy.json --port 8000

所有日期字段使用 ISO 格式 (YYYY-MM-DD)，元组字段使用 JSON 数组。
领域异常在此统一映射为 HTTP 状态码，响应体为 JSON。
"""

import argparse
import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .contracts import (
    ChangeProposal,
    CountingRole,
    Evidence,
    EvidenceAdoption,
    MeasureStatus,
    Milestone,
    PolicyMeasure,
    ProposalType,
    ReplacementDraft,
    ResponsibleBody,
    Target,
)
from .errors import (
    CaliberMismatchError,
    CycleError,
    DuplicateCountingError,
    DuplicateError,
    MilestoneLockedError,
    NotFoundError,
    PolicyDomainError,
    PrerequisiteBlockedError,
    ShareOverlapError,
    ValidationError,
    WorkflowStateError,
)
from .repository import JsonRepository
from .service import PolicyTracker

_STATUS_CODES = {
    NotFoundError: 404,
    DuplicateError: 409,
    ValidationError: 400,
    CycleError: 422,
    PrerequisiteBlockedError: 422,
    MilestoneLockedError: 409,
    DuplicateCountingError: 409,
    ShareOverlapError: 409,
    CaliberMismatchError: 409,
    WorkflowStateError: 409,
}


def d(value):
    return date.fromisoformat(value) if value is not None else None


def t(values):
    return tuple(values) if values is not None else None


def build_tracker(db_path: str | None) -> PolicyTracker:
    return PolicyTracker(JsonRepository(db_path))


def make_handler(tracker: PolicyTracker, repo: JsonRepository):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "PolicyTracker/1.0"

        def log_message(self, fmt, *args):  # 静音，测试输出更干净
            pass

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def _query_date(self, params, name="date") -> date:
            value = params.get(name, [None])[0]
            if not value:
                raise ValidationError(f"缺少查询参数: {name}")
            return date.fromisoformat(value)

        # -- 分派 --------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            params = parse_qs(parts.query)
            try:
                with lock:
                    routed = self._route(method, path, params)
                    if routed is None:
                        return
                    status, payload = routed
                    if method == "POST":
                        repo.save()
                self._send(status, payload)
            except PolicyDomainError as exc:
                code = 400
                for etype, mapped in _STATUS_CODES.items():
                    if isinstance(exc, etype):
                        code = mapped
                        break
                self._send(code, {"error": exc.code, "message": exc.message, "details": exc.details})
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": "bad_request", "message": str(exc)})

        def _route(self, method: str, path: str, params) -> tuple[int, dict]:
            seg = [s for s in path.split("/") if s]

            if method == "GET" and path == "/":
                return 200, {"service": "policy-commitment-tracker", "routes": [
                    "POST /api/bodies", "POST /api/measures", "POST /api/targets",
                    "POST /api/milestones", "POST /api/milestones/{id}/reschedule",
                    "POST /api/milestones/{id}/complete", "POST /api/evidence",
                    "POST /api/evidence/{id}/adoptions",
                    "GET  /api/evidence/{id}/boundary", "POST /api/proposals",
                    "POST /api/proposals/{id}/approve", "POST /api/proposals/{id}/reject",
                    "GET  /api/measures/{id}/status?date=",
                    "GET  /api/measures/{id}/blockers?date=",
                    "GET  /api/measures/{id}/impact?date=",
                    "GET  /api/measures/{id}/history",
                    "GET  /api/report?date=&owner=", "GET /api/events",
                ]}

            if method == "POST" and path == "/api/bodies":
                body = self._read_json()
                return 201, tracker.add_body(ResponsibleBody(
                    body_id=body["body_id"], name=body["name"], sector=body["sector"]))

            if method == "POST" and path == "/api/measures":
                body = self._read_json()
                actor_on = d(body.get("registered_on"))
                measure = PolicyMeasure(
                    measure_id=body["measure_id"], title=body.get("title", ""),
                    owner=body["owner"], effective_from=d(body["effective_from"]),
                    effective_until=d(body.get("effective_until")),
                    prerequisite_ids=tuple(body.get("prerequisite_ids", [])),
                    status=MeasureStatus(body.get("status", "active")),
                    revision=int(body.get("revision", 1)))
                return 201, tracker.add_measure(measure, actor_on)

            if method == "POST" and path == "/api/targets":
                body = self._read_json()
                return 201, tracker.add_target(Target(
                    target_id=body["target_id"], measure_id=body["measure_id"],
                    metric=body["metric"], unit=body["unit"],
                    target_value=float(body["target_value"]), due_date=d(body["due_date"]),
                    caliber_id=body["caliber_id"], baseline=float(body.get("baseline", 0.0))))

            if method == "POST" and path == "/api/milestones":
                body = self._read_json()
                return 201, tracker.add_milestone(Milestone(
                    milestone_id=body["milestone_id"], target_id=body["target_id"],
                    label=body.get("label", ""), planned_date=d(body["planned_date"])))

            if method == "POST" and len(seg) == 4 and seg[0] == "api" and seg[1] == "milestones":
                ms_id, action = seg[2], seg[3]
                body = self._read_json()
                if action == "complete":
                    return 200, tracker.complete_milestone(
                        ms_id, d(body["date"]), body.get("actor", "api"),
                        body.get("evidence_id"))
                if action == "reschedule":
                    return 200, tracker.reschedule_milestone(
                        ms_id, d(body["new_planned_date"]), body.get("actor", "api"), d(body["date"]))

            if method == "POST" and path == "/api/evidence":
                body = self._read_json()
                return 201, tracker.add_evidence(Evidence(
                    evidence_id=body["evidence_id"], title=body.get("title", ""),
                    source_owner=body["source_owner"], metric=body["metric"], unit=body["unit"],
                    quantity=float(body["quantity"]), report_date=d(body["report_date"]),
                    caliber_id=body["caliber_id"]))

            if method == "POST" and len(seg) == 4 and seg[0] == "api" and seg[1] == "evidence" \
                    and seg[3] == "adoptions":
                ev_id = seg[2]
                body = self._read_json()
                return 201, tracker.adopt_evidence(EvidenceAdoption(
                    adoption_id=body["adoption_id"], evidence_id=ev_id,
                    target_id=body["target_id"], adopted_by=body["adopted_by"],
                    role=CountingRole(body.get("role", "primary")),
                    share=float(body.get("share", 1.0)), on_date=d(body["date"]),
                    caliber_mismatch=bool(body.get("caliber_mismatch", False)),
                    duplicate_confirmed=bool(body.get("duplicate_confirmed", False)),
                    overlap_confirmed=bool(body.get("overlap_confirmed", False))))

            if method == "GET" and len(seg) == 4 and seg[0] == "api" and seg[1] == "evidence" \
                    and seg[3] == "boundary":
                return 200, tracker.evidence_boundary(seg[2])

            if method == "POST" and path == "/api/proposals":
                body = self._read_json()
                ptype = ProposalType(body["type"])
                replacement = None
                if body.get("replacement"):
                    r = body["replacement"]
                    replacement = ReplacementDraft(
                        measure_id=r["measure_id"], title=r.get("title", ""), owner=r["owner"],
                        effective_from=d(r["effective_from"]),
                        effective_until=d(r.get("effective_until")),
                        prerequisite_ids=tuple(r.get("prerequisite_ids", [])))
                proposal = ChangeProposal(
                    proposal_id=body["proposal_id"], measure_id=body["measure_id"],
                    ptype=ptype, rationale=body.get("rationale", ""),
                    submitted_by=body.get("submitted_by", "api"),
                    submitted_on=d(body["submitted_on"]),
                    new_effective_from=d(body.get("new_effective_from")),
                    new_effective_until=d(body.get("new_effective_until")),
                    new_prerequisite_ids=t(body.get("new_prerequisite_ids")),
                    replacement=replacement)
                return 201, tracker.submit_proposal(proposal)

            if method == "POST" and len(seg) == 4 and seg[0] == "api" and seg[1] == "proposals" \
                    and seg[3] in ("approve", "reject"):
                pid, action = seg[2], seg[3]
                body = self._read_json()
                on_date = d(body["date"])
                actor = body.get("actor", "api")
                if action == "approve":
                    return 200, tracker.approve_proposal(pid, actor, on_date)
                return 200, tracker.reject_proposal(pid, actor, on_date, body.get("reason", ""))

            if method == "POST" and len(seg) == 4 and seg[0] == "api" and seg[1] == "measures" \
                    and seg[3] == "activate":
                body = self._read_json()
                return 200, tracker.activate_measure(seg[2], body.get("actor", "api"), d(body["date"]))

            if method == "GET" and len(seg) == 4 and seg[0] == "api" and seg[1] == "measures":
                mid, resource = seg[2], seg[3]
                if resource == "status":
                    return 200, tracker.status_on(mid, self._query_date(params))
                if resource == "blockers":
                    return 200, {"measure_id": mid, "date": self._query_date(params).isoformat(),
                                 "blockers": tracker.blockers_on(mid, self._query_date(params))}
                if resource == "impact":
                    return 200, tracker.preview_withdrawal_impact(mid, self._query_date(params))
                if resource == "history":
                    return 200, tracker.history(mid)

            if method == "GET" and path == "/api/report":
                return 200, tracker.report(self._query_date(params), params.get("owner", [None])[0])

            if method == "GET" and path == "/api/events":
                return 200, {"events": tracker.events()}

            self._send(404, {"error": "not_found", "message": f"无此路由: {method} {path}"})
            return None

    return Handler


def run(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None) -> ThreadingHTTPServer:
    repo = JsonRepository(db_path)
    tracker = PolicyTracker(repo)
    httpd = ThreadingHTTPServer((host, port), make_handler(tracker, repo))
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="政策承诺跟踪 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/policy.json", help="JSON 数据文件；不存在则自动创建")
    args = parser.parse_args()
    httpd = run(args.host, args.port, args.db)
    print(f"政策承诺跟踪 API 运行于 http://{args.host}:{args.port}  数据文件: {args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
