# -*- coding: utf-8 -*-
"""
agent/api.py —— 编排层的 HTTP 入口

  POST /api/agent/chat              SSE 流：一轮对话（可能以 interrupt 结束）
  POST /api/agent/confirm           人工确认 / 取消，确认后才执行写工具
  GET  /api/agent/runs/{id}         任务详情 + 全轨迹（前端重放、审计核对）
  GET  /api/agent/runs              我自己的最近任务
  GET  /api/admin/agent/runs        管理端：按人/状态/日期筛任务
  GET  /api/admin/agent/stats       管理端：按工具聚合（阶段 5 分析的数据源）
  GET  /admin/agent                 管理台页面（轨迹回放 + 统计）
  GET  /agent                       员工对话页（static/agent.html）

SSE 帧序（正常路径）：
  run → decide → tool → observe → decide → … → final → done
中断路径：
  run → decide → tool → observe → decide → draft×N → interrupt   （停在这里等确认）
确认后：
  POST /api/agent/confirm → JSON（含后续 events，前端按同样规则渲染）
"""

import asyncio
import json
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import runtime

router = APIRouter()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

MAX_GOAL_LEN = 300


def _p():
    import portal
    return portal


def _require_api(*roles):
    """与 tools/api.py 同规则：HTTPException 原样放行，别把 40103 吞成 40101。"""
    def dep(request: Request):
        p = _p()
        from fastapi import HTTPException
        try:
            user = p.current_user(request)
        except HTTPException:
            raise
        except Exception:
            p.fail(401, 40101)
        if roles and user["role"] not in roles:
            p.fail(403, 40302)
        return user
    return dep


def _require_page(*roles):
    import admin

    def dep(request: Request):
        p = _p()
        try:
            user = p.current_user(request)
        except Exception:
            raise admin._Forbidden(login=True)
        if roles and user["role"] not in roles:
            raise admin._Forbidden(user=user)
        return user
    return dep


def _sse(event: str, data) -> str:
    """SSE 帧：event: <类型>\ndata: <JSON>\n\n（前端按空行切帧）"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ============================================================
# 对话（流式）
# ============================================================
@router.post("/api/agent/chat")
async def agent_chat(request: Request, user=Depends(_require_api())):
    p = _p()
    try:
        body = await request.json()
    except Exception:
        body = {}
    goal = (body.get("goal") or "").strip()
    if not goal:
        p.fail(400, 40001, message="请先说明你要做什么，例如『把 LS-02 交接给傅景，截止 11-05』")
    if len(goal) > MAX_GOAL_LEN:
        p.fail(400, 40002, message=f"任务描述过长（上限 {MAX_GOAL_LEN} 字）")

    async def gen():
        try:
            for ev in runtime.run_stream(user, goal):
                # 草稿逐行推：前端看到的是"一行一行写出来"，不是一次性刷出
                yield _sse(ev["type"], ev)
                if ev["type"] == "draft":
                    await asyncio.sleep(0.012)
                else:
                    await asyncio.sleep(0)      # 主动让出，保证多帧顺序不乱
        except Exception as exc:                # 循环内部异常也要以 SSE 帧告知，不能静默断流
            yield _sse("error", {"code": 50001,
                                 "message": f"编排异常：{type(exc).__name__}: {exc}"[:200]})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/agent/confirm")
async def agent_confirm(request: Request, user=Depends(_require_api())):
    """
    人工确认入口。这是「写操作必须人点头」的唯一放行开关：
      · 不传 accept=true 就永远执行不到 create_handover；
      · 确认动作本身也写日志（谁确认的、什么时候），阶段 5 可查"谁放行了这笔交接"。
    """
    p = _p()
    try:
        body = await request.json()
    except Exception:
        body = {}
    run_id = (body.get("run_id") or "").strip()
    if not run_id:
        p.fail(400, 40001, message="缺少 run_id")
    accept = bool(body.get("accept"))
    note = (body.get("note") or "").strip()[:200]
    out = runtime.resume(user, run_id, accept, note)
    with p.db() as conn:
        conn.execute(
            "INSERT INTO audit_logs(actor_id,action,target_type,target_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (user["id"], "agent_confirm:" + ("accept" if accept else "reject"),
             "agent_run", run_id, p.now()))
        conn.commit()
    return {"ok": True, **out}


# ============================================================
# 轨迹查询
# ============================================================
@router.get("/api/agent/runs/{run_id}")
async def api_run(run_id: str, user=Depends(_require_api())):
    p = _p()
    d = runtime.get_run(run_id)
    if d["user_id"] != user["id"] and user["role"] not in ("admin", "qa"):
        p.fail(403, 40301)
    return d


@router.get("/api/agent/runs")
async def api_my_runs(user=Depends(_require_api())):
    with _p().db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,goal,intent,status,rounds,total_ms,created_at FROM agent_runs "
            "WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (user["id"],))]
    return {"items": rows}


@router.get("/api/admin/agent/runs")
async def api_admin_runs(user_id: str = "", status: str = "", dfrom: str = "",
                         page: int = 1, size: int = 20,
                         user=Depends(_require_api("admin", "qa", "coordinator"))):
    p = _p()
    where, args = ["1=1"], []
    if user_id:
        where.append("r.user_id=?")
        args.append(int(user_id))
    if status:
        where.append("r.status=?")
        args.append(status)
    if dfrom:
        where.append("substr(r.created_at,1,10)>=?")
        args.append(dfrom)
    w = " AND ".join(where)
    with p.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM agent_runs r WHERE {w}",
                             args).fetchone()["c"]
        rows = [dict(r) for r in conn.execute(
            f"SELECT r.id,r.user_id,u.display_name,r.goal,r.intent,r.decider,r.status,"
            f"r.rounds,r.total_ms,r.created_at FROM agent_runs r "
            f"JOIN users u ON u.id=r.user_id WHERE {w} "
            f"ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
            args + [min(size, 100), (max(page, 1) - 1) * size])]
    return {"total": total, "items": rows}


@router.get("/api/admin/agent/stats")
async def api_stats(days: int = 7, user=Depends(_require_api("admin", "qa"))):
    """
    阶段 5 分析入口：按工具聚合的次数/成功率/平均耗时/最大耗时 + 错误码分布 + 中断转化率。
    这些数直接来自 agent_steps，不用再埋点 —— 循环在跑的时候就已经在记账了。
    """
    p = _p()
    with p.db() as conn:
        by_tool = [dict(r) for r in conn.execute(
            "SELECT s.tool, COUNT(*) calls,"
            " SUM(CASE WHEN s.ok=1 THEN 1 ELSE 0 END) ok_calls,"
            " ROUND(AVG(s.duration_ms),1) avg_ms, MAX(s.duration_ms) max_ms"
            " FROM agent_steps s JOIN agent_runs r ON r.id=s.run_id"
            " WHERE s.phase='act' AND s.created_at >= datetime('now', ?)"
            " GROUP BY s.tool ORDER BY calls DESC", (f"-{int(days)} day",))]
        codes = [dict(r) for r in conn.execute(
            "SELECT s.tool, s.code, COUNT(*) n FROM agent_steps s"
            " JOIN agent_runs r ON r.id=s.run_id"
            " WHERE s.phase='act' AND s.ok=0 AND s.created_at >= datetime('now', ?)"
            " GROUP BY s.tool, s.code ORDER BY n DESC LIMIT 20", (f"-{int(days)} day",))]
        status_mix = [dict(r) for r in conn.execute(
            "SELECT status, COUNT(*) n FROM agent_runs"
            " WHERE created_at >= datetime('now', ?) GROUP BY status",
            (f"-{int(days)} day",))]
        rounds = conn.execute(
            "SELECT ROUND(AVG(rounds),2) a, MAX(rounds) m, COUNT(*) n FROM agent_runs"
            " WHERE created_at >= datetime('now', ?)", (f"-{int(days)} day",)).fetchone()
    return {"days": days, "by_tool": by_tool, "error_codes": codes,
            "status_mix": status_mix,
            "rounds": {"avg": rounds["a"], "max": rounds["m"], "runs": rounds["n"]}}


# ============================================================
# 页面
# ============================================================
@router.get("/agent")
async def agent_page(user=Depends(_require_page())):
    """员工对话页（静态单页，走 /api/agent/* 的 JWT 接口）。"""
    return RedirectResponse("/static/agent.html")


@router.get("/admin/agent", response_class=HTMLResponse)
async def admin_agent(request: Request, run_id: str = "", status: str = "",
                      user=Depends(_require_page("admin", "qa", "coordinator"))):
    import admin
    p = _p()
    with p.db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id,username,display_name,role FROM users ORDER BY id")]
        where, args = ["1=1"], []
        if status:
            where.append("r.status=?")
            args.append(status)
        rows = [dict(r) for r in conn.execute(
            f"SELECT r.id,r.goal,r.intent,r.decider,r.status,r.rounds,r.total_ms,r.created_at,"
            f"u.display_name FROM agent_runs r JOIN users u ON u.id=r.user_id "
            f"WHERE {' AND '.join(where)} ORDER BY r.created_at DESC LIMIT 30", args)]
        detail, steps = None, []
        if run_id:
            d = runtime.get_run(run_id)
            detail, steps = d, d["steps"]
        stats = [dict(r) for r in conn.execute(
            "SELECT tool, COUNT(*) calls, SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) ok_calls,"
            " ROUND(AVG(duration_ms),1) avg_ms, MAX(duration_ms) max_ms FROM agent_steps"
            " WHERE phase='act' GROUP BY tool ORDER BY calls DESC")]
    return templates.TemplateResponse(request, "agent_admin.html", admin.page_ctx(
        request, user, "agent", runs=rows, users=users, stats=stats,
        detail=detail, steps=steps, run_id=run_id, status=status,
        status_cn={"running": "执行中", "awaiting_confirm": "待确认", "done": "已完成",
                   "cancelled": "已取消", "failed": "失败", "max_rounds": "轮次用尽"}))
