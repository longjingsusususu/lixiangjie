# -*- coding: utf-8 -*-
"""
会议活动服务需求汇总系统 · 一期 MVP（f24015105_meeting_server.py）

对齐已确认十问基线（2026-11-06"全按推荐"）与《技术架构选型》Walking Skeleton：
  五角色 JWT 鉴权（编辑权/确认权分离 f22）｜登记补录（双时间戳 f13/f34 + 五态分列 f31）
  CSV 导入 + 编号映射幂等（f30）｜版本定版（f24 双条件）｜交接三要素拦截卡口（f20，服务端强制）
  异常人工队列（不得默认通过 f44）｜留痕四项审计（f44/f45）｜M6/M7 看板

运行：python f24015105_meeting_server.py  （默认 127.0.0.1:8800）
数据：MEETING_MVP_DB 环境变量或同目录 meeting_mvp.db；先跑 f24015105_meeting_seed.py 灌样例。
依赖：fastapi、uvicorn（数据层为标准库 sqlite3）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("MEETING_MVP_DB", BASE_DIR / "meeting_mvp.db"))
SECRET = os.environ.get("MEETING_MVP_SECRET", "mvp-secret-please-change")
TOKEN_TTL = 12 * 3600
CONTENT_LIMIT = 2000

ROLES = ("staff", "coordinator", "biz_owner", "qa", "admin")
ROLE_NAME = {"staff": "一线专员", "coordinator": "协调员", "biz_owner": "业务负责人",
             "qa": "质量验收", "admin": "管理员"}
STATUSES = ("missing", "unconfirmed", "confirmed", "finalized", "handed_over")
STATUS_NAME = {"missing": "缺失", "unconfirmed": "未确认", "confirmed": "已确认",
               "finalized": "已定版", "handed_over": "已交接"}
EXC_TYPES = ("missing_field", "conflict", "dirty_data", "version_unverifiable")
CATS = ("equipment", "language", "catering", "venue", "other")
CAT_NAME = {"equipment": "设备", "language": "语言", "catering": "餐饮", "venue": "场地", "other": "其他"}
SOURCES = ("servicedesk", "ledger", "filelib", "manual")
SOURCE_NAME = {"servicedesk": "服务台", "ledger": "台账", "filelib": "文件库", "manual": "手工补录"}

app = FastAPI(title="会议活动服务需求汇总系统 MVP")
_lock = threading.RLock()   # 可重入锁：定版/交接/导入在持锁期间会调用 change() 内部再加锁
_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL, role TEXT NOT NULL, is_active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, planned_items INTEGER DEFAULT 50,
  status TEXT DEFAULT 'active', created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS requirement_items(
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  category TEXT NOT NULL, content TEXT NOT NULL, owner_id INTEGER REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'unconfirmed',
  source_tool TEXT NOT NULL, source_row_ref TEXT, record_channel TEXT DEFAULT 'system_first',
  biz_occurred_at TEXT NOT NULL, received_at TEXT NOT NULL,
  confirmed_by INTEGER REFERENCES users(id), confirmed_at TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS item_changes(
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES requirement_items(id),
  field_name TEXT NOT NULL, old_value TEXT, new_value TEXT,
  changed_by INTEGER REFERENCES users(id), change_source TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS versions(
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
  version_no TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
  valid_until TEXT, finalized_by INTEGER REFERENCES users(id), finalized_at TEXT,
  void_reason TEXT, created_by INTEGER REFERENCES users(id),
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS version_items(
  version_id INTEGER NOT NULL REFERENCES versions(id),
  item_id INTEGER NOT NULL REFERENCES requirement_items(id), PRIMARY KEY(version_id, item_id));
CREATE TABLE IF NOT EXISTS handovers(
  id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES versions(id),
  receiver_id INTEGER NOT NULL REFERENCES users(id),
  open_items_count INTEGER NOT NULL, deadline TEXT NOT NULL,
  transfer_point TEXT DEFAULT 'on_send', ack_by INTEGER, ack_at TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS exception_queue(
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES requirement_items(id),
  exception_type TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'open',
  assigned_to INTEGER, sla_due TEXT, resolution TEXT, resolved_by INTEGER, closed_at TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, action TEXT NOT NULL,
  target_type TEXT NOT NULL, target_id INTEGER, target_version TEXT,
  suggestion_snapshot TEXT, confirmed_by INTEGER,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS id_mappings(
  id INTEGER PRIMARY KEY, source_tool TEXT NOT NULL, external_no TEXT NOT NULL,
  item_id INTEGER NOT NULL, mapped_by INTEGER,
  created_at TEXT DEFAULT (datetime('now')), UNIQUE(source_tool, external_no));
CREATE TABLE IF NOT EXISTS source_snapshots(
  id INTEGER PRIMARY KEY, tool TEXT NOT NULL, fetched_at TEXT NOT NULL,
  status TEXT NOT NULL, file_path TEXT, note TEXT);
"""

# ---- 权限矩阵（编辑权与确认权分离；确认类仅 biz_owner/qa） ----
P_CREATE_ITEM = {"staff", "coordinator", "admin"}
P_CONFIRM = {"biz_owner"}
P_VERSION = {"coordinator"}
P_HANDOVER = {"coordinator"}
P_EXC_CREATE = {"staff", "coordinator", "qa", "admin", "biz_owner"}
P_EXC_CLAIM = {"qa", "admin"}
P_EXC_CLOSE = {"qa"}
P_IMPORT = {"admin"}
P_AUDIT = {"biz_owner", "qa", "admin"}


def now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def init_schema() -> None:
    with _lock:
        db().executescript(SCHEMA)
        db().commit()


# ---- 认证：hmac 签名 token（uid.exp.sig），Cookie 或 Bearer ----
def _sig(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(uid: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"uid": uid, "exp": int(time.time()) + TOKEN_TTL}).encode()).decode()
    return payload + "." + _sig(payload)


def parse_token(token: str):
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sig(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def current_user(request: Request):
    token = request.cookies.get("mtoken")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    if not token:
        return None
    data = parse_token(token)
    if not data:
        return None
    row = db().execute("SELECT * FROM users WHERE id=? AND is_active=1", (data["uid"],)).fetchone()
    return dict(row) if row else None


def need(request: Request, roles: set[str] | None = None):
    """鉴权依赖：未登录抛 401，角色不符抛 403。返回 (user, err_response)。"""
    user = current_user(request)
    if user is None:
        return None, JSONResponse({"error_code": "401", "message": "未登录或凭证无效/过期"}, status_code=401)
    if roles is not None and user["role"] not in roles:
        return None, JSONResponse({"error_code": "403", "message": f"角色 {ROLE_NAME[user['role']]} 无权执行此操作"}, status_code=403)
    return user, None


async def payload(request: Request) -> dict:
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return await request.json()
        except Exception:
            return {}
    form = await request.form()
    return {k: form[k] for k in form}


def err(code: str, msg: str, status: int = 400):
    return JSONResponse({"error_code": code, "message": msg}, status_code=status)


def audit(actor: int, action: str, ttype: str, tid=None, tver=None, suggestion=None, confirmed_by=None):
    with _lock:
        db().execute(
            "INSERT INTO audit_logs(actor_id,action,target_type,target_id,target_version,suggestion_snapshot,confirmed_by) VALUES(?,?,?,?,?,?,?)",
            (actor, action, ttype, tid, tver, suggestion, confirmed_by))
        db().commit()


def change(item_id: int, field: str, old, new, by: int, source: str):
    with _lock:
        db().execute(
            "INSERT INTO item_changes(item_id,field_name,old_value,new_value,changed_by,change_source) VALUES(?,?,?,?,?,?)",
            (item_id, field, str(old) if old is not None else None, str(new) if new is not None else None, by, source))
        db().commit()


# ---------------------------------------------------------------- API
@app.post("/api/login")
async def login(request: Request):
    body = await payload(request)
    uname, pwd = str(body.get("username", "")).strip(), str(body.get("password", ""))
    row = db().execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone()
    h = hashlib.sha256((uname + ":" + pwd).encode()).hexdigest()
    if row is None or row["password_hash"] != h or not row["is_active"]:
        return err("401", "用户名或密码错误，或账号已停用", 401)
    token = make_token(row["id"])
    resp = JSONResponse({"token": token, "user": {"id": row["id"], "username": row["username"],
                                                  "display_name": row["display_name"], "role": row["role"],
                                                  "role_name": ROLE_NAME[row["role"]]}})
    resp.set_cookie("mtoken", token, httponly=True, samesite="lax")
    return resp


@app.get("/api/me")
async def me(request: Request):
    user, e = need(request)
    if e:
        return e
    return {"user": {**user, "role_name": ROLE_NAME[user["role"]]}}


@app.post("/api/items")
async def create_item(request: Request):
    user, e = need(request, P_CREATE_ITEM)
    if e:
        return e
    b = await payload(request)
    content = str(b.get("content", "")).strip()
    if not content:
        return err("E_CONTENT", "需求描述不能为空")
    if len(content) > CONTENT_LIMIT:
        return err("E_CONTENT_LEN", f"需求描述超过 {CONTENT_LIMIT} 字符上限")
    cat = b.get("category", "other")
    if cat not in CATS:
        return err("E_CATEGORY", f"无效类别，允许值：{','.join(CATS)}")
    src = b.get("source_tool", "manual")
    if src not in SOURCES:
        return err("E_SOURCE", f"无效来源工具，允许值：{','.join(SOURCES)}")
    biz = str(b.get("biz_occurred_at", "")).strip()
    try:
        datetime.strptime(biz, "%Y-%m-%d %H:%M")
    except ValueError:
        return err("E_TIME", "业务发生时间格式须为 YYYY-MM-DD HH:MM（双时间戳之业务发生时间，f34）")
    batch_id = int(b.get("batch_id") or 1)
    if not db().execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone():
        return err("E_BATCH", "批次不存在")
    try:
        owner_id = int(b["owner_id"]) if b.get("owner_id") not in (None, "", "0") else None
    except (TypeError, ValueError):
        return err("E_OWNER", "责任人字段须为有效用户 ID（故障注入防护：拒绝脏数据落库）")
    if owner_id is not None and not db().execute("SELECT 1 FROM users WHERE id=?", (owner_id,)).fetchone():
        return err("E_OWNER", "责任人不存在")
    status = "unconfirmed" if owner_id else "missing"   # 缺失≠未确认，分列（f31）
    with _lock:
        cur = db().execute(
            "INSERT INTO requirement_items(batch_id,category,content,owner_id,status,source_tool,source_row_ref,"
            "record_channel,biz_occurred_at,received_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, cat, content, owner_id, status, src, b.get("source_row_ref") or None,
             b.get("record_channel", "system_first"), biz + ":00", now(), user["id"]))
        item_id = cur.lastrowid
        db().commit()
    change(item_id, "status", None, status, user["id"], "manual")
    audit(user["id"], "suggest", "item", item_id, suggestion=f"登记需求项[{CAT_NAME[cat]}]：{content[:50]}")
    return {"id": item_id, "status": status, "biz_occurred_at": biz + ":00", "received_at": db().execute(
        "SELECT received_at FROM requirement_items WHERE id=?", (item_id,)).fetchone()[0]}


@app.get("/api/items")
async def list_items(request: Request):
    user, e = need(request)
    if e:
        return e
    batch = request.query_params.get("batch_id", "1")
    rows = db().execute(
        "SELECT r.*, u.display_name AS owner_name, c.display_name AS confirmed_by_name FROM requirement_items r "
        "LEFT JOIN users u ON u.id=r.owner_id LEFT JOIN users c ON c.id=r.confirmed_by "
        "WHERE r.batch_id=? ORDER BY r.id", (batch,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/items/{item_id}")
async def item_detail(item_id: int, request: Request):
    user, e = need(request)
    if e:
        return e
    row = db().execute("SELECT * FROM requirement_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return err("404", "需求项不存在", 404)
    audit(user["id"], "view", "item", item_id, tver=row["status"])
    changes = db().execute(
        "SELECT ic.*, u.display_name FROM item_changes ic LEFT JOIN users u ON u.id=ic.changed_by "
        "WHERE item_id=? ORDER BY ic.id", (item_id,)).fetchall()
    return {"item": dict(row), "changes": [dict(c) for c in changes]}


@app.post("/api/items/{item_id}/confirm")
async def confirm_item(item_id: int, request: Request):
    user, e = need(request, P_CONFIRM)   # 确认签字仅业务负责人（f22：模型/系统无审批权）
    if e:
        return e
    row = db().execute("SELECT * FROM requirement_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return err("404", "需求项不存在", 404)
    if row["status"] not in ("missing", "unconfirmed"):
        return err("E_STATE", f"当前状态[{STATUS_NAME[row['status']]}]不可确认，仅缺失/未确认可确认")
    with _lock:
        db().execute("UPDATE requirement_items SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
                     (user["id"], now(), item_id))
        db().commit()
    change(item_id, "status", row["status"], "confirmed", user["id"], "manual")
    audit(user["id"], "confirm", "item", item_id, tver="confirmed", confirmed_by=user["id"])
    return {"id": item_id, "status": "confirmed"}


@app.post("/api/versions")
async def create_version(request: Request):
    user, e = need(request, P_VERSION)
    if e:
        return e
    b = await payload(request)
    batch_id = int(b.get("batch_id") or 1)
    ids = b.get("item_ids") or []
    if not ids:
        return err("E_ITEMS", "版本至少包含一个需求项")
    valid_until = str(b.get("valid_until", "")).strip()
    try:
        datetime.strptime(valid_until, "%Y-%m-%d")
    except ValueError:
        return err("E_TIME", "有效期格式须为 YYYY-MM-DD（f24 双条件之一）")
    with _lock:
        seq = (db().execute("SELECT COALESCE(MAX(id),0)+1 FROM versions").fetchone()[0])
        vno = f"V-{seq:04d}"   # 系统生成版本号，不可编辑（f24）
        cur = db().execute(
            "INSERT INTO versions(batch_id,version_no,status,valid_until,created_by) VALUES(?,?,?,?,?)",
            (batch_id, vno, "draft", valid_until, user["id"]))
        vid = cur.lastrowid
        for i in ids:
            r = db().execute("SELECT batch_id,status FROM requirement_items WHERE id=?", (i,)).fetchone()
            if r is None or r["batch_id"] != batch_id:
                db().rollback()
                return err("E_ITEM_BATCH", f"需求项 {i} 不存在或不属于该批次")
            if r["status"] not in ("unconfirmed", "confirmed"):
                db().rollback()
                return err("E_ITEM_STATE", f"需求项 {i} 状态[{STATUS_NAME[r['status']]}]不可纳入版本")
            db().execute("INSERT INTO version_items(version_id,item_id) VALUES(?,?)", (vid, i))
        db().commit()
    audit(user["id"], "suggest", "version", vid, tver=vno, suggestion=f"创建草稿版本 {vno}，含 {len(ids)} 项")
    return {"version_id": vid, "version_no": vno, "status": "draft"}


@app.post("/api/versions/{vid}/finalize")
async def finalize_version(vid: int, request: Request):
    user, e = need(request, P_VERSION)
    if e:
        return e
    v = db().execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
    if v is None:
        return err("404", "版本不存在", 404)
    if v["status"] != "draft":
        return err("E_STATE", f"版本状态[{v['status']}]不可定版")
    # f24 双条件：有效期 + 确认状态
    if not v["valid_until"] or v["valid_until"] < datetime.utcnow().strftime("%Y-%m-%d"):
        return err("E_VALID", "定版被拒（f24）：版本无有效期或有效期已过期")
    bad = db().execute(
        "SELECT id,status FROM requirement_items WHERE id IN (SELECT item_id FROM version_items WHERE version_id=?) "
        "AND status<>'confirmed'", (vid,)).fetchall()
    if bad:
        return err("E_CONFIRM", "定版被拒（f24 双条件确认状态）：以下需求项未全部确认：" +
                   ",".join(f"#{r['id']}({STATUS_NAME[r['status']]})" for r in bad))
    with _lock:
        db().execute("UPDATE versions SET status='finalized', finalized_by=?, finalized_at=? WHERE id=?",
                     (user["id"], now(), vid))
        for r in db().execute("SELECT item_id FROM version_items WHERE version_id=?", (vid,)).fetchall():
            old = db().execute("SELECT status FROM requirement_items WHERE id=?", (r["item_id"],)).fetchone()[0]
            db().execute("UPDATE requirement_items SET status='finalized' WHERE id=?", (r["item_id"],))
            change(r["item_id"], "status", old, "finalized", user["id"], "system")
        db().commit()
    audit(user["id"], "finalize", "version", vid, tver=v["version_no"], confirmed_by=user["id"])
    return {"version_id": vid, "version_no": v["version_no"], "status": "finalized"}


@app.post("/api/versions/{vid}/void")
async def void_version(vid: int, request: Request):
    user, e = need(request, P_VERSION)
    if e:
        return e
    b = await payload(request)
    reason = str(b.get("reason", "")).strip()
    if not reason:
        return err("E_REASON", "作废必须留因（f24）")
    v = db().execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
    if v is None or v["status"] != "draft":
        return err("E_STATE", "仅草稿版本可作废")
    with _lock:
        db().execute("UPDATE versions SET status='void', void_reason=? WHERE id=?", (reason, vid))
        db().commit()
    audit(user["id"], "finalize", "version", vid, tver=v["version_no"], suggestion=f"作废留因：{reason}")
    return {"version_id": vid, "status": "void", "reason": reason}


@app.post("/api/handovers")
async def create_handover(request: Request):
    user, e = need(request, P_HANDOVER)
    if e:
        return e
    b = await payload(request)
    try:
        vid = int(b.get("version_id") or 0)
    except (TypeError, ValueError):
        return err("E_VERSION", "交接卡口拦截（f20）：版本标识无效")
    v = db().execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
    if v is None or v["status"] != "finalized":
        return err("E_VERSION", "交接卡口拦截（f20）：版本不存在或未定版")
    receiver = int(b.get("receiver_id") or 0)
    if not receiver or not db().execute("SELECT 1 FROM users WHERE id=? AND is_active=1", (receiver,)).fetchone():
        return err("E_RECEIVER", "交接卡口拦截（f20 三要素①）：接收人缺失或无效")
    deadline = str(b.get("deadline", "")).strip()
    try:
        datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        return err("E_DEADLINE", "交接卡口拦截（f20 三要素③）：截止时间缺失或格式错误（YYYY-MM-DD）")
    if "open_items_count" not in b:
        return err("E_OPEN", "交接卡口拦截（f20 三要素②）：未显式列明未结项数（如实列明，哪怕为 0）")
    open_real = db().execute(
        "SELECT COUNT(*) FROM requirement_items WHERE batch_id=? AND status NOT IN ('finalized','handed_over')",
        (v["batch_id"],)).fetchone()[0]
    try:
        open_declared = int(b["open_items_count"])
    except (TypeError, ValueError):
        return err("E_OPEN", "交接卡口拦截（f20 三要素②）：未结项数须为整数")
    if open_declared != open_real:
        return err("E_OPEN", f"交接卡口拦截（f20 三要素②）：列明未结项 {open_declared} 与实际 {open_real} 不符——不允许只报已处理（E-01 教训）")
    tp = b.get("transfer_point", "on_send")
    if tp not in ("on_send", "on_ack"):
        return err("E_TP", "责任转移时点仅允许 on_send/on_ack（两口径并存，待 R1 签字）")
    with _lock:
        cur = db().execute(
            "INSERT INTO handovers(version_id,receiver_id,open_items_count,deadline,transfer_point,created_by) VALUES(?,?,?,?,?,?)",
            (vid, receiver, open_declared, deadline, tp, user["id"]))
        hid = cur.lastrowid
        for r in db().execute("SELECT item_id FROM version_items WHERE version_id=?", (vid,)).fetchall():
            old = db().execute("SELECT status FROM requirement_items WHERE id=?", (r["item_id"],)).fetchone()[0]
            db().execute("UPDATE requirement_items SET status='handed_over' WHERE id=?", (r["item_id"],))
            change(r["item_id"], "status", old, "handed_over", user["id"], "system")
        db().commit()
    audit(user["id"], "handover", "handover", hid, tver=v["version_no"],
          suggestion=f"交接 {v['version_no']} → 接收人#{receiver}，未结项 {open_declared}，截止 {deadline}")
    return {"handover_id": hid, "version_no": v["version_no"], "open_items_count": open_declared,
            "handed_items": db().execute("SELECT COUNT(*) FROM version_items WHERE version_id=?", (vid,)).fetchone()[0]}


@app.post("/api/handovers/{hid}/ack")
async def ack_handover(hid: int, request: Request):
    user, e = need(request)
    if e:
        return e
    h = db().execute("SELECT * FROM handovers WHERE id=?", (hid,)).fetchone()
    if h is None:
        return err("404", "交接记录不存在", 404)
    if h["ack_by"]:
        return err("E_ACKED", "该交接已确认接收")
    if user["id"] != h["receiver_id"] and user["role"] != "admin":
        return err("E_ACK_ROLE", "仅接收人本人可确认接收")
    with _lock:
        db().execute("UPDATE handovers SET ack_by=?, ack_at=? WHERE id=?", (user["id"], now(), hid))
        db().commit()
    audit(user["id"], "confirm", "handover", hid, tver=str(h["version_id"]), confirmed_by=user["id"])
    return {"handover_id": hid, "acked_at": now()}


@app.post("/api/exceptions")
async def create_exception(request: Request):
    user, e = need(request, P_EXC_CREATE)
    if e:
        return e
    b = await payload(request)
    item_id = int(b.get("item_id") or 0)
    if not db().execute("SELECT 1 FROM requirement_items WHERE id=?", (item_id,)).fetchone():
        return err("404", "需求项不存在", 404)
    et = b.get("exception_type")
    if et not in EXC_TYPES:
        return err("E_TYPE", f"异常类型允许值：{','.join(EXC_TYPES)}")
    with _lock:
        cur = db().execute(
            "INSERT INTO exception_queue(item_id,exception_type,note,created_by) VALUES(?,?,?,?)",
            (item_id, et, b.get("note"), user["id"]))
        eid = cur.lastrowid
        db().commit()
    audit(user["id"], "suggest", "exception", eid, suggestion=b.get("note") or EXC_NAME.get(et, et))
    return {"exception_id": eid, "status": "open"}   # 无自动通过路径（f44），只能人工闭环


@app.get("/api/exceptions")
async def list_exceptions(request: Request):
    user, e = need(request)
    if e:
        return e
    rows = db().execute(
        "SELECT q.*, u1.display_name AS creator, u2.display_name AS resolver, r.content AS item_content "
        "FROM exception_queue q LEFT JOIN users u1 ON u1.id=q.created_by LEFT JOIN users u2 ON u2.id=q.resolved_by "
        "LEFT JOIN requirement_items r ON r.id=q.item_id ORDER BY q.id DESC").fetchall()
    return {"exceptions": [dict(r) for r in rows]}


@app.post("/api/exceptions/{eid}/claim")
async def claim_exception(eid: int, request: Request):
    user, e = need(request, P_EXC_CLAIM)
    if e:
        return e
    q = db().execute("SELECT * FROM exception_queue WHERE id=?", (eid,)).fetchone()
    if q is None or q["status"] != "open":
        return err("E_STATE", "仅 open 状态可认领")
    with _lock:
        sla = (datetime.utcnow() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        db().execute("UPDATE exception_queue SET status='processing', assigned_to=?, sla_due=? WHERE id=?",
                     (user["id"], sla, eid))
        db().commit()
    return {"exception_id": eid, "status": "processing", "sla_due": sla}


@app.post("/api/exceptions/{eid}/close")
async def close_exception(eid: int, request: Request):
    user, e = need(request, P_EXC_CLOSE)   # 仅质量验收可闭环（f44），无默认通过
    if e:
        return e
    b = await payload(request)
    resolution = str(b.get("resolution", "")).strip()
    if not resolution:
        return err("E_RESOLUTION", "闭环必须填写复核结论（留痕四项：谁/依据/结论）")
    q = db().execute("SELECT * FROM exception_queue WHERE id=?", (eid,)).fetchone()
    if q is None or q["status"] == "closed":
        return err("E_STATE", "工单不存在或已闭环")
    with _lock:
        db().execute("UPDATE exception_queue SET status='closed', resolution=?, resolved_by=?, closed_at=? WHERE id=?",
                     (resolution, user["id"], now(), eid))
        db().commit()
    audit(user["id"], "confirm", "exception", eid, suggestion=resolution, confirmed_by=user["id"])
    return {"exception_id": eid, "status": "closed", "resolution": resolution}


@app.post("/api/import/csv")
async def import_csv(request: Request):
    user, e = need(request, P_IMPORT)
    if e:
        return e
    import csv as _csv
    import io as _io
    b = await payload(request)
    text = str(b.get("csv_text", "")).replace("\ufeff", "")
    src = b.get("source_tool", "servicedesk")
    if src not in SOURCES:
        return err("E_SOURCE", f"来源工具允许值：{','.join(SOURCES)}")
    rows = list(_csv.DictReader(_io.StringIO(text)))
    if not rows:
        return err("E_CSV", "CSV 为空或无数据行（列：external_no,category,content,owner,biz_occurred_at）")
    need_cols = {"external_no", "category", "content", "owner", "biz_occurred_at"}
    if not need_cols.issubset(rows[0].keys()):
        return err("E_CSV", f"CSV 缺少必需列：{','.join(need_cols)}")
    created = skipped = 0
    with _lock:
        for r in rows:
            ext = str(r.get("external_no", "")).strip()
            if not ext:
                continue
            if db().execute("SELECT 1 FROM id_mappings WHERE source_tool=? AND external_no=?", (src, ext)).fetchone():
                skipped += 1   # 幂等：同源同编号不重复建项
                continue
            owner_id = None
            owner_name = str(r.get("owner", "")).strip()
            if owner_name:
                u = db().execute("SELECT id FROM users WHERE username=? OR display_name=?", (owner_name, owner_name)).fetchone()
                owner_id = u["id"] if u else None
            content = str(r.get("content", "")).strip()
            biz = str(r.get("biz_occurred_at", "")).strip()
            status = "unconfirmed" if (owner_id and content) else "missing"
            cur = db().execute(
                "INSERT INTO requirement_items(batch_id,category,content,owner_id,status,source_tool,source_row_ref,"
                "record_channel,biz_occurred_at,received_at,created_by) VALUES(1,?,?,?,?,?,?, 'im_first',?, ?,?)",
                (r.get("category", "other") if r.get("category") in CATS else "other",
                 content or f"[导入缺项] external_no={ext}", owner_id, status, src, ext, biz, now(), user["id"]))
            item_id = cur.lastrowid
            db().execute("INSERT INTO id_mappings(source_tool,external_no,item_id,mapped_by) VALUES(?,?,?,?)",
                         (src, ext, item_id, user["id"]))
            change(item_id, "status", None, status, user["id"], "import")
            created += 1
        db().commit()
    audit(user["id"], "import", "batch", 1, tver=src, suggestion=f"CSV 导入（{SOURCE_NAME[src]}）：新建 {created}，幂等跳过 {skipped}")
    return {"created": created, "skipped": skipped, "source_tool": src}


@app.get("/api/board")
async def board(request: Request):
    user, e = need(request)
    if e:
        return e
    batch_id = int(request.query_params.get("batch_id", "1"))
    b = db().execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if b is None:
        return err("404", "批次不存在", 404)
    total = db().execute("SELECT COUNT(*) FROM requirement_items WHERE batch_id=?", (batch_id,)).fetchone()[0]
    dist = {s: db().execute("SELECT COUNT(*) FROM requirement_items WHERE batch_id=? AND status=?", (batch_id, s)).fetchone()[0]
            for s in STATUSES}
    # M6 确认完整率：责任人 + 确认状态 + 变更可追溯（f02/f42）
    m6_num = db().execute(
        "SELECT COUNT(*) FROM requirement_items r WHERE batch_id=? AND owner_id IS NOT NULL "
        "AND status IN ('confirmed','finalized','handed_over') "
        "AND EXISTS(SELECT 1 FROM item_changes ic WHERE ic.item_id=r.id)", (batch_id,)).fetchone()[0]
    # M7 可追溯率：有来源记录（来源工具+原始行定位）
    m7_num = db().execute(
        "SELECT COUNT(*) FROM requirement_items WHERE batch_id=? AND source_row_ref IS NOT NULL AND source_row_ref<>''",
        (batch_id,)).fetchone()[0]
    planned = b["planned_items"] or 50
    dual_ts = db().execute(
        "SELECT COUNT(*) FROM requirement_items WHERE batch_id=? AND biz_occurred_at<>received_at", (batch_id,)).fetchone()[0]
    exc_open = db().execute("SELECT COUNT(*) FROM exception_queue WHERE status<>'closed'").fetchone()[0]
    return {"batch": dict(b), "total": total, "status_dist": {STATUS_NAME[s]: dist[s] for s in STATUSES},
            "M6": {"numerator": m6_num, "denominator": planned,
                   "rate": round(m6_num / planned * 100, 1) if planned else 0},
            "M7": {"numerator": m7_num, "denominator": total or 1,
                   "rate": round(m7_num / (total or 1) * 100, 1)},
            "dual_timestamp_distinct": dual_ts, "exception_open": exc_open}


@app.get("/api/audit")
async def audit_list(request: Request):
    user, e = need(request, P_AUDIT)
    if e:
        return e
    rows = db().execute(
        "SELECT a.*, u.display_name AS actor, c.display_name AS confirmer FROM audit_logs a "
        "LEFT JOIN users u ON u.id=a.actor_id LEFT JOIN users c ON c.id=a.confirmed_by "
        "ORDER BY a.id DESC LIMIT 200").fetchall()
    return {"audit": [dict(r) for r in rows]}


@app.get("/api/versions")
async def list_versions(request: Request):
    user, e = need(request)
    if e:
        return e
    vs = db().execute("SELECT * FROM versions ORDER BY id DESC").fetchall()
    hs = db().execute(
        "SELECT h.*, u.display_name AS receiver, v.version_no FROM handovers h "
        "LEFT JOIN users u ON u.id=h.receiver_id LEFT JOIN versions v ON v.id=h.version_id ORDER BY h.id DESC").fetchall()
    return {"versions": [dict(v) for v in vs], "handovers": [dict(h) for h in hs]}


@app.get("/api/users")
async def users(request: Request):
    user, e = need(request)
    if e:
        return e
    rows = db().execute("SELECT id,username,display_name,role,is_active FROM users ORDER BY id").fetchall()
    return {"users": [dict(r) for r in rows]}


# ---------------------------------------------------------------- Agent 问答（零 LLM 规则 Harness）
@app.get("/api/ask")
async def api_ask(request: Request):
    """Agent 问答入口：Harness 编排（decide→invoke→observe），知识库检索+看板统计。

    红线落地：本接口只读——只做检索与统计，不产生任何确认/定版/交接动作（f22 模型无审批权）；
    知识库未覆盖自动转人工（人工审核点④的问答侧兜底）。调用留痕进审计表。
    """
    user, e = need(request)
    if e:
        return e
    q = request.query_params.get("q", "")
    try:
        from f24015105_agent_harness import ask as harness_ask
        result = harness_ask(q, role=user["role"], db=db())
    except ImportError:
        return JSONResponse({"code": "E_AGENT_OFF", "message": "Agent 模块未部署（一期可独立运行）"}, status_code=501)
    audit(user["id"], "suggest", "ask", suggestion=f"问答：{q[:60]}")
    db().commit()
    return {"question": q, "role": user["role"], **result}


# ---------------------------------------------------------------- 页面
PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>会议活动服务需求汇总系统 MVP</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f4f6f9;color:#223}
header{background:#1f3a5f;color:#fff;padding:12px 20px;display:flex;gap:16px;align-items:center}
header h1{font-size:18px;margin:0;flex:1}
nav button{margin-right:6px;padding:6px 12px;border:0;border-radius:4px;cursor:pointer;background:#31517c;color:#fff}
nav button.on{background:#fff;color:#1f3a5f}
main{padding:16px;max-width:1100px;margin:0 auto}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px}
th,td{border:1px solid #dde3ea;padding:6px 8px;text-align:left}
th{background:#e8eef6}
.card{background:#fff;border:1px solid #dde3ea;border-radius:6px;padding:12px 16px;margin-bottom:14px}
input,select,textarea{padding:5px;border:1px solid #bcc6d2;border-radius:4px;margin:2px 4px 2px 0}
button.act{padding:5px 12px;border:0;border-radius:4px;background:#1f3a5f;color:#fff;cursor:pointer}
.kpi{display:flex;gap:12px;flex-wrap:wrap}
.kpi div{background:#eef3fa;border-radius:6px;padding:10px 16px;font-size:13px}
.kpi b{font-size:22px;color:#1f3a5f;display:block}
#msg{position:fixed;top:12px;right:12px;background:#223;color:#fff;padding:8px 14px;border-radius:6px;display:none}
.tag{padding:1px 6px;border-radius:3px;font-size:12px}
</style></head><body>
<header><h1>会议活动服务需求汇总系统 · MVP</h1>
<span id="who"></span><button class="act" onclick="logout()" style="background:#5b6676">退出</button></header>
<nav style="padding:10px 20px;background:#fff;border-bottom:1px solid #dde3ea">
<button data-p="items" class="on">登记与清单</button><button data-p="ver">版本与交接</button>
<button data-p="exc">异常队列</button><button data-p="board">看板</button><button data-p="audit">审计留痕</button>
</nav><main id="view"></main><div id="msg"></div>
<script>
let TOKEN=localStorage.getItem('mtoken')||'';let ME=null;
const H=()=>({Authorization:'Bearer '+TOKEN,'Content-Type':'application/json'});
async function api(path,method,body){const r=await fetch('/api'+path,{method:method||'GET',headers:H(),body:body?JSON.stringify(body):undefined});
 const j=await r.json().catch(()=>({}));if(r.status===401&&path!=='/login'){show('登录失效，请重新登录');tab('items');return{j,_e:1}}
 if(!r.ok){show(j.message||('HTTP '+r.status));j._e=1}return j}
function show(t){const m=document.getElementById('msg');m.textContent=t;m.style.display='block';setTimeout(()=>m.style.display='none',3200)}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
const TAG=s=>{const m={'缺失':'#c0392b','未确认':'#e67e22','已确认':'#27ae60','已定版':'#2980b9','已交接':'#7f8c8d'};return `<span class="tag" style="background:${m[s]||'#999'}22;color:${m[s]||'#555'}">${esc(s)}</span>`};
async function logout(){await fetch('/api/logout',{method:'POST'});localStorage.removeItem('mtoken');TOKEN='';render('items')}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');render(b.dataset.p)});
async function render(p){if(!TOKEN){return loginPage()}const m=await api('/me');if(m._e)return;ME=m.user;
 document.getElementById('who').textContent=`${ME.display_name}（${ME.role_name}）`;
 ({items:pgItems,ver:pgVer,exc:pgExc,board:pgBoard,audit:pgAudit}[p]||pgItems)()}
function loginPage(){document.getElementById('view').innerHTML=`<div class="card" style="max-width:360px;margin:60px auto">
 <h3>登录</h3>账号 <input id="u" value="lin"><br>密码 <input id="p" type="password" value="123456"><br>
 <button class="act" onclick="doLogin()">登录</button><p style="font-size:12px;color:#5b6676">五角色：lin 一线 / tang 协调 / guml 业务负责人 / fujing 质量 / qinrui 管理员，密码 123456</p></div>`}
async function doLogin(){const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({username:u.value,password:p.value})});const j=await r.json();
 if(!r.ok)return show(j.message);TOKEN=j.token;localStorage.setItem('mtoken',TOKEN);render('items')}
async function pgItems(){const j=await api('/items');const rows=j.items.map(i=>`<tr><td>${i.id}</td><td>${esc(i.category)}</td>
 <td>${esc(i.content.slice(0,40))}</td><td>${esc(i.owner_name||'—')}</td><td>${TAG(STATUS_NAME[i.status]||i.status)}</td>
 <td>${esc(i.source_tool)}/${esc(i.source_row_ref||'—')}</td><td>${esc(i.biz_occurred_at.slice(0,16))}</td><td>${esc(i.received_at.slice(0,16))}</td>
 <td>${['missing','unconfirmed'].includes(i.status)&&ME.role==='biz_owner'?`<button class="act" onclick="confirmItem(${i.id})">确认</button>`:''}
 <button class="act" style="background:#5b6676" onclick="addExc(${i.id})">报异常</button></td></tr>`).join('');
 document.getElementById('view').innerHTML=`<div class="card"><b>登记需求项</b><br>
 类别 <select id="f_cat">${['equipment','language','catering','venue','other'].map(c=>`<option>${c}</option>`).join('')}</select>
 描述 <input id="f_ct" size="40" placeholder="需求描述"> 责任人 <select id="f_ow"><option value="">（缺失）</option></select>
 来源 <select id="f_src">${['servicedesk','ledger','filelib','manual'].map(c=>`<option>${c}</option>`).join('')}</select>
 业务发生时间 <input id="f_tm" placeholder="YYYY-MM-DD HH:MM" size="16">
 <button class="act" onclick="addItem()">登记</button></div>
 <div class="card"><b>需求项清单（双时间戳）</b><table><tr><th>ID</th><th>类别</th><th>描述</th><th>责任人</th><th>五态</th><th>来源/行</th><th>业务发生</th><th>系统接收</th><th>操作</th></tr>${rows}</table></div>`;
 const us=await api('/users');f_ow.innerHTML+='<option value="0">（缺失）</option>'+us.users.filter(u=>u.is_active).map(u=>`<option value="${u.id}">${u.display_name}</option>`).join('')}
async function addItem(){const j=await api('/items','POST',{category:f_cat.value,content:f_ct.value,owner_id:f_ow.value==='0'?'':f_ow.value,source_tool:f_src.value,biz_occurred_at:f_tm.value});if(!j._e){show(`已登记 #${j.id}（${j.status==='missing'?'缺失':'未确认'}）`);pgItems()}}
async function confirmItem(id){const j=await api(`/items/${id}/confirm`,'POST',{});if(!j._e){show(`#${id} 已确认签字`);pgItems()}}
async function addExc(id){const t=prompt('异常说明（作为建议快照留痕）：');if(t===null)return;const j=await api('/exceptions','POST',{item_id:id,exception_type:'dirty_data',note:t});if(!j._e)show('已入异常队列')}
async function pgVer(){const[vs,its,us]=await Promise.all([api('/versions'),api('/items'),api('/users')]);
 const vrows=(vs.versions||[]).map(v=>`<tr><td>${v.id}</td><td><b>${esc(v.version_no)}</b></td><td>${esc(v.status)}</td><td>${esc(v.valid_until||'—')}</td>
 <td>${v.status==='draft'&&ME.role==='coordinator'?`<button class="act" onclick="finVer(${v.id})">定版</button>`:''}</td></tr>`).join('');
 const hrows=(vs.handovers||[]).map(h=>`<tr><td>${h.id}</td><td>${esc(h.version_no)}</td><td>${esc(h.receiver)}</td><td>${h.open_items_count}</td><td>${esc(h.deadline)}</td><td>${h.ack_by?'已接收':'待接收'}</td>
 <td>${!h.ack_by&&h.receiver_id===ME.id?`<button class="act" onclick="ackH(${h.id})">确认接收</button>`:''}</td></tr>`).join('');
 const cands=its.items.filter(i=>['unconfirmed','confirmed'].includes(i.status));
 document.getElementById('view').innerHTML=`<div class="card"><b>创建版本（系统生成版本号，不可编辑）</b><br>
 有效期 <input id="v_vu" placeholder="YYYY-MM-DD" size="10">
 <select id="v_items" multiple size="5">${cands.map(i=>`<option value="${i.id}">#${i.id} ${esc(i.content.slice(0,20))}（${STATUS_NAME[i.status]}）</option>`).join('')}</select>
 <button class="act" onclick="mkVer()">创建草稿</button><table><tr><th>ID</th><th>版本号</th><th>状态</th><th>有效期</th><th>操作</th></tr>${vrows}</table></div>
 <div class="card"><b>发起交接（三要素卡口 f20）</b><br>
 版本 <select id="h_ver">${(vs.versions||[]).filter(v=>v.status==='finalized').map(v=>`<option value="${v.id}">${esc(v.version_no)}</option>`).join('')}</select>
 接收人 <select id="h_rcv">${us.users.filter(u=>u.is_active).map(u=>`<option value="${u.id}">${u.display_name}</option>`).join('')}</select>
 未结项数 <input id="h_open" size="4"> 截止 <input id="h_dl" placeholder="YYYY-MM-DD" size="10">
 <button class="act" onclick="mkH()">发起交接</button><table><tr><th>ID</th><th>版本</th><th>接收人</th><th>未结项</th><th>截止</th><th>接收</th><th></th></tr>${hrows}</table></div>`}
async function mkVer(){const ids=[...v_items.selectedOptions].map(o=>+o.value);const j=await api('/versions','POST',{item_ids:ids,valid_until:v_vu.value});if(!j._e){show(`草稿 ${j.version_no} 已创建`);pgVer()}}
async function finVer(id){const j=await api(`/versions/${id}/finalize`,'POST',{});if(!j._e){show(`${j.version_no} 已定版`);pgVer()}}
async function mkH(){const j=await api('/handovers','POST',{version_id:+h_ver.value,receiver_id:+h_rcv.value,open_items_count:h_open.value===''?undefined:+h_open.value,deadline:h_dl.value});if(!j._e){show(`交接成功，${j.handed_items} 项转为已交接`);pgVer()}}
async function ackH(id){const j=await api(`/handovers/${id}/ack`,'POST',{});if(!j._e){show('已确认接收');pgVer()}}
async function pgExc(){const j=await api('/exceptions');const rows=j.exceptions.map(q=>`<tr><td>${q.id}</td><td>#${q.item_id}</td>
 <td>${esc(q.exception_type)}</td><td>${esc(q.note||'')}</td><td>${esc(q.status)}</td><td>${esc(q.creator||'')}</td>
 <td>${esc(q.resolution||'—')}</td>
 <td>${q.status==='open'&&['qa','admin'].includes(ME.role)?`<button class="act" onclick="claim(${q.id})">认领</button>`:''}
 ${q.status==='processing'&&ME.role==='qa'?`<button class="act" onclick="closeE(${q.id})">闭环</button>`:''}</td></tr>`).join('');
 document.getElementById('view').innerHTML=`<div class="card"><b>异常人工队列（不得默认通过 f44；闭环仅质量验收）</b>
 <table><tr><th>ID</th><th>需求项</th><th>类型</th><th>说明</th><th>状态</th><th>提出人</th><th>复核结论</th><th>操作</th></tr>${rows}</table></div>`}
async function claim(id){const j=await api(`/exceptions/${id}/claim`,'POST',{});if(!j._e){show('已认领');pgExc()}}
async function closeE(id){const t=prompt('复核结论（必填）：');if(t===null)return;const j=await api(`/exceptions/${id}/close`,'POST',{resolution:t});if(!j._e){show('已闭环');pgExc()}}
async function pgBoard(){const j=await api('/board');
 document.getElementById('view').innerHTML=`<div class="kpi">
 <div>批次<b>${esc(j.batch.name)}</b></div><div>M6 确认完整率<b>${j.M6.rate}%</b><span>${j.M6.numerator}/${j.M6.denominator}</span></div>
 <div>M7 可追溯率<b>${j.M7.rate}%</b><span>${j.M7.numerator}/${j.M7.denominator}</span></div>
 <div>双时间戳可区分<b>${j.dual_timestamp_distinct}</b>项</div><div>异常未闭环<b>${j.exception_open}</b></div></div>
 <div class="card"><b>五态分布</b><table><tr>${Object.keys(j.status_dist).map(k=>`<th>${k}</th>`).join('')}</tr>
 <tr>${Object.values(j.status_dist).map(v=>`<td style="font-size:20px"><b>${v}</b></td>`).join('')}</tr></table></div>`}
async function pgAudit(){const j=await api('/audit');if(j._e)return;const rows=j.audit.map(a=>`<tr><td>${a.id}</td><td>${esc(a.created_at)}</td>
 <td>${esc(a.actor||'')}</td><td>${esc(a.action)}</td><td>${esc(a.target_type)}#${a.target_id??''}</td><td>${esc(a.target_version||'')}</td>
 <td>${esc(a.suggestion_snapshot||'')}</td><td>${esc(a.confirmer||'—')}</td></tr>`).join('');
 document.getElementById('view').innerHTML=`<div class="card"><b>审计留痕（谁查过/哪个版本/什么建议/谁确认，f44/f45）</b>
 <table><tr><th>ID</th><th>时间</th><th>谁</th><th>动作</th><th>对象</th><th>版本</th><th>建议快照</th><th>谁确认</th></tr>${rows}</table></div>`}
render('items');
</script></body></html>"""


@app.get("/api/logout")
@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("mtoken")
    return resp


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return PAGE


def ensure_demo_data() -> None:
    """空库自动灌入演示数据（沙箱部署时无预跑 seed 的机会）：五角色 + 批次 + 50 项样本。"""
    with _lock:
        if db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            import hashlib as _h
            for uname, disp, role in (("lin", "林亦舟", "staff"), ("tang", "唐映秋", "coordinator"),
                                      ("guml", "顾明岚", "biz_owner"), ("fujing", "傅景", "qa"),
                                      ("qinrui", "秦芮", "admin")):
                h = _h.sha256(f"{uname}:123456".encode()).hexdigest()
                db().execute("INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
                             (uname, h, disp, role))
        if not db().execute("SELECT 1 FROM batches WHERE id=1").fetchone():
            db().execute("INSERT INTO batches(id,name,planned_items) VALUES(1,'单批次试点',50)")
        if db().execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0] == 0:
            cats = ["equipment", "language", "catering", "venue", "other"]
            for i in range(1, 51):
                dirty = i > 45
                owner = None if dirty else (1 if i % 2 else 2)
                status = "missing" if dirty else ("confirmed" if i <= 10 else "unconfirmed")
                src = ["servicedesk", "ledger", "filelib", "manual"][i % 4]
                db().execute(
                    "INSERT INTO requirement_items(id,batch_id,category,content,owner_id,status,source_tool,"
                    "source_row_ref,record_channel,biz_occurred_at,received_at,confirmed_by,confirmed_at,created_by) "
                    "VALUES(?, 1, ?, ?, ?, ?, ?, ?, 'im_first', ?, ?, ?, ?, ?)",
                    (i, cats[i % 5], f"演示样本需求项 #{i}" + ("（脏数据留原行）" if dirty else ""),
                     owner, status, src, f"{src[:2].upper()}-{1000 + i}",
                     f"2026-11-{(i % 5) + 1:02d} 0{i % 9}:30:00", f"2026-11-06 0{i % 8}:30:00",
                     3 if status == "confirmed" else None,
                     "2026-11-06 09:00:00" if status == "confirmed" else None, 1))
                db().execute("INSERT INTO item_changes(item_id,field_name,old_value,new_value,changed_by,change_source) "
                             "VALUES(?, 'status', NULL, ?, 1, 'import')", (i, status))
                db().execute("INSERT OR IGNORE INTO id_mappings(source_tool,external_no,item_id,mapped_by) VALUES(?,?,?,1)",
                             (src, f"{src[:2].upper()}-{1000 + i}", i))
        db().commit()


@app.on_event("startup")
def _startup():
    init_schema()
    ensure_demo_data()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT") or os.environ.get("MEETING_MVP_PORT") or "8800")
    uvicorn.run(app, host="0.0.0.0", port=port)
