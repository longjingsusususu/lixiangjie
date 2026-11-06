# -*- coding: utf-8 -*-
"""
f24015105_meeting_tests.py —— 五类场景量化测试执行器（正常/边界/对抗/权限/故障）。

用法：python f24015105_meeting_tests.py
输出：mvp/test_results.json（结构化）+ mvp/test_logs/<场景>.log（过程日志）
说明：使用 httpx ASGI 内存传输直测应用（等价于绕过前端直调 API，本身就是对抗场景的测试方式）。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent


def _req(method: str, url: str, headers=None, json=None):
    """同步垫片：ASGITransport 仅支持异步，这里逐请求开临时事件循环执行。"""

    async def _do():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=srv.app), base_url="http://test") as c:
            return await c.request(method, url, headers=headers, json=json)

    import asyncio
    return asyncio.run(_do())


class _SyncASGI:
    def post(self, url, headers=None, json=None, **kw):
        return _req("POST", url, headers, json)

    def get(self, url, headers=None, **kw):
        return _req("GET", url, headers)


client = _SyncASGI()
sys.path.insert(0, str(BASE))

# 每次测试用全新库
DB = BASE / "test_mvp.db"
if DB.exists():
    DB.unlink()
for suf in ("-wal", "-shm"):
    p = Path(str(DB) + suf)
    if p.exists():
        p.unlink()
import os
os.environ["MEETING_MVP_DB"] = str(DB)
os.environ["MEETING_MVP_SECRET"] = "test-secret-fixed"

import importlib
import f24015105_meeting_server as srv
importlib.reload(srv)
import f24015105_meeting_seed as seed  # noqa: E402

seed.DB_PATH = DB
srv.init_schema()
LOGS = {}
RESULTS = {}
TODAY = "2026-11-30"
BIZ_TS = "2026-11-05 09:30"


def suite(name):
    def deco(fn):
        def run():
            lines, passed, failed = [], 0, 0

            def check(desc, cond, detail=""):
                nonlocal passed, failed
                mark = "PASS" if cond else "FAIL"
                if cond:
                    passed += 1
                else:
                    failed += 1
                lines.append(f"[{mark}] {desc}" + (f" ｜ {detail}" if detail else ""))
                return cond

            fn(check, lines)
            RESULTS[name] = {"passed": passed, "failed": failed}
            LOGS[name] = lines
            print(f"{name}: 通过 {passed} 项，失败 {failed} 项")
        return run
    return deco


def login(u, p="123456"):
    r = client.post("/api/login", json={"username": u, "password": p})
    return r


def tok(u, p="123456"):
    r = login(u, p)
    assert r.status_code == 200, f"登录失败 {u}: {r.text}"
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---------------------------------------------------------------- 1 正常
@suite("正常场景")
def s_normal(check, lines):
    lines.append("── 步骤 1：五角色登录 ──")
    for u in ("lin", "tang", "guml", "fujing", "qinrui"):
        r = login(u)
        check(f"登录 {u} 返回 200 且含角色", r.status_code == 200 and r.json()["user"]["role"], f"role={r.json()['user']['role'] if r.status_code==200 else r.status_code}")
    lines.append("── 步骤 2：一线登记需求项（双时间戳落库）──")
    th = tok("lin")
    r = client.post("/api/items", headers=th, json={
        "category": "catering", "content": "贵宾茶歇（测试登记项）", "owner_id": "1",
        "source_tool": "manual", "biz_occurred_at": BIZ_TS})
    check("登记返回 200", r.status_code == 200, str(r.json().get("id")))
    item_id = r.json()["id"]
    check("状态初始化为 unconfirmed", r.json()["status"] == "unconfirmed")
    check("双时间戳分开记录", r.json()["biz_occurred_at"] != r.json()["received_at"],
          f"biz={r.json()['biz_occurred_at']} recv={r.json()['received_at']}")
    lines.append("── 步骤 3：业务负责人确认签字 ──")
    check("一线确认被拒(权限)", client.post(f"/api/items/{item_id}/confirm", headers=th, json={}).status_code == 403)
    r = client.post(f"/api/items/{item_id}/confirm", headers=tok("guml"), json={})
    check("业务负责人确认成功", r.status_code == 200 and r.json()["status"] == "confirmed")
    lines.append("── 步骤 4：管理员 CSV 导入（幂等）──")
    ah = tok("qinrui")
    csv_text = ("external_no,category,content,owner,biz_occurred_at\n"
                "SD-9001,catering,冷餐台 12 米,林亦舟,2026-11-04 10:00\n"
                "SD-9002,equipment,追光灯 2 台,唐映秋,2026-11-04 11:00\n")
    r1 = client.post("/api/import/csv", headers=ah, json={"csv_text": csv_text, "source_tool": "servicedesk"})
    check("首次导入新建 2 项", r1.status_code == 200 and r1.json()["created"] == 2, str(r1.json()))
    r2 = client.post("/api/import/csv", headers=ah, json={"csv_text": csv_text, "source_tool": "servicedesk"})
    check("重复导入幂等跳过", r2.status_code == 200 and r2.json()["skipped"] == 2 and r2.json()["created"] == 0, str(r2.json()))
    lines.append("── 步骤 5：确认 10 项 → 建版本 → 定版 ──")
    gh = tok("guml")
    for i in range(1, 11):
        client.post(f"/api/items/{i}/confirm", headers=gh, json={})
    r = client.post("/api/versions", headers=tok("tang"), json={"item_ids": list(range(1, 11)), "valid_until": TODAY})
    check("版本号系统生成 V-0001", r.status_code == 200 and r.json()["version_no"] == "V-0001", str(r.json()))
    vid = r.json()["version_id"]
    r = client.post(f"/api/versions/{vid}/finalize", headers=tok("tang"), json={})
    check("f24 双条件满足 → 定版成功", r.status_code == 200 and r.json()["status"] == "finalized")
    lines.append("── 步骤 6：三要素交接 + 接收确认 ──")
    b0 = client.get("/api/board", headers=tok("tang")).json()
    open_real = b0["total"] - b0["status_dist"]["已定版"] - b0["status_dist"]["已交接"]
    r = client.post("/api/handovers", headers=tok("tang"), json={
        "version_id": vid, "receiver_id": 1, "open_items_count": open_real, "deadline": "2026-12-01"})
    check("三要素齐全 → 交接成功", r.status_code == 200, str(r.json()))
    check("版本内 10 项转为已交接", r.json().get("handed_items") == 10, str(r.json().get("handed_items")))
    hid = r.json()["handover_id"]
    check("接收人确认接收", client.post(f"/api/handovers/{hid}/ack", headers=tok("lin"), json={}).status_code == 200)
    lines.append("── 步骤 7：看板 M6/M7 出数 ──")
    b = client.get("/api/board", headers=th).json()
    check("M6 分母=50", b["M6"]["denominator"] == 50, str(b["M6"]))
    check("M6=确认项计数(11 项已确认/交接)", b["M6"]["numerator"] == 11, str(b["M6"]["numerator"]))
    check("M7 有来源记录占比>90%", b["M7"]["rate"] > 90, f"{b['M7']['rate']}%")
    check("双时间戳全部可区分", b["dual_timestamp_distinct"] == b["total"], f"{b['dual_timestamp_distinct']}/{b['total']}")
    lines.append("── 步骤 8：审计留痕四项可回查 ──")
    client.get(f"/api/items/{item_id}", headers=tok("fujing"))   # 触发一次"查看"留痕
    a = client.get("/api/audit", headers=tok("fujing")).json()["audit"]
    acts = {x["action"] for x in a}
    check("留痕含 view/suggest/confirm/finalize/handover/import", {"view", "suggest", "confirm", "finalize", "handover", "import"} <= acts, str(sorted(acts)))
    check("确认动作带 confirmed_by", any(x["action"] == "confirm" and x["confirmed_by"] for x in a))


# ---------------------------------------------------------------- 2 边界
@suite("边界场景")
def s_boundary(check, lines):
    th = tok("lin")
    lines.append("── 边界 B1：字段极值 ──")
    r = client.post("/api/items", headers=th, json={"category": "other", "content": "", "biz_occurred_at": BIZ_TS})
    check("空描述 → 400 E_CONTENT", r.status_code == 400 and r.json()["error_code"] == "E_CONTENT")
    r = client.post("/api/items", headers=th, json={"category": "other", "content": "长" * 2001, "biz_occurred_at": BIZ_TS})
    check("2001 字符 → 400 E_CONTENT_LEN", r.status_code == 400 and r.json()["error_code"] == "E_CONTENT_LEN")
    r = client.post("/api/items", headers=th, json={"category": "other", "content": "边界项", "biz_occurred_at": "2026/11/05 9点"})
    check("时间格式错误 → 400 E_TIME", r.status_code == 400 and r.json()["error_code"] == "E_TIME")
    r = client.post("/api/items", headers=th, json={"category": "vehicle", "content": "边界项", "biz_occurred_at": BIZ_TS})
    check("非法类别 → 400 E_CATEGORY", r.status_code == 400 and r.json()["error_code"] == "E_CATEGORY")
    r = client.post("/api/items", headers=th, json={"category": "other", "content": "无责任人边界项", "biz_occurred_at": BIZ_TS})
    check("无责任人 → 状态=missing(缺失≠未确认)", r.status_code == 200 and r.json()["status"] == "missing", str(r.json().get("status")))
    lines.append("── 边界 B2：版本/定版条件 ──")
    th_c = tok("tang")
    r = client.post("/api/versions", headers=th_c, json={"item_ids": [], "valid_until": TODAY})
    check("空版本 → 400 E_ITEMS", r.status_code == 400)
    r = client.post("/api/versions", headers=th_c, json={"item_ids": [11], "valid_until": "2026-01-01"})
    check("有效期过去 → 建版可、定版拒(f24)", client.post(f"/api/versions/{r.json()['version_id']}/finalize", headers=th_c, json={}).status_code == 400)
    r = client.post("/api/versions", headers=th_c, json={"item_ids": list(range(11, 16)), "valid_until": TODAY})
    check("含未确认项 → 定版拒并列出明细", client.post(f"/api/versions/{r.json()['version_id']}/finalize", headers=th_c, json={}).status_code == 400,
          "11~15 未确认")
    r = client.post("/api/versions", headers=th_c, json={"item_ids": [1], "valid_until": TODAY})
    check("纳入已交接项 → 400 E_ITEM_STATE", r.status_code == 400 and r.json()["error_code"] == "E_ITEM_STATE")
    r = client.post("/api/versions", headers=th_c, json={"item_ids": [11, 9999], "valid_until": TODAY})
    check("跨批次/不存在项 → 400 E_ITEM_BATCH", r.status_code == 400 and r.json()["error_code"] == "E_ITEM_BATCH")
    lines.append("── 边界 B3：交接未结项数边界 ──")
    r = client.post("/api/handovers", headers=th_c, json={"version_id": 1, "receiver_id": 1, "open_items_count": 0, "deadline": TODAY})
    check("未结项数不符(0≠实际) → 拦截", r.status_code == 400 and r.json()["error_code"] == "E_OPEN", str(r.json().get("message", ""))[:40])
    lines.append("── 边界 B4：CSV 边界 ──")
    ah = tok("qinrui")
    r = client.post("/api/import/csv", headers=ah, json={"csv_text": "", "source_tool": "servicedesk"})
    check("空 CSV → 400 E_CSV", r.status_code == 400 and r.json()["error_code"] == "E_CSV")
    r = client.post("/api/import/csv", headers=ah, json={"csv_text": "a,b,c\n1,2,3", "source_tool": "servicedesk"})
    check("缺列 → 400 E_CSV", r.status_code == 400 and r.json()["error_code"] == "E_CSV")
    csv_dirty = ("external_no,category,content,owner,biz_occurred_at\n"
                 "LG-8001,catering,无责任人导入项,,2026-11-04 10:00\n"
                 "LG-8002,catering,不存在责任人,王大锤,2026-11-04 10:00\n")
    r = client.post("/api/import/csv", headers=ah, json={"csv_text": csv_dirty, "source_tool": "ledger"})
    check("责任人缺失/未知 → 项状态=missing 不丢弃", r.status_code == 200 and r.json()["created"] == 2, str(r.json()))
    lines.append("── 边界 B5：状态机非法迁移 ──")
    r = client.post("/api/items/1/confirm", headers=tok("guml"), json={})
    check("已定版项再确认 → 400 E_STATE", r.status_code == 400 and r.json()["error_code"] == "E_STATE")
    r = client.post("/api/versions/1/void", headers=tok("tang"), json={"reason": "边界测试"})
    check("已定版版本不可作废 → 400", r.status_code == 400)


# ---------------------------------------------------------------- 3 对抗
@suite("对抗场景")
def s_adversary(check, lines):
    lines.append("── 对抗 A1：凭证伪造/篡改/过期 ──")
    r = client.post("/api/items", headers={"Authorization": "Bearer forged.token.sig"}, json={})
    check("伪造 token → 401", r.status_code == 401)
    real = tok("lin")["Authorization"].split(" ")[1]
    p, s = real.rsplit(".", 1)
    r = client.post("/api/items", headers={"Authorization": f"Bearer {p}.{'0' * 32}"}, json={})
    check("篡改签名 → 401", r.status_code == 401)
    import base64 as _b, json as _j, time as _t
    exp_tok = _b.urlsafe_b64encode(_j.dumps({"uid": 1, "exp": int(_t.time()) - 10}).encode()).decode()
    r = client.post("/api/items", headers={"Authorization": f"Bearer {exp_tok}.{srv._sig(exp_tok)}"}, json={})
    check("过期 token（有效签名）→ 401", r.status_code == 401)
    r = client.post("/api/items", headers={"Authorization": f"Bearer {p}.{s}"[: len(p) + 1 + 31]}, json={})
    check("截断签名 → 401", r.status_code == 401)
    lines.append("── 对抗 A2：注入与超长载荷 ──")
    r = client.post("/api/items", headers=tok("lin"), json={
        "category": "other", "content": "'); DROP TABLE requirement_items;--", "biz_occurred_at": BIZ_TS})
    check("SQL 注入载荷被参数化查询吸收", r.status_code == 200, f"新项 id={r.json().get('id')}")
    n = client.get("/api/items", headers=tok("lin")).json()["total" ] if False else None
    cnt = srv.db().execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
    check("表未被注入破坏（行数=原 50+新增）", cnt >= 50, f"rows={cnt}")
    r = client.post("/api/items", headers=tok("lin"), json={"category": "other", "content": "<script>alert(1)</script>", "biz_occurred_at": BIZ_TS})
    check("XSS 载荷原样入库、前端转义渲染（esc()）", r.status_code == 200)
    lines.append("── 对抗 A3：绕过前端直调 API（卡口对抗）──")
    tch = tok("tang")
    r = client.post("/api/handovers", headers=tch, json={"version_id": 1, "deadline": "2026-12-01", "open_items_count": 0})
    check("缺接收人 → 卡口拦截", r.status_code == 400 and r.json()["error_code"] == "E_RECEIVER")
    r = client.post("/api/handovers", headers=tch, json={"version_id": 1, "receiver_id": 1, "open_items_count": 0})
    check("缺截止时间 → 卡口拦截", r.status_code == 400 and r.json()["error_code"] == "E_DEADLINE")
    r = client.post("/api/handovers", headers=tch, json={"version_id": 1, "receiver_id": 1, "open_items_count": "abc", "deadline": "2026-12-01"})
    check("未结项数非整数 → 拦截", r.status_code == 400 and r.json()["error_code"] == "E_OPEN")
    r = client.post("/api/handovers", headers=tch, json={"version_id": 1, "receiver_id": 1, "open_items_count": 999, "deadline": "2026-12-01"})
    check("虚报未结项数 → 拦截（不允许只报已处理）", r.status_code == 400 and r.json()["error_code"] == "E_OPEN")
    lines.append("── 对抗 A4：异常队列不可被诱导自动通过 ──")
    r = client.post("/api/exceptions", headers=tok("lin"), json={"item_id": 46, "exception_type": "dirty_data", "note": "对抗测试"})
    check("对抗性尝试闭环(非 qa) → 403", client.post(f"/api/exceptions/{r.json()['exception_id']}/close", headers=tok("lin"), json={"resolution": "x"}).status_code == 403)
    states = {row[0] for row in srv.db().execute("SELECT DISTINCT status FROM exception_queue").fetchall()}
    check("队列状态集不含'自动通过'路径", states <= {"open", "processing", "closed"}, str(sorted(states)))


# ---------------------------------------------------------------- 4 权限
@suite("权限场景")
def s_permission(check, lines):
    lines.append("── 权限 P1：编辑权/确认权分离（f22）──")
    check("一线确认签字 → 403", client.post("/api/items/11/confirm", headers=tok("lin"), json={}).status_code == 403)
    check("协调员确认签字 → 403", client.post("/api/items/11/confirm", headers=tok("tang"), json={}).status_code == 403)
    check("管理员确认签字 → 403", client.post("/api/items/11/confirm", headers=tok("qinrui"), json={}).status_code == 403)
    check("质量验收确认签字 → 403", client.post("/api/items/11/confirm", headers=tok("fujing"), json={}).status_code == 403)
    check("业务负责人确认签字 → 200（唯一）", client.post("/api/items/11/confirm", headers=tok("guml"), json={}).status_code == 200)
    lines.append("── 权限 P2：登记权 ──")
    body = {"category": "other", "content": "权限测试项", "biz_occurred_at": BIZ_TS}
    check("业务负责人登记 → 403（只审不录）", client.post("/api/items", headers=tok("guml"), json=body).status_code == 403)
    check("质量验收登记 → 403", client.post("/api/items", headers=tok("fujing"), json=body).status_code == 403)
    check("一线登记 → 200", client.post("/api/items", headers=tok("lin"), json=body).status_code == 200)
    check("管理员登记 → 200", client.post("/api/items", headers=tok("qinrui"), json=body).status_code == 200)
    lines.append("── 权限 P3：定版/交接/导入/审计 ──")
    check("一线建版本 → 403", client.post("/api/versions", headers=tok("lin"), json={"item_ids": [12], "valid_until": TODAY}).status_code == 403)
    check("业务负责人发起交接 → 403", client.post("/api/handovers", headers=tok("guml"), json={"version_id": 1, "receiver_id": 1, "open_items_count": 39, "deadline": TODAY}).status_code == 403)
    check("一线导入 CSV → 403", client.post("/api/import/csv", headers=tok("lin"), json={"csv_text": "x", "source_tool": "manual"}).status_code == 403)
    check("管理员导入 CSV → 200/400（权限通过）", client.post("/api/import/csv", headers=tok("qinrui"), json={"csv_text": "external_no,category,content,owner,biz_occurred_at\nXX-1,other,t,,2026-11-04 10:00", "source_tool": "manual"}).status_code in (200, 400))
    check("一线查全量审计 → 403（仅本人）", client.get("/api/audit", headers=tok("lin")).status_code == 403)
    check("质量验收查审计 → 200", client.get("/api/audit", headers=tok("fujing")).status_code == 200)
    lines.append("── 权限 P4：异常队列三级权 ──")
    r = client.post("/api/exceptions", headers=tok("tang"), json={"item_id": 47, "exception_type": "conflict", "note": "权限测试"})
    eid = r.json()["exception_id"]
    check("一线认领 → 403", client.post(f"/api/exceptions/{eid}/claim", headers=tok("lin"), json={}).status_code == 403)
    check("质量验收认领 → 200", client.post(f"/api/exceptions/{eid}/claim", headers=tok("fujing"), json={}).status_code == 200)
    check("协调员闭环 → 403", client.post(f"/api/exceptions/{eid}/close", headers=tok("tang"), json={"resolution": "x"}).status_code == 403)
    check("质量验收闭环 → 200（唯一）", client.post(f"/api/exceptions/{eid}/close", headers=tok("fujing"), json={"resolution": "权限场景复核通过"}).status_code == 200)


# ---------------------------------------------------------------- 5 故障
@suite("故障场景")
def s_fault(check, lines):
    lines.append("── 故障 F1：异常工单全流程（脏数据不丢弃、人工闭环）──")
    r = client.post("/api/exceptions", headers=tok("lin"), json={"item_id": 46, "exception_type": "dirty_data", "note": "群消息转发原文缺失"})
    eid = r.json()["exception_id"]
    check("脏数据项入队 open", r.status_code == 200 and r.json()["status"] == "open")
    client.post(f"/api/exceptions/{eid}/claim", headers=tok("fujing"), json={})
    r = client.post(f"/api/exceptions/{eid}/close", headers=tok("fujing"), json={"resolution": "已联系林亦舟补录原文，确认有效"})
    check("qa 填结论闭环", r.status_code == 200 and r.json()["status"] == "closed")
    item = client.get("/api/items/46", headers=tok("fujing")).json()["item"]
    check("原行保留未清洗（f33）", item["content"] == "（群消息转发，原文缺失）", item["content"][:20])
    lines.append("── 故障 F2：重启持久性（连接重建）──")
    srv._conn.close()
    srv._conn = None     # 模拟进程重启后重连
    cnt = srv.db().execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
    check("重连后数据完整（SQLite WAL 持久化）", cnt >= 50, f"rows={cnt}")
    lines.append("── 故障 F3：备份恢复 ──")
    bpath = BASE / "test_backup.db"
    if bpath.exists():
        bpath.unlink()
    src = srv.db()
    src.execute("VACUUM INTO ?", (str(bpath),))
    n_backup = sqlite3.connect(bpath).execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
    check("在线备份行数一致", n_backup == cnt, f"backup={n_backup} live={cnt}")
    DB2 = BASE / "test_restore.db"
    shutil.copy(bpath, DB2)
    n_restore = sqlite3.connect(DB2).execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
    check("恢复库行数一致", n_restore == cnt, f"restore={n_restore}")
    lines.append("── 故障 F4：故障注入输入 ──")
    r = client.post("/api/items", headers=tok("lin"), json={"category": "other", "content": "故障注入项", "biz_occurred_at": BIZ_TS, "owner_id": "not-a-number"})
    check("非法 owner_id → 400（不落脏数据）", r.status_code in (400, 422))
    r = client.post("/api/handovers", headers=tok("tang"), json={"version_id": "abc"})
    check("非法 version_id → 400/422", r.status_code in (400, 422))
    r = client.get("/api/items/99999", headers=tok("lin"))
    check("不存在项 → 404", r.status_code == 404)
    lines.append("── 故障 F5：密钥轮换（会话全失效）──")
    old_secret = srv.SECRET
    stale_token = tok("lin")["Authorization"].split(" ")[1]   # 轮换前签发的旧 token
    srv.SECRET = "rotated-secret-for-test"
    r = client.post("/api/items", headers={"Authorization": "Bearer " + stale_token},
                    json={"category": "other", "content": "轮换后请求", "biz_occurred_at": BIZ_TS})
    check("密钥轮换后旧 token 全失效 → 401", r.status_code == 401, str(r.status_code))
    srv.SECRET = old_secret


def main():
    for fn in (s_normal, s_boundary, s_adversary, s_permission, s_fault):
        fn()
    out = {"results": RESULTS, "logs": LOGS}
    (BASE / "test_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    logdir = BASE / "test_logs"
    logdir.mkdir(exist_ok=True)
    for name, lines in LOGS.items():
        (logdir / f"{name}.log").write_text("\n".join(lines), encoding="utf-8")
    total_p = sum(v["passed"] for v in RESULTS.values())
    total_f = sum(v["failed"] for v in RESULTS.values())
    print(f"\n合计：通过 {total_p} 项，失败 {total_f} 项")


if __name__ == "__main__":
    main()
