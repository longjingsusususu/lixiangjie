# -*- coding: utf-8 -*-
"""
admin_batch2_smoke.py —— 管理后台【批 2】冒烟：需求项库 / 批量导入 / 编号映射

覆盖：
  ① 列表页 200、筛选生效、分页
  ② 新增：格式校验（40001）、编号重复（40901）、新增后立刻可被员工端检索到
  ③ 编辑：写 item_changes（逐字段留痕）
  ④ 软删：列表消失、检索消失、审计仍可回查（is_deleted=1 而记录还在）
  ⑤ 导入 md：预检报告（4 错误行 + 2 重复跳过）→ 确认导入 → 幂等重跑 0 新增
  ⑥ 导入 CSV：模板下载 / 类型不符 41501 / 非 UTF-8 42201
  ⑦ 编号映射：新增、重复 40901、删除
运行：PYTHONPATH=<venv> python admin_batch2_smoke.py
"""

import io
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


def login_admin(c):
    r = c.post("/login", data={"username": "qinrui", "password": "admin123", "next": "/admin"},
               follow_redirects=False)
    assert r.status_code == 302, r.text


def sse_items(text):
    """取 SSE 里的 item 事件编号列表（用于验证检索是否命中新数据）。"""
    out = []
    for frame in text.split("\n\n"):
        if "event: item" in frame:
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    import json as _j
                    out.append(_j.loads(line[5:])["item_no"])
    return out


with TestClient(portal.app) as c:
    login_admin(c)
    print("\n【1】列表与筛选")
    r = c.get("/admin/items")
    check("需求项库页 200 且含 18 项", r.status_code == 200 and "共 18 项" in r.text, r.status_code)

    r = c.get("/admin/items?status=缺失")
    check("按状态筛选（缺失 4 项）", "共 4 项" in r.text, r.text[:200])

    r = c.get("/admin/items?q=LS-02")
    check("关键词筛选命中 LS-02", "LS-02" in r.text and "共 1 项" in r.text)

    print("\n【2】新增")
    r = c.post("/api/admin/items", json={"item_no": "bad", "category": "设备",
                                          "status": "未确认", "content": "x"})
    check("编号格式非法 40001", r.status_code == 400 and r.json()["code"] == 40001, r.text)

    r = c.post("/api/admin/items", json={"item_no": "EQ-01", "category": "设备",
                                          "status": "未确认", "content": "重复编号"})
    check("编号重复 40901", r.status_code == 409 and r.json()["code"] == 40901, r.text)

    r = c.post("/api/admin/items", json={
        "item_no": "EQ-09", "category": "设备", "status": "未确认", "content": "新增测试：备用投影遥控器",
        "owner": "林亦舟", "source_tool": "手工补录", "source_ref": "测试", "biz_occurred_at": "2026-10-29 10:00"})
    check("新增 EQ-09 成功", r.status_code == 200 and r.json()["ok"], r.text)

    # 员工端立刻能检索到（rebuild_index 生效）
    r = c.get("/api/sessions")   # 随便一次请求确保 cookie 有效
    r2 = c.post("/api/chat", json={"session_id": None, "question": "投影遥控器"},
                headers={"Authorization": "Bearer " + portal.jwt.encode(
                    {"uid": 5, "exp": 9999999999}, portal.JWT_SECRET, algorithm=portal.JWT_ALG)})
    hits = sse_items(r2.text)
    check("新增项立即可被检索（EQ-09）", "EQ-09" in hits, str(hits))

    print("\n【3】编辑与留痕")
    r = c.patch("/api/admin/items/EQ-09", json={"status": "已确认", "owner": "唐映秋"})
    check("编辑 2 个字段", r.status_code == 200 and r.json()["changed"] == 2, r.text)

    import sqlite3
    con = sqlite3.connect(db_file)
    changes = con.execute("SELECT field,old_value,new_value FROM item_changes "
                          "WHERE item_no='EQ-09' ORDER BY id").fetchall()
    con.close()
    check("item_changes 记了 status 与 owner 两条", len(changes) >= 3,
          str(changes))  # 含新增时的 "*" 记录
    check("留痕含改前改后", any(ch[0] == "status" and ch[1] == "未确认" and ch[2] == "已确认"
                                for ch in changes), str(changes))

    print("\n【4】软删")
    r = c.delete("/api/admin/items/EQ-09")
    check("删除 EQ-09", r.status_code == 200, r.text)

    r = c.get("/admin/items?q=EQ-09")
    check("列表不再显示", "共 0 项" in r.text)

    r2 = c.post("/api/chat", json={"session_id": None, "question": "投影遥控器"},
                headers={"Authorization": "Bearer " + portal.jwt.encode(
                    {"uid": 5, "exp": 9999999999}, portal.JWT_SECRET, algorithm=portal.JWT_ALG)})
    check("检索不再命中（软删即剔除）", "EQ-09" not in sse_items(r2.text))

    con = sqlite3.connect(db_file)
    n = con.execute("SELECT COUNT(*) FROM requirement_items WHERE item_no='EQ-09' AND is_deleted=1").fetchone()[0]
    con.close()
    check("数据仍在库（可审计回查）", n == 1, str(n))

    print("\n【5】导入 md（预检 → 确认 → 幂等）")
    md = """# 导入测试清单

### [VN-09] 分会场签到台
- 内容: 第二分会场签到台 1 组
- 状态: 未确认
- 责任人: 林亦舟
- 来源: 台账|LEDGER-11月|第3行
- 发生时间: 2026-10-29 09:00
- 补录时间: 2026-10-29 10:00

### [bad-no] 编号错误项
- 内容: 编号不合规
- 状态: 未确认
- 责任人: 林亦舟
- 来源: 台账|X|第4行

### [CT-09] 茶歇加订
- 内容: 下午茶歇加订 50 人份
- 状态: 未确认
- 责任人: 唐映秋
- 来源: 服务台|SD-2026-0140|第2行

### [EQ-01] 重复编号项
- 内容: 这条与库内 EQ-01 重复
- 状态: 未确认
- 责任人: 林亦舟
- 来源: 服务台|SD-2026-0112|第3行
"""
    r = c.post("/api/admin/items/import",
               files={"file": ("t.md", md.encode("utf-8"), "text/markdown")},
               data={"dry_run": "true"})
    d = r.json()
    check("预检返回报告", r.status_code == 200, r.text[:300])
    check("预检识别 4 条", d["total"] == 4, str(d.get("total")))
    check("1 条错误（编号格式）", len(d["errors"]) == 1 and "编号" in d["errors"][0]["reason"],
          str(d.get("errors")))
    check("1 条重复跳过", d["dup"] == 1 and d["duplicates"][0]["item_no"] == "EQ-01",
          str(d.get("duplicates")))
    check("预检不写库（dry_run）", d["ok"] == 2 and "inserted" not in d, str(d))

    r = c.post("/api/admin/items/import",
               files={"file": ("t.md", md.encode("utf-8"), "text/markdown")},
               data={"dry_run": "false"})
    d = r.json()
    check("确认导入写入 2 条", d.get("inserted") == 2, str(d.get("inserted")))

    r = c.post("/api/admin/items/import",
               files={"file": ("t.md", md.encode("utf-8"), "text/markdown")},
               data={"dry_run": "false"})
    d = r.json()
    check("重跑幂等（新增 0 条）", d.get("inserted") == 0, str(d.get("inserted")))

    print("\n【6】导入 CSV 与边界")
    csv_text = ("item_no,category,content,status,owner,source_tool,source_ref,"
                "biz_occurred_at,received_at\n"
                "OT-09,其他,雨伞架 2 个,未确认,秦芮,台账,LEDGER-11月|第9行,"
                "2026-10-29 11:00,2026-10-29 12:00\n")
    r = c.post("/api/admin/items/import",
               files={"file": ("t.csv", csv_text.encode("utf-8-sig"), "text/csv")},
               data={"dry_run": "false"})
    check("CSV 导入成功", r.status_code == 200 and r.json().get("inserted") == 1, r.text[:200])

    r = c.get("/api/admin/items/template.csv")
    check("模板下载带 BOM", r.status_code == 200 and r.text.startswith("\ufeff"), r.text[:20])

    r = c.post("/api/admin/items/import",
               files={"file": ("x.txt", b"abc", "text/plain")}, data={"dry_run": "true"})
    check("非 md/csv → 41501", r.status_code == 415 and r.json()["code"] == 41501, r.text)

    r = c.post("/api/admin/items/import",
               files={"file": ("x.csv", "编号,内容\n1,测试".encode("gbk"), "text/csv")},
               data={"dry_run": "true"})
    check("非 UTF-8 → 42201", r.status_code == 422 and r.json()["code"] == 42201, r.text)

    print("\n【7】编号映射")
    # 首启会按需求项的来源字段自动建映射（Agent 的 find_mapping 依赖它），所以这里用一个新单号
    r = c.post("/api/admin/mappings", json={"source_tool": "服务台", "external_no": "SD-2026-0999",
                                            "item_no": "EQ-01"})
    check("新增映射", r.status_code == 200, r.text)

    r = c.get("/api/admin/mappings")
    check("首启已自动建立来源映射（f30 映射表）", len(r.json()["items"]) >= 10,
          str(len(r.json()["items"])))

    r = c.post("/api/admin/mappings", json={"source_tool": "服务台", "external_no": "SD-2026-0999",
                                            "item_no": "EQ-02"})
    check("同一原始单号重复 → 40901", r.status_code == 409 and r.json()["code"] == 40901, r.text)

    r = c.post("/api/admin/mappings", json={"source_tool": "服务台", "external_no": "SD-X",
                                            "item_no": "ZZ-99"})
    check("映射到不存在的项 → 40401", r.status_code == 404 and r.json()["code"] == 40401, r.text)

    r = c.get("/api/admin/mappings")
    mid = r.json()["items"][0]["id"]
    r = c.delete(f"/api/admin/mappings/{mid}")
    check("删除映射", r.status_code == 200, r.text)

print(f"\n结果：通过 {ok} 项，失败 {bad} 项")
sys.exit(0 if bad == 0 else 1)
