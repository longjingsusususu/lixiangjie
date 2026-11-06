# -*- coding: utf-8 -*-
"""
tools/registry.py —— Agent 工具注册表（A 档：确定性工具层）

设计要点（对应《会议活动需求_Agent工具设计.md》）：
  1. 10 个工具，读 5 / 校验 1 / 写 3 / 通知 1；工具是唯一出口，模型绕不过去。
  2. 顺序约束不靠提示词：validate_handover 通过才发一次性票据（绑参数哈希 + 5 分钟 +
     用后作废），create_handover 必须验票。伪造、重放、换参数一律拒。
  3. 人类保留权不暴露：confirm_item / finalize_version / delete_item / close_exception
     根本不在注册表里，调用即 unknown_tool。
  4. 留痕由服务端强制写：item_changes、audit_logs 不提供写工具。
  5. A 档用法：前端按钮按顺序串工具；B 档把"调用方"换成 LLM，本文件零改动。
"""

import hashlib
import json
import re
import secrets
import time
from datetime import datetime

# ============================================================
# 常量
# ============================================================
TICKET_TTL = 300                      # 票据有效期（秒）
ITEM_NO_RE = re.compile(r"^[A-Z]{2}-\d{2,3}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATUS_ORDER = {"缺失": 0, "未确认": 1, "已确认": 2, "已定版": 3, "已交接": 4}
READ_ONLY_KINDS = ("read", "validate")   # 只读 + 校验：可并发、无副作用

# 工具执行上下文里允许出现的字段（upsert_item 用）
UPDATABLE = {"category", "content", "status", "owner", "source_tool", "source_ref",
             "biz_occurred_at", "change_note"}

# 覆盖门停用词：这些词在"任何需求类问题"里都会出现，对判断"清单有没有覆盖"
# 没有区分度（比如"需求"两个字，条目正文里反而不常出现）。
_COVER_STOP = {
    "的", "吗", "呢", "了", "是", "有", "没有", "都", "还", "再", "先", "把", "请",
    "哪些", "哪条", "哪一", "多少", "什么", "怎么", "一下", "现在", "一共", "相关",
    "需求", "系统", "这个", "那个", "它", "看看", "查查", "一条", "有的", "给我",
    "帮我", "还是", "以及", "并且", "进行", "是否",
    # 领域泛词：问"状态/情况/明细"时它们必然出现，但对"清单是否覆盖"无区分度
    "状态", "分别", "情况", "详情", "信息", "明细", "结果", "内容", "字段",
}


def _informative_terms(q: str, syn: dict) -> list:
    """
    从问题里抽出"实质词"：jieba 分词 → 去停用词/单字/数字。
    同义词组里的词各自保留（命中时互查），比如"麦克风"与"话筒"。
    """
    import jieba
    out = []
    for t in jieba.lcut(q or ""):
        t = t.strip()
        if len(t) < 2 or t in _COVER_STOP or t.isdigit():
            continue
        if t not in out:
            out.append(t)
    return out


def _p():
    """延迟导入 portal，避免循环引用。"""
    import portal
    return portal


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 工具注册（装饰器：函数即工具，schema 与实现写在一起，避免两处漂移）
# ============================================================
TOOLS = {}


def tool(name, kind, description, parameters, roles=()):
    """
    kind: read / validate / write / notify
    roles: 允许调用的角色，空表示任意登录角色
    """
    def deco(fn):
        TOOLS[name] = {"name": name, "kind": kind, "description": description,
                       "parameters": parameters, "roles": list(roles), "handler": fn}
        return fn
    return deco


def schemas():
    """OpenAI / Anthropic 通用 function-calling 结构，B 档直接喂给模型。"""
    out = []
    for t in TOOLS.values():
        out.append({"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t["parameters"]}})
    return out


# ============================================================
# 票据机制（顺序约束的硬保证）
# ============================================================
def params_hash(item_no: str, receiver: str, deadline: str) -> str:
    raw = f"{item_no}|{receiver}|{deadline}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def issue_ticket(conn, uid: int, item_no: str, receiver: str, deadline: str) -> dict:
    ticket = "tk_" + secrets.token_hex(4)
    exp = int(time.time()) + TICKET_TTL
    conn.execute(
        "INSERT INTO handover_tickets(ticket,item_no,params_hash,issued_to,expires_at) "
        "VALUES(?,?,?,?,?)",
        (ticket, item_no, params_hash(item_no, receiver, deadline), uid, exp))
    conn.commit()
    return {"ticket": ticket, "expires_in": TICKET_TTL, "expires_at": exp,
            "params_hash": params_hash(item_no, receiver, deadline)}


def consume_ticket(conn, ticket: str, item_no: str, receiver: str, deadline: str):
    """
    验票五步（任一失败即拒）：
      存在 → 未过期 → 未使用 → 参数哈希一致 → 立刻标记已用
    这里的顺序就是设计文档里的 S1，写死在服务端，与模型是否"记得"无关。
    """
    p = _p()
    row = conn.execute("SELECT * FROM handover_tickets WHERE ticket=?", (ticket or "",)).fetchone()
    if row is None:
        p.fail(412, 41201, message="票据不存在：必须先调用 validate_handover 获取票据，不得直接建交接")
    if row["used_at"]:
        p.fail(412, 41203)
    if int(row["expires_at"]) < int(time.time()):
        p.fail(412, 41202)
    if row["params_hash"] != params_hash(item_no, receiver, deadline):
        p.fail(412, 41204)
    conn.execute("UPDATE handover_tickets SET used_at=? WHERE ticket=?", (_now(), ticket))
    conn.commit()
    return row


# ============================================================
# 校验内核（validate_handover 与 /api/handover/check 共用，避免两套规则漂移）
# ============================================================
def validate_core(conn, item_no: str, receiver: str, deadline: str):
    """返回 (row, problems, gates)。problems 为空即通过。"""
    row = conn.execute(
        "SELECT * FROM requirement_items WHERE item_no=? AND is_deleted=0", (item_no,)).fetchone()
    gates = _p().get_rule(conn, "gates", {}) or {}
    if row is None:
        return None, ["需求项不存在或已删除"], gates

    problems = []
    # f20 三要素：②交付物口径看 content；①接收人 ③截止时间 由本次入参给
    if gates.get("handover_3elements", True):
        if not (row["content"] or "").strip():
            problems.append("缺要素②交付物口径（需求内容为空）")
        if not receiver:
            problems.append("缺要素①接收人")
        if not DATE_RE.match(deadline or ""):
            problems.append("缺要素③截止时间（YYYY-MM-DD）")
    # f24 双条件：系统版本已定版 + 人工确认过
    if gates.get("version_double_condition", True) and row["status"] != "已定版":
        problems.append(f"状态为[{row['status']}]，未定版不得交接（f24 双条件）")
    # f44 不得默认通过
    if gates.get("no_auto_pass", True) and not (row["owner"] or "").strip():
        problems.append("无责任人，不得默认通过（f44）")
    return row, problems, gates


# ============================================================
# 读类工具（5 个）
# ============================================================
@tool("search_items", "read",
      "按关键词/类别/状态检索需求项，返回最多 5 条摘要。用户问『有哪些…』时用。"
      "只返回已入库项；无命中必须返回空列表，不得拿低相关结果凑数。",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "自然语言关键词，如『双语主持』『话筒』"},
           "category": {"type": "string", "enum": ["LS", "EQ", "CT", "VN", "TR", "OT"]},
           "status": {"type": "string", "enum": ["缺失", "未确认", "已确认", "已定版", "已交接"]},
           "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3}},
       "required": ["query"]})
def t_search_items(conn, user, args):
    p = _p()
    q = (args.get("query") or "").strip()
    if not q:
        p.fail(400, 40001, message="query 不能为空")
    limit = max(1, min(int(args.get("limit") or 3), 5))
    retriever = p.state.get("retriever")
    if retriever is None:
        p.fail(503, 50301)

    hits = retriever.search(q, max(limit * 3, 6))

    # ── 关键词覆盖门（防"硬凑 3 条"）────────────────────────
    # 检索器对任何问题都能排出名次，但这不代表清单真的覆盖了这个问题。
    # 陷阱题（"无人机航拍""接送机报销标准"）靠字符二元组也能凑出弱命中，
    # 必须在工具层再做一道语义覆盖检查：抽出去停用词后的实质词，
    # 命中率不足一半的条目视为弱相关，直接丢弃——宁可答"清单未覆盖"。
    syn = p.get_rule(conn, "synonyms", {}) or {}
    terms = _informative_terms(q, syn)
    if terms:
        strong = []
        for it, score in hits:
            text = f"{it.category}|{it.content}|{it.owner}"
            hit_n = sum(1 for t in terms
                        if t in text or any(s in text for s in syn.get(t, ())))
            if hit_n / len(terms) >= 0.5:
                strong.append((it, score))
        hits = strong

    cat, st = args.get("category"), args.get("status")
    items = []
    for it, score in hits:
        if cat and not it.item_no.startswith(cat):
            continue
        if st and it.status != st:
            continue
        items.append({"item_no": it.item_no, "category": it.category, "content": it.content,
                      "status": it.status, "owner": it.owner,
                      "source": f"{it.source_tool}|{it.source_ref}", "score": round(float(score), 3)})
        if len(items) >= limit:
            break
    return {"items": items, "covered": bool(items), "query": q}


@tool("get_item", "read",
      "读取单个需求项的完整字段、来源位置与全部变更历史。回答『为什么改过』『原始出处』必须用它。",
      {"type": "object",
       "properties": {"item_no": {"type": "string", "pattern": "^[A-Z]{2}-\\d{2,3}$"}},
       "required": ["item_no"]})
def t_get_item(conn, user, args):
    item_no = (args.get("item_no") or "").strip().upper()
    row = conn.execute("SELECT * FROM requirement_items WHERE item_no=?", (item_no,)).fetchone()
    if row is None:
        _p().fail(404, 40401, message="需求项不存在")
    changes = [dict(r) for r in conn.execute(
        "SELECT field,old_value,new_value,changed_by,created_at FROM item_changes "
        "WHERE item_no=? ORDER BY id DESC LIMIT 20", (item_no,))]
    d = dict(row)
    d.pop("is_deleted", None)
    d["changes"] = changes
    return d


@tool("check_completeness", "read",
      "需求完整性体检。带 item_no：体检单项是否具备三要素与责任人；"
      "不带 item_no：输出全量缺项报告（缺失/未确认分列，f31 口径）。"
      "只读诊断，不修改数据。注意：本工具通过 ≠ 可以交接，交接仍需 validate_handover 发票据。",
      {"type": "object",
       "properties": {"item_no": {"type": "string",
                                  "description": "不传则输出全量缺项报告"}},
       "required": []})
def t_check_completeness(conn, user, args):
    # ── 全量模式：不带 item_no，输出"一页缺项清单"（f15 的原始诉求）──
    item_no = (args.get("item_no") or "").strip().upper()
    if not item_no:
        rows = conn.execute(
            "SELECT item_no,category,content,status,owner FROM requirement_items "
            "WHERE is_deleted=0").fetchall()
        missing, unconfirmed = [], []
        for r in rows:
            reasons = []
            if not (r["content"] or "").strip():
                reasons.append("缺交付物口径")
            if not (r["owner"] or "").strip():
                reasons.append("缺责任人")
            if r["status"] == "缺失":
                reasons.append("状态=缺失")
            if reasons:
                missing.append({"item_no": r["item_no"], "category": r["category"],
                                "owner": r["owner"] or "", "reasons": reasons})
            elif r["status"] == "未确认":
                unconfirmed.append({"item_no": r["item_no"], "category": r["category"],
                                    "owner": r["owner"] or ""})
        # f31 纪律：缺失与未确认是两种不同的病，分列统计，绝不混算
        return {"scope": "all", "total": len(rows),
                "missing_count": len(missing), "unconfirmed_count": len(unconfirmed),
                "missing_items": missing, "unconfirmed_items": unconfirmed}

    # ── 单项模式：原有逻辑不动 ──
    row = conn.execute(
        "SELECT * FROM requirement_items WHERE item_no=? AND is_deleted=0", (item_no,)).fetchone()
    if row is None:
        _p().fail(404, 40401, message="需求项不存在")
    missing, blocking = [], []
    if not (row["content"] or "").strip():
        missing.append({"field": "content", "label": "交付物口径", "hint": "写清交付什么、到什么程度"})
    if not (row["owner"] or "").strip():
        missing.append({"field": "owner", "label": "责任人", "hint": "必须落到具体人"})
    if row["status"] == "缺失":
        missing.append({"field": "status", "label": "确认状态", "hint": "先补录内容再走确认"})
    if row["status"] not in ("已定版", "已交接"):
        blocking.append(f"状态为[{row['status']}]，未定版不得交接（f24）")
    if (row["received_at"] or "") <= (row["biz_occurred_at"] or ""):
        blocking.append("补录时间不晚于发生时间，疑似补录时间被改过（f13 双时间戳）")
    return {"item_no": item_no, "ready": not missing and not blocking,
            "status": row["status"], "owner": row["owner"],
            "missing": missing, "blocking_reasons": blocking}


@tool("diff_sources", "read",
      "把同一需求项在服务台/台账/文件库三处的记录并排对比，指出字段冲突与缺失。"
      "只报冲突不下结论——谁对谁错必须人判（f33 不清洗原行）。",
      {"type": "object",
       "properties": {
           "item_no": {"type": "string"},
           "fields": {"type": "array", "items": {"type": "string"},
                      "description": "只比这几个字段，缺省 content/owner/status"}},
       "required": ["item_no"]})
def t_diff_sources(conn, user, args):
    item_no = (args.get("item_no") or "").strip().upper()
    fields = args.get("fields") or ["content", "owner", "status"]
    rows = conn.execute(
        "SELECT source_tool,field,value,raw_ref FROM source_records WHERE item_no=? "
        "ORDER BY source_tool", (item_no,)).fetchall()
    if not rows:
        return {"item_no": item_no, "conflicts": [], "sources_present": [],
                "note": "该需求项只有单一来源记录，无可对比项"}
    matrix, sources = {}, []
    for r in rows:
        sources.append(r["source_tool"])
        matrix.setdefault(r["field"], {})[r["source_tool"]] = r["value"] or "—"
    conflicts = []
    for f in fields:
        if f not in matrix:
            continue
        vals = set(matrix[f].values())
        if len(vals) > 1:
            conflicts.append({"field": f, "by_source": matrix[f],
                              "note": "三源不一致，需人工裁定后由责任人确认"})
    return {"item_no": item_no, "conflicts": conflicts,
            "sources_present": sorted(set(sources)),
            "matrix": matrix, "judgement": "none"}   # judgement 恒为 none：Agent 不替人裁定


@tool("find_mapping", "read",
      "用来源工具的原始单号查系统内部编号。任何来源数据入库前必须先调它判断是否已存在。",
      {"type": "object",
       "properties": {
           "source_tool": {"type": "string",
                           "enum": ["服务台", "台账", "文件库", "手工补录"],
                           "description": "三源 + 手工补录，与需求项的来源字段同一套口径"},
           "source_ref": {"type": "string", "description": "来源原始单号，如 SD-2026-1031"}},
       "required": ["source_tool", "source_ref"]})
def t_find_mapping(conn, user, args):
    st = (args.get("source_tool") or "").strip()
    ref = (args.get("source_ref") or "").strip()
    if not st or not ref:
        _p().fail(400, 40001, message="source_tool 与 source_ref 都必须提供")
    row = conn.execute(
        "SELECT item_no,mapped_by,created_at FROM id_mappings WHERE source_tool=? AND external_no=?",
        (st, ref)).fetchone()
    return {"found": row is not None, "item_no": row["item_no"] if row else None,
            "source_tool": st, "source_ref": ref}


# ============================================================
# 校验类工具（1 个）：只读 + 发票
# ============================================================
@tool("validate_handover", "validate",
      "交接前置校验。检查三要素、状态是否已定版、是否有责任人。全部通过才返回一次性票据；"
      "否则 pass=false 并给出 blocking_reasons。本工具不产生任何业务数据变更。",
      {"type": "object",
       "properties": {
           "item_no": {"type": "string"},
           "receiver": {"type": "string", "description": "接收人姓名"},
           "deadline": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}},
       "required": ["item_no", "receiver", "deadline"]})
def t_validate_handover(conn, user, args):
    item_no = (args.get("item_no") or "").strip().upper()
    receiver = (args.get("receiver") or "").strip()
    deadline = (args.get("deadline") or "").strip()
    row, problems, gates = validate_core(conn, item_no, receiver, deadline)
    if row is None:
        _p().fail(404, 40401, message="需求项不存在")
    if problems:
        return {"pass": False, "item_no": item_no, "status": row["status"],
                "reason": "；".join(problems), "blocking_reasons": problems,
                "gates": gates, "next_action": _next_action(problems)}
    tk = issue_ticket(conn, user["id"], item_no, receiver, deadline)
    return {"pass": True, "item_no": item_no, "status": row["status"], "reason": "通过",
            "gates": gates, "next_action": "带 ticket 调 create_handover（参数须与本次一致）",
            **tk}


def _next_action(problems) -> str:
    text = "；".join(problems)
    if "未定版" in text:
        return "请协调员在需求项库把状态推进到[已定版]后再交接"
    if "责任人" in text:
        return "先指定责任人（request_confirm 或人工补录），不得默认通过"
    if "接收人" in text or "截止时间" in text:
        return "补齐接收人与截止时间后重新 validate_handover"
    if "交付物口径" in text:
        return "先补需求内容（upsert_item），再重新校验"
    return "按 blocking_reasons 逐项处理"


# ============================================================
# 写类工具（3 个，串行、幂等、带前置）
# ============================================================
@tool("upsert_item", "write",
      "新增或修正一个需求项字段。前置：必须已用 find_mapping 确认来源单号（或 item_no 已存在）。"
      "服务端自动写 item_changes 变更历史，调用方不得也无法代写。禁止批量。",
      {"type": "object",
       "properties": {
           "item_no": {"type": "string", "pattern": "^[A-Z]{2}-\\d{2,3}$"},
           "fields": {"type": "object", "properties": {
               "category": {"type": "string"}, "content": {"type": "string"},
               "status": {"type": "string", "enum": ["缺失", "未确认", "已确认", "已定版", "已交接"]},
               "owner": {"type": "string"}, "source_tool": {"type": "string"},
               "source_ref": {"type": "string"}, "biz_occurred_at": {"type": "string"},
               "change_note": {"type": "string"}},
               "required": ["change_note"]},
           "mapping_checked": {"type": "boolean", "description": "声明已完成 find_mapping 检查"}},
       "required": ["item_no", "fields", "mapping_checked"]})
def t_upsert_item(conn, user, args):
    p = _p()
    item_no = (args.get("item_no") or "").strip().upper()
    fields = {k: v for k, v in (args.get("fields") or {}).items() if k in UPDATABLE}
    if not ITEM_NO_RE.match(item_no):
        p.fail(400, 40001, message="item_no 格式应为 XX-00，如 LS-02")
    if not (fields.get("change_note") or "").strip():
        p.fail(400, 40001, message="change_note 必填：改了什么、为什么改")

    row = conn.execute("SELECT * FROM requirement_items WHERE item_no=?", (item_no,)).fetchone()
    if row is None:
        if not args.get("mapping_checked"):
            p.fail(422, 42211)
        conn.execute(
            "INSERT INTO requirement_items(item_no,category,content,status,owner,source_tool,"
            "source_ref,biz_occurred_at,received_at,change_note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item_no, fields.get("category"), fields.get("content"),
             fields.get("status") or "缺失", fields.get("owner"),
             fields.get("source_tool"), fields.get("source_ref"),
             fields.get("biz_occurred_at"), _now(), fields.get("change_note")))
        for k, v in fields.items():
            if k != "change_note" and v:
                conn.execute("INSERT INTO item_changes(item_no,field,old_value,new_value,"
                             "changed_by,created_at) VALUES(?,?,?,?,?,?)",
                             (item_no, k, None, str(v), user["id"], _now()))
        conn.commit()
        p.rebuild_index()
        return {"ok": True, "created": True, "item_no": item_no, "changed": list(fields)}

    # 已存在：逐字段留痕；状态只允许向前一格（S6 单调不可跳格）
    old_status = row["status"]
    new_status = fields.get("status")
    if new_status and new_status != old_status:
        if STATUS_ORDER.get(new_status, -1) != STATUS_ORDER.get(old_status, -1) + 1:
            p.fail(409, 42214, message=f"状态只能向前推进一格：{old_status} → "
                                       f"{new_status} 不允许（需逐级确认）")
    changed = []
    for k, v in fields.items():
        if k == "change_note":
            continue
        if str(row[k] or "") == str(v or ""):
            continue
        conn.execute(f"UPDATE requirement_items SET {k}=? WHERE item_no=?", (v, item_no))
        conn.execute("INSERT INTO item_changes(item_no,field,old_value,new_value,"
                     "changed_by,created_at) VALUES(?,?,?,?,?,?)",
                     (item_no, k, row[k], v, user["id"], _now()))
        changed.append(k)
    if not changed:
        return {"ok": True, "created": False, "item_no": item_no, "changed": [],
                "note": "字段无变化，未写入任何记录（幂等）"}
    conn.execute("UPDATE requirement_items SET change_note=?, received_at=? WHERE item_no=?",
                 (fields["change_note"], _now(), item_no))
    conn.commit()
    p.rebuild_index()
    return {"ok": True, "created": False, "item_no": item_no, "changed": changed}


@tool("create_handover", "write",
      "建立交接记录，把责任转移到 receiver。硬前置：必须携带 validate_handover 签发、"
      "未过期未使用、且 item_no/receiver/deadline 完全一致的 ticket。无票一律 41201。",
      {"type": "object",
       "properties": {
           "ticket": {"type": "string", "description": "validate_handover 签发的一次性票据"},
           "item_no": {"type": "string"},
           "receiver": {"type": "string"},
           "deadline": {"type": "string"},
           "note": {"type": "string"}},
       "required": ["ticket", "item_no", "receiver", "deadline"]})
def t_create_handover(conn, user, args):
    p = _p()
    item_no = (args.get("item_no") or "").strip().upper()
    receiver = (args.get("receiver") or "").strip()
    deadline = (args.get("deadline") or "").strip()
    ticket = (args.get("ticket") or "").strip()

    consume_ticket(conn, ticket, item_no, receiver, deadline)   # ← 顺序约束在这里落地

    row = conn.execute("SELECT * FROM requirement_items WHERE item_no=?", (item_no,)).fetchone()
    if row is None:
        p.fail(404, 40401, message="需求项不存在")

    from_owner = row["owner"]
    conn.execute(
        "INSERT INTO handovers(item_no,from_owner,receiver,deadline,note,ticket,created_by,"
        "created_at) VALUES(?,?,?,?,?,?,?,?)",
        (item_no, from_owner, receiver, deadline, (args.get("note") or ""), ticket,
         user["id"], _now()))
    conn.execute("UPDATE requirement_items SET status=?, owner=? WHERE item_no=?",
                 ("已交接", receiver, item_no))
    for f, old, new in (("status", row["status"], "已交接"), ("owner", from_owner, receiver)):
        conn.execute("INSERT INTO item_changes(item_no,field,old_value,new_value,changed_by,"
                     "created_at) VALUES(?,?,?,?,?,?)", (item_no, f, old, new, user["id"], _now()))
    conn.commit()
    p.rebuild_index()
    return {"ok": True, "item_no": item_no, "from": from_owner, "to": receiver,
            "deadline": deadline, "status": "已交接", "ticket_used": ticket}


@tool("manage_exception", "write",
      "把异常项放入人工队列，或给已入队异常补充说明。本工具不能关闭异常——"
      "关闭必须由质量验收人上传证据后人工操作（f44）。",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "enum": ["open", "annotate"]},
           "item_no": {"type": "string"},
           "reason": {"type": "string", "description": "open 时必填"},
           "exception_id": {"type": "integer", "description": "annotate 时必填"},
           "note": {"type": "string"}},
       "required": ["action"]})
def t_manage_exception(conn, user, args):
    p = _p()
    action = args.get("action")
    if action == "open":
        item_no = (args.get("item_no") or "").strip().upper()
        reason = (args.get("reason") or "").strip()
        if not item_no or not reason:
            p.fail(400, 40001, message="open 需要 item_no 与 reason")
        cur = conn.execute(
            "INSERT INTO exception_queue(item_no,reason,note,status,opened_by,created_at) "
            "VALUES(?,?,?,?,?,?)", (item_no, reason, args.get("note"), "open", user["id"], _now()))
        conn.commit()
        return {"ok": True, "exception_id": cur.lastrowid, "status": "open",
                "note": "已入人工队列；Agent 无权关闭，需质量验收上传证据后人工处理"}
    if action == "annotate":
        eid = args.get("exception_id")
        row = conn.execute("SELECT * FROM exception_queue WHERE id=?", (eid,)).fetchone()
        if row is None:
            p.fail(404, 40404)
        if row["status"] != "open":
            p.fail(409, 40902, message="异常已关闭，不能再补充说明")
        merged = ((row["note"] or "") + "\n" + (args.get("note") or "")).strip()
        conn.execute("UPDATE exception_queue SET note=? WHERE id=?", (merged, eid))
        conn.commit()
        return {"ok": True, "exception_id": eid, "note": merged}
    p.fail(400, 40001, message="action 只支持 open / annotate（关闭异常不在 Agent 权限内）")


# ============================================================
# 通知类工具（1 个）：只发请求，不代办
# ============================================================
@tool("request_confirm", "notify",
      "给责任人发一条『待确认』站内待办，附上需要他确认的字段与来源位置。"
      "只发通知，不改变确认状态——确认必须责任人本人签字（f02）。",
      {"type": "object",
       "properties": {
           "item_no": {"type": "string"},
           "owner": {"type": "string"},
           "fields": {"type": "array", "items": {"type": "string"}},
           "message": {"type": "string"}},
       "required": ["item_no", "owner", "fields"]})
def t_request_confirm(conn, user, args):
    p = _p()
    item_no = (args.get("item_no") or "").strip().upper()
    owner = (args.get("owner") or "").strip()
    fields = args.get("fields") or []
    if not item_no or not owner:
        p.fail(400, 40001, message="item_no 与 owner 都必须提供")
    target = conn.execute("SELECT id FROM users WHERE display_name=? AND is_active=1",
                          (owner,)).fetchone()
    cur = conn.execute(
        "INSERT INTO confirm_requests(item_no,owner,owner_id,fields_json,message,status,"
        "requested_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (item_no, owner, target["id"] if target else None,
         json.dumps(fields, ensure_ascii=False), args.get("message") or "", "pending",
         user["id"], _now()))
    conn.commit()
    return {"ok": True, "request_id": cur.lastrowid, "item_no": item_no, "owner": owner,
            "status": "pending", "deliver": "站内待办",
            "note": "已通知，确认动作仍需责任人本人在系统完成"}


# ============================================================
# 统一入口
# ============================================================
WRITE_KINDS = ("write",)


def invoke(name: str, args: dict, user: dict) -> dict:
    """
    执行一个工具。所有调用都写审计（成功/被拒都记），
    这是"谁在什么时候让 Agent 做了什么"的唯一真相来源。
    """
    p = _p()
    t = TOOLS.get(name)
    with p.db() as conn:
        if t is None:
            _audit(conn, user, name, "unknown_tool")
            conn.commit()
            p.fail(404, 40405, message=f"工具不存在：{name}（Agent 无权调用未注册能力）")
        if t["roles"] and user["role"] not in t["roles"]:
            _audit(conn, user, name, "forbidden")
            conn.commit()
            p.fail(403, 40302)
        try:
            result = t["handler"](conn, user, args or {})
        except Exception as exc:
            # HTTPException 由上层处理器展开；这里只补审计
            _audit(conn, user, name, "rejected", str(getattr(exc, "detail", exc))[:200])
            conn.commit()
            raise
        _audit(conn, user, name, "ok", _target_of(result))
        conn.commit()
    return result


def _target_of(result) -> str:
    if not isinstance(result, dict):
        return ""
    for k in ("item_no", "exception_id", "request_id"):
        if result.get(k):
            return str(result[k])
    return ""


def _audit(conn, user, name, status, target=""):
    conn.execute(
        "INSERT INTO audit_logs(actor_id,action,target_type,target_id,created_at) "
        "VALUES(?,?,?,?,?)",
        (user["id"], "tool_call:" + status, "tool", f"{name}|{target}" if target else name,
         _now()))
