"""跨部门演示数据（能源 / 交通 / 制造业）。

前置链：
    m-grid（电网清洁化，能源部）
      └─ m-charge（充换电网络，交通部，前置 m-grid）
      └─ m-greenmfg（制造业绿电替代，前置 m-grid + m-charge）
           └─ m-fleet（重点企业车队电动化，交通部，前置 m-greenmfg）

凭据：
    e-meter  计量数据：能源部 0.8 + 制造业 0.2 → 共享（合计 100%）
    e-audit  核查报告：交通部 0.7 + 制造业 0.7 → 重复统计（合计 140%）

所有审批均按“审批日前置有效”的顺序完成，保留审批依据快照。
"""

from __future__ import annotations

from datetime import date

from .contracts import (
    EvidenceAdoption,
    Milestone,
    ProposalType,
)
from .store import Registry

SEED_TODAY = date(2026, 10, 5)


def build_seed_registry() -> Registry:
    reg = Registry()

    reg.add_body("b-energy", "能源转型司", "energy")
    reg.add_body("b-transport", "综合运输司", "transport")
    reg.add_body("b-mfg", "工业绿色发展司", "manufacturing")

    # --- m-grid：能源部，已审批生效 ---------------------------------
    reg.draft_measure(
        owner="energy", title="电网清洁化改造",
        measure_id="m-grid",
        effective_from=date(2026, 1, 1),
        effective_until=date(2028, 12, 31),
        raised_by="b-energy", on=date(2025, 12, 1),
    )
    reg.add_target(
        "m-grid", "非化石能源装机占比", "%", 50.0, date(2028, 12, 31),
        baseline=32.0, target_id="t-renew",
        milestones=(
            Milestone("ms-r1", "t-renew", "一期并网改造", date(2026, 6, 30)),
            Milestone("ms-r2", "t-renew", "二期跨省通道", date(2027, 12, 31)),
        ),
    )
    reg.raise_proposal(
        ProposalType.ADOPT, "m-grid", "b-energy", date(2025, 12, 15),
        "产业转型会议能源部门承诺", proposal_id="p-grid-adopt",
    )
    reg.decide_proposal("p-grid-adopt", True, "committee", date(2025, 12, 15))

    # --- m-charge：交通部，前置 m-grid ------------------------------
    reg.draft_measure(
        owner="transport", title="充换电基础设施网络",
        measure_id="m-charge",
        effective_from=date(2026, 6, 1),
        effective_until=date(2029, 12, 31),
        prerequisite_ids=("m-grid",),
        raised_by="b-transport", on=date(2026, 5, 1),
    )
    reg.add_target(
        "m-charge", "高速公路服务区快充覆盖率", "%", 90.0,
        date(2029, 12, 31), target_id="t-charger", additive_across_departments=True,
        milestones=(
            Milestone("ms-c1", "t-charger", "干线服务区覆盖", date(2026, 9, 30)),
            Milestone("ms-c2", "t-charger", "全省网络闭环", date(2028, 6, 30)),
        ),
    )
    reg.raise_proposal(
        ProposalType.ADOPT, "m-charge", "b-transport", date(2026, 5, 20),
        "充电网络依赖电网改造按期投运", proposal_id="p-charge-adopt",
    )
    reg.decide_proposal("p-charge-adopt", True, "committee", date(2026, 5, 20))

    # --- m-greenmfg：制造业，前置 m-grid + m-charge ------------------
    reg.draft_measure(
        owner="manufacturing", title="重点园区绿电替代",
        measure_id="m-greenmfg",
        effective_from=date(2027, 1, 1),
        effective_until=None,
        prerequisite_ids=("m-grid", "m-charge"),
        raised_by="b-mfg", on=date(2026, 11, 20),
    )
    reg.add_target(
        "m-greenmfg", "园区绿电消费比例", "%", 35.0, date(2030, 12, 31),
        baseline=8.0, target_id="t-subst",
        milestones=(
            Milestone("ms-g1", "t-subst", "首批 10 园区签约", date(2027, 3, 31)),
            Milestone("ms-g2", "t-subst", "规模替代 30%", date(2029, 12, 31)),
        ),
    )
    reg.raise_proposal(
        ProposalType.ADOPT, "m-greenmfg", "b-mfg", date(2026, 12, 20),
        "绿电替代以电网清洁化和充电网络为前置",
        proposal_id="p-greenmfg-adopt",
    )
    # 审批日 m-grid 已生效、m-charge 已生效（2026-06-01 起）
    reg.decide_proposal("p-greenmfg-adopt", True, "committee",
                        date(2026, 12, 20))

    # --- m-fleet：交通部，前置 m-greenmfg ----------------------------
    reg.draft_measure(
        owner="transport", title="重点物流车队电动化",
        measure_id="m-fleet",
        effective_from=date(2027, 7, 1),
        effective_until=None,
        prerequisite_ids=("m-greenmfg",),
        raised_by="b-transport", on=date(2027, 5, 1),
    )
    reg.add_target(
        "m-fleet", "试点车队电动化比例", "%", 60.0, date(2030, 6, 30),
        target_id="t-fleet",
        milestones=(
            Milestone("ms-f1", "t-fleet", "干线试点投运", date(2028, 6, 30)),
        ),
    )
    reg.raise_proposal(
        ProposalType.ADOPT, "m-fleet", "b-transport", date(2027, 6, 15),
        "车队电动化以园区绿电供应稳定为前置", proposal_id="p-fleet-adopt",
    )
    reg.decide_proposal("p-fleet-adopt", True, "committee", date(2027, 6, 15))

    # --- 凭据：共享（能源 0.8 + 制造 0.2） ---------------------------
    reg.register_evidence(
        target_id="t-renew",
        title="省级电网计量结算单 Q2",
        kind="metering", reference="GRID-METER-2026Q2",
        origin_department="energy", value=41.2, unit="%",
        period_start=date(2026, 4, 1), period_end=date(2026, 6, 30),
        evidence_id="e-meter",
        adoptions=(
            EvidenceAdoption("energy", 0.8, "计入非化石装机进度",
                             date(2026, 7, 5)),
            EvidenceAdoption("manufacturing", 0.2,
                             "制造业按购电协议份额主张同一电量",
                             date(2026, 7, 12)),
        ),
    )
    reg.complete_milestone("t-renew", "ms-r1", date(2026, 7, 10),
                           "b-energy", evidence_id="e-meter")

    # --- 凭据：重复统计风险（交通 0.7 + 制造 0.7 = 1.4） --------------
    reg.register_evidence(
        target_id="t-charger",
        title="充电设施第三方核查报告",
        kind="audit", reference="AUDIT-EV-2026Q3",
        origin_department="transport", value=128000, unit="座",
        period_start=date(2026, 7, 1), period_end=date(2026, 9, 30),
        evidence_id="e-audit",
        adoptions=(
            EvidenceAdoption("transport", 0.7, "交通部按建设口径主张",
                             date(2026, 10, 1)),
            EvidenceAdoption("manufacturing", 0.7,
                             "制造业按厂区桩口径全额主张，边界未扣减",
                             date(2026, 10, 3)),
        ),
    )

    return reg
