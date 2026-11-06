# -*- coding: utf-8 -*-
"""
agent/planner.py —— 决策层（Harness 的「大脑插槽」）

为什么单独一层？
    执行循环（runtime.py）只认一种输入：{"type": "tool", "tool": ..., "args": ...}
    或 {"type": "final", "answer": ...}。谁来产出这个结构，循环并不关心。
    于是「一期零 LLM 的规则决策」和「二期接模型的 Function Calling」可以互换，
    循环代码、工具代码、日志表、中断机制一行都不用改 —— 这是选 Harness 式
    薄编排的核心收益：框架只提供循环骨架，决策与工具都是可替换的零件。

两种决策实现：
    rule_decide(ctx)  A 档：确定性状态机（一期默认，符合 f01 客户红线 + 32 人时预算）
    llm_decide(ctx)   B 档：把工具 schema 喂给模型，让它自己选（MEETING_LLM_PROVIDER=on 才走）
    decide(ctx)       统一入口：B 档失败/未开启时无感回落到 A 档，循环不受影响
"""

import json
import os
import re
import urllib.request
from datetime import datetime

# ============================================================
# 意图与槽位（规则决策的输入）
# ============================================================
ITEM_NO_RE = re.compile(r"\b([A-Z]{2}-\d{2,3})\b")
FULL_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[-/月](\d{1,2})(?:日|号)?(?!\d)")

# 意图关键词：命名一律用"动作词 + 语气词"两套，靠有没有「把/帮我/给我/请」区分
KW_DO = ("把", "帮我", "给我", "请", "麻烦")          # 祈使句标志：真要做动作
KW_HANDOVER = ("交接", "交给", "转给", "移交给", "派给")
KW_PRECHECK = ("能交接", "可以交接", "能不能", "可不可以", "能否", "通过吗",
               "能过吗", "行不行", "有问题吗", "有风险吗")
KW_CONFLICT = ("冲突", "不一致", "对不上", "几个", "几间", "哪边", "哪个对", "三源", "差异")
KW_QUERY = ("有哪些", "是什么", "多少", "谁负责", "查一下", "看看", "汇总", "列出", "状态")


def detect_intent(goal: str) -> str:
    """
    意图识别（纯规则，可解释、可测试，不需要模型）。

    关键难点是区分「要我做」和「问能不能」——两者都出现"交接"：
      · "把 LS-02 交接给傅景"        → handover（祈使句，有"把"，真要做）
      · "LS-02 交接给傅景，能过吗"   → precheck（在问结果，不该产生任何写入）
    判据：出现祈使标志词(把/帮我/给我/请) 才算要做；否则只要命中疑问词就是只校验。
    这个判断错了的代价不对称——误判成 handover 会在用户只想问问时发起写操作，
    所以宁可保守：拿不准就往"只看不动"那边靠。
    """
    g = goal or ""
    has_handover = any(k in g for k in KW_HANDOVER)
    imperative = any(k in g for k in KW_DO)

    if has_handover and imperative:
        return "handover"
    if any(k in g for k in KW_PRECHECK) or ("吗" in g and has_handover):
        return "precheck"
    if has_handover:
        return "handover"
    if any(k in g for k in KW_CONFLICT):
        return "conflict"
    return "query"


def _norm_deadline(text: str):
    """
    把「11-05」「11月5日」「2026-11-05」统一成 YYYY-MM-DD。
    没写年份时用当前年——这是业务上最常见的说法（"截止 11-05"）。
    返回 None 表示没抽取到。
    """
    m = FULL_DATE_RE.search(text or "")
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = SHORT_DATE_RE.search(text or "")
    if m:
        mo, d = m.groups()
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{datetime.now().year:04d}-{int(mo):02d}-{int(d):02d}"
    return None


def extract_slots(goal: str, names=()) -> dict:
    """
    抽槽位：需求项编号 / 接收人 / 截止时间 / 关键词。
    接收人用「已知人员名单」匹配而不是随便截词——避免把"傅景负责的"里的"负责的"当人名。
    """
    g = goal or ""
    slots = {"item_no": None, "receiver": None, "deadline": _norm_deadline(g),
             "keywords": g}
    m = ITEM_NO_RE.search(g.upper())
    if m:
        slots["item_no"] = m.group(1)
    # 人名：取最长匹配（"唐映秋"优先于"唐"）
    hits = [n for n in names if n and n in g]
    if hits:
        slots["receiver"] = max(hits, key=len)
    return slots


# ============================================================
# A 档：规则决策（确定性状态机）
# ============================================================
def _obs_map(ctx):
    """把观察历史压成 {工具名: 最近一次观察}，决策时只关心"每个工具做没做过、结果如何"。"""
    m = {}
    for o in ctx.get("obs", []):
        m[o["tool"]] = o
    return m


def rule_decide(ctx) -> dict:
    """
    决策函数签名统一：(ctx) -> action。

    它做的事本质上是"看已有哪些观察，决定下一步"，不是写死的流水线：
      · 校验没过 → 直接给结论，不会硬着头皮去建交接
      · 建交接失败（票据过期等） → 给原因，不重试（重试只会再失败一次）
      · 缺槽位 → 先追问，不拿空参数去调工具
    这正是"再决策"的意义：每一步都重新看证据。
    """
    intent = ctx["intent"]
    slots = ctx["slots"]
    item_no = slots.get("item_no")
    obs = _obs_map(ctx)

    # ── 规则知识问答（FAQ，零工具）────────────────────────
    # 必须放在所有意图分支之前："交接单需要哪些要素"同时含"交接"，
    # 若先走意图分类会被当成 handover 追问编号，永远轮不到 FAQ。
    # 这类问题考的是口径本身，答案在规则里而不在需求项里——
    # 检索只会给出弱相关条目，不如直接答。
    faq = _faq_answer(ctx["goal"])
    if faq:
        return _final(faq)

    # ── 建立交接（写操作链路）────────────────────────────
    if intent == "handover":
        if not item_no:
            return _final("请告诉我需求项编号（如 LS-02），我再核对交接条件。")
        if "get_item" not in obs:
            return _tool("get_item", {"item_no": item_no},
                         "先看清现状：状态/责任人/交付物口径，再决定能不能交接")
        gi = obs["get_item"]
        if not gi["ok"]:
            return _final(f"查不到 {item_no}：{gi['message']}。请确认编号，或让协调员先登记。")
        if not slots.get("receiver") or not slots.get("deadline"):
            lack = []
            if not slots.get("receiver"):
                lack.append("接收人")
            if not slots.get("deadline"):
                lack.append("截止时间")
            cur = gi["result"]
            return _final(
                f"{item_no} 当前状态为【{cur['status']}】，责任人是"
                f"{cur['owner'] or '（空）'}。还缺{'、'.join(lack)}——"
                f"交接三要素不齐我不建单，请补充后再让我执行。")
        if "validate_handover" not in obs:
            return _tool("validate_handover",
                         {"item_no": item_no, "receiver": slots["receiver"],
                          "deadline": slots["deadline"]},
                         "建单前必须校验三要素与定版状态，否则拿不到票据")
        v = obs["validate_handover"]
        if not v["ok"]:
            return _final(f"校验接口报错：{v['message']}（未建单，请稍后重试或人工处理）")
        if not v["result"].get("pass"):
            reasons = "；".join(v["result"].get("blocking_reasons") or [])
            return _final(
                f"{item_no} 现在不能交接：{reasons}。\n"
                f"下一步：{v['result'].get('next_action')}。\n"
                f"（校验未通过，我没有建任何交接记录）")
        if "create_handover" not in obs:
            return _tool("create_handover",
                         {"ticket": v["result"]["ticket"], "item_no": item_no,
                          "receiver": slots["receiver"], "deadline": slots["deadline"],
                          "note": f"Agent 建单：{ctx['goal'][:40]}"},
                         "校验已过并发票，带票建交接（此步会中断等人工确认）")
        c = obs["create_handover"]
        if not c["ok"]:
            return _final(
                f"建交接失败：{c['message']}。\n"
                f"（常见原因：确认超时导致票据过期，重新说一次「把 {item_no} 交接给…」即可）")
        r = c["result"]
        return _final(
            f"已建立交接：{r['item_no']} {r['from'] or '（空）'} → {r['to']}，"
            f"截止 {r['deadline']}，状态已置为【已交接】。\n"
            f"票据 {r['ticket_used']} 已作废（一次性）；变更已写入 item_changes，可追溯。")

    # ── 只校验（"能交接吗"这类问题，不写数据）─────────────
    if intent == "precheck":
        if not item_no:
            return _final("请告诉我需求项编号（如 LS-02），我来核对交接条件。")
        if "validate_handover" not in obs:
            if not slots.get("receiver") or not slots.get("deadline"):
                return _final(
                    f"校验需要完整三要素：接收人和截止时间都要有。"
                    f"你可以说「LS-02 交接给傅景，截止 11-05，能过吗」。")
            return _tool("validate_handover",
                         {"item_no": item_no, "receiver": slots["receiver"],
                          "deadline": slots["deadline"]},
                         "只校验不建单，结论来自服务端规则而非我的判断")
        v = obs["validate_handover"]
        if not v["ok"]:
            return _final(f"校验接口报错：{v['message']}")
        r = v["result"]
        if r.get("pass"):
            return _final(
                f"{item_no} 可以通过交接校验（三要素齐、已定版、有责任人）。\n"
                f"票据 {r['ticket']} 已签发，5 分钟内有效；要执行就说"
                f"「把 {item_no} 交接给{slots.get('receiver')}，截止 {slots.get('deadline')}」。\n"
                f"（现在只是校验，**没有**产生任何业务数据变更）")
        return _final(
            f"{item_no} 现在不能交接：{'；'.join(r.get('blocking_reasons') or [])}。\n"
            f"下一步：{r.get('next_action')}。")

    # ── 多源冲突（只报冲突，不裁定）──────────────────────
    if intent == "conflict":
        if not item_no:
            return _final("请告诉我是哪一条（如 EQ-05），我把三处记录并排给你看。")
        if "diff_sources" not in obs:
            return _tool("diff_sources", {"item_no": item_no},
                         "三源并排对比，冲突只描述不裁定")
        d = obs["diff_sources"]
        if not d["ok"]:
            return _final(f"对比失败：{d['message']}")
        r = d["result"]
        if not r["conflicts"]:
            return _final(f"{item_no} 在 {'、'.join(r['sources_present']) or '唯一来源'} "
                          f"上字段一致，没有发现冲突。")
        lines = [f"{item_no} 三源存在 {len(r['conflicts'])} 处不一致："]
        for c in r["conflicts"]:
            pairs = "；".join(f"{k}={v}" for k, v in c["by_source"].items())
            lines.append(f"· {c['field']}：{pairs}")
        lines.append("按 f33 不做清洗、不替人裁定——请责任人或质量验收确认哪一版为准，"
                     "确认后我再更新（upsert_item）。")
        return _final("\n".join(lines))

    # ── 缺项/完整性统计（check_completeness 全量模式）──────
    # "给我一份缺项清单""还有多少没确认"——统计口径必须来自服务端
    # （f31：缺失≠未确认，分列），不允许 Agent 拿检索结果口算。
    if not item_no and _wants_completeness(ctx["goal"]):
        if "check_completeness" not in obs:
            return _tool("check_completeness", {},
                         "缺项统计必须来自服务端分列口径（f31），不口算")
        r = obs["check_completeness"]
        if not r["ok"]:
            return _final(f"统计失败：{r['message']}")
        res = r["result"]
        lines = [f"全库共 {res['total']} 条：缺失 {res['missing_count']} 条、"
                 f"未确认 {res['unconfirmed_count']} 条（两者口径不同，分列如下）。"]
        if res["missing_items"]:
            lines.append("【缺失】（字段残缺，连确认都无从谈起）")
            for m in res["missing_items"]:
                lines.append(f"· {m['item_no']}｜{m['category']}｜{'、'.join(m['reasons'])}"
                             f"｜责任人：{m['owner'] or '（空）'}")
        if res["unconfirmed_items"]:
            lines.append("【未确认】（有内容，等责任人签字）")
            for u in res["unconfirmed_items"]:
                lines.append(f"· {u['item_no']}｜{u['category']}｜责任人：{u['owner'] or '（空）'}")
        return _final("\n".join(lines))

    # ── 普通查询（默认）──────────────────────────────────
    if "search_items" not in obs:
        return _tool("search_items", {"query": ctx["goal"], "limit": 3},
                     "先检索，检索不到就说未覆盖，不凑数")
    s = obs["search_items"]
    if not s["ok"]:
        return _final(f"检索失败：{s['message']}")
    items = s["result"].get("items") or []
    if not items:
        return _final("清单中未找到与该问题相关的需求项。\n"
                      "（宁可说没有，也不给你编一条；如果确实提过，可能是还没登记，"
                      "可以让协调员补录或走批量导入。）")
    lines = [f"共命中 {len(items)} 条："]
    for it in items:
        lines.append(f"· {it['item_no']}｜{it['content']}｜状态：{it['status']}"
                     f"｜责任人：{it['owner'] or '未指定'}｜来源：{it['source']}")
    if len(items) >= 3:
        lines.append("（只列前 3 条，需要更精确可按类别或编号再问）")
    return _final("\n".join(lines))


def _tool(name, args, why) -> dict:
    return {"type": "tool", "tool": name, "args": args, "why": why}


def _final(answer) -> dict:
    return {"type": "final", "answer": answer}


# ============================================================
# 规则知识 FAQ 与缺项统计的触发判定
# ============================================================
_COMPLETENESS_KEYS = ("缺项", "缺失", "没确认", "未确认", "完整性", "体检")
_COMPLETENESS_TRIGGERS = ("清单", "统计", "多少", "汇总", "摸底", "报告", "列一下", "哪些")


def _wants_completeness(goal: str) -> bool:
    """是否想要一份缺项/完整性统计。为什么类的口径解释走 FAQ，不进这里。"""
    g = goal or ""
    if "为什么" in g or "区别" in g:
        return False
    return (any(k in g for k in _COMPLETENESS_KEYS)
            and any(t in g for t in _COMPLETENESS_TRIGGERS))


def _faq_answer(goal):
    """
    内置规则问答（只覆盖系统自己的口径，不编造外部知识）：
      · 交接三要素 + f24 双条件 + f44 不得默认通过
      · f31：缺失与未确认为什么必须分列
    没命中的返回 None，走正常检索路径——清单里没有的就答"清单未覆盖"。
    """
    g = goal or ""
    if "交接" in g and ("要素" in g or "具备" in g or "需要什么" in g or "条件" in g):
        return (
            "建立交接必须同时满足：\n"
            "① 三要素齐全：接收人、截止时间（YYYY-MM-DD）、需求项已定版；\n"
            "② f24 双条件：状态推进到【已定版】且责任人签字确认，缺一不可；\n"
            "③ f44：无责任人的条目不得默认通过。\n"
            "要素不全时服务端卡口会直接拦截，Agent 与后台按钮共用同一套判定。")
    if ("缺失" in g and "未确认" in g) and (
            "为什么" in g or "分开" in g or "区别" in g or "口径" in g or "统计" in g):
        return (
            "f31 口径：缺失与未确认是两种不同的病，必须分列统计。\n"
            "· 缺失＝字段残缺（没有交付物口径/责任人），连发起确认的对象都没有；\n"
            "· 未确认＝内容已录，等责任人签字。\n"
            "混算会掩盖真实风险：把'缺失'算进'未确认'，看起来只是没人签字，"
            "实际上连确认动作都无法发起。")
    return None


# ============================================================
# B 档：LLM 决策（Function Calling 槽位，默认关闭）
# ============================================================
LLM_SYSTEM = """你是会议服务需求汇总系统的需求项管家。只能通过给定工具读取与修改数据。

红线（违反即视为事故）：
1. 不得跳过校验直接建交接；create_handover 必须带 validate_handover 刚签发的 ticket。
2. 不得代任何人签字确认，不得修改确认状态。
3. 不得替人裁定三源冲突，只报告差异。
4. 状态只能逐级前进，不得跳格。
5. 检索无命中就说"清单中未找到"，不得编造编号、责任人、状态。
6. 一次只调一个工具。

工作方式：查（search/get）→ 诊（check/diff）→ 验（validate）→ 做（write）。
输出必须是 JSON：{"type":"tool","tool":"<名字>","args":{...},"why":"<一句话>"}
或 {"type":"final","answer":"<给人看的结论文本>"}。不要输出 JSON 以外的内容。"""


def llm_decide(ctx) -> dict:
    """
    调模型做一次决策。任何异常都抛给上层，由 decide() 回落到规则决策——
    一期零 LLM 时这段代码根本不会被执行，但接口已经留好，二期改环境变量即启用。
    """
    import sys

    base = os.environ.get("MEETING_LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("MEETING_LLM_API_KEY", "")
    model = os.environ.get("MEETING_LLM_MODEL", "gpt-4o-mini")
    if not base or not key:
        raise RuntimeError("未配置 LLM Base URL / API Key")

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools import registry          # 工具 schema 直接取自注册表，不在提示词里手抄

    history = [{"role": "user", "content": f"用户诉求：{ctx['goal']}"}]
    for o in ctx.get("obs", []):
        history.append({"role": "assistant", "content": json.dumps(
            {"type": "tool", "tool": o["tool"], "args": {}}, ensure_ascii=False)})
        history.append({"role": "tool", "content": json.dumps(
            {"ok": o["ok"], "code": o.get("code"), "result": o.get("result"),
             "message": o.get("message")}, ensure_ascii=False)[:2000]})

    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "system", "content": LLM_SYSTEM}] + history,
               "tools": registry.schemas(), "tool_choice": "auto"}
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())

    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if calls:
        fn = calls[0]["function"]
        return {"type": "tool", "tool": fn["name"],
                "args": json.loads(fn.get("arguments") or "{}"), "why": "LLM 决策"}
    text = (msg.get("content") or "").strip()
    try:
        parsed = json.loads(text)
        if parsed.get("type") in ("tool", "final"):
            return parsed
    except Exception:
        pass
    return _final(text or "模型未给出结论")


def decide(ctx) -> dict:
    """
    统一决策入口。B 档开启时先试模型；模型不可用/输出不合法则回落规则决策，
    并在 ctx 里打标记 —— 这样阶段 5 分析能区分"模型决策"和"降级决策"。
    """
    if os.environ.get("MEETING_LLM_PROVIDER", "off").lower() == "on":
        try:
            action = llm_decide(ctx)
            ctx["decider"] = "llm"
            return action
        except Exception as exc:                      # 网络/额度/格式问题都走这里
            ctx["decider"] = "rule"
            ctx["llm_error"] = f"{type(exc).__name__}: {exc}"[:200]
    else:
        ctx["decider"] = "rule"
    return rule_decide(ctx)
