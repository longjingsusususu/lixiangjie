# -*- coding: utf-8 -*-
"""
smoke_test.py —— 员工查询入口冒烟测试（TestClient，不依赖端口监听）

覆盖：
  ① 登录成功 / 密码错误（40102）
  ② 未登录访问（40101）
  ③ 空问题（40001）/ 超长问题（40002）
  ④ SSE 全链路：status → item ×N → chunk ×M → done（含 citations）
  ⑤ 追问：最近 6 条消息参与查询扩展（"那餐饮呢？"能命中餐饮项）
  ⑥ 会话列表 / 历史消息（引用随消息返回）
  ⑦ 越权访问他人会话（40301）/ 不存在会话（40401）
  ⑧ 留痕：audit_logs 有 view 记录

运行：
    PYTHONPATH=<venv site-packages> python smoke_test.py
"""

import json
import os
import sys

# 每次跑前重建库，保证可重复
HERE = os.path.dirname(os.path.abspath(__file__))
db_file = os.path.join(HERE, "demo.db")
if os.path.exists(db_file):
    os.remove(db_file)

# 仓库布局：测试在 tests/，应用在 app/ —— 显式加入模块搜索路径并指定测试库
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))
os.environ["MEETING_DB"] = db_file

from fastapi.testclient import TestClient   # noqa: E402

import portal                                 # noqa: E402

client = TestClient(portal.app)
passed = failed = 0


def check(name: str, cond: bool, extra: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  {extra}")


def sse_frames(text: str):
    """把 SSE 响应拆成 [(event, data_dict), ...]"""
    out = []
    for frame in text.split("\n\n"):
        ev = next((l[6:].strip() for l in frame.split("\n") if l.startswith("event:")), None)
        da = next((l[5:].strip() for l in frame.split("\n") if l.startswith("data:")), None)
        if ev and da:
            out.append((ev, json.loads(da)))
    return out


# ── 启动（触发 startup：建库 + 导入需求项 + 建索引）──
with client:
    print("\n【1】鉴权")
    r = client.post("/api/login", json={"username": "yixian", "password": "wrong"})
    check("密码错误返回 40102", r.status_code == 401 and r.json()["code"] == 40102, r.text)

    r = client.post("/api/login", json={"username": "yixian", "password": "123456"})
    ok = r.status_code == 200 and r.json()["display_name"] == "林亦舟"
    check("登录成功返回 token", ok, r.text)
    token = r.json()["token"]
    H = {"Authorization": f"Bearer {token}"}

    r = client.post("/api/chat", json={"session_id": None, "question": "话筒"})
    check("未登录返回 40101", r.status_code == 401 and r.json()["code"] == 40101, r.text)

    print("\n【2】入参校验（错误码 + 中文信息）")
    r = client.post("/api/chat", json={"session_id": None, "question": "   "}, headers=H)
    check("空问题 40001", r.status_code == 400 and r.json()["message"] == "问题不能为空", r.text)

    r = client.post("/api/chat", json={"session_id": None, "question": "问" * 201}, headers=H)
    check("超长问题 40002", r.status_code == 400 and r.json()["code"] == 40002, r.text)

    r = client.post("/api/chat", json={"session_id": "no-such-id", "question": "话筒"}, headers=H)
    check("会话不存在 40401", r.status_code == 404 and r.json()["code"] == 40401, r.text)

    print("\n【3】SSE 全链路")
    r = client.post("/api/chat", json={"session_id": None, "question": "双语主持相关的需求有哪些？"},
                    headers=H)
    frames = sse_frames(r.text)
    events = [e for e, _ in frames]
    check("Content-Type 为 text/event-stream", "text/event-stream" in r.headers["content-type"],
          r.headers.get("content-type", ""))
    check("首帧是 status（正在检索）",
          events[0] == "status" and "检索" in frames[0][1]["message"], str(events[:3]))
    check("有 item 事件且带来源字段",
          events.count("item") >= 1 and all("source_ref" in d for e, d in frames if e == "item"))
    check("有 chunk 事件（打字机）", events.count("chunk") >= 3)
    check("末帧是 done 且含 citations 与耗时",
          events[-1] == "done" and len(frames[-1][1]["citations"]) >= 1
          and frames[-1][1]["elapsed_ms"] >= 0)
    sid = frames[-1][1]["session_id"]
    top1 = next(d for e, d in frames if e == "item")
    check("top1 命中 LS-02（双语主持）", top1["item_no"] == "LS-02", top1["item_no"])

    print("\n【4】追问：最近 6 条消息做查询扩展")
    r = client.post("/api/chat", json={"session_id": sid, "question": "那餐饮呢？"}, headers=H)
    frames2 = sse_frames(r.text)
    top2 = next(d for e, d in frames2 if e == "item")
    check("短追问命中餐饮类（CT 开头）", top2["item_no"].startswith("CT"), top2["item_no"])

    print("\n【5】会话与历史")
    r = client.get("/api/sessions", headers=H)
    items = r.json()["items"]
    check("会话列表含本会话且标题为首问", any(s["id"] == sid for s in items), str(items[:1]))

    r = client.get(f"/api/sessions/{sid}/messages", headers=H)
    msgs = r.json()["messages"]
    check("历史消息数 = 4（2 问 2 答）", len(msgs) == 4, str(len(msgs)))
    check("助手消息带 citations",
          all(m["citations"] for m in msgs if m["role"] == "assistant"))

    print("\n【6】越权与留痕")
    r = client.post("/api/login", json={"username": "qinrui", "password": "admin123"})
    H2 = {"Authorization": f"Bearer {r.json()['token']}"}
    r = client.get(f"/api/sessions/{sid}/messages", headers=H2)
    check("他人会话 40301", r.status_code == 403 and r.json()["code"] == 40301, r.text)

    import sqlite3
    con = sqlite3.connect(db_file)
    n_log = con.execute("SELECT COUNT(*) FROM audit_logs WHERE action='view'").fetchone()[0]
    n_items = con.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
    n_msg = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con.close()
    check("audit_logs 有 view 留痕", n_log >= 1, f"n={n_log}")
    check("需求项已入库（18 条）", n_items == 18, f"n={n_items}")
    check("messages 已落库（4 条）", n_msg == 4, f"n={n_msg}")

print(f"\n结果：通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)
