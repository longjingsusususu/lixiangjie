# -*- coding: utf-8 -*-
"""
agent_trace_smoke.py —— Agent 编排层冒烟（独立库，可反复跑）

覆盖：
  【1】轨迹 A：只查清单（不调写工具，2 轮内收束）
  【2】轨迹 B：建交接（查→验→中断→确认→落库），含"确认前绝不落库"的硬断言
  【3】轨迹 C：三源冲突（只报冲突、judgement 恒 none）
  【4】只校验（"能交接吗"）：给结论但不写数据
  【5】取消：accept=false 全程零业务写入
  【6】票据过期后确认：41202 → 如实告知，不崩、不重试
  【7】轮次上限：强制决策器永远选工具，验证第 5 轮收束
  【8】日志表：每轮决策一行 decide、每次执行一行 act（含耗时/摘要/错误码）
  【9】越权与重复确认

运行：
  set PYTHONPATH=...\envs\default\Lib\site-packages
  python agent_trace_smoke.py
"""

import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(BASE), "app"))   # 应用在 app/ 下
DB = os.path.join(BASE, "demo_trace.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except OSError:
        pass
os.environ["MEETING_DB"] = DB                 # 必须在 import portal 之前设置
os.environ["MEETING_LLM_PROVIDER"] = "off"    # 一期零 LLM

from fastapi.testclient import TestClient   # noqa: E402
from portal import app                      # noqa: E402

client = TestClient(app)
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  <<< {extra}")


def frames(text: str):
    """把 SSE 原文切成 [(event, data_dict)]"""
    out = []
    for raw in text.split("\n\n"):
        if not raw.strip():
            continue
        ev, data = "", ""
        for line in raw.split("\n"):
            if line.startswith("event: "):
                ev = line[7:].strip()
            elif line.startswith("data: "):
                data += line[6:]
        if ev and data:
            try:
                out.append((ev, json.loads(data)))
            except Exception:
                pass
    return out


def login(u, p="123456"):
    r = client.post("/api/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def chat(h, goal):
    r = client.post("/api/agent/chat", json={"goal": goal}, headers=h)
    return r, frames(r.text)


def step_rows(run_id):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(x) for x in conn.execute(
        "SELECT * FROM agent_steps WHERE run_id=? ORDER BY id", (run_id,))]
    conn.close()
    return rows


def db_one(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, args).fetchone()
    conn.close()
    return dict(row) if row else None


def db_all(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(x) for x in conn.execute(sql, args)]
    conn.close()
    return rows


with client:
    h_coord = login("tangyq")     # 协调员：可交接
    h_admin = login("qinrui", "admin123")

    # 找两条「已定版」的项做交接用例（避免写死编号，换样本文件也不崩）
    ready = db_all("SELECT item_no,owner,content FROM requirement_items "
                   "WHERE status='已定版' AND is_deleted=0 ORDER BY item_no")
    print(f"\n[准备] 已定版需求项 {len(ready)} 条："
          f"{'、'.join(r['item_no'] for r in ready)}")
    target = ready[0]["item_no"]
    target2 = ready[1]["item_no"] if len(ready) > 1 else None

    # ========================================================
    print("\n【1】轨迹 A：只查清单（不该出现任何写工具）")
    r, fs = chat(h_coord, "双语主持相关的需求有哪些？")
    kinds = [e for e, _ in fs]
    run_id = fs[0][1]["run_id"]
    check("HTTP 200 + SSE", r.status_code == 200 and "text/event-stream" in r.headers["content-type"])
    check("首帧 run", kinds[0] == "run", kinds[:3])
    check("意图=query", fs[0][1]["intent"] == "query")
    act_tools = [d["tool"] for e, d in fs if e == "observe"]
    check("只调了 search_items", act_tools == ["search_items"], act_tools)
    check("无写工具", not any(t in ("create_handover", "upsert_item") for t in act_tools))
    check("末帧 done", kinds[-1] == "done")
    last = db_one("SELECT * FROM agent_runs WHERE id=?", (run_id,))
    check("run 状态 done 且轮次≤5", last["status"] == "done" and last["rounds"] <= 5,
          f"{last['status']}/{last['rounds']}")
    check("结论含命中条目", "LS-02" in (last["final_answer"] or ""),
          (last["final_answer"] or "")[:60])
    rows = step_rows(run_id)
    check("日志有 decide/act/final 三类",
          {"decide", "act", "final"} <= {x["phase"] for x in rows},
          [x["phase"] for x in rows])
    check("act 行有耗时与摘要", any(x["phase"] == "act" and x["duration_ms"] >= 0
                                   and x["digest"] for x in rows))

    # ========================================================
    print("\n【2】轨迹 B：建交接（中断 → 确认 → 落库）")
    before = db_one("SELECT COUNT(*) c FROM handovers")["c"]
    r, fs = chat(h_coord, f"把 {target} 交接给傅景，截止 11-05")
    run_id = fs[0][1]["run_id"]
    kinds = [e for e, _ in fs]
    check("意图=handover", fs[0][1]["intent"] == "handover")
    check("槽位抽到编号/接收人/截止", fs[0][1]["slots"]["item_no"] == target
          and fs[0][1]["slots"]["receiver"] == "傅景"
          and fs[0][1]["slots"]["deadline"].endswith("-11-05"), fs[0][1]["slots"])
    check("出现了 draft 帧（草稿流式）", "draft" in kinds, kinds)
    check("以 interrupt 结束（未出 final）", kinds[-1] == "interrupt", kinds[-3:])
    raw_draft = "".join(d["delta"] for e, d in fs if e == "draft")
    check("草稿含接收人与截止时间", "傅景" in raw_draft and "11-05" in raw_draft)
    check("草稿说明确认前不落库", "确认前不产生" in raw_draft, raw_draft[:80])
    act_tools = [d["tool"] for e, d in fs if e == "observe"]
    check("中断前走完 查→验", act_tools == ["get_item", "validate_handover"], act_tools)

    pend = db_one("SELECT * FROM agent_runs WHERE id=?", (run_id,))
    check("run 置 awaiting_confirm", pend["status"] == "awaiting_confirm", pend["status"])
    check("【硬断言】确认前 handovers 未增加",
          db_one("SELECT COUNT(*) c FROM handovers")["c"] == before)
    check("【硬断言】确认前项状态未变",
          db_one("SELECT status FROM requirement_items WHERE item_no=?",
                 (target,))["status"] == "已定版")

    r = client.post("/api/agent/confirm", json={"run_id": run_id, "accept": True},
                    headers=h_coord)
    d = r.json()
    check("确认接口 200", r.status_code == 200, r.text[:120])
    check("确认后状态 done", d["status"] == "done", d["status"])
    check("确认后确实落库", db_one("SELECT COUNT(*) c FROM handovers")["c"] == before + 1)
    check("项已变更为已交接",
          db_one("SELECT status FROM requirement_items WHERE item_no=?",
                 (target,))["status"] == "已交接")
    check("结论提到已建立交接", "已建立交接" in d["answer"], d["answer"][:60])
    rows = step_rows(run_id)
    check("日志记录了 interrupt 阶段",
          any(x["phase"] == "interrupt" for x in rows), [x["phase"] for x in rows])
    check("日志记录了 create_handover 的 act",
          any(x["phase"] == "act" and x["tool"] == "create_handover" for x in rows))
    check("确认也进了 audit_logs",
          any("agent_confirm:accept" == a["action"] for a in db_all(
              "SELECT action FROM audit_logs WHERE target_id=?", (run_id,))))

    r = client.post("/api/agent/confirm", json={"run_id": run_id, "accept": True},
                    headers=h_coord)
    check("重复确认 → 40902", r.status_code == 409 and r.json()["code"] == 40902, r.text[:120])

    # ========================================================
    print("\n【3】轨迹 C：三源冲突（只报不裁）")
    r, fs = chat(h_coord, "EQ-05 的翻译间到底几间？")
    run_id = fs[0][1]["run_id"]
    check("意图=conflict", fs[0][1]["intent"] == "conflict")
    obs = [d for e, d in fs if e == "observe"]
    check("调用 diff_sources", [o["tool"] for o in obs] == ["diff_sources"],
          [o["tool"] for o in obs])
    check("报告了冲突", obs[0]["ok"] and obs[0]["result"]["conflicts"])
    check("judgement 恒 none", obs[0]["result"]["judgement"] == "none")
    ans = db_one("SELECT final_answer FROM agent_runs WHERE id=?", (run_id,))["final_answer"]
    check("结论不替人裁定", "不做清洗" in ans or "不替人裁定" in ans, ans[:80])

    # ========================================================
    print("\n【4】只校验（能交接吗）：给结论、不写数据")
    before = db_one("SELECT COUNT(*) c FROM handovers")["c"]
    r, fs = chat(h_coord, "LS-02 交接给傅景，截止 11-05，能过吗？")
    check("意图=precheck", fs[0][1]["intent"] == "precheck", fs[0][1]["intent"])
    check("只调 validate_handover",
          [d["tool"] for e, d in fs if e == "observe"] == ["validate_handover"])
    check("没有 draft / interrupt", "draft" not in [e for e, _ in fs])
    ans = [d["answer"] for e, d in fs if e == "final"][0]
    check("给出不能交接的原因", "不能交接" in ans, ans[:80])
    check("未产生交接记录", db_one("SELECT COUNT(*) c FROM handovers")["c"] == before)

    # ========================================================
    print("\n【5】取消：零业务写入")
    if target2:
        before_h, before_s = (db_one("SELECT COUNT(*) c FROM handovers")["c"],
                              db_one("SELECT status FROM requirement_items WHERE item_no=?",
                                     (target2,))["status"])
        r, fs = chat(h_coord, f"把 {target2} 交接给傅景，截止 12-01")
        run_id = fs[0][1]["run_id"]
        r = client.post("/api/agent/confirm",
                        json={"run_id": run_id, "accept": False, "note": "先不交"}, headers=h_coord)
        d = r.json()
        check("取消后状态 cancelled", d["status"] == "cancelled", d["status"])
        check("取消后未落库", db_one("SELECT COUNT(*) c FROM handovers")["c"] == before_h)
        check("取消后状态未变", db_one(
            "SELECT status FROM requirement_items WHERE item_no=?",
            (target2,))["status"] == before_s)
        check("取消也写了审计", any("agent_confirm:reject" == a["action"] for a in db_all(
            "SELECT action FROM audit_logs WHERE target_id=?", (run_id,))))
    else:
        print("  （样本中只有一条已定版项，跳过取消用例）")

    # ========================================================
    print("\n【6】票据过期后再确认 → 41202，如实告知")
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE requirement_items SET status='已定版' WHERE item_no='CT-02'")
    conn.commit()
    conn.close()
    r, fs = chat(h_coord, "把 CT-02 交接给傅景，截止 12-15")
    run_id = fs[0][1]["run_id"]
    pend = json.loads(db_one("SELECT pending_action FROM agent_runs WHERE id=?",
                             (run_id,))["pending_action"])
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE handover_tickets SET expires_at=? WHERE ticket=?",
                 (int(time.time()) - 10, pend["args"]["ticket"]))
    conn.commit()
    conn.close()
    r = client.post("/api/agent/confirm", json={"run_id": run_id, "accept": True}, headers=h_coord)
    d = r.json()
    check("过期确认不炸（仍 200）", r.status_code == 200, r.text[:120])
    check("结论提示票据过期", "过期" in d["answer"], d["answer"][:80])
    check("未产生交接", db_one("SELECT COUNT(*) c FROM handovers WHERE item_no='CT-02'")["c"] == 0)
    check("日志把 41202 记下来了",
          any(x["code"] == 41202 for x in step_rows(run_id)), step_rows(run_id))

    # ========================================================
    print("\n【7】轮次上限：强制决策器永远选工具 → 第 5 轮收束")
    from agent import planner as planner_mod, runtime as rt

    orig = planner_mod.decide
    planner_mod.decide = lambda ctx: {"type": "tool", "tool": "search_items",
                                      "args": {"query": ctx["goal"]}, "why": "上限测试"}
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    u = dict(conn.execute("SELECT * FROM users WHERE username='tangyq'").fetchone())
    conn.close()
    evs = list(rt.run_stream(u, "无限循环测试"))
    status = [e for e in evs if e["type"] == "done"][0]["status"]
    planner_mod.decide = orig
    check("状态=max_rounds", status == "max_rounds", status)
    run_id = evs[0]["run_id"]
    row = db_one("SELECT rounds,status FROM agent_runs WHERE id=?", (run_id,))
    check("轮次正好 5", row["rounds"] == 5, row)
    check("act 恰好 5 次",
          len([x for x in step_rows(run_id) if x["phase"] == "act"]) == 5)

    # ========================================================
    print("\n【8】越权：别人的 run 不能确认")
    if target2:
        r, fs = chat(h_coord, f"把 {target2} 交接给傅景，截止 12-20")
        run_id = fs[0][1]["run_id"]
        h_other = login("yixian")
        r = client.post("/api/agent/confirm", json={"run_id": run_id, "accept": True},
                        headers=h_other)
        check("他人确认 → 40301", r.status_code == 403 and r.json()["code"] == 40301,
              r.text[:120])

    # ========================================================
    print("\n【9】阶段 5 统计接口")
    r = client.get("/api/admin/agent/stats?days=7", headers=h_admin)
    st = r.json()
    check("stats 200", r.status_code == 200, r.text[:120])
    check("按工具聚合有数据", len(st["by_tool"]) >= 3, st["by_tool"])
    check("含错误码分布（41202）",
          any(c["code"] == 41202 for c in st["error_codes"]), st["error_codes"])
    check("含状态分布", any(s["status"] in ("done", "cancelled", "max_rounds")
                            for s in st["status_mix"]), st["status_mix"])
    check("平均轮次 ≤5", (st["rounds"]["max"] or 0) <= 5, st["rounds"])

    r = client.get("/admin/agent", headers=h_admin)
    check("管理页 200", r.status_code == 200, r.text[:80])
    r = client.get("/api/agent/runs", headers=h_coord)
    check("我的任务列表可用", r.status_code == 200 and len(r.json()["items"]) >= 3)

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
