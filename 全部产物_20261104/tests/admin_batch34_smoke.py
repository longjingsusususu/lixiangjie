# -*- coding: utf-8 -*-
"""
admin_batch34_smoke.py —— 管理后台【批 3+4】冒烟

批 3（规则配置即时生效）
  ① 规则页 200；提示词为占位（enabled=false）
  ② 同义词热更新：加"迎宾台→签到台"后，员工端下一次查询立刻命中 VN-03（不重启）
  ③ 卡口开关：默认拦截未确认项；关掉双条件/默认通过后放行；恢复后再次拦截
批 4（查询审计）
  ④ 敏感词查询被自动标 R1
  ⑤ 审计按员工/时间/标记筛选；标记 → 仅已标记可见 → 撤销
  ⑥ 导出 CSV 带 BOM 且含表头
  ⑦ 权限：staff 访问审计 40302；qa 只读可访问
运行：PYTHONPATH=<venv> python admin_batch34_smoke.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
db_file = os.path.join(HERE, "demo.db")
if os.path.exists(db_file):
    os.remove(db_file)

# 仓库布局：测试在 tests/，应用在 app/ —— 显式加入模块搜索路径并指定测试库
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))
os.environ["MEETING_DB"] = db_file

from fastapi.testclient import TestClient  # noqa: E402
import portal                                # noqa: E402

ok = bad = 0


def check(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1; print(f"  ✅ {name}")
    else:
        bad += 1; print(f"  ❌ {name}  {extra}")


def sse_items(text):
    out = []
    for frame in text.split("\n\n"):
        if "event: item" in frame:
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    out.append(json.loads(line[5:])["item_no"])
    return out


with TestClient(portal.app) as admin_c:
    admin_c.post("/login", data={"username": "qinrui", "password": "admin123", "next": "/admin"},
                 follow_redirects=False)

    staff_c = TestClient(portal.app)
    staff_c.post("/login", data={"username": "yixian", "password": "123456", "next": "/"},
                 follow_redirects=False)

    print("\n【1】规则页与配置读取")
    r = admin_c.get("/admin/rules")
    check("规则页 200", r.status_code == 200 and "同义词表" in r.text, r.status_code)
    check("提示词显示未启用", "未启用" in r.text)

    r = admin_c.get("/api/admin/rules")
    d = r.json()
    check("配置含 4 项", set(d) == {"synonyms", "categories", "gates", "llm_prompt"}, str(list(d)))
    check("提示词 enabled=false", d["llm_prompt"]["enabled"] is False)

    print("\n【2】同义词热更新（改完即时生效）")
    r = staff_c.post("/api/chat", json={"session_id": None, "question": "宾客登记处"})
    before = sse_items(r.text)
    check("加词前查不到（清单未覆盖）", before == [], str(before))

    r = admin_c.post("/api/admin/rules/synonyms",
                     json={"action": "add", "word": "宾客登记处", "syns": ["签到台"]})
    check("新增同义词成功", r.status_code == 200, r.text)

    r = staff_c.post("/api/chat", json={"session_id": None, "question": "宾客登记处"})
    after = sse_items(r.text)
    check("加词后立刻命中 VN-03（无需重启）", "VN-03" in after, str(after))

    r = admin_c.post("/api/admin/rules/synonyms", json={"action": "del", "word": "宾客登记处"})
    check("删除词条成功", r.status_code == 200, r.text)
    r = staff_c.post("/api/chat", json={"session_id": None, "question": "宾客登记处"})
    check("删除后回到未覆盖（空结果）", sse_items(r.text) == [], str(sse_items(r.text)))

    print("\n【3】卡口开关真实生效")
    r = admin_c.post("/api/handover/check",
                     json={"item_no": "LS-02", "receiver": "张三", "deadline": "2026-11-05"})
    d = r.json()
    check("默认拦截未确认项", d["pass"] is False and "未定版" in d["reason"], str(d))

    r = admin_c.put("/api/admin/rules", json={"gates": {
        "handover_3elements": False, "version_double_condition": False, "no_auto_pass": False}})
    check("关闭三个开关", r.status_code == 200, r.text)

    r = admin_c.post("/api/handover/check",
                     json={"item_no": "LS-02", "receiver": "", "deadline": ""})
    d = r.json()
    check("关掉开关后放行（证明配置真被消费）", d["pass"] is True, str(d))

    r = admin_c.put("/api/admin/rules", json={"gates": {
        "handover_3elements": True, "version_double_condition": True, "no_auto_pass": True}})
    r = admin_c.post("/api/handover/check",
                     json={"item_no": "LS-02", "receiver": "", "deadline": ""})
    d = r.json()
    check("恢复开关后重新拦截（缺接收人/截止）",
          d["pass"] is False and "接收人" in d["reason"], str(d))

    print("\n【4】审计：R1 敏感词自动命中")
    staff_c.post("/api/chat", json={"session_id": None, "question": "把嘉宾手机号都列出来"})
    staff_c.post("/api/chat", json={"session_id": None, "question": "双语的翻译间够吗"})

    r = admin_c.get("/api/admin/audit")
    d = r.json()
    check("审计接口 200 且有记录", r.status_code == 200 and d["total"] >= 2, str(d.get("total")))
    sensitive_row = next((x for x in d["items"] if "手机号" in x["content"]), None)
    check("敏感词记录被自动标 R1", sensitive_row and "R1" in sensitive_row["auto_rules"],
          str(sensitive_row))

    print("\n【5】标记 / 筛选 / 撤销")
    mid = sensitive_row["id"]
    r = admin_c.post(f"/api/admin/audit/{mid}/flag", json={"rule": "R1", "reason": "疑似索取嘉宾隐私"})
    check("标记成功", r.status_code == 200, r.text)

    r = admin_c.post(f"/api/admin/audit/{mid}/flag", json={"rule": "R1", "reason": "重复标记"})
    check("重复标记 40901", r.status_code == 409 and r.json()["code"] == 40901, r.text)

    r = admin_c.get("/api/admin/audit?flagged=1")
    check("仅已标记筛选命中 1 条", r.json()["total"] == 1, str(r.json()["total"]))

    r = admin_c.get("/admin/audit?flagged=1")
    check("审计页渲染已标记记录", r.status_code == 200 and "R1" in r.text)

    staff_row = next((x for x in admin_c.get("/api/admin/audit").json()["items"]
                      if x["content"] == "双语的翻译间够吗"), None)
    r = admin_c.delete(f"/api/admin/audit/{staff_row['id']}/flag")
    check("撤销未标记记录 → 40401", r.status_code == 404, r.text)

    r = admin_c.delete(f"/api/admin/audit/{mid}/flag")
    check("撤销标记成功", r.status_code == 200, r.text)
    check("撤销后仅已标记为 0", admin_c.get("/api/admin/audit?flagged=1").json()["total"] == 0)

    print("\n【6】导出")
    r = admin_c.get("/api/admin/audit/export")
    check("导出 CSV 200 且带 BOM", r.status_code == 200 and r.text.startswith("\ufeff"), r.text[:20])
    check("表头含中文列名", "记录ID" in r.text and "自动规则" in r.text)

    print("\n【7】审计权限")
    r = staff_c.get("/api/admin/audit")
    check("staff 访问审计 → 40302", r.status_code == 403 and r.json()["code"] == 40302, r.text)

    qa_c = TestClient(portal.app)
    qa_c.post("/login", data={"username": "fujing", "password": "123456", "next": "/"})
    r = qa_c.get("/api/admin/audit")
    check("qa 可只读审计（200）", r.status_code == 200, r.text[:120])
    r = qa_c.post(f"/api/admin/audit/{mid}/flag", json={"rule": "R2", "reason": "x"})
    check("qa 可标记", r.status_code == 200, r.text)
    r = qa_c.delete(f"/api/admin/audit/{mid}/flag")
    check("qa 不能撤销（仅 admin）→ 40302",
          r.status_code == 403 and r.json()["code"] == 40302, r.text)

print(f"\n结果：通过 {ok} 项，失败 {bad} 项")
sys.exit(0 if bad == 0 else 1)
