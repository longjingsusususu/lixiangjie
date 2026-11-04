# -*- coding: utf-8 -*-
"""
admin.py —— 管理后台（SSR，Jinja2）

批 1 内容：
  P1 /login         登录页（表单 POST，写 HttpOnly cookie）
  P2 /admin         概览（数据卡 + 近 7 天查询柱状）
  P7 /admin/users   用户与角色（改角色、停用/启用，不能改自己）
  API /api/admin/users       列表
  API /api/admin/users/{id}  改角色/停用

权限模型：五角色 staff / coordinator / biz_owner / qa / admin。
  · 页面与接口都在服务端二次校验（隐藏入口不是权限）；
  · JWT 只放 uid，角色每次查库（改角色即时生效）；
  · 管理员不能修改自己的角色（防自提权）。

批 2/3/4 会在此文件追加：需求项库、导入、映射、规则配置、审计。
"""

import csv
import io
import json
import os
import re
import time

import jwt
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# 需求项字典（批 3 会改成从 rule_configs 表读，改配置即时生效）
CATEGORIES = ["设备", "语言服务", "餐饮", "场地", "其他"]
STATUSES = ["缺失", "未确认", "已确认", "已定版", "已交接"]
ITEM_NO_RE = re.compile(r"^[A-Z]{2}-\d{2,3}$")
DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
MAX_UPLOAD = 2 * 1024 * 1024      # 2MB

# 角色中文名（模板里直接用）
ROLE_CN = {"staff": "一线专员", "coordinator": "协调员",
           "biz_owner": "业务负责人", "qa": "质量验收", "admin": "管理员"}


def _p():
    """延迟导入 portal，避免循环引用（portal 在文件末尾才 include 本路由）。"""
    import portal
    return portal


def page_ctx(request: Request, user: dict, active: str, **kw) -> dict:
    """所有后台页面的公共上下文：当前用户、角色中文名、侧栏高亮项。"""
    return {"request": request, "user": user, "role_cn": ROLE_CN.get(user["role"], user["role"]),
            "active": active, "roles": ROLE_CN, **kw}


def require_page(*roles):
    """页面权限：未登录 → 跳登录页；登录但角色不符 → 403 页面（明示无权，不假装 404）。"""
    def dep(request: Request):
        p = _p()
        try:
            user = p.current_user(request)
        except Exception:
            raise _Forbidden(login=True)
        if roles and user["role"] not in roles:
            raise _Forbidden(user=user)
        return user
    return dep


def require_api(*roles):
    """接口权限：统一返回 401/403 + 中文码，不跳转。
    注意：不要一把梭 except Exception——会把 current_user 抛的 40103（账号停用）
    吞成 40101，前端提示就错了。HTTPException 必须原样放行。"""
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
            # 记一次越权尝试：审计页 R3 规则（近 7 天 ≥3 次）靠它判定
            with p.db() as conn:
                conn.execute("INSERT INTO audit_logs(actor_id,action,target_type,target_id,"
                             "created_at) VALUES(?,?,?,?,?)",
                             (user["id"], "forbidden", "api", str(request.url.path), p.now()))
                conn.commit()
            p.fail(403, 40302)
        return user
    return dep


class _Forbidden(Exception):
    """页面级鉴权失败：login=True 表示未登录；否则是角色不足。"""
    def __init__(self, login=False, user=None):
        self.login = login
        self.user = user


def _forbidden_response(request: Request, exc: _Forbidden):
    if exc.login:
        return RedirectResponse("/login?next=" + request.url.path, status_code=302)
    return templates.TemplateResponse(
        request, "403.html",
        page_ctx(request, exc.user or {"display_name": "—", "role": "staff"}, ""),
        status_code=403)


# ============================================================
# P1 登录页
# ============================================================
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/admin"):
    return templates.TemplateResponse(request, "login.html",
                                      {"next": next, "error": ""})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""),
                       password: str = Form(""), next: str = Form("/admin")):
    p = _p()
    with p.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
        if row is None or row["password_hash"] != p.hash_pwd(password):
            return templates.TemplateResponse(
                request, "login.html",
                {"next": next, "error": "[40102] 用户名或密码错误"}, status_code=401)
        if not row["is_active"]:
            return templates.TemplateResponse(
                request, "login.html",
                {"next": next, "error": "[40103] 账号已停用，请联系管理员"}, status_code=403)
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (p.now(), row["id"]))
        conn.commit()
        # 只允许管理员进后台；业务角色登录后回员工端（避免"进了后台什么都点不了"）
        target = next if row["role"] == "admin" else "/"
    token = jwt.encode({"uid": row["id"], "exp": int(time.time()) + p.TOKEN_HOURS * 3600},
                       p.JWT_SECRET, algorithm=p.JWT_ALG)
    resp = RedirectResponse(target, status_code=302)
    resp.set_cookie("mt_token", token, httponly=True, samesite="lax",
                    max_age=p.TOKEN_HOURS * 3600)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("mt_token")
    return resp


# ============================================================
# P2 概览
# ============================================================
@router.get("/admin", response_class=HTMLResponse)
async def overview(request: Request, user=Depends(require_page("admin"))):
    p = _p()
    with p.db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM requirement_items WHERE is_deleted=0").fetchone()["c"]
        missing = conn.execute("SELECT COUNT(*) c FROM requirement_items "
                               "WHERE is_deleted=0 AND status='缺失'").fetchone()["c"]
        unconfirmed = conn.execute("SELECT COUNT(*) c FROM requirement_items "
                                   "WHERE is_deleted=0 AND status='未确认'").fetchone()["c"]
        today = p.now()[:10]
        today_q = conn.execute("SELECT COUNT(*) c FROM messages WHERE role='user' "
                               "AND substr(created_at,1,10)=?", (today,)).fetchone()["c"]
        flagged = conn.execute("SELECT COUNT(*) c FROM item_flags WHERE revoked_at IS NULL").fetchone()["c"]
        # 近 7 天查询量（纯 CSS 柱状，不引图表库）
        rows = conn.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) c FROM messages "
            "WHERE role='user' GROUP BY d ORDER BY d DESC LIMIT 7").fetchall()
        trend = [{"d": r["d"][5:], "c": r["c"]} for r in reversed(rows)]
        max_c = max([t["c"] for t in trend], default=1)
        for t in trend:
            t["h"] = int(t["c"] / max_c * 100) if max_c else 0

    return templates.TemplateResponse(request, "overview.html", page_ctx(
        request, user, "overview",
        cards=[("需求项总数", total, "入库且未删除"),
               ("缺失", missing, "字段残缺，连确认都无从谈起"),
               ("未确认", unconfirmed, "有内容，等责任人签字"),
               ("今日查询", today_q, "员工端提问次数")],
        flagged=flagged, trend=trend, today=today))


# ============================================================
# P7 用户与角色
# ============================================================
@router.get("/admin/users", response_class=HTMLResponse)
async def users_page(request: Request, user=Depends(require_page("admin"))):
    p = _p()
    with p.db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return templates.TemplateResponse(request, "users.html", page_ctx(
        request, user, "users", users=[dict(r) for r in rows]))


@router.get("/api/admin/users")
async def api_users(user=Depends(require_api("admin"))):
    p = _p()
    with p.db() as conn:
        rows = conn.execute(
            "SELECT id,username,display_name,role,is_active,created_at,last_login_at "
            "FROM users ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows], "roles": ROLE_CN}


@router.patch("/api/admin/users/{uid}")
async def api_update_user(uid: int, request: Request, user=Depends(require_api("admin"))):
    """改角色 / 停用启用。硬规则：不能改自己（防自提权、防自锁死）。"""
    p = _p()
    body = await request.json()
    role = body.get("role")
    active = body.get("is_active")
    if role is not None and role not in ROLE_CN:
        p.fail(400, 40001, message="角色取值非法")
    if uid == user["id"] and (role is not None or active is not None):
        p.fail(403, 40302, message="不能修改自己的角色或启用状态（防自提权）")
    with p.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            p.fail(404, 40401, message="用户不存在")
        fields, args = [], []
        if role is not None:
            fields.append("role=?")
            args.append(role)
        if active is not None:
            fields.append("is_active=?")
            args.append(1 if active else 0)
        if not fields:
            p.fail(400, 40001, message="没有需要修改的字段")
        args.append(uid)
        conn.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?", args)
        conn.commit()
    return {"ok": True, "uid": uid}


# ============================================================
# 批 2：需求项库（P3）+ 批量导入（P4）+ 编号映射
# ============================================================
CSV_COLS = ["item_no", "category", "content", "status", "owner", "source_tool",
            "source_ref", "biz_occurred_at", "received_at", "change_note"]


def _rows_from_csv(text: str) -> list:
    """CSV → 行列表；line 记录原始行号（预检报告要能指到具体一行）。"""
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for i, row in enumerate(reader, start=2):     # 第 1 行是表头
        rows.append({"line": i, **{c: (row.get(c) or "").strip() for c in CSV_COLS}})
    return rows


def _rows_from_md(text: str) -> list:
    """
    md → 行列表。
    注意：不能直接用阶段 1 的解析器当唯一来源——它只认合规编号（EQ-01），
    编号写错的条目会被**静默丢弃**，用户以为导入了其实没有。
    这里按"任意 [xxx] 标题"逐条扫描：能匹配的取解析结果，匹配不上的也产出一行，
    交给预检去报"编号格式错误 + 行号"，绝不悄悄吞数据。
    """
    p = _p()
    parsed = {it.item_no: it for it in p.parse_requirements(text)}
    rows = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"^#{2,4}\s*\[([^\]]+)\]\s*(.+)$", line.strip())
        if not m:
            continue
        iid = m.group(1).strip()
        it = parsed.get(iid)
        if it:
            rows.append({"line": i, "item_no": it.item_no, "category": it.category,
                         "content": it.content.strip(), "status": it.status, "owner": it.owner,
                         "source_tool": it.source_tool, "source_ref": it.source_ref,
                         "biz_occurred_at": it.biz_occurred_at, "received_at": it.received_at,
                         "change_note": it.change_note})
        else:
            rows.append({"line": i, "item_no": iid, "category": "", "content": m.group(2).strip(),
                         "status": "", "owner": "", "source_tool": "", "source_ref": "",
                         "biz_occurred_at": "", "received_at": "", "change_note": ""})
    return rows


def _precheck(rows: list) -> dict:
    """
    行级预检：编号格式 / 类别 / 五态 / 日期格式 / 内容非空。
    重复编号不算错误，单独统计为"跳过"（幂等导入，重复跑不会产生副本）。
    """
    p = _p()
    with p.db() as conn:
        existing = {r["item_no"] for r in conn.execute(
            "SELECT item_no FROM requirement_items")}
    seen, good, errors, dups = set(), [], [], []
    for r in rows:
        reasons = []
        if not ITEM_NO_RE.match(r["item_no"] or ""):
            reasons.append("编号格式应为「两字母-两位数字」，如 EQ-01")
        if r["category"] not in CATEGORIES:
            reasons.append(f"类别须为：{'/'.join(CATEGORIES)}")
        if r["status"] not in STATUSES:
            reasons.append(f"状态须为：{'/'.join(STATUSES)}")
        if not r["content"]:
            reasons.append("内容不能为空")
        for f in ("biz_occurred_at", "received_at"):
            if r[f] and not DT_RE.match(r[f]):
                reasons.append(f"{f} 格式应为 YYYY-MM-DD HH:MM")
        if reasons:
            errors.append({"line": r["line"], "item_no": r["item_no"] or "—",
                           "reason": "；".join(reasons)})
            continue
        if r["item_no"] in existing or r["item_no"] in seen:
            dups.append({"line": r["line"], "item_no": r["item_no"]})
            continue
        seen.add(r["item_no"])
        good.append(r)
    return {"total": len(rows), "ok": len(good), "dup": len(dups),
            "errors": errors, "duplicates": dups, "preview": good[:8], "_good": good}


@router.get("/admin/items", response_class=HTMLResponse)
async def items_page(request: Request, q: str = "", status: str = "", category: str = "",
                     page: int = 1, user=Depends(require_page("admin"))):
    p = _p()
    size, page = 20, max(1, page)
    where, args = ["is_deleted=0"], []
    if q:
        where.append("(item_no LIKE ? OR content LIKE ? OR owner LIKE ?)")
        args += [f"%{q}%"] * 3
    if status:
        where.append("status=?")
        args.append(status)
    if category:
        where.append("category=?")
        args.append(category)
    w = " AND ".join(where)
    with p.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM requirement_items WHERE {w}",
                             args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM requirement_items WHERE {w} ORDER BY item_no LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size]).fetchall()
        counts = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM requirement_items WHERE is_deleted=0 GROUP BY status")}
    pages = (total + size - 1) // size
    return templates.TemplateResponse(request, "items.html", page_ctx(
        request, user, "items", items=[dict(r) for r in rows], total=total,
        page=page, pages=pages, q=q, status=status, category=category,
        categories=CATEGORIES, statuses=STATUSES, counts=counts, items_ready=True))


@router.get("/admin/import", response_class=HTMLResponse)
async def import_page(request: Request, user=Depends(require_page("admin"))):
    p = _p()
    with p.db() as conn:
        maps = [dict(r) for r in conn.execute(
            "SELECT * FROM id_mappings ORDER BY id DESC LIMIT 50")]
    return templates.TemplateResponse(request, "import.html", page_ctx(
        request, user, "import", mappings=maps, import_ready=True,
        categories=CATEGORIES, statuses=STATUSES))


@router.get("/api/admin/items")
async def api_items(request: Request, q: str = "", status: str = "", category: str = "",
                    page: int = 1, size: int = 20, user=Depends(require_api("admin"))):
    p = _p()
    size = min(max(size, 1), 100)
    where, args = ["is_deleted=0"], []
    if q:
        where.append("(item_no LIKE ? OR content LIKE ?)")
        args += [f"%{q}%"] * 2
    if status:
        where.append("status=?")
        args.append(status)
    if category:
        where.append("category=?")
        args.append(category)
    w = " AND ".join(where)
    with p.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM requirement_items WHERE {w}",
                             args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM requirement_items WHERE {w} ORDER BY item_no LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size]).fetchall()
    return {"total": total, "page": page, "size": size,
            "items": [dict(r) for r in rows], "categories": CATEGORIES, "statuses": STATUSES}


@router.post("/api/admin/items")
async def api_create_item(request: Request, user=Depends(require_api("admin"))):
    p = _p()
    b = await request.json()
    item_no = (b.get("item_no") or "").strip().upper()
    if not ITEM_NO_RE.match(item_no):
        p.fail(400, 40001, message="编号格式应为「两字母-两位数字」，如 EQ-07")
    if b.get("category") not in CATEGORIES:
        p.fail(400, 40001, message=f"类别须为：{'/'.join(CATEGORIES)}")
    if b.get("status") not in STATUSES:
        p.fail(400, 40001, message=f"状态须为：{'/'.join(STATUSES)}")
    with p.db() as conn:
        if conn.execute("SELECT 1 FROM requirement_items WHERE item_no=?", (item_no,)).fetchone():
            p.fail(409, 40901, message=f"编号 {item_no} 已存在")
        conn.execute(
            "INSERT INTO requirement_items(item_no,category,content,status,owner,source_tool,"
            "source_ref,biz_occurred_at,received_at,change_note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item_no, b.get("category"), (b.get("content") or "").strip(), b.get("status"),
             b.get("owner") or "", b.get("source_tool") or "", b.get("source_ref") or "",
             b.get("biz_occurred_at") or "", b.get("received_at") or p.now(),
             b.get("change_note") or ""))
        conn.execute("INSERT INTO item_changes(item_no,field,old_value,new_value,changed_by,created_at) "
                     "VALUES(?,?,?,?,?,?)", (item_no, "*", "", "新增", user["id"], p.now()))
        conn.commit()
    p.rebuild_index()                            # 新增后立刻可被检索到
    return {"ok": True, "item_no": item_no}


@router.patch("/api/admin/items/{item_no}")
async def api_update_item(item_no: str, request: Request, user=Depends(require_api("admin"))):
    """编辑需求项：逐字段比对写 item_changes（谁改的、改前改后），改完重建索引。"""
    p = _p()
    b = await request.json()
    editable = ["category", "content", "status", "owner", "source_tool", "source_ref",
                "biz_occurred_at", "received_at", "change_note"]
    with p.db() as conn:
        row = conn.execute("SELECT * FROM requirement_items WHERE item_no=?", (item_no,)).fetchone()
        if row is None:
            p.fail(404, 40401, message="需求项不存在")
        sets, args, changes = [], [], []
        for f in editable:
            if f in b and str(b[f]) != str(row[f] or ""):
                sets.append(f"{f}=?")
                args.append(b[f])
                changes.append((f, row[f], b[f]))
        if not sets:
            return {"ok": True, "changed": 0}
        conn.execute(f"UPDATE requirement_items SET {','.join(sets)} WHERE item_no=?",
                     args + [item_no])
        for f, old, new in changes:
            conn.execute("INSERT INTO item_changes(item_no,field,old_value,new_value,changed_by,"
                         "created_at) VALUES(?,?,?,?,?,?)",
                         (item_no, f, str(old or ""), str(new or ""), user["id"], p.now()))
        conn.commit()
    p.rebuild_index()
    return {"ok": True, "changed": len(changes)}


@router.delete("/api/admin/items/{item_no}")
async def api_delete_item(item_no: str, user=Depends(require_api("admin"))):
    """软删：数据保留，审计可回查；检索立刻剔除。不提供硬删。"""
    p = _p()
    with p.db() as conn:
        row = conn.execute("SELECT * FROM requirement_items WHERE item_no=?", (item_no,)).fetchone()
        if row is None:
            p.fail(404, 40401, message="需求项不存在")
        conn.execute("UPDATE requirement_items SET is_deleted=1 WHERE item_no=?", (item_no,))
        conn.execute("INSERT INTO item_changes(item_no,field,old_value,new_value,changed_by,"
                     "created_at) VALUES(?,?,?,?,?,?)",
                     (item_no, "is_deleted", "0", "1", user["id"], p.now()))
        conn.commit()
    p.rebuild_index()
    return {"ok": True}


@router.post("/api/admin/items/import")
async def api_import(request: Request, file: UploadFile = File(...),
                     dry_run: str = Form("true"), user=Depends(require_api("admin"))):
    """导入：dry_run=true 只出预检报告（不写库）；false 执行导入，幂等。"""
    p = _p()
    name = (file.filename or "").lower()
    if not (name.endswith(".csv") or name.endswith(".md")):
        p.fail(415, 41501)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        p.fail(413, 41301)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        p.fail(422, 42201, message="文件需为 UTF-8 编码")

    try:
        rows = _rows_from_md(text) if name.endswith(".md") else _rows_from_csv(text)
    except Exception as e:
        p.fail(422, 42201, message=f"解析失败：{e}")
    if not rows:
        p.fail(422, 42201, message="文件里没有解析到任何需求项")

    report = _precheck(rows)
    good = report.pop("_good")
    report["filename"] = file.filename
    report["dry_run"] = dry_run.lower() != "false"

    if not report["dry_run"]:
        inserted = 0
        if good:
            with p.db() as conn:
                for r in good:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO requirement_items(item_no,category,content,status,"
                        "owner,source_tool,source_ref,biz_occurred_at,received_at,change_note) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (r["item_no"], r["category"], r["content"], r["status"], r["owner"],
                         r["source_tool"], r["source_ref"], r["biz_occurred_at"],
                         r["received_at"] or p.now(), r["change_note"]))
                    inserted += cur.rowcount
                conn.commit()
            p.rebuild_index()
        # 幂等重跑时 good 为空，也要明确回 0，而不是缺字段让前端显示 undefined
        report["inserted"] = inserted
    return report


@router.get("/api/admin/items/template.csv")
async def api_template(user=Depends(require_api("admin"))):
    """下载导入模板（带 BOM，Excel 直接打开不乱码）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLS)
    w.writerow(["EQ-07", "设备", "签到台备用话筒 2 支", "未确认", "林亦舟", "服务台",
                "SD-2026-0130|第5行", "2026-10-28 09:00", "2026-10-28 10:00", ""])
    return PlainTextResponse("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition":
                                      "attachment; filename=items_template.csv"})


# ── 编号映射（三源无统一编号，f30）──
@router.get("/api/admin/mappings")
async def api_mappings(user=Depends(require_api("admin"))):
    p = _p()
    with p.db() as conn:
        rows = conn.execute("SELECT * FROM id_mappings ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/admin/mappings")
async def api_add_mapping(request: Request, user=Depends(require_api("admin"))):
    p = _p()
    b = await request.json()
    tool = (b.get("source_tool") or "").strip()
    ext = (b.get("external_no") or "").strip()
    item_no = (b.get("item_no") or "").strip().upper()
    if not tool or not ext or not ITEM_NO_RE.match(item_no):
        p.fail(400, 40001, message="来源工具、原始单号必填，需求项编号格式须正确")
    with p.db() as conn:
        if not conn.execute("SELECT 1 FROM requirement_items WHERE item_no=?",
                            (item_no,)).fetchone():
            p.fail(404, 40401, message=f"需求项 {item_no} 不存在")
        try:
            conn.execute("INSERT INTO id_mappings(source_tool,external_no,item_no,mapped_by,"
                         "created_at) VALUES(?,?,?,?,?)",
                         (tool, ext, item_no, user["id"], p.now()))
            conn.commit()
        except Exception:
            p.fail(409, 40901, message="该来源单号已映射过（同一来源单号只能映射一次）")
    return {"ok": True}


@router.delete("/api/admin/mappings/{mid}")
async def api_del_mapping(mid: int, user=Depends(require_api("admin"))):
    p = _p()
    with p.db() as conn:
        cur = conn.execute("DELETE FROM id_mappings WHERE id=?", (mid,))
        conn.commit()
    if cur.rowcount == 0:
        p.fail(404, 40401, message="映射不存在")
    return {"ok": True}


# ============================================================
# 批 3：规则配置（同义词 / 类别 / 卡口开关 / LLM 提示词占位）
#   核心要求：改完即时生效（不重启、不改代码）
# ============================================================
@router.get("/admin/rules", response_class=HTMLResponse)
async def rules_page(request: Request, user=Depends(require_page("admin"))):
    p = _p()
    with p.db() as conn:
        rules = {k: p.get_rule(conn, k) for k in ("synonyms", "categories", "gates", "llm_prompt")}
    return templates.TemplateResponse(request, "rules.html", page_ctx(
        request, user, "rules", rules=rules, rules_ready=True, statuses=STATUSES))


@router.get("/api/admin/rules")
async def api_get_rules(user=Depends(require_api("admin"))):
    p = _p()
    with p.db() as conn:
        return {k: p.get_rule(conn, k) for k in ("synonyms", "categories", "gates", "llm_prompt")}


@router.put("/api/admin/rules")
async def api_put_rules(request: Request, user=Depends(require_api("admin"))):
    """
    整体保存配置。保存后立即把同义词灌进运行时并重建索引——
    下一次员工查询就用新词表，不需要重启服务。
    """
    p = _p()
    b = await request.json()
    allowed = {"synonyms", "categories", "gates", "llm_prompt"}
    unknown = set(b) - allowed
    if unknown:
        p.fail(400, 40001, message=f"不支持的配置项：{'、'.join(unknown)}")
    with p.db() as conn:
        for k, v in b.items():
            p.set_rule(conn, k, v, user["id"])
        conn.commit()
        syn = p.get_rule(conn, "synonyms", {})
    from stage1_bridge import set_synonyms
    set_synonyms(syn or {})
    n = p.rebuild_index()
    return {"ok": True, "indexed": n, "synonym_groups": len(syn or {})}


@router.post("/api/admin/rules/synonyms")
async def api_synonym(request: Request, user=Depends(require_api("admin"))):
    """单词条增删：{"action":"add","word":"话筒","syns":["麦克风"]} / {"action":"del","word":"话筒"}"""
    p = _p()
    b = await request.json()
    action = b.get("action")
    word = (b.get("word") or "").strip()
    syns = [x.strip() for x in (b.get("syns") or []) if x.strip()]
    if not word:
        p.fail(400, 40001, message="词条不能为空")
    with p.db() as conn:
        table = p.get_rule(conn, "synonyms", {}) or {}
        if action == "add":
            table.setdefault(word, [])
            for s in syns:
                if s not in table[word]:
                    table[word].append(s)
            # 双向补全：麦克风 也应反查到 话筒（否则只在一个方向生效）
            for s in syns:
                back = table.setdefault(s, [])
                if word not in back:
                    back.append(word)
        elif action == "del":
            table.pop(word, None)
        else:
            p.fail(400, 40001, message="action 只支持 add / del")
        p.set_rule(conn, "synonyms", table, user["id"])
        conn.commit()
    from stage1_bridge import set_synonyms
    set_synonyms(table)
    p.rebuild_index()
    return {"ok": True, "groups": len(table)}


@router.post("/api/handover/check")
async def handover_check(request: Request, user=Depends(require_api())):
    """
    交接卡口校验（f20 三要素 / f24 双条件 / f44 不默认通过）。
    规则内核已抽到 tools/registry.validate_core —— 页面按钮与 Agent 工具共用同一份判定，
    不允许出现"后台放行、Agent 拦住"这种两套规则。
    gates 开关在规则配置页可切，改完即时生效。
    """
    from tools import registry
    p = _p()
    b = await request.json()
    item_no = (b.get("item_no") or "").strip().upper()
    receiver = (b.get("receiver") or "").strip()
    deadline = (b.get("deadline") or "").strip()

    with p.db() as conn:
        row, problems, gates = registry.validate_core(conn, item_no, receiver, deadline)
        if row is None:
            p.fail(404, 40401, message="需求项不存在")
        passed = not problems
        out = {"pass": passed, "item_no": item_no, "status": row["status"],
               "reason": "通过" if passed else "；".join(problems), "gates": gates}
        if passed:
            # 校验通过才发票：5 分钟、绑参数哈希、用后作废（顺序约束的物理保证）
            out.update(registry.issue_ticket(conn, user["id"], item_no, receiver, deadline))
            out["next_action"] = "带 ticket 调 create_handover（参数须与本次一致）"
    return out


# ============================================================
# 批 4：查询审计（按员工/时间筛选 + 疑似越界标记 + 导出）
#   规则只标记不拦截：审计是为了复盘，不是为了卡住业务
# ============================================================
SENSITIVE_WORDS = ["身份证", "护照", "手机号", "电话", "房间号", "房号",
                   "家属", "陪同", "住址", "微信", "银行卡"]


def _auto_rules(conn, row, user_daily_count, same_q_count) -> list:
    """自动规则命中（只提示，标记动作仍由人做）。"""
    hits = []
    q = row["content"] or ""
    if any(w in q for w in SENSITIVE_WORDS):
        hits.append("R1")
    if same_q_count >= 5 or user_daily_count > 50:
        hits.append("R2")
    r3 = conn.execute(
        "SELECT COUNT(*) c FROM audit_logs WHERE actor_id=? AND action='forbidden' "
        "AND created_at >= datetime('now','-7 day')", (row["user_id"],)).fetchone()["c"]
    if r3 >= 3:
        hits.append("R3")
    return hits


def _audit_query(conn, uid=None, dfrom="", dto="", flagged=None, q="", limit=20, offset=0):
    """审计列表：以"用户提问"为主记录，配上同会话紧随其后的助手回答与标记。"""
    where, args = ["m.role='user'"], []
    if uid:
        where.append("s.user_id=?")
        args.append(uid)
    if dfrom:
        where.append("substr(m.created_at,1,10) >= ?")
        args.append(dfrom)
    if dto:
        where.append("substr(m.created_at,1,10) <= ?")
        args.append(dto)
    if q:
        where.append("m.content LIKE ?")
        args.append(f"%{q}%")
    if flagged is True:
        where.append("EXISTS (SELECT 1 FROM item_flags f WHERE f.message_id=m.id AND f.revoked_at IS NULL)")
    elif flagged is False:
        where.append("NOT EXISTS (SELECT 1 FROM item_flags f WHERE f.message_id=m.id AND f.revoked_at IS NULL)")
    w = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) c FROM messages m JOIN chat_sessions s ON s.id=m.session_id WHERE {w}",
        args).fetchone()["c"]

    rows = conn.execute(
        f"""SELECT m.id, m.content, m.created_at, m.session_id, s.user_id, u.display_name,
                   (SELECT m2.content FROM messages m2 WHERE m2.session_id=m.session_id
                     AND m2.id>m.id AND m2.role='assistant' ORDER BY m2.id LIMIT 1) AS answer,
                   (SELECT COUNT(*) FROM item_flags f
                     WHERE f.message_id=m.id AND f.revoked_at IS NULL) AS flag_count,
                   (SELECT f.rule FROM item_flags f
                     WHERE f.message_id=m.id AND f.revoked_at IS NULL ORDER BY f.id DESC LIMIT 1) AS flag_rule
            FROM messages m JOIN chat_sessions s ON s.id=m.session_id
            JOIN users u ON u.id=s.user_id
            WHERE {w} ORDER BY m.id DESC LIMIT ? OFFSET ?""",
        args + [limit, offset]).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        day = r["created_at"][:10]
        daily = conn.execute(
            "SELECT COUNT(*) c FROM messages m JOIN chat_sessions s ON s.id=m.session_id "
            "WHERE m.role='user' AND s.user_id=? AND substr(m.created_at,1,10)=?",
            (r["user_id"], day)).fetchone()["c"]
        same = conn.execute(
            "SELECT COUNT(*) c FROM messages m JOIN chat_sessions s ON s.id=m.session_id "
            "WHERE m.role='user' AND s.user_id=? AND m.content=? AND substr(m.created_at,1,10)=?",
            (r["user_id"], r["content"], day)).fetchone()["c"]
        d["auto_rules"] = _auto_rules(conn, r, daily, same)
        d["daily_count"] = daily
        out.append(d)
    return total, out


@router.get("/admin/audit", response_class=HTMLResponse)
async def audit_page(request: Request, user_id: str = "", dfrom: str = "", dto: str = "",
                     flagged: str = "", q: str = "", page: int = 1,
                     user=Depends(require_page("admin"))):
    p = _p()
    page = max(1, page)
    f = None if flagged == "" else (flagged == "1")
    with p.db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id,username,display_name,role FROM users ORDER BY id")]
        total, rows = _audit_query(conn, int(user_id) if user_id else None, dfrom, dto, f, q,
                                   limit=20, offset=(page - 1) * 20)
    return templates.TemplateResponse(request, "audit.html", page_ctx(
        request, user, "audit", rows=rows, total=total, page=page,
        pages=(total + 19) // 20, users=users, user_id=user_id, dfrom=dfrom, dto=dto,
        flagged=flagged, q=q, audit_ready=True, sensitive=SENSITIVE_WORDS))


@router.get("/api/admin/audit")
async def api_audit(user_id: str = "", dfrom: str = "", dto: str = "", flagged: str = "",
                    q: str = "", page: int = 1, size: int = 20,
                    user=Depends(require_api("admin", "qa", "biz_owner"))):
    p = _p()
    f = None if flagged == "" else (flagged == "1")
    with p.db() as conn:
        total, rows = _audit_query(conn, int(user_id) if user_id else None, dfrom, dto, f, q,
                                   limit=min(size, 100), offset=(max(page, 1) - 1) * size)
    return {"total": total, "items": rows}


@router.post("/api/admin/audit/{message_id}/flag")
async def api_flag(message_id: int, request: Request,
                   user=Depends(require_api("admin", "qa"))):
    """标记疑似越界。只标记不拦截——查询照常可用，标记进审计台账。"""
    p = _p()
    b = await request.json()
    rule = b.get("rule") or "R1"
    reason = (b.get("reason") or "").strip()
    if rule not in ("R1", "R2", "R3"):
        p.fail(400, 40001, message="规则只能是 R1 / R2 / R3")
    with p.db() as conn:
        if not conn.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
            p.fail(404, 40401, message="消息不存在")
        if conn.execute("SELECT 1 FROM item_flags WHERE message_id=? AND revoked_at IS NULL",
                        (message_id,)).fetchone():
            p.fail(409, 40901, message="该记录已被标记，撤销后才能重复标记")
        conn.execute("INSERT INTO item_flags(message_id,rule,reason,flagged_by,created_at) "
                     "VALUES(?,?,?,?,?)", (message_id, rule, reason, user["id"], p.now()))
        conn.commit()
    return {"ok": True}


@router.delete("/api/admin/audit/{message_id}/flag")
async def api_unflag(message_id: int, user=Depends(require_api("admin"))):
    """撤销标记（误标要能撤，否则审计台账会被脏标记污染）。"""
    p = _p()
    with p.db() as conn:
        cur = conn.execute("UPDATE item_flags SET revoked_at=? "
                           "WHERE message_id=? AND revoked_at IS NULL",
                           (p.now(), message_id))
        conn.commit()
    if cur.rowcount == 0:
        p.fail(404, 40401, message="该记录没有生效中的标记")
    return {"ok": True}


@router.get("/api/admin/audit/export")
async def api_audit_export(user_id: str = "", dfrom: str = "", dto: str = "", flagged: str = "",
                           q: str = "", user=Depends(require_api("admin"))):
    """导出 CSV（带 BOM，Excel 直开不乱码）。"""
    p = _p()
    f = None if flagged == "" else (flagged == "1")
    with p.db() as conn:
        total, rows = _audit_query(conn, int(user_id) if user_id else None, dfrom, dto, f, q,
                                   limit=5000, offset=0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["记录ID", "时间", "员工", "问题", "回答", "当日查询数",
                "自动规则", "标记规则", "标记状态"])
    for r in rows:
        w.writerow([r["id"], r["created_at"], r["display_name"], r["content"],
                    (r["answer"] or "").replace("\n", " "), r["daily_count"],
                    "/".join(r["auto_rules"]) or "—", r["flag_rule"] or "—",
                    "已标记" if r["flag_count"] else "未标记"])
    return PlainTextResponse("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition":
                                      "attachment; filename=audit_export.csv"})
