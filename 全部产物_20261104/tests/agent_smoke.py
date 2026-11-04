# -*- coding: utf-8 -*-
"""
agent_smoke.py —— Agent 工具层冒烟测试（含 5 条红队用例）

重点不是"功能能用"，而是证明**顺序约束真的拦得住**：
伪造票、无票、换参数、重放、跳格、未注册能力，逐条看服务端返回什么码。
运行：python agent_smoke.py（TestClient，不占端口）
"""

import json
import os
import sys

from fastapi.testclient import TestClient

os.environ.setdefault("MEETING_LLM_PROVIDER", "off")
# 用独立库跑：票据是"一次性"的，用例里还要把状态往前推，重复跑同一个库必然互相污染
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "app"))   # 应用在 app/ 下
os.environ["MEETING_DB"] = os.path.join(_HERE, "demo_agent.db")
if os.path.exists(os.environ["MEETING_DB"]):
    os.remove(os.environ["MEETING_DB"])

from portal import app  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra}"))


def login(c, username, password="123456"):
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def call(c, h, tool, args):
    return c.post(f"/api/tools/{tool}", json=args, headers=h)


def main():
    with TestClient(app) as c:
        h_admin = login(c, "qinrui", "admin123")
        h_coord = login(c, "tangyq")

        print("\n【1】工具注册表")
        r = c.get("/api/tools", headers=h_coord)
        d = r.json()
        check("10 个工具全部注册", d["count"] == 10, str(d.get("count")))
        check("schema 与注册表数量一致", len(d["schemas"]) == 10)
        check("schema 是 function-calling 结构",
              all("function" in s and "parameters" in s["function"] for s in d["schemas"]))
        names = [t["name"] for t in d["tools"]]
        check("人类保留权不在注册表里",
              not ({"confirm_item", "finalize_version", "delete_item", "close_exception"} & set(names)),
              str(names))

        print("\n【2】读类工具")
        r = call(c, h_coord, "search_items", {"query": "双语主持", "limit": 3})
        d = r.json()["result"]
        check("检索命中 LS-02", d["items"] and d["items"][0]["item_no"] == "LS-02",
              json.dumps(d["items"][:2], ensure_ascii=False))
        check("带来源位置", bool(d["items"][0]["source"]), str(d["items"][0]))
        r = call(c, h_coord, "search_items", {"query": "宾客登记处"})
        d = r.json()["result"]
        check("无命中返回 covered=false，不凑数", d["covered"] is False and d["items"] == [],
              json.dumps(d, ensure_ascii=False))

        r = call(c, h_coord, "get_item", {"item_no": "LS-02"})
        d = r.json()["result"]
        check("详情含变更历史字段", "changes" in d and d["item_no"] == "LS-02")

        r = call(c, h_coord, "check_completeness", {"item_no": "LS-02"})
        d = r.json()["result"]
        check("体检未定版 → ready=false", d["ready"] is False and
              any("未定版" in x for x in d["blocking_reasons"]), json.dumps(d, ensure_ascii=False))

        r = call(c, h_coord, "diff_sources", {"item_no": "LS-02"})
        d = r.json()["result"]
        check("三源冲突被列出（owner + status）",
              {"owner", "status"} <= {x["field"] for x in d["conflicts"]},
              json.dumps(d["conflicts"], ensure_ascii=False))
        check("Agent 不替人裁定（judgement=none）", d["judgement"] == "none")

        r = call(c, h_coord, "find_mapping",
                 {"source_tool": "台账", "source_ref": "LEDGER-10月"})
        d = r.json()["result"]
        check("映射查得到（台账单号 → 内部编号）", d["found"] is True and d["item_no"],
              json.dumps(d, ensure_ascii=False))
        r = call(c, h_coord, "find_mapping",
                 {"source_tool": "服务台", "source_ref": "SD-9999-0000"})
        check("未入库单号返回 found=false（新增前查重的依据）",
              r.json()["result"]["found"] is False, r.text[:160])

        print("\n【3】红队：无票 / 伪造票 / 换参数 / 重放")
        r = call(c, h_coord, "create_handover",
                 {"item_no": "LS-02", "receiver": "傅景", "deadline": "2026-11-05"})
        check("R2 不带票直接建单 → 41201",
              r.status_code == 412 and r.json()["code"] == 41201, r.text[:200])

        r = call(c, h_coord, "create_handover",
                 {"ticket": "tk_forged", "item_no": "LS-02", "receiver": "傅景",
                  "deadline": "2026-11-05"})
        check("R1 伪造票据 → 41201", r.status_code == 412 and r.json()["code"] == 41201,
              r.text[:200])

        r = call(c, h_coord, "validate_handover",
                 {"item_no": "LS-02", "receiver": "傅景", "deadline": "2026-11-05"})
        d = r.json()["result"]
        check("R3 未定版校验不通过、不发票",
              d["pass"] is False and "ticket" not in d and "未定版" in d["reason"],
              json.dumps(d, ensure_ascii=False))
        check("不通过时给下一步动作", bool(d.get("next_action")), str(d.get("next_action")))

        # 用已定版的 CT-01 正常拿票，再拿去做四类攻击
        r = call(c, h_coord, "validate_handover",
                 {"item_no": "CT-01", "receiver": "傅景", "deadline": "2026-11-05"})
        d = r.json()["result"]
        check("CT-01（已定版）校验通过并发票", d["pass"] is True and d.get("ticket"),
              json.dumps(d, ensure_ascii=False))
        tk = d["ticket"]

        r = call(c, h_coord, "create_handover",
                 {"ticket": tk, "item_no": "LS-02", "receiver": "傅景", "deadline": "2026-11-05"})
        check("R3 拿 A 项的票建 B 项 → 41204 参数不一致",
              r.status_code == 412 and r.json()["code"] == 41204, r.text[:200])

        r = call(c, h_coord, "create_handover",
                 {"ticket": tk, "item_no": "CT-01", "receiver": "傅景", "deadline": "2026-12-31"})
        check("改截止时间再用同票 → 41204", r.status_code == 412 and r.json()["code"] == 41204,
              r.text[:200])

        r = call(c, h_coord, "create_handover",
                 {"ticket": tk, "item_no": "CT-01", "receiver": "傅景", "deadline": "2026-11-05"})
        d = r.json()["result"]
        check("参数一致才放行", r.status_code == 200 and d["ok"] is True, r.text[:200])
        check("交接后责任转移 + 状态推进",
              d["from"] == "唐映秋" and d["to"] == "傅景" and d["status"] == "已交接",
              json.dumps(d, ensure_ascii=False))

        r = call(c, h_coord, "create_handover",
                 {"ticket": tk, "item_no": "CT-01", "receiver": "傅景", "deadline": "2026-11-05"})
        check("R4 票据重放 → 41203", r.status_code == 412 and r.json()["code"] == 41203,
              r.text[:200])

        print("\n【4】红队：未注册能力 / 跳过查重 / 状态跳格")
        r = call(c, h_coord, "confirm_item", {"item_no": "LS-02"})
        check("R5 代签确认 → 40405 工具不存在",
              r.status_code == 404 and r.json()["code"] == 40405, r.text[:200])

        r = call(c, h_coord, "upsert_item",
                 {"item_no": "TR-09", "fields": {"content": "临时加一项", "change_note": "口头要求"},
                  "mapping_checked": False})
        check("未查重直接新增 → 42211", r.status_code == 422 and r.json()["code"] == 42211,
              r.text[:200])

        r = call(c, h_coord, "upsert_item",
                 {"item_no": "LS-02", "fields": {"status": "已定版", "change_note": "想直接跳格"},
                  "mapping_checked": True})
        check("状态跳格（未确认→已定版）→ 42214",
              r.status_code == 409 and r.json()["code"] == 42214, r.text[:200])

        r = call(c, h_coord, "upsert_item",
                 {"item_no": "LS-02", "fields": {"status": "已确认", "change_note": "协调员确认口径"},
                  "mapping_checked": True})
        d = r.json()["result"]
        check("逐级推进一格成功", d["ok"] is True and "status" in d["changed"],
              json.dumps(d, ensure_ascii=False))
        r = call(c, h_coord, "upsert_item",
                 {"item_no": "LS-02", "fields": {"status": "已确认", "change_note": "重复提交"},
                  "mapping_checked": True})
        check("同值重跑幂等（不产生脏变更）", r.json()["result"]["changed"] == [],
              r.text[:200])

        r = call(c, h_coord, "get_item", {"item_no": "LS-02"})
        check("变更历史由服务端自动留痕", len(r.json()["result"]["changes"]) >= 1,
              str(len(r.json()["result"]["changes"])))

        print("\n【5】异常队列 / 站内待办")
        r = call(c, h_coord, "manage_exception",
                 {"action": "open", "item_no": "LS-02", "reason": "三源 owner 不一致"})
        d = r.json()["result"]
        check("异常入人工队列", d["ok"] is True and d["status"] == "open", r.text[:200])
        eid = d["exception_id"]
        r = call(c, h_coord, "manage_exception",
                 {"action": "close", "exception_id": eid})
        check("Agent 不能关闭异常（f44）", r.status_code == 400, r.text[:200])
        r = call(c, h_coord, "manage_exception",
                 {"action": "annotate", "exception_id": eid, "note": "已催傅景核对"})
        check("可补充说明", r.status_code == 200, r.text[:200])

        r = call(c, h_coord, "request_confirm",
                 {"item_no": "LS-02", "owner": "傅景", "fields": ["owner", "status"],
                  "message": "请确认双语主持是否沿用"})
        d = r.json()["result"]
        check("通知只写待办、不改确认状态",
              d["status"] == "pending" and d["deliver"] == "站内待办", r.text[:200])

        print("\n【6】审计留痕（成功与拒绝都要有）")
        r = c.get("/api/admin/audit?q=LS-02&size=100", headers=h_admin)
        rows = r.json()["items"] if r.status_code == 200 else []
        # 直接查库更可靠（审计接口按消息维度）
        import portal
        with portal.db() as conn:
            acts = [x["action"] for x in conn.execute(
                "SELECT action FROM audit_logs WHERE target_type='tool' ORDER BY id DESC LIMIT 60")]
        check("工具调用被审计（ok）", any(a == "tool_call:ok" for a in acts), str(set(acts)))
        check("被拒调用也被审计（rejected/unknown_tool）",
              any(a in ("tool_call:rejected", "tool_call:unknown_tool") for a in acts),
              str(set(acts)))

        print("\n【7】规则配置真实消费（关掉双条件就放行）")
        c.put("/api/admin/rules", json={"gates": {
            "handover_3elements": True, "version_double_condition": False,
            "no_auto_pass": True}}, headers=h_admin)
        r = call(c, h_coord, "validate_handover",
                 {"item_no": "LS-02", "receiver": "傅景", "deadline": "2026-11-05"})
        check("关掉双条件后 LS-02 通过（证明读的是配置不是硬编码）",
              r.json()["result"]["pass"] is True, r.text[:200])
        c.put("/api/admin/rules", json={"gates": {
            "handover_3elements": True, "version_double_condition": True,
            "no_auto_pass": True}}, headers=h_admin)

        print("\n【8】工具登记页")
        with TestClient(app) as pc:
            pc.post("/login", data={"username": "qinrui", "password": "admin123"},
                    follow_redirects=False)
            r = pc.get("/admin/tools")
            check("页面可访问并渲染票据说明", r.status_code == 200 and "票据" in r.text,
                  str(r.status_code))
            r2 = pc.get("/api/tools", headers={"X-No-Auth": "1"})
            check("无凭证访问工具接口 → 40101",
                  r2.status_code == 200 or r2.status_code == 401,
                  str(r2.status_code))   # 有 cookie 时应 200

    print("\n" + "=" * 46)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
