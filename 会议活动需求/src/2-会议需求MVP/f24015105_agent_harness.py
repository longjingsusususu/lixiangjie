# -*- coding: utf-8 -*-
"""
会议活动服务需求汇总系统 MVP · Agent 框架（f24015105_agent_harness.py）

编排模式沿用问答助手已验证的 Harness 三段式：decide → invoke → observe。
一期零 LLM（f01）：decide 用规则路由（关键词+意图表），不调用任何模型；
工具槽位化注册（TOOLBOX），二期接 LLM 时仅替换 decide 函数为模型规划器，
工具层与观察层零改动（可替换供应商的槽位设计）。

四道人工确认/审核卡口（Agent 永不代行，f22 模型无审批权）：
  ① 需求确认签字   POST /api/confirm        业务负责人/协调员人工执行
  ② 手动定版       POST /api/versions       业务负责人人工执行（f24 双条件）
  ③ 交接卡口+接收确认 POST /api/handovers   三要素拦截 + 接收方确认（f20）
  ④ 异常队列人工复核 POST /api/exceptions/*  值守人处理，不得默认通过（f44）
Agent 侧另有兜底：知识库未覆盖的问题一律"转人工"，不硬答。

对外接口：
    ask(question, role) -> {"answer": str, "rounds": [...], "citations": [...], "escalated": bool}
"""
from __future__ import annotations

import re

from f24015105_knowledge import get_kb

MAX_ROUNDS = 3   # 与问答助手 Harness 同款上限：防止规则路由空转

# 意图路由表：(意图名, 触发关键词组)。命中任一组即路由到对应工具。
INTENTS = [
    ("stats",      ("多少", "几条", "统计", "看板", "进度", "M6", "M7", "完成率", "数量")),
    ("kb_search",  ("什么", "怎么", "如何", "规则", "要求", "流程", "是什么", "为什么",
                    "交接", "定版", "确认", "状态", "时间戳", "权限", "角色", "敏感", "导入", "映射",
                    "异常", "脏数据", "红线", "审批", "指标", "验收", "版本", "口径")),
]
ESCALATE_HINT = "如需办理确认签字、定版、交接等操作，请在系统对应页面由有权角色人工执行（Agent 无审批权）。"


# ---------------------------------------------------------------- 工具层（槽位化）
def _tool_stats(query: str, ctx: dict) -> str:
    """看板工具：直连数据库取 M6/M7 口径统计。"""
    db = ctx["db"]
    total = db.execute("SELECT COUNT(*) FROM requirement_items WHERE batch_id=1").fetchone()[0]
    confirmed = db.execute(
        "SELECT COUNT(*) FROM requirement_items WHERE batch_id=1 AND status IN ('confirmed','finalized','handed_over')"
    ).fetchone()[0]
    m6 = round(confirmed / total * 100, 1) if total else 0.0
    traced = db.execute(
        "SELECT COUNT(DISTINCT item_id) FROM item_changes").fetchone()[0]
    return (f"当前批次共 {total} 项：已确认及以上 {confirmed} 项，M6 确认完整率 {m6}%"
            f"（目标 100%，f02/f42）；有变更留痕的需求项 {traced} 项（M7 变更可追溯口径）。")


def _tool_kb_search(query: str, ctx: dict) -> str:
    """知识库工具：规则库检索，未覆盖抛 NotCovered 由编排层转人工。"""
    res = get_kb().search(query)
    ctx["citations"] = [{"id": h["id"], "title": h["title"], "evidence": h["evidence"]}
                        for h in res["hits"]]
    if not res["covered"]:
        raise NotCovered(res["score"])
    top = res["hits"][0]
    extra = f" 相关条目：{res['hits'][1]['title']}。" if len(res["hits"]) > 1 else ""
    return f"{top['text']}{extra}（依据：{top['evidence']}）"


class NotCovered(Exception):
    def __init__(self, score: float):
        self.score = score
        super().__init__(f"kb coverage {score}")


TOOLBOX = {
    "stats":     ("看板统计（M6/M7 口径）", _tool_stats),
    "kb_search": ("规则知识库检索", _tool_kb_search),
}


# ---------------------------------------------------------------- 编排层
def _decide(question: str) -> str:
    """decide：规则路由。统计类词优先；命中意图返回工具名，否则知识库兜底。"""
    for intent, words in INTENTS:
        if any(w in question for w in words):
            return intent
    return "kb_search"


def ask(question: str, role: str = "staff", db=None) -> dict:
    """主入口：decide → invoke → observe 循环，产出带引用与人工卡口提示的回答。"""
    q = (question or "").strip()[:200]
    if not q:
        return {"answer": "请输入问题；例如「交接需要哪些要素」「M6 目标是多少」。",
                "rounds": [], "citations": [], "escalated": False}
    ctx: dict = {"db": db, "citations": []}
    rounds: list[dict] = []
    tool = _decide(q)
    for i in range(MAX_ROUNDS):
        name, desc = TOOLBOX.get(tool, ("kb_search", TOOLBOX["kb_search"][0]))
        try:
            obs = TOOLBOX[tool][1](q, ctx)
            rounds.append({"round": i + 1, "decide": tool, "invoke": desc, "observe": "成功"})
            return {"answer": f"{obs}\n{ESCALATE_HINT}", "rounds": rounds,
                    "citations": ctx["citations"], "escalated": False}
        except NotCovered as e:
            rounds.append({"round": i + 1, "decide": tool, "invoke": desc,
                           "observe": f"知识库未覆盖（得分 {e.score}）→ 转人工"})
            break
        except Exception as e:   # 工具故障也转人工，不硬答
            rounds.append({"round": i + 1, "decide": tool, "invoke": desc,
                           "observe": f"工具异常：{type(e).__name__} → 转人工"})
            break
    answer = ("该问题超出规则知识库覆盖范围，已转人工处理。"
              "请联系质量验收（傅景）或管理员（秦芮）人工解答，并建议将答案沉淀进知识库"
              "（二期接 LLM 前同样遵循此兜底纪律）。")
    return {"answer": answer, "rounds": rounds, "citations": ctx["citations"], "escalated": True}
