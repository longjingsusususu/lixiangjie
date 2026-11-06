# 会议活动服务需求汇总系统 · Agent 编排与执行循环

> 提交稿 · 已实现并跑通（`meeting-portal/agent/`）
> 你的原话是「把报销助手从固定流程问答升级为会自主决策的智能体」，本项目的对应物是
> **「把需求汇总系统从固定五态流程升级为会自己查、自己诊、自己验的需求项管家」**。
> 术语映射如下，后文一律用会议项目命名：

| 你的说法（报销语境） | 本项目对应物 | 实际工具名 |
|---|---|---|
| 查制度 | 查需求项清单 | `search_items` / `get_item` |
| 查额度 | 查三要素与责任人是否齐（体检） | `check_completeness` |
| 三源核对 | 服务台/台账/文件库并排对比 | `diff_sources` |
| 校验发票 | 交接前置校验（发一次性票据） | `validate_handover` |
| create_draft 建草稿 | 建交接单（责任转移） | `create_handover` |
| 提交审批 | 推进状态 / 发待确认 | `upsert_item` / `request_confirm` |

---

## 1. 框架选型：为什么是 Harness（薄循环）

结论：**不引入 LangGraph / AutoGen / CrewAI，自己写一个 120 行的 Harness 循环**。

| 方案 | 能给你什么 | 在这个项目里的代价 | 结论 |
|---|---|---|---|
| **Harness（手写薄循环 + 工具注册表）** | 决策/执行/回填/中断四个动作，全链路可见 | 需要自己实现循环状态与日志（本文件第 2、3 节，约 120 行） | ✅ 选它 |
| LangGraph | 图编排、检查点、断点续跑、可视化 | 节点与边是第二套「顺序约束」，和票据机制**两处漂移**；状态机要重写成图 | ❌ 约束会重复定义 |
| AutoGen / CrewAI | 多智能体对话、角色分工 | 本项目只有 1 个角色、10 个工具、5 轮上限，多智能体是纯开销 | ❌ 过度设计 |
| 直接用 Function Calling 裸循环 | 最少代码 | 没有中断点、没有轮次上限、没有逐步留痕——正好是你这次要求补的三件事 | ❌ 缺三件套 |

选 Harness 的三条硬理由：

1. **约束只能有一处定义**。顺序约束已经在服务端由「票据」保证（`validate_handover` 发票、`create_handover` 验票）。再叠一层图编排的边，等于把同一条规则写两遍，改一处必漏一处。
2. **审计粒度天然对齐**。Harness 里「一轮决策 = 一行日志」，阶段 5 的 `group by tool` 直接就是分析结果；图编排要把 checkpoint 反解成业务语义。
3. **换模型不动骨架**。决策层是插槽（`planner.decide`），一期规则、二期 LLM，循环、工具、日志、中断全部零改动 —— 见第 7 节。

> 什么时候该换成 LangGraph：出现**分支并行**（同时向三个部门发确认）、**长时间挂起**（等外部系统回调数小时）、**多角色协同**（Agent 之间互相委派）。这三条现在一条都不成立，等成立了再换，迁移面只有 `agent/runtime.py` 一个文件。

---

## 2. 执行循环：决策 → 执行 → 回填 → 再决策（最多 5 轮）

```
POST /api/agent/chat  {goal}
        │
        ▼
   ┌─ 建 run（写 agent_runs，status=running）
   │
   ▼
┌──→ ① decide(ctx)  ──── planner.rule_decide / llm_decide
│      │                 产出 {"type":"tool",tool,args} 或 {"type":"final",answer}
│      ├── final ───────→ 写 final 日志 → run=done → 出结论（结束）
│      │
│      │  tool ∈ {create_handover}?
│      ├── 是 ──────────→ ② **中断**：草稿逐行流给员工 → run=awaiting_confirm
│      │                     （不执行、不落库，等 /api/agent/confirm）
│      ▼
│    ③ registry.invoke(tool,args)   ← 工具层负责票据、留痕、权限
│      │      异常(41201/42214/…)一律转成 observation，不让循环崩
│      ▼
│    ④ obs.append({ok,code,result,duration_ms}) → 写 act 日志
│      │
└──────┘  round_no += 1，回到 ①
                       │
                  第 6 轮 → run=max_rounds，强制收束
```

代码落点：

| 环节 | 文件 | 关键点 |
|---|---|---|
| 循环主体 | `agent/runtime.py: _drive()` | 生成器，边跑边 yield 事件（SSE 直接消费） |
| 决策 | `agent/planner.py: decide()` | B 档失败自动回落 A 档，`ctx["decider"]` 记下是谁做的决定 |
| 执行 | `tools/registry.py: invoke()` | 工具是唯一出口；未注册能力调用即 40405 |
| 异常回填 | `runtime._exec_tool()` | `HTTPException` → `{ok:false,code,message}`，**拒绝是正常观察**，不是崩溃 |
| 轮次上限 | `MAX_ROUNDS = 5` | 硬编码常量，不靠提示词「请你不要超过5轮」 |
| 中断 | `INTERRUPT_TOOLS = ("create_handover",)` | 白名单，新增写工具默认不中断，必须显式加入 |

**「再决策」不是重复上一轮**。决策函数每次都重新看全部观察：

- 校验没过 → 直接给结论，不会硬着头皮去建交接；
- 建交接失败（票据过期）→ 给原因，**不重试**（重试只会再失败一次，还多烧一轮）；
- 缺接收人/截止时间 → 先追问，不拿空参数去调工具（避免把 41201 当作"系统坏了"报给用户）。

---

## 3. 日志表：每轮决策可回溯，供阶段 5 分析

两张表，一次任务一行 run、一轮一件事一行 step：

```sql
agent_runs(id, user_id, goal, intent, decider, status, rounds,
           pending_action, context_json, final_answer,
           started_ts, total_ms, created_at, ended_at)

agent_steps(id, run_id, round_no, phase, tool, args_json,
            ok, code, digest, duration_ms, created_at)
```

`phase` 四种，正好对应循环的四个动作：

| phase | 何时写 | 关键字段 |
|---|---|---|
| `decide` | 每次决策 | `tool`（选中什么）、`digest`（为什么选它） |
| `act` | 每次真正执行 | `tool`、`args_json`、`duration_ms`、`ok`、`code`、`digest`（结果摘要） |
| `interrupt` | 撞上写操作被拦 | `tool`、`args_json`（含票据）、`digest`="等待人工确认" |
| `final` | 出结论 | `digest`=结论全文（截断 240 字） |

`digest` 只留判断成败与规模的信息，不整段落库：命中列表压成「N 条 + 前三条编号」，冲突压成「N 处 + 字段名」。

**阶段 5 可以直接跑的 SQL**（对应 `/api/admin/agent/stats`）：

```sql
-- 哪个工具最慢、哪种错最多
SELECT tool, COUNT(*) calls,
       SUM(ok=1) ok_calls,
       ROUND(AVG(duration_ms),1) avg_ms, MAX(duration_ms) max_ms
FROM agent_steps WHERE phase='act' GROUP BY tool ORDER BY calls DESC;

-- 错误码分布（定位"用户为什么放弃"）
SELECT tool, code, COUNT(*) n FROM agent_steps
WHERE phase='act' AND ok=0 GROUP BY tool, code ORDER BY n DESC;

-- 中断转化率：跑到中断的任务，最后有多少人真的点了确认
SELECT SUM(status='done') 完成, SUM(status='cancelled') 取消, SUM(status='awaiting_confirm') 挂着
FROM agent_runs WHERE created_at >= date('now','-7 day');

-- 轮次分布：如果大量任务贴着 5 轮，说明工具粒度太粗、需要拆
SELECT rounds, COUNT(*) FROM agent_runs GROUP BY rounds ORDER BY rounds;
```

> 这几条不需要额外埋点。循环跑的时候就在记账 —— 这也是选 Harness 的直接收益。

---

## 4. create_handover 前的强制中断

### 4.1 为什么不能只靠提示词

「请你在建单前先让用户确认」写在提示词里，等于把**拦截权交给模型的服从性**。模型会在这些情况下绕过：多轮压缩掉系统提示、把"确认"理解成"用户已经说了要做"，或者干脆幻觉一句"已确认"。

所以这里用**三层，从软到硬**：

| 层 | 手段 | 谁保证 |
|---|---|---|
| L1 提示词 | 「写操作前必须先出草稿」写进系统提示（第 7 节全文） | 模型自觉，随时可能失效 |
| L2 循环中断 | `create_handover` 命中 `INTERRUPT_TOOLS`，**执行前 return**，run 置 `awaiting_confirm` | 代码保证，模型无法跳过 |
| L3 票据 | 真正执行时必须带 `validate_handover` 签发的票（绑参数哈希 + 5 分钟 + 一次性） | 数据库保证 |

L2 是关键：**在循环里"决定要执行"和"执行"之间插了一个 return**。少了人工确认这一步，`_drive()` 根本走不到 `registry.invoke`；就算有人绕过前端直接调 `/api/tools/create_handover`，也会因为没有票被 L3 拦下（41201）。

### 4.2 中断时的用户体验

`draft` 事件逐行推，前端看起来就是"草稿一行一行被写出来"：

```
【待确认 · 交接单草稿】
需求项：CT-01　茶歇 上午/下午茶歇各 1 场，200 人份，含咖啡机 2 台
当前状态：已定版　原责任人：唐映秋
接收人：傅景
截止时间：2026-11-20
交接后：状态 → 已交接，责任人 → 傅景
校验结论：通过（三要素齐 / 已定版 / 有责任人）
票据：tk_7d973e53（5 分钟内有效，确认后一次性作废）
确认即落库并写 item_changes；确认前不产生任何业务数据。
        [ 确认执行 ]  [ 取消 ]
```

### 4.3 四种边角情况的处理

| 情况 | 行为 | 实测 |
|---|---|---|
| 确认超时（>5 分钟） | 票据过期 → 41202 → 结论提示"重新发起"，**不重试** | ✅ 冒烟【6】 |
| 用户点取消 | run=cancelled，零业务写入，票据自然失效 | ✅ 冒烟【5】 |
| 重复确认 | 40902，不重复建单 | ✅ 冒烟【2】 |
| 别人的 run | 40301 + 403 页 | ✅ 冒烟【8】 |
| 确认动作本身 | 写 `audit_logs(action='agent_confirm:accept|reject')`，可查"谁放行了这笔交接" | ✅ |

---

## 5. 三条典型轨迹（真实日志，非示意）

### 轨迹 A：只查清单，不碰写工具

> 你说：**"出差上海3天住宿标准是多少"**（只查制度，不调工具 2-4）
> 本项目：**"双语主持相关的需求有哪些？"**

| 轮 | 决策 | 动作 | 耗时 | 结果 |
|---|---|---|---|---|
| 1 | `search_items`（"先检索，检索不到就说未覆盖，不凑数"） | 混合检索 top3 | 191ms | 命中 3 条：LS-02、EQ-02、EQ-05 |
| 2 | 收束（final） | — | — | 列出 3 条：编号/内容/状态/责任人/来源位置 |

```
请执行状态: done ｜ rounds=2 ｜ total=705ms ｜ act 工具数=1（search_items）
结论节选：共命中 3 条：
· LS-02｜主持人双语支持 主持人临时改为中英双语主持（E-01 场景…）｜状态：未确认｜…
```

**关键点**：只调 1 个工具就收束。写工具（`create_handover` / `upsert_item`）一次都没出现 —— 不是"模型很乖"，是 `rule_decide` 在 `query` 意图下**压根没有通往写工具的路径**。

### 轨迹 B：查 → 验 → 中断 → 确认 → 落库

> 你说：**"帮我报上个月去北京的差旅"**（查制度→查额度→要发票→校验→建草稿→确认）
> 本项目：**"把 CT-01 交接给傅景，截止 11-20"**

| 轮 | 决策 | 动作 | 耗时 | 结果摘要 |
|---|---|---|---|---|
| 1 | `get_item`（先看清现状） | 读 LS-02/CT-01 全字段 + 变更历史 | 92ms | `item_no=CT-01，status=已定版` |
| 2 | `validate_handover`（没票不许建） | 三要素 + 定版 + 责任人校验 | 194ms | `pass=True`，发票 `tk_7d973e53` |
| 3 | `create_handover` | **中断**（draft×9 帧 → interrupt） | — | 等人工确认，未落库 |
| 3 | （人工点"确认执行"） | `create_handover` | 341ms | `status=已交接，to=傅景` |
| 4 | 收束（final） | — | — | 已建立交接：唐映秋 → 傅景，票据已作废 |

```
run_7abd869238 ｜ status=done ｜ rounds=4 ｜ total_ms=1785
（total 含人工确认的等待时间；工具实际耗时合计 627ms —— 这两个数在阶段 5 要分开看）
```

**两个硬断言**（冒烟里写成断言，每次回归都验）：

- 中断发生的那一刻，`handovers` 行数 **不变**、需求项状态 **仍为已定版**；
- 确认之后才 `+1`，且 `item_changes` 多出两条（status / owner）。

### 轨迹 C：校验 → 给结论（不写任何东西）

> 你说：**"这个发票能报吗"**（校验发票 → 给结论）
> 本项目：**"LS-02 交接给傅景，截止 11-05，能过吗？"**

| 轮 | 决策 | 动作 | 耗时 | 结果 |
|---|---|---|---|---|
| 1 | `validate_handover`（"只校验不建单，结论来自服务端规则而非我的判断"） | 校验 | 188ms | `pass=False`，`status=未确认` |
| 2 | 收束（final） | — | — | LS-02 现在不能交接：状态为[未确认]，未定版不得交接（f24 双条件）。下一步：请协调员在需求项库把状态推进到[已定版]后再交接。 |

**关键点**：同样是「交接」两个字，这句话被判成 `precheck` 而不是 `handover` —— 判据是**有没有祈使标志词（把/帮我/给我/请）**。

这个判断的代价不对称：误判成 handover，会在用户只想问问的时候发起写操作；误判成 precheck，最多是多问一句。所以拿不准就往"只看不动"那边靠。

**附：第 4 条轨迹（多源冲突，只报不裁）**

> "EQ-05 的翻译间到底几间？" → `diff_sources`（191ms）→ final：
> "EQ-05 三源存在 1 处不一致：content：台账=翻译间 1 间；服务台=翻译间 2 间…
> 按 f33 不做清洗、不替人裁定 —— 请责任人或质量验收确认哪一版为准。"
> `judgement` 字段恒为 `none`：Agent 不说谁对。

---

## 6. 验收（冒烟 58 项 + 回归 138 项）

`agent_trace_smoke.py`，独立库 `demo_trace.db`，可反复跑：

| 组 | 验的是什么 | 项数 |
|---|---|---|
| 【1】轨迹 A | 意图/只调 search_items/无写工具/日志三类齐/耗时与摘要非空 | 10 |
| 【2】轨迹 B | 槽位抽取、draft 帧、以 interrupt 结束、**确认前零落库**、确认后落库、重复确认 40902 | 17 |
| 【3】轨迹 C | 只调 diff_sources、冲突非空、judgement=none、结论不裁定 | 6 |
| 【4】只校验 | 意图=precheck、无 draft、给原因、零写入 | 5 |
| 【5】取消 | cancelled、零落库、状态未变、审计有 reject | 4 |
| 【6】票据过期 | 确认不炸、提示过期、零落库、日志记 41202 | 4 |
| 【7】轮次上限 | 强制决策器永远选工具 → 第 5 轮收束、act 恰好 5 次 | 3 |
| 【8】越权 | 他人 run 确认 → 40301 | 1 |
| 【9】阶段 5 统计 | 聚合/错误码/状态分布/平均轮次 | 8 |

累计：**20 + 22 + 30 + 28 + 38 + 58 = 196 项断言全过**。

---

## 7. 从 A 档到 B 档（接模型只换一个函数）

```python
# agent/planner.py
def decide(ctx):
    if os.environ.get("MEETING_LLM_PROVIDER", "off").lower() == "on":
        try:
            action = llm_decide(ctx)      # ← 只换这一行
            ctx["decider"] = "llm"
            return action
        except Exception:
            ctx["decider"] = "rule"       # 模型挂了无感回落，循环不受影响
    return rule_decide(ctx)
```

`llm_decide()` 已经把 `registry.schemas()` 直接喂给模型（不手抄 schema，改工具不会漂移），系统提示词红线六条：

```
1. 不得跳过校验直接建交接；create_handover 必须带 validate_handover 刚签发的 ticket。
2. 不得代任何人签字确认，不得修改确认状态。
3. 不得替人裁定三源冲突，只报告差异。
4. 状态只能逐级前进，不得跳格。
5. 检索无命中就说"清单中未找到"，不得编造编号、责任人、状态。
6. 一次只调一个工具。
工作方式：查（search/get）→ 诊（check/diff）→ 验（validate）→ 做（write）。
```

**B 档上线后，第 5 节那三条轨迹应当一字不变** —— 如果变了（比如 A 轨迹出现了写工具），说明模型在跳步，L2 中断和 L3 票据会拦住它，`decider='llm'` 也能让你在阶段 5 把这类任务单独筛出来。

---

## 8. 文件清单

| 文件 | 作用 |
|---|---|
| `agent/planner.py` | 决策层：意图识别、槽位抽取、规则决策状态机、LLM 决策槽位、统一入口 |
| `agent/runtime.py` | 执行循环：`_drive()`（生成器）、日志写入、中断与恢复、`MAX_ROUNDS=5` |
| `agent/api.py` | `POST /api/agent/chat`（SSE）、`POST /api/agent/confirm`、轨迹与阶段 5 统计接口、`/admin/agent` |
| `static/agent.html` | 员工对话页：SSE 打字机、草稿卡、确认/取消、右侧实时轨迹 |
| `templates/agent_admin.html` | 管理端：按工具聚合 + 任务列表 + 轨迹回放 + 三个示例一键跑 |
| `agent_trace_smoke.py` | 58 项断言（三条轨迹 + 中断 + 过期 + 上限 + 越权 + 统计） |
| `portal.py` | 新增 `agent_runs` / `agent_steps` 两张表；挂载 agent 路由 |
