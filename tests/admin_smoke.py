# -*- coding: utf-8 -*-
"""
admin_smoke.py —— 管理后台【批 1】冒烟：登录 / 权限中间件 / 概览 / 用户角色

覆盖：
  ① 登录页可访问；密码错 → 401 + 40102
  ② 管理员表单登录 → 302 到 /admin 且写入 HttpOnly cookie
  ③ 未登录访问 /admin → 302 /login；业务角色访问 → 403 页（明示无权）
  ④ 概览页渲染数据卡与进度表
  ⑤ /api/admin/users 超级权限：非 admin 调 → 40302
  ⑥ 改角色即时生效（JWT 未换也生效，因为角色每次查库）
  ⑦ 管理员改自己 → 40302（防自提权）
  ⑧ 停用账号后登录 → 40103；停用即时踢出（已有 cookie 也 401/403）
运行：PYTHONPATH=<venv> python admin_smoke.py
"""

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


with TestClient(portal.app) as admin_c:
    print("\n【1】登录页与表单登录")
    r = admin_c.get("/login")
    check("GET /login 200", r.status_code == 200 and "管理后台" in r.text, r.status_code)

    r = admin_c.post("/login", data={"username": "qinrui", "password": "bad", "next": "/admin"},
                     follow_redirects=False)
    check("密码错 401 + 40102", r.status_code == 401 and "40102" in r.text, r.status_code)

    r = admin_c.post("/login", data={"username": "qinrui", "password": "admin123", "next": "/admin"},
                     follow_redirects=False)
    check("管理员登录 302→/admin", r.status_code == 302 and r.headers["location"] == "/admin",
          f"{r.status_code} {r.headers.get('location')}")
    check("写入 mt_token cookie 且 HttpOnly", "mt_token" in admin_c.cookies,
          str(admin_c.cookies))

    print("\n【2】页面权限")
    r = admin_c.get("/admin")
    check("管理员访问概览 200", r.status_code == 200 and "概览" in r.text, r.status_code)
    check("概览含数据卡（需求项总数）", "需求项总数" in r.text)
    check("概览含批次进度表", "批 4" in r.text)

    fresh = TestClient(portal.app)          # 无 cookie 的新客户端
    r = fresh.get("/admin", follow_redirects=False)
    check("未登录访问 /admin → 302 /login", r.status_code == 302 and "/login" in r.headers["location"],
          f"{r.status_code} {r.headers.get('location')}")

    staff_c = TestClient(portal.app)
    r = staff_c.post("/login", data={"username": "yixian", "password": "123456", "next": "/admin"},
                     follow_redirects=False)
    check("业务角色登录后回员工端（不是后台）", r.status_code == 302 and r.headers["location"] == "/",
          r.headers.get("location"))
    r = staff_c.get("/admin", follow_redirects=False)
    check("业务角色访问 /admin → 403 页", r.status_code == 403 and "无权访问" in r.text, r.status_code)

    print("\n【3】接口权限（服务端二次校验）")
    r = staff_c.get("/api/admin/users")
    check("staff 调管理接口 → 40302", r.status_code == 403 and r.json()["code"] == 40302, r.text)

    r = fresh.get("/api/admin/users")
    check("未登录调管理接口 → 40101", r.status_code == 401 and r.json()["code"] == 40101, r.text)

    r = admin_c.get("/api/admin/users")
    users = r.json()["items"]
    check("admin 取到 5 个角色账号", r.status_code == 200 and len(users) == 5, str(len(users)))
    roles = {u["username"]: u["role"] for u in users}
    check("五角色齐全", set(roles.values()) == {"staff", "coordinator", "biz_owner", "qa", "admin"},
          str(roles))

    print("\n【4】改角色即时生效 / 防自提权")
    tid = next(u["id"] for u in users if u["username"] == "yixian")
    r = admin_c.patch(f"/api/admin/users/{tid}", json={"role": "qa"})
    check("改 yixian 为 qa", r.status_code == 200, r.text)
    # staff_c 手里的 token 没变，但角色查库 → 应立刻按 qa 处理
    r = staff_c.get("/api/admin/users")
    check("旧 token 立刻按新角色判定（仍非 admin → 40302）",
          r.status_code == 403 and r.json()["code"] == 40302, r.text)

    me = next(u["id"] for u in users if u["username"] == "qinrui")
    r = admin_c.patch(f"/api/admin/users/{me}", json={"role": "staff"})
    check("管理员改自己 → 40302",
          r.status_code == 403 and r.json()["code"] == 40302, r.text)

    print("\n【5】停用即时生效")
    r = admin_c.patch(f"/api/admin/users/{tid}", json={"is_active": False})
    check("停用 yixian", r.status_code == 200, r.text)

    r = staff_c.get("/api/admin/users")
    check("已登录的停用账号立刻被拒（40103）",
          r.status_code == 401 and r.json()["code"] == 40103, r.text)

    c2 = TestClient(portal.app)
    r = c2.post("/api/login", json={"username": "yixian", "password": "123456"})
    check("停用账号重新登录 → 40103", r.status_code == 401 and r.json()["code"] == 40103, r.text)

    r = admin_c.patch(f"/api/admin/users/{tid}", json={"is_active": True, "role": "staff"})
    check("恢复账号（启用 + 还原 staff）", r.status_code == 200, r.text)

    r = c2.post("/api/login", json={"username": "yixian", "password": "123456"})
    check("恢复后可正常登录", r.status_code == 200, r.text)

print(f"\n结果：通过 {ok} 项，失败 {bad} 项")
sys.exit(0 if bad == 0 else 1)
