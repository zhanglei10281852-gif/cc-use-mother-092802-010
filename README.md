# 绿色转型政策承诺跟踪后端

产业转型会议中，能源、交通、制造业等部门的阶段承诺带有不同生效日期与统计口径。
本系统将**政策措施、责任主体、目标指标、前置条件、进度凭据、生效/失效区间**
联系起来，支持跨部门引用、变更提案、审批与撤销，并在措施撤回或被替代时
生成**影响清单**。

纯 Python 标准库实现，**离线可运行**，无需安装任何第三方依赖（Python 3.10+）。

## 核心业务规则

1. **审批依据冻结**：一次审批只能基于审批日当时有效的前置措施（未审批、
   未生效、已届满、已撤回、已被替代均不得作为依据）；审批时留痕前置快照
   `approval_basis`，事后前置失效不改变审批的历史效力。
2. **影响清单**：撤回 / 替代经提案审批生效后，沿前置链找出全部直接与传递
   依赖方，列出阻塞原因、受波及的未完成里程碑。
3. **里程碑不被静默重排**：已完成里程碑冻结，延期提案只能移动未完成里程碑，
   且只能延后不能提前；影响清单将已完成里程碑单列保留。
4. **历史快照稳定**：状态按报告日推导。撤回 / 替代生效日之前的报告日，
   仍返回当时的 active 状态，不被后续事件覆盖。
5. **凭据共享与重复统计边界**：同一凭据被多个部门采用时，按主张份额之和
   判定 `exclusive`（≤1 个部门）/ `shared`（合计 ≤ 100%）/
   `double_count`（合计 > 100%）；日报自动对后者给出预警。
6. **变更全走提案工作流**：adopt（审批）/ extend（延期）/ replace（替代）/
   repoint（重指前置）/ withdraw（撤回），全部记录 raised/approved/rejected
   时间线与事件日志；支持经 repoint 摘除失效前置以解除下游阻塞。

## 目录结构

```
src/transition_policy/
  contracts.py  不可变领域对象（措施/目标/里程碑/凭据/提案/影响清单/快照）
  store.py      注册表与全部业务规则、报告日查询、阻塞推导、变更历史
  seed.py       能源→交通→制造业跨部门演示数据（含共享与重复统计凭据）
  api.py        零依赖 HTTP API（http.server）
  demo.py       离线场景演示脚本
tests/
  test_contracts.py     原有契约测试
  test_store_rules.py   业务规则测试（39 项断言场景）
  test_api.py           真实 HTTP 往返端到端测试
```

## 运行自动化验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

## 离线场景演示（不启动服务）

```bash
PYTHONPATH=src python -m transition_policy.demo
```

演示跨部门依赖链、撤回影响清单、历史快照、冻结里程碑与重复统计识别。

## 启动 HTTP API

```bash
PYTHONPATH=src python -m transition_policy.api --seed --port 8080
```

### 管理人员常用查询

```bash
# 某一报告日的全部承诺状态、阻塞与汇总（可按部门过滤）
curl 'http://127.0.0.1:8080/api/report?on=2027-06-30'
curl 'http://127.0.0.1:8080/api/report?on=2027-09-02&department=transport'

# 单项措施报告（状态、阻塞原因、目标里程碑、审批依据快照、凭据预警）
curl 'http://127.0.0.1:8080/api/measures/m-greenmfg?on=2027-09-02'

# 变更历史（事件时间线 + 上下游影响清单）
curl 'http://127.0.0.1:8080/api/measures/m-grid/history'

# 凭据的共享 / 重复统计边界
curl 'http://127.0.0.1:8080/api/evidence/e-audit/boundary'

# 提案列表与某次撤回的影响清单
curl 'http://127.0.0.1:8080/api/proposals'
curl 'http://127.0.0.1:8080/api/impacts/p-grid-wd'
```

### 写操作（均为 JSON POST）

| 路径 | 说明 |
|---|---|
| `/api/bodies` | 登记责任主体 |
| `/api/measures` | 草拟措施（初始 proposed） |
| `/api/measures/{id}/targets` | 登记目标指标与里程碑 |
| `/api/targets/{id}/evidence` | 登记进度凭据（可附部门采用份额） |
| `/api/evidence/{id}/adoptions` | 其他部门采用同一凭据 |
| `/api/measures/.../milestones/{id}/complete` | 完成里程碑（冻结，可挂凭据） |
| `/api/proposals` | 提出 adopt/extend/replace/repoint/withdraw 提案 |
| `/api/proposals/{id}/decision` | 审批/驳回；撤回与替代随审批返回影响清单 |

示例：审批一项措施

```bash
curl -X POST 'http://127.0.0.1:8080/api/proposals/p-x/decision' \
  -H 'Content-Type: application/json' \
  -d '{"approve": true, "decided_by": "committee", "decided_on": "2026-03-05"}'
```

前置在审批日非有效时返回 400 并给出逐条原因（`not_approved` /
`future_effective` / `expired` / `withdrawn` / `superseded`）。

## 种子数据的依赖链

```
m-grid（能源：电网清洁化 2026-01-01~2028-12-31）
  └─ m-charge（交通：充换电网络，前置 m-grid）
  └─ m-greenmfg（制造：园区绿电替代，前置 m-grid + m-charge）
       └─ m-fleet（交通：车队电动化，前置 m-greenmfg）
```

凭据 `e-meter` 被能源（0.8）与制造业（0.2）采用 → `shared`；
凭据 `e-audit` 被交通（0.7）与制造业（0.7）采用 → `double_count`。
