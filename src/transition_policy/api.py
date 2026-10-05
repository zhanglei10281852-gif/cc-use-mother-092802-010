"""离线 HTTP API（仅依赖 Python 标准库）。

启动::

    python -m transition_policy.api --seed --port 8080

所有写操作即时反映在内存注册表中；用 ``--seed`` 载入跨部门演示数据。
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .contracts import ProposalType
from .seed import build_seed_registry
from .store import PolicyError, Registry

ISO = "%Y-%m-%d"


def _iso(value) -> date | None:
    return None if value is None else date.fromisoformat(value)


class ApiState:
    def __init__(self, registry: Registry | None = None) -> None:
        self.lock = threading.RLock()
        self.registry = registry or Registry()


class PolicyHandler(BaseHTTPRequestHandler):
    server_version = "PolicyTracker/1.0"

    # ------------------------------------------------------------ helpers
    @property
    def state(self) -> ApiState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise PolicyError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise PolicyError("请求体须为 JSON 对象")
        return data

    def _query(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # --------------------------------------------------------------- GET
    def do_GET(self) -> None:
        try:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            reg = self.state.registry
            with self.state.lock:
                if path == "/health":
                    self._send({"status": "ok"})
                elif path == "/api/report":
                    q = self._query()
                    report = reg.daily_report(_iso(q["on"]), q.get("department") or None)
                    self._send(report)
                elif path.startswith("/api/measures/"):
                    rest = path.split("/api/measures/")[1].split("/")
                    mid = rest[0]
                    if len(rest) == 1:
                        on = _iso(self._query()["on"])
                        self._send(reg.measure_report(mid, on))
                    elif rest[1] == "history":
                        self._send(reg.change_history(mid))
                    else:
                        self._send({"error": "not found"}, HTTPStatus.NOT_FOUND)
                elif path.startswith("/api/evidence/") and \
                        path.endswith("/boundary"):
                    eid = path.split("/api/evidence/")[1].split("/")[0]
                    self._send(reg.evidence_boundary(eid))
                elif path == "/api/proposals":
                    self._send({
                        "proposals": [
                            {
                                "proposal_id": p.proposal_id,
                                "proposal_type": p.proposal_type.value,
                                "measure_id": p.measure_id,
                                "status": p.status.value,
                                "raised_by": p.raised_by,
                                "raised_on": p.raised_on.isoformat(),
                                "rationale": p.rationale,
                                "payload": p.payload,
                                "decided_on": p.decided_on.isoformat()
                                if p.decided_on else None,
                                "decision_note": p.decision_note,
                            }
                            for p in reg.proposals.values()
                        ]
                    })
                elif path.startswith("/api/impacts/"):
                    pid = path.split("/api/impacts/")[1]
                    report = reg.impacts.get(pid)
                    if report is None:
                        self._send({"error": "该提案无影响清单或提案不存在"},
                                   HTTPStatus.NOT_FOUND)
                    else:
                        self._send(Registry._impact_to_prim(report))
                else:
                    self._send({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except PolicyError as exc:
            self._send({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except KeyError as exc:
            self._send({"error": f"缺少查询参数: {exc.args[0]}"},
                       HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send({"error": f"服务器错误: {exc}"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)

    # -------------------------------------------------------------- POST
    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path.rstrip("/")
            reg = self.state.registry
            data = self._read_json()
            with self.state.lock:
                if path == "/api/bodies":
                    body = reg.add_body(data["body_id"], data["name"],
                                        data["department"])
                    self._send({"body_id": body.body_id}, 201)

                elif path == "/api/measures":
                    measure = reg.draft_measure(
                        owner=data["owner"],
                        effective_from=_iso(data["effective_from"]),
                        effective_until=_iso(data.get("effective_until")),
                        prerequisite_ids=tuple(data.get("prerequisite_ids", ())),
                        title=data.get("title", ""),
                        measure_id=data.get("measure_id"),
                        raised_by=data.get("raised_by"),
                    )
                    self._send({"measure_id": measure.measure_id,
                                "status": measure.status.value}, 201)

                elif path.startswith("/api/measures/") and \
                        path.endswith("/targets"):
                    mid = path.split("/api/measures/")[1].split("/")[0]
                    milestones = tuple(
                        _milestone_from_dict(m)
                        for m in data.pop("milestones", [])
                    )
                    target = reg.add_target(
                        measure_id=mid,
                        name=data["name"],
                        unit=data["unit"],
                        target_value=float(data["target_value"]),
                        due_date=_iso(data["due_date"]),
                        baseline=data.get("baseline"),
                        additive_across_departments=data.get(
                            "additive_across_departments", True),
                        target_id=data.get("target_id"),
                        milestones=milestones,
                    )
                    self._send({"target_id": target.target_id}, 201)

                elif path.startswith("/api/targets/") and \
                        path.endswith("/evidence"):
                    tid = path.split("/api/targets/")[1].split("/")[0]
                    from .contracts import EvidenceAdoption

                    adoptions = tuple(
                        EvidenceAdoption(
                            department=a["department"],
                            share=float(a["share"]),
                            scope_note=a.get("scope_note", ""),
                            adopted_on=_iso(a["adopted_on"]),
                        )
                        for a in data.get("adoptions", [])
                    )
                    record = reg.register_evidence(
                        target_id=tid,
                        title=data["title"],
                        kind=data.get("kind", "document"),
                        reference=data["reference"],
                        origin_department=data["origin_department"],
                        value=float(data["value"]),
                        unit=data["unit"],
                        period_start=_iso(data["period_start"]),
                        period_end=_iso(data["period_end"]),
                        evidence_id=data.get("evidence_id"),
                        adoptions=adoptions,
                    )
                    self._send({"evidence_id": record.evidence_id,
                                "boundary": reg.evidence_boundary(
                                    record.evidence_id)}, 201)

                elif path.startswith("/api/evidence/") and \
                        path.endswith("/adoptions"):
                    eid = path.split("/api/evidence/")[1].split("/")[0]
                    record = reg.adopt_evidence(
                        eid, data["department"], float(data["share"]),
                        data.get("scope_note", ""), _iso(data["adopted_on"]),
                    )
                    self._send(reg.evidence_boundary(record.evidence_id))

                elif ("/milestones/" in path
                      and path.endswith("/complete")):
                    parts = path.split("/")
                    # /api/measures/{mid}/targets/{tid}/milestones/{msid}/complete
                    tid = parts[parts.index("targets") + 1]
                    msid = parts[parts.index("milestones") + 1]
                    ms = reg.complete_milestone(
                        target_id=tid,
                        milestone_id=msid,
                        completed_on=_iso(data["completed_on"]),
                        completed_by=data["completed_by"],
                        evidence_id=data.get("evidence_id"),
                    )
                    self._send({"milestone_id": ms.milestone_id,
                                "status": ms.status.value})

                elif path == "/api/proposals":
                    proposal = reg.raise_proposal(
                        proposal_type=ProposalType(data["proposal_type"]),
                        measure_id=data["measure_id"],
                        raised_by=data["raised_by"],
                        raised_on=_iso(data["raised_on"]),
                        rationale=data.get("rationale", ""),
                        payload=data.get("payload", {}),
                        proposal_id=data.get("proposal_id"),
                    )
                    self._send({"proposal_id": proposal.proposal_id,
                                "status": proposal.status.value}, 201)

                elif path.startswith("/api/proposals/") and \
                        path.endswith("/decision"):
                    pid = path.split("/api/proposals/")[1].split("/")[0]
                    result = reg.decide_proposal(
                        proposal_id=pid,
                        approve=bool(data["approve"]),
                        decided_by=data["decided_by"],
                        decided_on=_iso(data["decided_on"]),
                        note=data.get("note"),
                    )
                    self._send(result)

                else:
                    self._send({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except PolicyError as exc:
            self._send({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except KeyError as exc:
            self._send({"error": f"缺少必填字段: {exc.args[0]}"},
                       HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._send({"error": f"字段格式错误: {exc}"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send({"error": f"服务器错误: {exc}"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)


def _milestone_from_dict(data: dict):
    from .contracts import Milestone, MilestoneStatus

    return Milestone(
        milestone_id=data["milestone_id"],
        target_id=data.get("target_id", ""),
        name=data["name"],
        due_date=_iso(data["due_date"]),
        status=MilestoneStatus(data.get("status", "planned")),
        completed_date=_iso(data.get("completed_date")),
        evidence_id=data.get("evidence_id"),
        completed_by=data.get("completed_by"),
    )


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  seed: bool = False, registry: Registry | None = None,
                  verbose: bool = False) -> ThreadingHTTPServer:
    if registry is None:
        registry = build_seed_registry() if seed else Registry()
    server = ThreadingHTTPServer((host, port), PolicyHandler)
    server.state = ApiState(registry)  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="政策承诺跟踪 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="载入跨部门演示数据")
    args = parser.parse_args(argv)
    server = create_server(args.host, args.port, seed=args.seed, verbose=True)
    print(f"政策跟踪 API 已启动: http://{args.host}:{args.port}")
    print("示例: curl 'http://127.0.0.1:8080/api/report?on=2027-06-30'")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
