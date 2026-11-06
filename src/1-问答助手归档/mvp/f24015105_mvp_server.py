# -*- coding: utf-8 -*-
"""
f24015105_mvp_server.py —— 员工问答入口 · MVP（f24015105）

范围（只做提示词里要的最小闭环，鉴权/权限/审计等在正式版 app/portal.py，不在此重复）：
  1. POST /api/chat          接收 {session_id, question}，返回 SSE 流式响应
  2. 会话与消息存 SQLite（sessions / messages 两张表，最小结构）
  3. 每次请求携带最近 6 轮历史做查询扩展（短问题补类别词，省略式追问才能命中）
  4. 前端见 f24015105_index.html（打字机 + "正在检索需求项…" 状态 + 引用来源）
  5. 回答底部展示引用来源（来源工具｜单据号｜行位置 + 片段）

约束：
  · 所有接口错误先于建流返回，统一 {"code": int, "message": 中文}
  · 检索复用阶段 1 的 HybridRetriever（BM25+TF-IDF 融合，零 LLM，无外部依赖）

启动：python f24015105_mvp_server.py   →  http://127.0.0.1:8020/static 路径下的页面见 HTML 文件
"""

import json
import os
import sqlite3
import sys
import time
import uuid

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

# ── 复用阶段 1 解析器与检索器（仓库内 app/ 目录） ─────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))          # .../meeting-service-agent/mvp
APP_DIR = os.path.join(HERE, "..", "app")
sys.path.insert(0, os.path.abspath(APP_DIR))
from stage1_bridge import parse_requirements, HybridRetriever  # noqa: E402

# ── 常量 ──────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("MVP_DB", os.path.join(HERE, "f24015105_mvp.db"))
DATA_MD = os.environ.get(
    "MVP_DATA",
    os.path.join(HERE, "..", "data", "demo_会议需求样例.md"))  # 18 条合成需求项
MAX_QUESTION_LEN = 200       # 单问最大长度（与正式版一致）
CONTEXT_MSGS = 12            # 最近 6 轮 = 12 条消息（问答各一条）
TOP_K = 3                    # 检索返回条数

# 错误码表：code → (http_status, 中文信息)
ERR = {
    40001: (400, "问题不能为空"),
    40002: (400, f"问题过长，最多 {MAX_QUESTION_LEN} 字"),
    40401: (404, "会话不存在"),
    50301: (503, "检索索引未就绪，请稍后再试"),
    50001: (500, "服务内部错误，请稍后再试"),
}

app = FastAPI(title="会议需求问答 MVP (f24015105)")
state = {"retriever": None}

# 前端单页（打字机 + 状态条 + 引用来源）
HTML_PATH = os.path.join(HERE, "f24015105_index.html")


@app.get("/")
def index():
    return FileResponse(HTML_PATH, media_type="text/html")


# ── 数据库（WAL，两表最小结构） ───────────────────────────────────────────
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,                -- user / assistant
    content TEXT NOT NULL,
    citations TEXT,                    -- assistant 消息的引用来源 JSON
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fail(http: int, code: int):
    """统一错误出口：先 JSON 返回错误，再建 SSE 流（契约：错误不进流）。"""
    return JSONResponse({"code": code, "message": ERR[code][1]}, status_code=http)


@app.on_event("startup")
def startup():
    """建表 + 加载样例数据 + 构建检索索引（启动一次，常驻内存）。"""
    conn = db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    with open(DATA_MD, encoding="utf-8") as f:
        items = parse_requirements(f.read())
    state["retriever"] = HybridRetriever(items)
    print(f"[f24015105 MVP] 已加载 {len(items)} 条需求项，索引就绪")


# ── 最近 6 轮历史 → 查询扩展 ──────────────────────────────────────────────
def expand_with_context(conn, session_id: str, question: str) -> str:
    """
    短问题（≤8 字）且历史里出现过类别词时，把类别词拼到问句后，
    让"那餐饮呢？"这类省略式追问也能命中。长问题自带语义，不拼防污染。
    """
    rows = conn.execute(
        "SELECT content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, CONTEXT_MSGS)).fetchall()
    cats = [c for c in ("设备", "语言服务", "餐饮", "场地", "其他")
            if any(c in r["content"] for r in rows)]
    if len(question) <= 8 and cats:
        return question + " " + " ".join(cats)
    return question


def rule_summary(hits) -> str:
    """零 LLM 汇总：只统计与点名，不推断（一期零幻觉约束）。"""
    if not hits:
        return "清单未覆盖该问题，可换个说法再问。"
    missing = [it for it, _ in hits if it.status == "缺失"]
    unconfirmed = [it for it, _ in hits if it.status == "未确认"]
    parts = [f"共命中 {len(hits)} 项"]
    if missing:
        parts.append(f"其中缺失 {len(missing)} 项（{'、'.join(i.item_no for i in missing)}）")
    if unconfirmed:
        parts.append(f"未确认 {len(unconfirmed)} 项（{'、'.join(i.item_no for i in unconfirmed)}）")
    focus = missing[0] if missing else (unconfirmed[0] if unconfirmed else hits[0][0])
    parts.append(f"最需盯的：{focus.item_no}（{focus.status}）")
    return "，".join(parts) + "。"


def sse(event: str, data: dict) -> str:
    """SSE 帧格式；中文不转义，前端直接可读。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── 接口 1：POST /api/chat（SSE 流式） ───────────────────────────────────
@app.post("/api/chat")
async def chat(body: dict):
    # ① 前置校验：全部在建流之前，失败返回普通 JSON
    q = (body.get("question") or "").strip()
    if not q:
        return fail(400, 40001)
    if len(q) > MAX_QUESTION_LEN:
        return fail(400, 40002)
    if state["retriever"] is None:
        return fail(503, 50301)

    conn = db()
    # ② 会话：传了 id 校验存在性；没传就新建（标题取问题前 20 字）
    sid = body.get("session_id")
    if sid:
        if conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is None:
            conn.close()
            return fail(404, 40401)
    else:
        sid = str(uuid.uuid4())
        conn.execute("INSERT INTO sessions(id,title,created_at) VALUES(?,?,?)",
                     (sid, q[:20], now()))
    # ③ 用户消息先落库——即使后面崩了，"问过什么"也留得下
    conn.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                 (sid, "user", q, now()))
    conn.commit()
    expanded = expand_with_context(conn, sid, q)

    # ④ SSE 事件序：status → item×N → chunk×M → done（异常 error）
    async def gen():
        t0 = time.time()
        try:
            yield sse("status", {"message": "正在检索需求项…"})
            hits = state["retriever"].search(expanded, TOP_K)
            for it, _score in hits:
                yield sse("item", {
                    "item_no": it.item_no, "category": it.category,
                    "status": it.status, "content": it.content,
                    "owner": it.owner or "—",
                    "source_tool": it.source_tool or "来源缺失",
                    "source_ref": it.source_ref or "",
                })
            text = rule_summary(hits)
            # 8 字一段推送，前端再逐字上屏形成打字机效果
            for i in range(0, len(text), 8):
                yield sse("chunk", {"text": text[i:i + 8]})
                time.sleep(0.02)
            # ⑤ 引用来源：来源工具｜单据号｜行位置 + 片段（可追溯到原始行）
            citations = [{"item_no": it.item_no, "source_tool": it.source_tool,
                          "source_ref": it.source_ref, "snippet": it.content[:40]}
                         for it, _ in hits]
            latency = int((time.time() - t0) * 1000)
            conn.execute(
                "INSERT INTO messages(session_id,role,content,citations,latency_ms,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (sid, "assistant", text, json.dumps(citations, ensure_ascii=False),
                 latency, now()))
            conn.commit()
            yield sse("done", {"session_id": sid, "elapsed_ms": latency,
                               "citations": citations})
        except Exception as e:  # 兜底：流内错误也以 error 事件告知前端
            yield sse("error", {"code": 50001, "message": f"{ERR[50001][1]}（{e}）"})
        finally:
            conn.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── 接口 2：会话列表 ─────────────────────────────────────────────────────
@app.get("/api/sessions")
def list_sessions():
    conn = db()
    rows = conn.execute(
        "SELECT s.id, s.title, s.created_at, COUNT(m.id) AS message_count "
        "FROM sessions s LEFT JOIN messages m ON m.session_id=s.id "
        "GROUP BY s.id ORDER BY s.created_at DESC").fetchall()
    conn.close()
    return {"sessions": [dict(r) for r in rows]}


# ── 接口 3：某会话的历史消息（含引用，回看可复现"当时的依据"） ────────────
@app.get("/api/sessions/{sid}/messages")
def list_messages(sid: str):
    conn = db()
    if conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is None:
        conn.close()
        return fail(404, 40401)
    rows = conn.execute(
        "SELECT id, role, content, citations, latency_ms, created_at "
        "FROM messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    conn.close()
    msgs = []
    for r in rows:
        d = dict(r)
        d["citations"] = json.loads(d.pop("citations") or "[]")
        msgs.append(d)
    return {"session_id": sid, "messages": msgs}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MVP_PORT", 8020)))
