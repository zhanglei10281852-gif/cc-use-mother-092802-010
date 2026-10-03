"""离线演示：构造能源/交通/制造三部门场景，演示审批、阻塞、撤销影响与凭据边界。

用法：
    python scripts/demo.py --db data/demo.json --reset     # 播种数据
    python scripts/demo.api ...（服务见 python -m transition_policy.api）
    python scripts/demo.py --db data/demo.json --report 2027-06-02
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from transition_policy.contracts import (
    ChangeProposal, CountingRole, Evidence, EvidenceAdoption, MeasureStatus,
    Milestone, PolicyMeasure, ProposalType, ReplacementDraft, ResponsibleBody, Target,
)
from transition_policy.errors import PolicyDomainError
from transition_policy.repository import JsonRepository
from transition_policy.service import PolicyTracker


def seed(db_path: str) -> None:
    path = Path(db_path)
    if path.exists():
        path.unlink()
    svc = PolicyTracker(JsonRepository(path))

    svc.add_body(ResponsibleBody("energy", "能源局", "energy"))
    svc.add_body(ResponsibleBody("transport", "交通局", "transport"))
    svc.add_body(ResponsibleBody("industry", "制造局", "manufacturing"))

    # 能源：电网改造（2026 年生效，2027 年底阶段性到期）
    svc.add_measure(PolicyMeasure(
        "m-grid", "energy", date(2026, 1, 1), date(2028, 1, 1), (),
        MeasureStatus.ACTIVE, title="跨省电网改造"))
    svc.add_target(Target("t-grid-cap", "m-grid", "新增供电能力", "MW", 500.0,
                          date(2028, 12, 31), "cal-2026"))
    svc.add_target(Target("t-grid-pile", "m-grid", "充电桩", "个", 200.0,
                          date(2028, 12, 31), "cal-2026"))

    # 交通：干线充电网络，前置为能源的电网改造（跨部门引用）
    svc.add_measure(PolicyMeasure(
        "m-charge", "transport", date(2026, 3, 1), None, ("m-grid",),
        MeasureStatus.PROPOSED, title="干线公路充电网络"))
    svc.add_target(Target("t-charge", "m-charge", "充电桩", "个", 1000.0,
                          date(2029, 12, 31), "cal-2026"))

    # 制造：物流车队电气化，间接依赖能源部门
    svc.add_measure(PolicyMeasure(
        "m-fleet", "industry", date(2026, 6, 1), None, ("m-charge",),
        MeasureStatus.PROPOSED, title="物流车队电气化"))
    svc.add_target(Target("t-fleet", "m-fleet", "电动车比例", "%", 80.0,
                          date(2030, 12, 31), "cal-2025"))

    # 审批交通措施：2026-03-01 前置有效
    svc.activate_measure("m-charge", "交通局审批人", date(2026, 3, 1))

    # 一份跨部门共享凭据：高速服务区充换电一体站（源属能源，交通也采用）
    svc.add_evidence(Evidence(
        "ev-station", "高速服务区充换电一体站（一期）", "energy", "充电桩", "个",
        100.0, date(2026, 7, 1), "cal-2026"))
    svc.adopt_evidence(EvidenceAdoption(
        "ad-energy", "ev-station", "t-grid-pile", "energy",
        CountingRole.PRIMARY, 1.0, date(2026, 7, 2)))
    # 交通部门只能分摊，不能全额主计；此处确认 60% 分摊并承认比例重叠
    svc.adopt_evidence(EvidenceAdoption(
        "ad-transport", "ev-station", "t-charge", "transport",
        CountingRole.SHARED, 0.6, date(2026, 7, 5), overlap_confirmed=True))

    # 里程碑：交通部门完成首批建设，凭据锁定
    svc.add_milestone(Milestone("ms-charge-1", "t-charge", "首批 60 桩投运",
                                date(2026, 7, 10)))
    svc.complete_milestone("ms-charge-1", date(2026, 7, 10), "交通局", "ev-station")

    # 制造部门 2026-06-01 审批时前置链全部有效
    svc.activate_measure("m-fleet", "制造局审批人", date(2026, 6, 1))

    # 2027-06-01：能源部门提出撤销电网改造（资金口径调整）
    svc.submit_proposal(ChangeProposal(
        "p-withdraw-grid", "m-grid", ProposalType.WITHDRAW,
        "中央资金退出，改造事权下放", "能源局", date(2027, 5, 20)))
    # 审批日 2027-06-01 本措施自身仍在有效期内；撤销生效，产出影响清单
    svc.approve_proposal("p-withdraw-grid", "转型会议", date(2027, 6, 1))

    # 交通部门随后提出替代提案重建措施（新国标），草案无前置（脱钩重建）。
    # 提案保持待审批状态，供会议查询；审批后旧措施在决策日闭合、目标与已完成里程碑原样迁移。
    svc.submit_proposal(ChangeProposal(
        "p-replace-charge", "m-charge", ProposalType.REPLACE,
        "充电国标更新，措施重立并脱钩电网改造", "交通局", date(2027, 8, 1),
        replacement=ReplacementDraft(
            "m-charge-v2", "干线公路充电网络（新国标）", "transport",
            date(2027, 9, 1), None, ())))

    save(svc)
    print(f"演示数据已写入 {db_path}")


def save(svc: PolicyTracker) -> None:
    svc.repo.save()


def show(db_path: str, report_date: date) -> None:
    svc = PolicyTracker(JsonRepository(db_path))
    print("=" * 70)
    print(f"报告日 {report_date} 全部门承诺状态")
    print("=" * 70)
    print(json.dumps(svc.report(report_date), ensure_ascii=False, indent=2))
    print("\n共享凭据 ev-station 的统计边界：")
    print(json.dumps(svc.evidence_boundary("ev-station"), ensure_ascii=False, indent=2))
    print("\nm-charge 变更历史（沿替代谱系）：")
    print(json.dumps(svc.history("m-charge"), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/demo.json")
    parser.add_argument("--reset", action="store_true", help="删除旧库并重新播种")
    parser.add_argument("--report", help="输出指定报告日的状态，格式 YYYY-MM-DD")
    args = parser.parse_args()
    if args.reset:
        seed(args.db)
    if args.report:
        if not Path(args.db).exists():
            raise SystemExit("数据库不存在，请先加 --reset")
        show(args.db, date.fromisoformat(args.report))
    if not args.reset and not args.report:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except PolicyDomainError as exc:
        print(f"[{exc.code}] {exc.message}\n{json.dumps(exc.details, ensure_ascii=False, indent=2)}",
              file=sys.stderr)
        sys.exit(1)
