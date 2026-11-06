# -*- coding: utf-8 -*-
"""
agent/runtime.py —— 执行循环（Harness 骨架）

    ┌──────────────────────────────────────────────────────────┐
    │  1 决策 decide(ctx)  →  {"type":"tool"} / {"type":"final"} │
    │  2 执行 registry.invoke(tool, args)                        │
    │  3 回填 obs.append({ok, result, code, duration_ms})        │
    │  4 再决策 …… 最多 MAX_ROUNDS=5 轮                          │
    └──────────────────────────────────────────────────────────┘

三条硬约束（都不依赖模型是否听话）：
  A. 轮次上限：第 6 轮直接停，状态记 max_rounds，不无限自转。
  B. 写操作中断：create_handover 走到「要执行」这一步时**不执行**，先把草稿
     逐行流给员工看，run 置 awaiting_confirm；员工确认后才真正落库（S2）。
  C. 全量留痕：每轮决策与每次工具执行都写 agent_steps（工具名/参数/耗时/摘要），
     供阶段 5 分析「哪一步最慢、哪种错最多、模型有没有跳步」。

为什么用 Harness 这种"薄循环"而不是 LangGraph/AutoGen：
  · 这个场景的动作空间只有 10 个工具、最大 5 轮，不需要图编排、不需要多智能体；
  · 顺序约束已经由票据在服务端保证，框架再叠一层"边和节点"只会让约束两处漂移；
  · 薄循环最容易被审计：一轮决策 = 一行日志，阶段 5 可以直接 SQL 分析；
  · 换模型只改 planner.decide，换框架要重写全部节点定义 —— 迁移成本不对等。
"""

import contextlib
import json
import os
import time
import uuid
from datetime import datetime

from fastapi import HTTPException

from . import planner

MAX_ROUNDS = 5                                  # 决策轮次上限
INTERRUPT_TOOLS = ("create_handover",)          # 必须人工确认才能执行的写工具
DIGEST_LIMIT = 240                              # 结果摘要截断长度（别把整张表塞进日志）


def _p():
    import portal
    return portal


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextlib.contextmanager
def _conn():
    """独立连接 + 结束时提交并关闭（循环里频繁开合，不能像请求级那样靠 GC）。"""
    conn = _p().db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ============================================================
# 日志：agent_runs（一次任务）+ agent_steps（每一轮）
# ============================================================
def _new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:10]


def _create_run(conn, run_id, user, goal, intent) -> None:
    conn.execute(
        "INSERT INTO agent_runs(id,user_id,goal,intent,decider,status,rounds,started_ts,"
        "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (run_id, user["id"], goal, intent, "rule", "running", 0, time.time(), _now()))


def _log_step(conn, run_id, round_no, phase, tool=None, args=None, ok=None, code=None,
              digest="", duration_ms=0):
    """
    一行日志 = 一轮里的一件事。
      phase=decide    选了哪个工具、为什么（args 为空，决策阶段还没有参数）
      phase=act       真正执行：工具名、入参、耗时、结果摘要、是否成功、错误码
      phase=interrupt 中断等确认（不执行）
      phase=final     给出结论
    阶段 5 直接 select * from agent_steps where run_id=? order by id 就能还原轨迹。
    """
    conn.execute(
        "INSERT INTO agent_steps(run_id,round_no,phase,tool,args_json,ok,code,digest,"
        "duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, round_no, phase, tool,
         json.dumps(args, ensure_ascii=False)[:800] if args is not None else None,
         None if ok is None else (1 if ok else 0), code, digest[:DIGEST_LIMIT],
         int(duration_ms), _now()))


def _digest(result) -> str:
    """
    结果摘要：只留"能判断成败和规模"的信息，不整段落库。
    长列表压成「N 条 + 前两条编号」，字典只留关键键。
    """
    if result is None:
        return ""
    if isinstance(result, dict):
        if "items" in result:
            items = result.get("items") or []
            return f"命中 {len(items)} 条：" + "、".join(
                str(i.get("item_no")) for i in items[:3])
        if "conflicts" in result:
            return (f"冲突 {len(result['conflicts'])} 处：" +
                    "、".join(str(c.get("field")) for c in result["conflicts"]))
        keys = ("item_no", "status", "pass", "ok", "created", "to", "changed")
        return "，".join(f"{k}={result[k]}" for k in keys if k in result)[:DIGEST_LIMIT]
    return str(result)[:DIGEST_LIMIT]


def _save_ctx(conn, run_id, ctx, rounds):
    conn.execute("UPDATE agent_runs SET context_json=?, rounds=?, decider=? WHERE id=?",
                 (json.dumps(ctx, ensure_ascii=False), rounds, ctx.get("decider"), run_id))


# ============================================================
# 中断卡片：给员工看的"草稿内容"（逐行流式发送）
# ============================================================
def draft_lines(tool: str, args: dict, ctx: dict) -> list:
    """
    把待执行的写操作渲染成人话，逐行返回 —— API 层一行一帧推给前端，
    视觉上就是"草稿正在被写出来"，员工看清后才点确认。
    """
    if tool != "create_handover":
        return [f"【待确认】{tool}", json.dumps(args, ensure_ascii=False, indent=1)]

    item_no = args.get("item_no")
    prev = next((o["result"] for o in ctx.get("obs", [])
                 if o["tool"] == "get_item" and o["ok"]), {}) or {}
    val = next((o["result"] for o in ctx.get("obs", [])
                if o["tool"] == "validate_handover" and o["ok"]), {}) or {}
    return [
        "【待确认 · 交接单草稿】",
        f"需求项：{item_no}　{str(prev.get('content') or '')[:40]}",
        f"当前状态：{prev.get('status') or '—'}　原责任人：{prev.get('owner') or '（空）'}",
        f"接收人：{args.get('receiver')}",
        f"截止时间：{args.get('deadline')}",
        f"交接后：状态 → 已交接，责任人 → {args.get('receiver')}",
        f"校验结论：{'通过（三要素齐 / 已定版 / 有责任人）' if val.get('pass') else '未通过'}",
        f"票据：{args.get('ticket')}（5 分钟内有效，确认后一次性作废）",
        "确认即落库并写 item_changes；确认前不产生任何业务数据。",
    ]


# ============================================================
# 执行一个工具：异常统一转成"观察"，循环才不会因为一次报错整体崩掉
# ============================================================
def _exec_tool(ctx, tool, args) -> tuple:
    """
    返回 (observation, duration_ms)。observation 结构固定：
      {tool, ok, code, message, result}
    HTTPException（工具主动拒绝）也走这里 —— 拒绝是正常的观察结果，
    比如 41202 票据过期，Agent 该做的是"如实告诉人"，而不是崩掉或重试。
    """
    from tools import registry
    t0 = time.perf_counter()
    try:
        result = registry.invoke(tool, args, ctx["user"])
        obs = {"tool": tool, "ok": True, "code": None, "message": "", "result": result}
    except HTTPException as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {}
        obs = {"tool": tool, "ok": False, "code": d.get("code") or exc.status_code,
               "message": d.get("message") or str(exc.detail), "result": None}
    except Exception as exc:                       # 兜底：任何 bug 都不该炸掉整轮对话
        obs = {"tool": tool, "ok": False, "code": 50001,
               "message": f"{type(exc).__name__}: {exc}"[:200], "result": None}
    return obs, int((time.perf_counter() - t0) * 1000)


# ============================================================
# 主循环
# ============================================================
def _drive(run_id, ctx, round_no=1, pending=None):
    """
    生成器：产出事件，同时把每轮的决策与执行写进 agent_steps。
    pending 不为空表示"中断恢复"—— 那个工具已经在上一段里决策过、拿到了人工确认，
    直接执行，不再重复决策（否则会重新要票，票据已被消耗一半）。
    """
    total_rounds = ctx.get("_rounds", 0)

    while round_no <= MAX_ROUNDS:
        # ── 1. 决策 ──────────────────────────────────────
        from_pending = False
        if pending:
            # 中断恢复：这个工具在上一段已决策过、也拿到了人工确认，直接执行
            action, pending, from_pending = pending, None, True
        else:
            action = planner.decide(ctx)
            with _conn() as conn:
                _log_step(conn, run_id, round_no, "decide",
                          tool=action.get("tool"), ok=True,
                          digest=action.get("why") or action.get("type"))
            yield {"type": "decide", "round": round_no,
                   "decider": ctx.get("decider"), "action": {
                       "type": action["type"], "tool": action.get("tool"),
                       "why": action.get("why") or ""}}

        # ── 2. 结论 ──────────────────────────────────────
        if action["type"] == "final":
            answer = action["answer"]
            with _conn() as conn:
                _log_step(conn, run_id, round_no, "final", ok=True, digest=answer)
                conn.execute(
                    "UPDATE agent_runs SET status=?, final_answer=?, rounds=?, decider=?,"
                    " total_ms=?, ended_at=? WHERE id=?",
                    ("done", answer, round_no, ctx.get("decider"),
                     int((time.time() - ctx["_started"]) * 1000), _now(), run_id))
            yield {"type": "final", "answer": answer}
            yield {"type": "done", "run_id": run_id, "status": "done", "rounds": round_no}
            return

        tool, args = action["tool"], action.get("args") or {}

        # ── 3. 写操作中断：先给人看，不落库 ────────────────
        # from_pending=True 表示这一步已经被人确认过，不能再中断一次（否则永远执行不到）
        if tool in INTERRUPT_TOOLS and not from_pending:
            lines = draft_lines(tool, args, ctx)
            with _conn() as conn:
                _log_step(conn, run_id, round_no, "interrupt", tool=tool, args=args,
                          ok=None, digest="等待人工确认：" + lines[2] if len(lines) > 2 else "")
                ctx["_rounds"] = round_no
                _save_ctx(conn, run_id, ctx, round_no)
                conn.execute(
                    "UPDATE agent_runs SET status=?, pending_action=? WHERE id=?",
                    ("awaiting_confirm",
                     json.dumps({"tool": tool, "args": args, "round": round_no,
                                 "run_id": run_id}, ensure_ascii=False), run_id))
            for line in lines:                       # 逐行流给员工（前端打字机效果）
                yield {"type": "draft", "delta": line + "\n"}
            yield {"type": "interrupt", "run_id": run_id, "round": round_no,
                   "tool": tool, "args": args,
                   "hint": "确认后才会真正执行；本次待执行 1 个写操作"}
            return

        # ── 4. 执行 + 回填 ───────────────────────────────
        yield {"type": "tool", "round": round_no, "tool": tool, "args": args}
        obs, ms = _exec_tool(ctx, tool, args)
        obs["round"] = round_no
        ctx.setdefault("obs", []).append(obs)
        with _conn() as conn:
            _log_step(conn, run_id, round_no, "act", tool=tool, args=args,
                      ok=obs["ok"], code=obs["code"], digest=_digest(obs["result"]),
                      duration_ms=ms)
            ctx["_rounds"] = round_no
            _save_ctx(conn, run_id, ctx, round_no)
        yield {"type": "observe", "round": round_no, "tool": tool, "ok": obs["ok"],
               "code": obs["code"], "message": obs["message"],
               "digest": _digest(obs["result"]), "duration_ms": ms,
               "result": None if not obs["ok"] else obs["result"]}

        round_no += 1

    # ── 5. 轮次用尽 ──────────────────────────────────────
    with _conn() as conn:
        _log_step(conn, run_id, MAX_ROUNDS, "final", ok=False,
                  digest=f"达到 {MAX_ROUNDS} 轮上限，强制收束")
        conn.execute(
            "UPDATE agent_runs SET status=?, final_answer=?, total_ms=?, ended_at=? WHERE id=?",
            ("max_rounds", f"已达到 {MAX_ROUNDS} 轮上限，我先停下。请把任务拆小一点，"
                           f"或者告诉我更明确的编号/接收人/截止时间。",
             int((time.time() - ctx["_started"]) * 1000), _now(), run_id))
    yield {"type": "final",
           "answer": f"已达到 {MAX_ROUNDS} 轮上限，我先停下。请把任务拆小一点，"
                     f"或补充更明确的编号 / 接收人 / 截止时间。"}
    yield {"type": "done", "run_id": run_id, "status": "max_rounds", "rounds": MAX_ROUNDS}


# ============================================================
# 对外入口
# ============================================================
def run_stream(user, goal, run_id=None):
    """新任务：建 run → 决策循环。SSE 端点直接迭代这个生成器。"""
    p = _p()
    run_id = run_id or _new_run_id()
    intent = planner.detect_intent(goal)
    with _conn() as conn:
        _create_run(conn, run_id, user, goal, intent)
        names = [r["display_name"] for r in
                 conn.execute("SELECT display_name FROM users WHERE is_active=1")]
    ctx = {"goal": goal, "user": user, "intent": intent,
           "slots": planner.extract_slots(goal, names),
           "obs": [], "_started": time.time(), "_rounds": 0}

    yield {"type": "run", "run_id": run_id, "intent": intent, "slots": ctx["slots"],
           "decider": "llm" if os.environ.get(
               "MEETING_LLM_PROVIDER", "off").lower() == "on" else "rule"}
    yield from _drive(run_id, ctx, 1)
    return run_id


def resume(user, run_id: str, accept: bool, note: str = "") -> dict:
    """
    员工对中断卡片作出决定后继续。
      accept=True  → 执行待执行的写工具，然后让循环继续决策（通常会收束成结论）
      accept=False → 放弃，run 置 cancelled，**不产生任何业务数据**
    返回 {"status":..., "answer":..., "events":[...]}，供前端一次性渲染。
    """
    p = _p()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        p.fail(404, 40401, message="任务不存在")
    if row["user_id"] != user["id"]:
        p.fail(403, 40301)
    if row["status"] != "awaiting_confirm":
        p.fail(409, 40902, message=f"当前状态[{row['status']}]不可确认，可能已处理过")

    pending = json.loads(row["pending_action"] or "{}")
    ctx = json.loads(row["context_json"] or "{}")
    ctx["user"] = user                      # user 对象不入库（含密码哈希），恢复时重新注入
    ctx["_started"] = row["started_ts"] or time.time()

    if not accept:
        with _conn() as conn:
            _log_step(conn, run_id, pending.get("round") or 1, "interrupt",
                      tool=pending.get("tool"), args=pending.get("args"), ok=False,
                      digest=f"人工取消{('：' + note) if note else ''}")
            conn.execute(
                "UPDATE agent_runs SET status=?, final_answer=?, total_ms=?, ended_at=? "
                "WHERE id=?", ("cancelled",
                               "已取消，没有产生任何业务数据（票据会在 5 分钟内自然失效）。",
                               int((time.time() - ctx["_started"]) * 1000), _now(), run_id))
        return {"status": "cancelled",
                "answer": "已取消，没有产生任何业务数据（票据会在 5 分钟内自然失效）。",
                "events": []}

    # 确认：把中断的那个工具作为本轮动作直接执行，之后回到正常决策循环
    events = list(_drive(run_id, ctx, pending.get("round") or 1,
                         pending={"type": "tool", "tool": pending["tool"],
                                  "args": pending["args"],
                                  "why": "人工确认后执行"}))
    final = next((e for e in events if e["type"] == "final"), {"answer": ""})
    with _conn() as conn:
        st = conn.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    return {"status": st["status"] if st else "done", "answer": final["answer"],
            "events": events}


def get_run(run_id: str) -> dict:
    """取任务详情 + 全轨迹（审计/前端重放都用它）。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT r.*, u.display_name, u.role FROM agent_runs r "
            "JOIN users u ON u.id=r.user_id WHERE r.id=?", (run_id,)).fetchone()
        if row is None:
            _p().fail(404, 40401, message="任务不存在")
        steps = [dict(s) for s in conn.execute(
            "SELECT round_no,phase,tool,args_json,ok,code,digest,duration_ms,created_at "
            "FROM agent_steps WHERE run_id=? ORDER BY id", (run_id,))]
    d = dict(row)
    d.pop("context_json", None)              # 上下文内部用，不对外
    d["steps"] = steps
    return d
