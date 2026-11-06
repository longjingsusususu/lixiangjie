# 会议活动服务需求汇总系统 · 技术架构选型

> 角色：资深 Python 后端架构师 ｜ 主题：**会议活动需求**（一期单批次试点）
> 约束：FastAPI（异步）· Python 3.11+ · LLM 走 API 但**一期不接**（f01 客户红线 + f03 预算，仅预留槽位）· 数据源为三套工具**只读接入**（非向量化）· 五角色权限分离
> 预算红线：四周 / 2 人 / 32 人时，架构必须"薄"，超支触发停止判据 S4。
> **基线状态：本方案所依赖的十问架构基线已于 2026-11-06 经项目发起人确认（"全按推荐"），第 5 节对应关系全部生效。** 原提示词中的"知识库向量化检索"映射为二期 LLM 网关 + RAG 问答槽位（第 1 节编排层【二期槽位】）；"普通员工/管理员两角色"按项目实际扩展为五角色（第 3 节 users 表）。

---

## 1. 四层架构（文字版）

```
┌──────────────────────────── 入口层 ────────────────────────────┐
│ 一线专员页(登记/补录)   协调员页(定版/交接)   负责人页(确认签字)     │
│ 质量验收页(异常队列/签字) 管理员页(字段配置/映射/导入/脱敏) 看板导出  │
│ （Jinja2 SSR，预留：IM 兜底补录仅作来源记录，不做 IM 直连）         │
└──────────────────────────────┬─────────────────────────────────┘
                               │ HTTPS / JWT（五角色）
┌──────────────────────────────▼─────────────────────────────────┐
│ 服务层（FastAPI async，只做校验与转发）                           │
│  · 认证与权限（五角色编辑/确认分离，接口级校验）                    │
│  · 批次服务（单批次 50 项，分批口径可配）                          │
│  · 需求项服务（登记/补录，双时间戳强制）                           │
│  · 版本与定版服务（系统版本号 + 手动定版）                         │
│  · 交接服务（三要素卡口）   · 异常队列服务（人工闭环）              │
│  · 导入服务（接口/CSV/手工 三路路由）  · 快照服务（失败留痕）        │
│  · 看板统计服务（M6/M7/M8 自动埋点，M1/M5 采样登记）               │
└──────────────────────────────┬─────────────────────────────────┘
                               │ 进程内调用
┌──────────────────────────────▼─────────────────────────────────┐
│ 流程编排层（对应"智能体编排层"，一期为规则引擎，无 LLM）             │
│  · 五态状态机：缺失→未确认→已确认→已定版→已交接                    │
│  · 卡口规则引擎：交接三要素(f20) / 版本双条件(f24) /              │
│    异常不得默认通过(f44) —— 全部服务端强制                        │
│  · 对账器：三源数据 ↔ 编号映射表 ↔ 需求项 一致性核对               │
│  · 指标计算器：M6 确认完整率 / M7 可追溯率 / M8 埋点               │
│  · 【二期槽位】LLM 网关（供应商可替换）+ 需求抽取器 + RAG 问答      │
│    ——直接复用报销助手已验证模块，一期只留接口位，不写实现           │
└──────────────────────────────┬─────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────┐
│ 数据模型层                                                      │
│  · SQLite（WAL 模式）起步：50 项 / 并发 ≤10 人，零运维最符合预算    │
│    SQLAlchemy ORM 保证可平迁 PostgreSQL（换连接串即可）            │
│  · 本地目录作对象存储：三源快照、CSV 导入原文件、导出报表            │
│  · 不引入 Redis / 独立向量库 / 消息队列（规模撑不起，预算不允许）    │
└────────────────────────────────────────────────────────────────┘
```

三个关键选型理由：

| 决策 | 理由 | 代价与对策 |
|---|---|---|
| 一期零 LLM，只留网关槽位 | f01 客户明确"先观察交接点不自动化"；32 人时装不下标注调优；卡口/队列/定版是纯规则问题 | 编排层留 `llm_gateway` 接口位，二期接需求抽取与 RAG 问答（复用报销助手模块，边际成本最低） |
| SQLite 起步 + ORM 平迁 | 单批次 50 项、10 人并发，PG/Redis 是过度设计；零运维省下的时间全部投给卡口与留痕 | SQLAlchemy 全程 ORM，禁止裸 SQL 方言；数据涨或多批次时换 PG 连接串 |
| 卡口全部服务端强制 | 行业结论"提醒会被忽略，拦截不会"（E-01 正是没人拦的交接环节）；前端隐藏菜单不可信 | 拦截逻辑放流程编排层，任何绕过前端的直接调 API 同样被拦 |

---

## 2. 目录结构（tree）

```
meeting-demand-app/
├── app/
│   ├── main.py                  # FastAPI 入口，挂路由与生命周期
│   ├── core/
│   │   ├── config.py            # 配置（pydantic-settings：批次口径/降级开关/LLM 预留）
│   │   ├── security.py          # JWT 签发/校验、五角色依赖
│   │   └── deps.py              # 依赖注入（DB 会话、当前用户、权限断言）
│   ├── api/                     # 入口层：路由，只做参数校验和转发
│   │   ├── auth.py
│   │   ├── items.py             # 需求项：登记/补录/查询
│   │   ├── versions.py          # 定版与作废
│   │   ├── handovers.py         # 交接（卡口校验在此触发）
│   │   ├── exceptions.py        # 异常队列：入队/认领/闭环
│   │   ├── imports.py           # CSV 导入 + 三源接口适配入口
│   │   ├── board.py             # 看板与导出
│   │   └── admin.py             # 管理员：字段配置/映射表/脱敏规则/用户
│   ├── services/                # 服务层：业务逻辑
│   │   ├── item_service.py      # 登记/补录（双时间戳、五态初判）
│   │   ├── version_service.py   # 版本号生成/定版/作废留因
│   │   ├── handover_service.py  # 交接记录 + 卡口前置校验
│   │   ├── exception_service.py # 队列/复核卡/SLA 提示
│   │   ├── import_service.py    # 接口/CSV/手工 三路路由 + 幂等
│   │   ├── snapshot_service.py  # 三源快照，失败留快照标时间
│   │   └── board_service.py     # M6/M7/M8 计算 + 导出
│   ├── workflow/                # 流程编排层（本项目核心）
│   │   ├── state_machine.py     # 五态流转与非法迁移拦截
│   │   ├── gate_rules.py        # 卡口规则：f20 三要素/f24 双条件/f44
│   │   ├── reconciler.py        # 对账器：三源↔映射表↔需求项
│   │   ├── metrics.py           # 指标计算器（M6/M7/M8）
│   │   └── llm_gateway/         # 【二期槽位，一期空实现】
│   │       └── base.py          # 抽象接口，二期塞抽取器+RAG
│   ├── models/                  # SQLAlchemy 模型（见第 3 节）
│   ├── schemas/                 # Pydantic 请求/响应模型
│   └── web/                     # Jinja2 模板 + 静态资源（SSR，无前后端分离）
├── migrations/                  # Alembic
├── scripts/
│   ├── seed_demo.py             # 50 项虚构样本（演练用，不混入真实数据）
│   └── smoke_test.py            # 第一周冒烟脚本
├── tests/
├── data/
│   ├── snapshots/               # 三源快照与失败留痕
│   └── imports/                 # CSV 原文件归档
└── docker-compose.yml           # 可选：仅 app 容器，无外部中间件
```

---

## 3. 数据库表设计（9 张）

**users — 用户（五角色）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| username / password_hash | varchar | JWT 登录 |
| display_name | varchar | 林亦舟/唐映秋/顾明岚/傅景/秦芮 |
| role | enum(staff, coordinator, biz_owner, qa, admin) | 编辑权与确认权分离的载体 |
| is_active / created_at | bool / timestamptz | |

**batches — 试点批次**

| 字段 | 类型 | 说明 |
|---|---|---|
| id / name | int PK / varchar | 如"单批次试点" |
| planned_items | int default 50 | M6 分母；分批验收时按批（口径待顾明岚确认，R5） |
| region_scope | enum(single, cross) | 单区/跨区待定（R5），跨区强制启用编号映射 |
| status / created_at | enum(active, paused, closed) / timestamptz | 暂停判据 S1–S5 落点 |

**requirement_items — 需求项（核心表）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | 系统内部编号 |
| batch_id | FK→batches | |
| category | enum(equipment, language, catering, venue, other) | 设备/语言/餐饮/场地（f10：设备与语言分人记录的教训） |
| content | text | 需求描述 |
| owner_id | FK→users | 责任人（验收要素 1） |
| status | enum(missing, unconfirmed, confirmed, finalized, handed_over) | **五态分列**，缺失≠未确认（f31，验收要素 2） |
| source_tool | enum(servicedesk, ledger, filelib, manual) | 来源工具（来源可溯，验收要素 3） |
| source_row_ref | varchar null | 原始行定位（f15"缺项清单要带来源位置"） |
| record_channel | enum(im_first, system_first) | 双轨：群消息先发生→系统补录（f13） |
| **biz_occurred_at** | timestamptz | **业务发生时间**（双时间戳之一，f34） |
| **received_at** | timestamptz | **系统接收时间**（回填不得当作发生时间） |
| confirmed_by / confirmed_at | FK / timestamptz null | 确认签字（仅 biz_owner/qa 可写） |

**item_changes — 变更历史（可追溯的载体）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id / item_id | int PK / FK | |
| field_name / old_value / new_value | varchar / text / text | 变更前后值 |
| changed_by / change_source | FK / enum(manual, import, system) | 谁改的、哪条路进来的 |
| version_id | FK→versions | 归属版本 |
| created_at | timestamptz | |

**versions — 清单版本**

| 字段 | 类型 | 说明 |
|---|---|---|
| id / batch_id | int PK / FK | |
| version_no | varchar | **系统生成不可编辑**（消灭文件名猜版本，f24） |
| status | enum(draft, finalized, void) | 有效 = 有效期 + 确认状态双条件 |
| finalized_by / finalized_at | FK / timestamptz | 手动定版动作，留操作人 |
| void_reason | varchar null | 作废留因（f24） |

**handovers — 交接记录**

| 字段 | 类型 | 说明 |
|---|---|---|
| id / version_id | int PK / FK | |
| receiver_id | FK→users | 三要素①接收人（f20） |
| open_items_count | int | 三要素②未确认项数 |
| deadline | date | 三要素③截止时间 |
| transfer_point | enum(on_send, on_ack) | 责任转移时点**两口径并存**，待顾明岚签字后锁定（R1） |
| ack_by / ack_at | FK / timestamptz null | 接收确认（口径 B 用） |
| created_by / created_at | FK / timestamptz | |

**exception_queue — 异常工单**

| 字段 | 类型 | 说明 |
|---|---|---|
| id / item_id | int PK / FK | |
| exception_type | enum(missing_field, conflict, dirty_data, version_unverifiable) | 缺项/冲突/脏数据/无法确认版本（f44） |
| status | enum(open, processing, closed) | **无"自动通过"态**——不得默认通过是硬规则 |
| assigned_to / sla_due | FK / timestamptz null | 值守人与时限待傅景点名（R3），字段先建 |
| resolved_by / resolution / closed_at | FK / text / timestamptz | 复核卡：谁/依据/结论 |

**audit_logs — 留痕四项（f44/f45）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| actor_id | FK | ①谁 |
| action | enum(view, suggest, confirm, finalize, handover, import) | 动作类型 |
| target_type / target_id / target_version | varchar / int / FK | ②针对哪个对象哪个版本 |
| suggestion_snapshot | text null | ③系统建议内容（建议不覆盖原值，f14） |
| confirmed_by | FK null | ④谁确认 |
| created_at | timestamptz | |

**source_snapshots + id_mappings — 接入侧两表**

| 表 | 关键字段 | 说明 |
|---|---|---|
| source_snapshots | tool, fetched_at, status(success/failure), file_path, note | 读取失败保留上次快照并标时间（f35/S2） |
| id_mappings | source_tool, external_no, item_id, mapped_by | 三源无统一编号（f30），映射可由管理员维护（ADR-2） |

设计要点：①**双时间戳与五态是 schema 第一天字段**（ADR-1，后补要迁数据）；②`transfer_point` 两口径并存字段，签字前功能可跑、口径可切；③异常表没有"自动通过"路径，规则在状态机里写死；④所有确认类字段只能由 biz_owner/qa 角色写入，模型/系统永无审批权（f22）。

---

## 4. 第一周最小验证目标（Walking Skeleton）

**一句话：打通"导入/补录 50 项 → 定版 → 交接卡口拦截 → 异常入队人工闭环 → M6 看板出数"的端到端链路。**

拆成 5 天：

| 天 | 目标 | 完成标志 |
|---|---|---|
| D1 | 脚手架 + 五角色登录 | SQLite(WAL)+Alembic 起库；五角色 JWT 登录，权限矩阵生效（员工改不了确认字段、qa 可暂停） |
| D2 | 登记补录 + CSV 导入 | 手工登记与 CSV 导入均落库；双时间戳强制；编号映射表可维护；重复导入幂等 |
| D3 | 版本定版 | 系统生成版本号、手动定版留操作人；f24 双条件校验生效：过期来源无法覆盖新版本 |
| D4 | 交接卡口 + 留痕 | 缺任一三要素的交接被服务端 100% 拦截；交接/查看/建议/确认四类动作全部进 audit_logs |
| D5 | 异常队列 + 看板冒烟 | 脏数据/缺项入队、复核卡闭环、**全程无一例默认通过**；M6/M7 看板出数；smoke_test 全绿 |

**验收判据（达不成就要调方案）：**
1. 用 seed_demo 的 50 项虚构样本跑完全流程，M6 确认完整率可自动算出（含"缺失/未确认"分列统计）；
2. 交接三要素缺失拦截率 100%（含绕过前端直接调 API 的用例）；
3. 抽查任意一项，可沿 item_changes + audit_logs 回答"谁查过、哪个版本、什么建议、谁确认"四个问题；
4. CSV 导入的回填时间与业务发生时间在统计上可区分（双时间戳生效的直接证据）。

**第一周明确不做**：三源接口直连（先走 CSV，接口开放性待秦芮确认 R4）、LLM 任何调用（二期槽位）、脱敏配置化（R6 确认后做）、跨批次与历史数据治理（N6）、真实数据接入（E-01 原件调取需授权，附录 A 第 7 项）。

---

## 5. 与需求分析结论的对应关系

| 需求分析推荐基线 | 本方案的落点 |
|---|---|
| Q1 自研轻量 Web（SSR 单体） | FastAPI + Jinja2 SSR + SQLite，无外部中间件 |
| Q2 三路入口留口子 | import_service 三路路由 + snapshot_service 降级留痕 |
| Q3 完整数据结构 | requirement_items + item_changes + versions 三表 |
| Q4/Q5 定版与拦截卡口 | version_service + gate_rules 服务端强制 |
| Q6 人工队列 | exception_queue，状态机无自动通过路径 |
| Q7 一期埋 LLM 槽位 | workflow/llm_gateway/base.py 空实现，二期复用报销助手 RAG |
| Q8 脱敏 | admin 脱敏规则配置位预留，R6 确认后实现 |
| Q9 混合测量 | metrics.py 自动算 M6/M7/M8，M1/M5 走采样登记 |
| Q10 轻量后台 | admin 路由：字段配置/映射/导入/用户，≤5 页面 |
