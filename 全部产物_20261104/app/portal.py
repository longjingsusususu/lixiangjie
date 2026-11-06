# -*- coding: utf-8 -*-
"""
portal.py —— 会议活动需求 · 员工查询入口（FastAPI + SSE + 单页前端）

接口（详见《会议活动需求_员工查询入口_接口设计.md》）：
    POST /api/login                          登录，返回 JWT
    POST /api/chat                           查询（SSE 流式：status → item ×N → chunk ×M → done）
    GET  /api/sessions                       我的会话列表（侧栏）
    GET  /api/sessions/{sid}/messages        单会话历史消息（含引用）
    GET  /static/index.html                  单页前端

主线约束（会议项目）：
    · 检索对象是需求项，不是制度文档；
    · 回答形态是"缺项清单 + 来源位置"（f15）；
    · LLM 汇总默认 off（f01 客户红线），改环境变量 MEETING_LLM_PROVIDER=on 即启用；
    · 每次查询写 audit_logs（留痕"谁查过"，f44）。

运行：
    set PYTHONPATH=<venv site-packages>
    python -m uvicorn portal:app --port 8010
    → 打开 http://127.0.0.1:8010/
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from stage1_bridge import HybridRetriever, parse_requirements

# ============================================================
# 配置区
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MEETING_DB") or os.path.join(BASE_DIR, "demo.db")


def _find_demo_md() -> str:
    """定位样例需求清单：优先仓库 data/，其次应用目录、仓库根目录。"""
    import glob
    candidates = [
        os.path.join(BASE_DIR, "..", "data", "demo_会议需求样例.md"),
        os.path.join(BASE_DIR, "demo_会议需求样例.md"),
        os.path.join(BASE_DIR, "..", "demo_会议需求样例.md"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return os.path.abspath(p)
    hits = glob.glob(os.path.join(BASE_DIR, "..", "**", "demo_会议需求样例.md"), recursive=True)
    return os.path.abspath(hits[0]) if hits else candidates[0]


DEMO_MD = _find_demo_md()
STATIC_DIR = os.path.join(BASE_DIR, "static")

JWT_SECRET = os.environ.get("MEETING_JWT_SECRET", "meeting-demo-secret-change-me-32bytes")
JWT_ALG = "HS256"
TOKEN_HOURS = 8

# LLM 汇总开关：off = 纯规则汇总文案（一期默认，f01）；on = 走 OpenAI 兼容接口
LLM_PROVIDER = os.environ.get("MEETING_LLM_PROVIDER", "off").lower()
LLM_BASE_URL = os.environ.get("MEETING_LLM_BASE_URL", "https://api.example.com/v1")
LLM_API_KEY = os.environ.get("MEETING_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("MEETING_LLM_MODEL", "gpt-4o-mini")

TOP_K = 3                 # 混合检索返回条数
MAX_QUESTION_LEN = 200    # 问题长度上限
RATE_WINDOW = 10          # 限流窗口（秒）
RATE_MAX = 5              # 窗口内最大请求数
CONTEXT_TURNS = 6         # 最近 6 条消息作为上下文（3 轮问答）

# ============================================================
# 中文错误码表：所有接口统一"先返回 code + 中文 message"
# ============================================================
ERR = {
    40001: "问题不能为空",
    40002: f"问题过长（上限 {MAX_QUESTION_LEN} 字）",
    40101: "未登录或登录已过期，请重新登录",
    40102: "用户名或密码错误",
    40103: "账号已停用，请联系管理员",
    40301: "无权访问该会话",
    40302: "当前角色无权执行此操作",
    40401: "会话不存在",
    40901: "编号重复",
    41301: "文件超过 2MB 限制",
    41501: "只支持 .md 或 .csv 文件",
    42201: "文件解析失败",
    42901: "请求太频繁，请稍后再试",
    42902: "单次请求只允许一个写操作，请逐项处理",
    50001: "系统繁忙，请稍后重试",
    50301: "检索索引未就绪，请联系管理员导入需求项",
    # ── Agent 工具层（票据与顺序约束）──
    41201: "票据不存在：必须先调用 validate_handover 获取票据，不得直接建交接",
    41202: "票据已过期（有效期 5 分钟），请重新调用 validate_handover",
    41203: "票据已被使用（一次性），请重新校验",
    41204: "票据与本次参数不一致：item_no/接收人/截止时间必须与校验时完全一致",
    42211: "未确认来源单号是否已存在：请先调用 find_mapping",
    42212: "未定版不得交接（f24 双条件），请先由协调员完成定版",
    42213: "无责任人，不得默认通过（f44）",
    42214: "状态只能向前推进一格，不允许跳格或回退",
    40902: "当前状态不允许该操作",
    40404: "异常记录不存在",
    40405: "工具不存在：Agent 无权调用未注册能力",
}


def fail(status_code: int, code: int, **overrides):
    """统一抛错：HTTPException 的 detail 直接带 {code, message}，由下面的处理器展开。"""
    msg = overrides.get("message") or ERR.get(code, "未知错误")
    raise HTTPException(status_code=status_code, detail={"code": code, "message": msg})


# ============================================================
# 数据库：SQLite（阶段 1 的形态延续，无 ORM，够轻）
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff',
    is_active INTEGER NOT NULL DEFAULT 1,   -- 停用即时生效（登录与接口都拒绝）
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,                    -- user / assistant
    content TEXT NOT NULL,
    citation_source_refs TEXT,             -- JSON: [{item_no,source_tool,source_ref,snippet}]
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requirement_items (
    item_no TEXT PRIMARY KEY,
    category TEXT, content TEXT, status TEXT, owner TEXT,
    source_tool TEXT, source_ref TEXT,
    biz_occurred_at TEXT, received_at TEXT, change_note TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0   -- 软删：审计可回查，不提供硬删
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER NOT NULL,             -- ①谁查的（f44 留痕四项之一）
    action TEXT NOT NULL,                  -- view
    target_type TEXT NOT NULL,             -- item
    target_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- ── 管理后台增量（批 1 建表，批 3/4 使用）──
CREATE TABLE IF NOT EXISTS rule_configs (
    key TEXT PRIMARY KEY,                  -- synonyms / categories / gates / llm_prompt
    value_json TEXT NOT NULL,              -- 配置内容（改这里即时生效，不写死代码）
    updated_by INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS item_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    rule TEXT NOT NULL,                    -- R1 敏感字段 / R2 批量高频 / R3 越权尝试
    reason TEXT,
    flagged_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS item_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_no TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT, new_value TEXT,
    changed_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS id_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_tool TEXT NOT NULL,
    external_no TEXT NOT NULL,
    item_no TEXT NOT NULL,
    mapped_by INTEGER, created_at TEXT,
    UNIQUE(source_tool, external_no)
);
-- ── Agent 工具层（A 档）：票据 + 交接 + 异常队列 + 站内待办 + 三源快照 ──
CREATE TABLE IF NOT EXISTS handover_tickets (
    ticket TEXT PRIMARY KEY,               -- 一次性票据：顺序约束的物理载体
    item_no TEXT NOT NULL,
    params_hash TEXT NOT NULL,             -- item_no|receiver|deadline 的哈希，防"校验A建B"
    issued_to INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,           -- epoch 秒：只在服务端读，不接受前端传
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_no TEXT NOT NULL,
    from_owner TEXT, receiver TEXT NOT NULL,
    deadline TEXT NOT NULL,
    note TEXT, ticket TEXT NOT NULL,       -- 留票号：事后可核对"这次交接是哪张票放行的"
    created_by INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exception_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_no TEXT NOT NULL, reason TEXT NOT NULL, note TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- 没有"自动通过"这个值（f44）
    opened_by INTEGER NOT NULL, created_at TEXT NOT NULL,
    closed_by INTEGER, closed_at TEXT, evidence TEXT
);
CREATE TABLE IF NOT EXISTS confirm_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_no TEXT NOT NULL, owner TEXT NOT NULL, owner_id INTEGER,
    fields_json TEXT, message TEXT,
    status TEXT NOT NULL DEFAULT 'pending',-- pending / done（done 只能人签字触发）
    requested_by INTEGER NOT NULL, created_at TEXT NOT NULL, done_at TEXT
);
CREATE TABLE IF NOT EXISTS source_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_no TEXT NOT NULL, source_tool TEXT NOT NULL,
    field TEXT NOT NULL, value TEXT, raw_ref TEXT,
    snapshot_at TEXT,
    UNIQUE(item_no, source_tool, field)
);
-- ── Agent 编排层（Harness 循环）：一次任务 + 每轮一步 ──
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,                    -- run_xxxxxxxxxx
    user_id INTEGER NOT NULL,
    goal TEXT NOT NULL,                     -- 用户原话（可追溯"他到底让 Agent 干什么"）
    intent TEXT,                            -- handover / precheck / conflict / query
    decider TEXT,                           -- rule（A 档）/ llm（B 档）
    status TEXT NOT NULL,                   -- running/awaiting_confirm/done/cancelled/max_rounds
    rounds INTEGER NOT NULL DEFAULT 0,      -- 已用决策轮次（≤5）
    pending_action TEXT,                    -- JSON：中断时待执行的那个工具（含票据）
    context_json TEXT,                      -- 观察历史（恢复执行时重建上下文）
    final_answer TEXT,
    started_ts REAL,                        -- time.time()，用于算真实总耗时
    total_ms INTEGER,
    created_at TEXT NOT NULL, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,              -- 第几轮决策（1..5）
    phase TEXT NOT NULL,                    -- decide / act / interrupt / final
    tool TEXT, args_json TEXT,
    ok INTEGER, code INTEGER,               -- 成功标志与错误码（阶段 5 的错误分布）
    digest TEXT,                            -- 结果摘要（截断，不整段落库）
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_user ON agent_runs(user_id, created_at);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_pwd(pwd: str) -> str:
    """PBKDF2 加盐哈希（演示用固定盐；正式环境每用户独立盐 + 迭代参数入库）。"""
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), b"meeting-salt", 120_000).hex()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_columns(conn: sqlite3.Connection):
    """轻量迁移：老库缺列时补上（SQLite 的 ALTER TABLE ADD COLUMN 很便宜）。"""
    def cols(table):
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "is_active" not in cols("users"):
        conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "last_login_at" not in cols("users"):
        conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
    if "is_deleted" not in cols("requirement_items"):
        conn.execute("ALTER TABLE requirement_items ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")


def _seed_rules(conn: sqlite3.Connection):
    """
    规则配置默认值入库（批 3）。配置存在 DB 而不是写死代码：
    后台改完即生效，版控里也能看到"什么时候谁改了什么"。
    """
    from stage1_bridge import get_synonyms
    defaults = {
        "synonyms": get_synonyms(),
        "categories": ["设备", "语言服务", "餐饮", "场地", "其他"],
        "gates": {"handover_3elements": True, "version_double_condition": True,
                  "no_auto_pass": True},
        "llm_prompt": {
            "enabled": False,      # 一期无 LLM：只存模板，二期启用才真正生效
            "text": ("你是会议服务需求汇总助手，只根据给出的需求项回答；"
                     "不确定时明确说“清单中未找到”；回答逐条附编号与来源位置；"
                     "不得编造需求项、责任人、状态或截止时间。"),
        },
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO rule_configs(key,value_json,updated_by,updated_at) "
                     "VALUES(?,?,?,?)", (k, json.dumps(v, ensure_ascii=False), None, now()))


def get_rule(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value_json FROM rule_configs WHERE key=?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default


def set_rule(conn: sqlite3.Connection, key: str, value, uid=None):
    conn.execute("INSERT INTO rule_configs(key,value_json,updated_by,updated_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                 "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                 (key, json.dumps(value, ensure_ascii=False), uid, now()))


def apply_rules():
    """把 DB 里的规则灌进运行时（同义词表热更新），随后重建索引。"""
    from stage1_bridge import set_synonyms
    with db() as conn:
        syn = get_rule(conn, "synonyms", {})
    set_synonyms(syn or {})
    return rebuild_index()


def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    # 五角色演示账号（编辑权与确认权分离，见权限矩阵）
    seed_users = [
        ("yixian", "123456", "林亦舟", "staff"),
        ("tangyq", "123456", "唐映秋", "coordinator"),
        ("guml", "123456", "顾明岚", "biz_owner"),
        ("fujing", "123456", "傅景", "qa"),
        ("qinrui", "admin123", "秦芮", "admin"),
    ]
    for u, p, n, r in seed_users:
        conn.execute(
            "INSERT OR IGNORE INTO users(username,password_hash,display_name,role,created_at) "
            "VALUES(?,?,?,?,?)", (u, hash_pwd(p), n, r, now()))
    # 首启导入需求项（来自 demo markdown，解析器就是阶段 1 那个）
    cur = conn.execute("SELECT COUNT(*) c FROM requirement_items")
    if cur.fetchone()["c"] == 0 and os.path.exists(DEMO_MD):
        with open(DEMO_MD, encoding="utf-8") as f:
            items = parse_requirements(f.read())
        for it in items:
            conn.execute(
                "INSERT OR REPLACE INTO requirement_items"
                "(item_no,category,content,status,owner,source_tool,source_ref,"
                " biz_occurred_at,received_at,change_note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (it.item_no, it.category, it.content, it.status, it.owner,
                 it.source_tool, it.source_ref,                  it.biz_occurred_at, it.received_at,
                 it.change_note))
    _seed_rules(conn)
    _seed_source_records(conn)
    # 首次导入时同步建立「来源单号 → 内部编号」映射（f30：三源没有统一编号，
    # 这张映射表就是 Agent 判断"这条来源数据是否已入库"的唯一依据，也是 upsert 幂等的前提）
    if conn.execute("SELECT COUNT(*) c FROM id_mappings").fetchone()["c"] == 0:
        for r in conn.execute("SELECT item_no,source_tool,source_ref FROM requirement_items "
                              "WHERE source_tool<>'' AND source_ref<>''").fetchall():
            ext = (r["source_ref"] or "").split("|")[0].strip()
            if ext:
                conn.execute(
                    "INSERT OR IGNORE INTO id_mappings(source_tool,external_no,item_no,"
                    "mapped_by,created_at) VALUES(?,?,?,?,?)",
                    (r["source_tool"], ext, r["item_no"], None, now()))
    conn.commit()


def _seed_source_records(conn: sqlite3.Connection):
    """
    三源快照演示数据（【演示数据】非真实客户数据）：
    给 diff_sources 工具一个能真正跑出冲突的样本——
      · LS-02 的 owner / status 三边不一致（群消息先、系统后补录的典型现场）
      · EQ-05 的 content 服务台与台账不一致（翻译间 2 间 vs 1 间）
    真实环境里这张表由接口/CSV 导入时逐字段落快照，保留原行不清洗（f33）。
    """
    if conn.execute("SELECT COUNT(*) c FROM source_records").fetchone()["c"] > 0:
        return
    demo = [
        ("LS-02", "服务台", "owner", "唐映秋", "SD-2026-1024#2"),
        ("LS-02", "服务台", "status", "未确认", "SD-2026-1024#2"),
        ("LS-02", "台账", "owner", "", "LEDGER-10月#88"),
        ("LS-02", "台账", "status", "未确认", "LEDGER-10月#88"),
        ("LS-02", "文件库", "owner", "傅景", "群消息2026-10-24"),
        ("LS-02", "文件库", "status", "已确认", "群消息2026-10-24"),
        ("EQ-05", "服务台", "content", "翻译间 2 间", "SD-2026-1021#1"),
        ("EQ-05", "台账", "content", "翻译间 1 间", "LEDGER-10月#41"),
    ]
    for item_no, src, field, val, ref in demo:
        conn.execute(
            "INSERT OR IGNORE INTO source_records(item_no,source_tool,field,value,raw_ref,snapshot_at) "
            "VALUES(?,?,?,?,?,?)", (item_no, src, field, val, ref, now()))


def load_items():
    """把需求项从库读成阶段 1 的 RequirementItem 列表（检索器直接用）。软删项不参与检索。"""
    with db() as conn:
        rows = conn.execute("SELECT * FROM requirement_items WHERE is_deleted=0").fetchall()
    from stage1_bridge import RequirementItem
    return [RequirementItem(
        item_no=r["item_no"], category=r["category"], content=r["content"],
        status=r["status"], owner=r["owner"], source_tool=r["source_tool"],
        source_ref=r["source_ref"], biz_occurred_at=r["biz_occurred_at"],
        received_at=r["received_at"], change_note=r["change_note"] or "",
    ) for r in rows]


def rebuild_index() -> int:
    """
    重建检索索引：需求项增删改后必须调用，否则检索仍是旧数据。
    规模 50 项级别，重建 <50ms，直接同步做，不必上任务队列。
    """
    items = load_items()
    state["retriever"] = HybridRetriever(items) if items else None
    return len(items)


# ============================================================
# 应用与鉴权
# ============================================================
app = FastAPI(title="会议活动需求查询台", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

state = {"retriever": None, "rate": {}}   # retriever：启动时构建；rate：内存限流


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    """把 detail 统一摊平成 {code, message}，前端只认这两个字段。"""
    d = exc.detail
    if isinstance(d, dict) and "code" in d:
        return JSONResponse(status_code=exc.status_code, content=d)
    return JSONResponse(status_code=exc.status_code,
                        content={"code": 50000, "message": str(d)})


@app.on_event("startup")
async def startup():
    with db() as conn:
        init_db(conn)
    items = load_items()
    if items:
        state["retriever"] = await asyncio.to_thread(HybridRetriever, items)
    # 用 DB 里的同义词覆盖内置表并重建索引：改后台配置 → 重启后依然是新配置
    with db() as conn:
        syn = get_rule(conn, "synonyms", None)
    if syn:
        from stage1_bridge import set_synonyms
        set_synonyms(syn)
        state["retriever"] = HybridRetriever(load_items()) if items else None
    print(f"[启动] 需求项 {len(items)} 条，检索器{'就绪' if state['retriever'] else '未就绪'}，"
          f"LLM={LLM_PROVIDER}，同义词 {len(syn or {})} 组")


@app.get("/")
async def index():
    return RedirectResponse("/static/index.html")


def current_user(request: Request) -> dict:
    """
    解析当前用户，两种来源：
      · API 调用：Authorization: Bearer <token>
      · SSR 页面：cookie mt_token（HttpOnly，登录表单写入）
    每次请求都回库查角色与启用状态——改角色/停用即时生效，不必等 token 过期。
    """
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    elif request.cookies.get("mt_token"):
        token = request.cookies["mt_token"]
    if not token:
        fail(401, 40101)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        fail(401, 40101)
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (payload["uid"],)).fetchone()
    if row is None:
        fail(401, 40101)
    if not row["is_active"]:
        fail(401, 40103)
    return dict(row)


def check_rate(uid: int):
    """内存限流：10 秒内超过 5 次即拒绝（单机单进程够用，多实例时换 Redis）。"""
    now_ts = time.time()
    stamps = [t for t in state["rate"].get(uid, []) if now_ts - t < RATE_WINDOW]
    if len(stamps) >= RATE_MAX:
        fail(429, 42901)
    stamps.append(now_ts)
    state["rate"][uid] = stamps


# ============================================================
# 请求体模型
# ============================================================
class LoginIn(BaseModel):
    username: str = Field(..., max_length=32)
    password: str = Field(..., max_length=64)


class ChatIn(BaseModel):
    session_id: str | None = None
    question: str = ""


# ============================================================
# 接口 1：登录
# ============================================================
@app.post("/api/login")
async def login(body: LoginIn):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
        if row is None or row["password_hash"] != hash_pwd(body.password):
            fail(401, 40102)
        if not row["is_active"]:
            fail(401, 40103)                      # 停用账号：密码对也不让进
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), row["id"]))
        conn.commit()
    token = jwt.encode(
        # 注意：不要用 datetime.utcnow().timestamp()——naive datetime 会被当本地时间换算，
        # 在东八区会直接把 exp 算成"8 小时前"，token 一签发就过期。统一用 time.time()。
        {"uid": row["id"], "name": row["display_name"],
         "exp": int(time.time()) + TOKEN_HOURS * 3600},
        JWT_SECRET, algorithm=JWT_ALG)
    return {"token": token, "display_name": row["display_name"], "role": row["role"]}


# ============================================================
# 接口 2：查询（SSE 流式）
#   事件顺序：status → item ×N → chunk ×M → done（异常时 error）
# ============================================================
def _expand_with_context(conn, session_id: str, question: str) -> str:
    """
    最近 6 条消息做查询扩展（一期规则版）：
    上一问提到"餐饮/语言服务"等类别，本问"那餐饮呢？"这类省略式追问才能命中。
    二期接 LLM 时，这 6 条直接拼进提示词，接口不变。
    """
    rows = conn.execute(
        "SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, CONTEXT_TURNS)).fetchall()
    if not rows:
        return question
    last_cats = [c for c in ("设备", "语言服务", "餐饮", "场地", "其他")
                 if any(c in r["content"] for r in rows)]
    # 短问题才补上下文，长问题自带语义，补了反而污染检索
    if len(question) <= 8 and last_cats:
        return question + " " + " ".join(last_cats)
    return question


def _rule_summary(hits) -> str:
    """LLM off 时的汇总文案：只做统计与点名，不生成任何推断（零幻觉）。"""
    if not hits:
        return "清单未覆盖该问题，可换个说法，或输入“缺项”查看全部缺项。"
    missing = [it for it, _ in hits if it.status == "缺失"]
    unconfirmed = [it for it, _ in hits if it.status == "未确认"]
    parts = [f"共命中 {len(hits)} 项"]
    if missing:
        parts.append(f"其中缺失 {len(missing)} 项（{('、'.join(i.item_no for i in missing))}）")
    if unconfirmed:
        parts.append(f"未确认 {len(unconfirmed)} 项（{('、'.join(i.item_no for i in unconfirmed))}）")
    focus = missing[0] if missing else (unconfirmed[0] if unconfirmed else hits[0][0])
    parts.append(f"最需盯的：{focus.item_no}（{focus.status}，来源 {focus.source_tool} {focus.source_ref}）")
    return "，".join(parts) + "。"


async def _llm_summary(question: str, hits) -> str | None:
    """LLM 汇总（默认关闭）。开启后失败也不影响清单输出——清单才是主交付物。"""
    if LLM_PROVIDER != "on":
        return None
    try:
        from openai import OpenAI
        ctx = "\n".join(f"[{it.item_no}|{it.source_tool} {it.source_ref}] {it.content}（{it.status}）"
                        for it, _ in hits)
        client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=LLM_MODEL, timeout=15,
            messages=[{"role": "system",
                       "content": "只依据给出的需求项作答，逐条带编号与来源；未覆盖必须回答“清单未覆盖”。"},
                      {"role": "user", "content": f"需求项：\n{ctx}\n\n问题：{question}"}])
        return resp.choices[0].message.content
    except Exception as e:
        return f"（LLM 汇总失败已降级为规则文案：{e}）"


def sse(event: str, data: dict) -> str:
    """SSE 帧格式：event: xxx\\ndata: {json}\\n\\n（中文不转义，前端直接可读）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request, user=Depends(current_user)):
    # ── ① 前置校验：全部在建立流之前完成，错误以普通 JSON 返回 ──
    q = (body.question or "").strip()
    if not q:
        fail(400, 40001)
    if len(q) > MAX_QUESTION_LEN:
        fail(400, 40002)
    check_rate(user["id"])
    if state["retriever"] is None:
        fail(503, 50301)

    conn = db()
    sid = body.session_id
    if sid:
        row = conn.execute("SELECT * FROM chat_sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            fail(404, 40401)
        if row["user_id"] != user["id"]:
            fail(403, 40301)
    else:
        sid = str(uuid.uuid4())
        conn.execute("INSERT INTO chat_sessions(id,user_id,title,created_at) VALUES(?,?,?,?)",
                     (sid, user["id"], q[:20], now()))
    # 用户消息先落库（即便后面出错，也留下"问过什么"的痕迹）
    conn.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                 (sid, "user", q, now()))
    conn.commit()
    # 查询扩展要用到"本次提问之前"的历史，所以放在插入 user 消息之后、检索之前
    expanded = _expand_with_context(conn, sid, q)

    async def gen():
        t0 = time.time()
        try:
            yield sse("status", {"stage": "search", "message": "正在检索需求项…"})
            hits = await asyncio.to_thread(state["retriever"].search, expanded, TOP_K)
            for it, _score in hits:
                yield sse("item", {
                    "item_no": it.item_no, "category": it.category, "status": it.status,
                    "content": it.content, "owner": it.owner or "—",
                    "source_tool": it.source_tool or "来源缺失",
                    "source_ref": it.source_ref or "",
                    "biz_occurred_at": it.biz_occurred_at,
                })
                # 留痕：谁查了哪一项（f44 四项之一）。失败不阻断查询。
                conn.execute("INSERT INTO audit_logs(actor_id,action,target_type,target_id,created_at) "
                             "VALUES(?,?,?,?,?)", (user["id"], "view", "item", it.item_no, now()))
            conn.commit()

            text = await _llm_summary(q, hits) or _rule_summary(hits)
            # 分段推 chunk，让前端有打字机效果（8 字一段，前端再逐字上屏）
            for i in range(0, len(text), 8):
                yield sse("chunk", {"text": text[i:i + 8]})
                await asyncio.sleep(0.02)

            citations = [{"item_no": it.item_no, "source_tool": it.source_tool,
                          "source_ref": it.source_ref, "snippet": it.content[:40]}
                         for it, _ in hits]
            latency = int((time.time() - t0) * 1000)
            cur = conn.execute(
                "INSERT INTO messages(session_id,role,content,citation_source_refs,latency_ms,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (sid, "assistant", text, json.dumps(citations, ensure_ascii=False), latency, now()))
            conn.commit()
            yield sse("done", {"session_id": sid, "message_id": cur.lastrowid,
                               "elapsed_ms": latency, "citations": citations})
        except Exception as e:
            yield sse("error", {"code": 50001, "message": f"{ERR[50001]}（{e}）"})
        finally:
            conn.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ============================================================
# 接口 3/4：会话列表、历史消息
# ============================================================
@app.get("/api/sessions")
async def list_sessions(user=Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.title, s.created_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS msg_count "
            "FROM chat_sessions s WHERE s.user_id=? ORDER BY s.created_at DESC LIMIT 50",
            (user["id"],)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/sessions/{sid}/messages")
async def session_messages(sid: str, user=Depends(current_user)):
    with db() as conn:
        s = conn.execute("SELECT * FROM chat_sessions WHERE id=?", (sid,)).fetchone()
        if s is None:
            fail(404, 40401)
        if s["user_id"] != user["id"]:
            fail(403, 40301)
        rows = conn.execute(
            "SELECT id,role,content,citation_source_refs,latency_ms,created_at "
            "FROM messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["citations"] = json.loads(d.pop("citation_source_refs") or "[]")
        out.append(d)
    return {"session": dict(s), "messages": out}


# ============================================================
# 管理后台（批 1）：/login、/admin、/admin/users 与用户接口
#   —— 路由定义在 admin.py，这里只做挂载与页面级 403 处理
# ============================================================
from admin import router as admin_router, _Forbidden, _forbidden_response  # noqa: E402

app.include_router(admin_router)

from tools.api import router as tools_router  # noqa: E402  Agent 工具层入口

app.include_router(tools_router)

from agent.api import router as agent_router  # noqa: E402  Agent 编排层入口（Harness 循环）

app.include_router(agent_router)


@app.exception_handler(_Forbidden)
async def forbidden_handler(request: Request, exc: _Forbidden):
    """页面级鉴权失败：未登录跳登录页，角色不足渲染 403 页（明示无权）。"""
    return _forbidden_response(request, exc)
