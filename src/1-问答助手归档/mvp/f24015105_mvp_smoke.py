# -*- coding: utf-8 -*-
"""
f24015105_mvp_smoke.py —— MVP 冒烟测试（TestClient，不起端口、不依赖网络）

覆盖：错误码契约、SSE 事件序、会话落库、历史带引用、查询扩展（省略式追问）。
运行：python tests/../mvp/f24015105_mvp_smoke.py  （任意目录可跑）
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, HERE)

# 独立临时库，测试互不污染、可重复执行
tmp = tempfile.mkdtemp(prefix="f24015105_mvp_")
os.environ["MVP_DB"] = os.path.join(tmp, "mvp.db")

from fastapi.testclient import TestClient  # noqa: E402
import f24015105_mvp_server as srv  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, extra: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {extra}")


client = TestClient(srv.app)
with client:  # 触发 startup：建表 + 加载索引
    # ── 1. 错误码契约：全部在建流之前以普通 JSON 返回 ──
    print("【1】错误码契约")
    r = client.post("/api/chat", json={"session_id": None, "question": "  "})
    check("空问题 → 400/40001", r.status_code == 400
          and r.json() == {"code": 40001, "message": "问题不能为空"}, r.text)
    r = client.post("/api/chat", json={"question": "长" * 201})
    check("超长问题 → 400/40002", r.status_code == 400 and r.json()["code"] == 40002)
    r = client.post("/api/chat", json={"question": "餐饮", "session_id": "不存在"})
    check("会话不存在 → 404/40401", r.status_code == 404 and r.json()["code"] == 40401)

    # ── 2. 正常问答：SSE 事件序 status → item×N → chunk×M → done ──
    print("【2】SSE 全链路")
    r = client.post("/api/chat", json={"session_id": None, "question": "双语主持定了吗"})
    check("HTTP 200 且 content-type 是 event-stream",
          r.status_code == 200 and "event-stream" in r.headers.get("content-type", ""))
    events = []
    for frame in r.text.split("\n\n"):
        ev = data = None
        for line in frame.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            events.append((ev, data))
    kinds = [e for e, _ in events]
    check("事件序以 status 开头", kinds and kinds[0] == "status")
    check("含 item 事件", "item" in kinds)
    check("含 chunk 事件", "chunk" in kinds)
    check("以 done 结尾", kinds[-1] == "done", str(kinds[-3:]))
    done = dict(events)["done"]
    check("done 带引用来源（来源工具｜单据号｜行位置 + 片段）",
          isinstance(done.get("citations"), list)
          and all("item_no" in c and "source_tool" in c and "snippet" in c
                  for c in done["citations"]), str(done)[:120])
    sid = done["session_id"]
    check("done 返回新建会话 id", bool(sid))

    # ── 3. 落库：会话与消息可回看，引用随历史返回 ──
    print("【3】SQLite 落库")
    r = client.get("/api/sessions")
    check("会话列表包含新会话",
          any(s["id"] == sid for s in r.json()["sessions"]))
    r = client.get(f"/api/sessions/{sid}/messages")
    msgs = r.json()["messages"]
    check("历史 2 条（user + assistant）", len(msgs) == 2, str(len(msgs)))
    check("assistant 消息带引用", bool(msgs[-1]["citations"]))
    check("assistant 消息带耗时", isinstance(msgs[-1]["latency_ms"], int))

    # ── 4. 查询扩展：省略式追问能命中 ──
    print("【4】最近 6 轮上下文（查询扩展）")
    r = client.post("/api/chat", json={"session_id": sid, "question": "那餐饮呢"})
    done2 = [json.loads(f.split("data: ", 1)[1])
             for f in r.text.split("\n\n") if f.startswith("event: done")]
    check("追问返回 done 且引用非空", bool(done2) and bool(done2[0]["citations"]))
    check("追问复用同一会话", bool(done2) and done2[0]["session_id"] == sid)

print(f"\n结果：通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
