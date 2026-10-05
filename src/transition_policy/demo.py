"""离线管理场景演示（无需启动 HTTP 服务）。

运行：``PYTHONPATH=src python -m transition_policy.demo``

展示：报告日状态 → 撤回审批 → 影响清单（已完成里程碑冻结）→
历史快照不被改写 → 阻塞原因 → 重指前置解除阻塞 → 凭据重复统计边界。
"""

from __future__ import annotations

import json
from datetime import date

from .contracts import ProposalType
from .seed import build_seed_registry
from .store import PolicyError


def _line(char: str = "=") -> None:
    print(char * 72)


def _show(reg, mid: str, on: str) -> None:
    report = reg.measure_report(mid, date.fromisoformat(on))
    print(f"[{on}] {mid}《{report['title']}》状态: {report['status']}, "
          f"阻塞: {'是' if report['blocked'] else '否'}")
    if report["blockers"]:
        for r in report["blockers"]:
            pre = r.get("prerequisite_id", "")
            since = r.get("since", "")
            print(f"    - {r['kind']} {pre} {r['reason'] if 'reason' in r else ''} "
                  f"{since}")
    for t in report["targets"]:
        for ms in t["milestones"]:
            flag = "✓完成(冻结)" if ms["frozen"] else ("逾期" if ms["overdue"] else "计划中")
            print(f"    里程碑 {ms['milestone_id']} {ms['name']} "
                  f"截止 {ms['due_date']} → {flag}")


def main() -> None:
    reg = build_seed_registry()

    _line()
    print("场景 1：报告日 2027-06-30 的跨部门承诺全景")
    _line("-")
    daily = reg.daily_report(date(2027, 6, 30))
    for m in daily["measures"]:
        print(f"  {m['measure_id']:12} {m['owner']:13} {m['status']:10} "
              f"前置={m['prerequisite_ids']} 阻塞={m['blocked']}")
    print(f"  汇总: {daily['summary']}")

    _line()
    print("场景 2：能源部撤回 m-grid（撤回须走提案与审批）")
    _line("-")
    reg.raise_proposal(
        ProposalType.WITHDRAW, "m-grid", "b-energy", date(2027, 8, 25),
        "资金与技术路线调整，电网清洁化措施撤回",
        payload={"reason": "资金与技术路线调整"}, proposal_id="p-grid-wd",
    )
    result = reg.decide_proposal("p-grid-wd", True, "committee",
                                 date(2027, 9, 1))
    impact = result["impact"]
    print(f"  撤回生效日: {impact['occurred_on']}，受影响依赖方 "
          f"{len(impact['items'])} 项：")
    for item in impact["items"]:
        print(f"  → {item['dependent_measure_id']}（{item['owner']}）")
        print(f"      阻塞原因: {item['blocker']}")
        print(f"      受波及的未完成里程碑: "
              f"{[r[0] for r in item['open_milestones']]}")
        print(f"      已完成里程碑予以保留、不重排: "
              f"{[r[0] for r in item['preserved_completed_milestones']]}")

    _line()
    print("场景 3：撤回后报告日阻塞 vs 撤回前历史快照不变")
    _line("-")
    _show(reg, "m-greenmfg", "2027-09-02")
    _show(reg, "m-greenmfg", "2027-08-31")
    done = next(ms for t in reg.targets.values() if t.measure_id == "m-grid"
                for ms in t.milestones if ms.milestone_id == "ms-r1")
    print(f"  已撤回措施的已完成里程碑 ms-r1 仍为 "
          f"{done.status.value}，完成日 {done.completed_date}（未被静默重排）")

    _line()
    print("场景 4：变更历史可追溯（m-grid）")
    _line("-")
    history = reg.change_history("m-grid")
    for e in history["events"]:
        print(f"  {e['on']}  {e['kind']:24} {e['actor']} {e['detail']}")

    _line()
    print("场景 5：凭据共享 / 重复统计边界")
    _line("-")
    for eid in ("e-meter", "e-audit"):
        b = reg.evidence_boundary(eid)
        print(f"  {eid}: 份额合计 {b['claimed_share_total']:.2f} → "
              f"{b['boundary']}"
              + ("  ⚠ 存在重复统计" if b["double_counted"] else ""))

    _line()
    print("场景 6：审批依据规则——前置失效时不得审批（演示拦截）")
    _line("-")
    try:
        # m-grid 已撤回，此时任何以它为前置的新措施审批都必须被拒绝
        reg.draft_measure("transport", date(2027, 10, 1), None,
                          prerequisite_ids=("m-grid",),
                          measure_id="m-demo-blocked",
                          title="前置已失效的措施")
        reg.raise_proposal(ProposalType.ADOPT, "m-demo-blocked", "b-transport",
                           date(2027, 9, 20), "", proposal_id="p-demo")
        reg.decide_proposal("p-demo", True, "committee", date(2027, 9, 25))
    except PolicyError as exc:
        print(f"  审批被拒绝: {exc}")

    print()
    print(json.dumps({"result": "演示完成，全部业务规则按预期执行"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
