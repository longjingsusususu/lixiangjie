# -*- coding: utf-8 -*-
"""
tools/api.py —— 工具层的 HTTP 入口（A 档：前端按顺序串工具；B 档：模型按 schema 调同一批接口）

  GET  /api/tools              → 10 个工具的 function-calling schema（B 档直接喂模型）
  POST /api/tools/{name}       → 执行一个工具，body 即 arguments
  GET  /admin/tools            → 工具登记页（看 schema、在页面上手动跑一遍红队用例）

约束：一次请求只允许一个工具调用。批量写入没有入口——这是刻意的（S5）。
"""

import json
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import registry

router = APIRouter()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _p():
    import portal
    return portal


def _require_api(*roles):
    """与 admin.require_api 同规则：HTTPException 原样放行，不吞 40103。"""
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
            p.fail(403, 40302)
        return user
    return dep


def _require_page(*roles):
    """页面鉴权复用 admin 的 _Forbidden（未登录跳登录，角色不足渲染 403 页）。"""
    import admin

    def dep(request: Request):
        p = _p()
        try:
            user = p.current_user(request)
        except Exception:
            raise admin._Forbidden(login=True)
        if roles and user["role"] not in roles:
            raise admin._Forbidden(user=user)
        return user
    return dep


@router.get("/api/tools")
async def list_tools(user=Depends(_require_api())):
    """工具清单 + schema。给 B 档模型用，也给前端做按钮映射。"""
    return {"count": len(registry.TOOLS),
            "tools": [{"name": t["name"], "kind": t["kind"], "roles": t["roles"],
                       "description": t["description"], "parameters": t["parameters"]}
                      for t in registry.TOOLS.values()],
            "schemas": registry.schemas(),
            "not_exposed": ["confirm_item", "finalize_version", "delete_item",
                            "close_exception", "change_role", "write_audit_log"]}


@router.post("/api/tools/{name}")
async def call_tool(name: str, request: Request, user=Depends(_require_api())):
    """
    执行单个工具。body 就是该工具的 arguments。
    服务端在该函数里完成：工具存在性 → 角色 → 前置条件 → 票据 → 留痕。
    """
    try:
        args = await request.json()
    except Exception:
        args = {}
    if not isinstance(args, dict):
        _p().fail(400, 40001, message="参数必须是 JSON 对象")
    result = registry.invoke(name, args, user)
    return {"ok": True, "tool": name, "result": result}


@router.get("/admin/tools", response_class=HTMLResponse)
async def tools_page(request: Request, user=Depends(_require_page("admin", "qa", "coordinator"))):
    import admin
    kinds_cn = {"read": "只读", "validate": "校验", "write": "写", "notify": "通知"}
    cards = []
    for t in registry.TOOLS.values():
        cards.append({"name": t["name"], "kind": t["kind"], "kind_cn": kinds_cn[t["kind"]],
                      "description": t["description"], "roles": t["roles"],
                      "required": t["parameters"].get("required", []),
                      "schema": json.dumps(t["parameters"], ensure_ascii=False, indent=1)})
    return templates.TemplateResponse(request, "tools.html", admin.page_ctx(
        request, user, "tools", cards=cards, total=len(cards),
        not_exposed=["confirm_item", "finalize_version", "delete_item",
                     "close_exception", "change_role", "write_audit_log"]))
