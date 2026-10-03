# 绿色转型政策承诺跟踪后端

一套**纯 Python 标准库、离线可运行**的政策承诺跟踪系统，把政策措施、责任主体、
目标指标、前置条件、进度凭据，以及生效/失效区间联系起来，支持跨部门引用、
变更提案（延期修订 / 替代 / 撤销）、审批留痕与影响分析。

## 核心规则（设计约束）

1. **审批只能基于当时有效的前置措施**：审批（或 PROPOSED 措施激活）时校验
   全部直接/间接前置在**审批日**的点时有效性，并把前置状态快照写入提案与事件，
   事后可审计当时究竟基于什么版本批准。
2. **撤销必须生成影响清单，禁止静默重排**：撤销在决策日闭合措施的生效区间，
   产出直接/间接依赖措施、受影响目标、涉及凭据采用、被保留的已完成里程碑清单；
   早于决策日的报告日状态完全不变。已完成里程碑（`achieved_date`）永久锁定，
   改期、重完成、以及会把历史成就挤出生效区间的修订一律拒绝；只能走替代流程。
3. **凭据共享 / 重复统计边界显式化**：同一凭据被多部门采用时：
   - 至多一个全额主计方（`primary`），第二个主计方须显式确认 `duplicate_confirmed`；
   - 主计 + 分摊比例之和不得超过 1，超过须确认 `overlap_confirmed`；
   - 凭据与目标的统计口径（`caliber_id`）不同须确认 `caliber_mismatch`，
     报告中该部分实物量单列，不计入兼容口径达成率；
   - 指标/单位不一致直接拒绝计账。
4. **所有变更追加留痕**：登记、激活、里程碑登记/完成/改期、凭据登记/采用、
   提案提交/批准/拒绝、修订、替代、撤销均生成有序事件，沿 `supersedes` 谱系
   可查任一措施全部历史。

## 目录结构

```
src/transition_policy/
├── contracts.py   # 不可变领域值对象：措施/主体/目标/里程碑/凭据/采用/提案/事件
├── errors.py      # 领域异常 → HTTP 状态码映射
├── repository.py  # JSON 文件仓储（原子写入、序列化往返），path=None 为纯内存模式
├── service.py     # 核心规则：点时状态推导、审批快照、阻塞链、影响清单、凭据边界
└── api.py         # 标准库 http.server 实现的离线 HTTP API（写操作自动落盘）
tests/             # 契约(2)/仓储(2)/领域规则(27)/HTTP 端到端(4) 共 35 个用例
scripts/demo.py    # 三部门完整场景演示
serve.py           # 根目录启动入口
```

## 快速开始（无需联网、无需第三方依赖）

```bash
# 自动验证
python -m unittest discover -s tests -v
python -m compileall -q src tests serve.py scripts/demo.py

# 播种演示数据（能源→交通→制造的跨部门依赖、共享凭据、撤销、待批替代提案）
python scripts/demo.py --db data/demo.json --reset

# 查询某报告日的承诺状态、阻塞原因与进度
python scripts/demo.py --db data/demo.json --report 2027-06-02

# 启动 HTTP API
python serve.py --db data/demo.json --port 8000
# 或：PYTHONPATH=src python -m transition_policy.api --port 8000
```

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/bodies` | 登记责任主体 |
| POST | `/api/measures` | 登记措施（生效半开区间 `[from, until)`，跨部门前置） |
| POST | `/api/measures/{id}/activate` | PROPOSED 措施审批激活（校验审批日前置有效性+快照） |
| POST | `/api/targets` / `/api/milestones` | 登记目标（含统计口径）/ 里程碑 |
| POST | `/api/milestones/{id}/complete` | 完成里程碑（日期+凭据锁定，不可再改） |
| POST | `/api/milestones/{id}/reschedule` | 开放里程碑改期（已完成 → 409 milestone_locked） |
| POST | `/api/evidence` | 登记进度凭据（不可变事实） |
| POST | `/api/evidence/{id}/adoptions` | 部门采用凭据（主计/分摊、口径与重叠确认标志） |
| GET  | `/api/evidence/{id}/boundary` | 凭据共享与重复统计边界报告 |
| POST | `/api/proposals` | 提交变更提案（amend / replace / withdraw） |
| POST | `/api/proposals/{id}/approve` | 审批（校验当时有效前置、快照；撤销随附影响清单） |
| POST | `/api/proposals/{id}/reject` | 拒绝提案 |
| GET  | `/api/measures/{id}/status?date=` | 某报告日状态（含阻塞） |
| GET  | `/api/measures/{id}/blockers?date=` | 阻塞原因（含 `via` 间接依赖链） |
| GET  | `/api/measures/{id}/impact?date=` | 撤销影响清单预览 |
| GET  | `/api/measures/{id}/history` | 沿修订谱系的事件与提案历史 |
| GET  | `/api/report?date=&owner=` | 报告日全部门（或单部门）承诺状态 |
| GET  | `/api/events` | 全量有序事件流 |

### 典型请求

```bash
# 审批：只能基于审批日有效前置（本例 2025 年充电措施的前置电网尚未生效 → 422）
curl -s -X POST localhost:8000/api/measures/m-charge/activate \
  -H 'Content-Type: application/json' \
  -d '{"date":"2025-12-01","actor":"交通局"}'

# 报告日查询
curl -s "localhost:8000/api/report?date=2027-06-02"

# 共享凭据：第二部门只能分摊；比例重叠需显式确认
curl -s -X POST localhost:8000/api/evidence/ev-station/adoptions \
  -H 'Content-Type: application/json' \
  -d '{"adoption_id":"ad-transport","target_id":"t-charge","adopted_by":"transport",
       "role":"shared","share":0.6,"date":"2026-07-05","overlap_confirmed":true}'
```

## 管理人员能看到的关键信息

- **点时状态**：任意报告日的 `proposed/approved/active/withdrawn/superseded`、
  是否生效、修订版本号、生效区间；
- **阻塞原因**：`not_approved / not_yet_effective / expired / withdrawn /
  superseded / missing`，间接阻塞带 `via` 引用链；
- **影响清单**：撤销审批响应与 `/impact` 预览直接列出受影响下游措施（含责任
  部门名称）、依赖链、目标、凭据采用，以及被原样保留的历史里程碑；
- **变更历史**：措施沿替代谱系的全部事件、提案理由、审批人、审批日及当时的
  前置措施状态快照。
