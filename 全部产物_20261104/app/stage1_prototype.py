# -*- coding: utf-8 -*-
"""
会议活动服务需求汇总 · 阶段 1 最小原型（单脚本，命令行，无 Web）

对应架构文档：《会议活动服务需求汇总系统_技术架构选型.md》第 4 节 Walking Skeleton
对应需求：REQ-4（f15 一线要"一页缺项清单 + 来源位置"，不要泛化摘要）

与"报销 RAG 原型"的差异（为什么不是切片+FAISS+LLM）：
  1. 会议项目一期是纯结构化流程（f01 客户红线：先观察交接点，不自动化）；
  2. 数据对象不是长文档而是"需求项"（短条目），所以检索对象是需求项，
     不需要 500 字切片；向量召回用轻量 TF-IDF 兜底语义（能装 FAISS 就用 FAISS）；
  3. LLM 调用按要求封装成独立函数、供应商可配置，但一期默认关闭（LLM_PROVIDER="off"），
     二期接"需求抽取 + 制度问答"时直接启用，接口不变。

命令行用法：
    python 会议活动需求_阶段1_最小原型.py
    交互命令：
        直接输入关键词  → 混合检索 top3 需求项（缺项清单格式输出）
        缺项            → 全量缺项清单（缺失 + 未确认 分列统计）
        交接 EQ-01 张三 2026-11-05   → 模拟交接卡口三要素校验（f20）
        quit / exit     → 退出
"""

# ============================================================
# 【核心复用模块 0】配置区
# 阶段 2 全部搬进 app/core/config.py（pydantic-settings）
# ============================================================

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

# ── 数据文件（阶段 2 换成三源导入 + SQLite 落库） ──────────────
DEMO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_会议需求样例.md")

# ── LLM 配置：供应商可配置（硬约束），一期默认 off ─────────────
LLM_PROVIDER = "off"          # off | mock | openai_compat
LLM_BASE_URL = "https://api.example.com/v1"   # openai_compat 时修改
LLM_API_KEY = "sk-xxx"
LLM_MODEL = "gpt-4o-mini"

# ── 检索配置 ────────────────────────────────────────────────
TOP_K = 3                     # 混合检索返回条数（与架构文档一致）

# ── 领域同义词表【核心复用】：一线口语 ↔ 清单用语 ─────────────
# 阶段 2 交给管理员在后台维护（对应 admin 页面的"字段配置"）
SYNONYMS = {
    "话筒": ["麦克风", "mic"],
    "麦克风": ["话筒", "mic"],
    "同传": ["翻译", " interpreter", "语言服务"],
    "翻译": ["同传", " interpreter", "语言服务", "双语"],
    "双语": ["中英", "翻译", "同传"],
    "主持": ["主持人", "emcee"],
    "桌牌": ["席位卡", "名牌"],
    "茶歇": ["餐饮", "点心", "咖啡"],
    "投影": ["投屏", "大屏", "led"],
    "大屏": ["投影", "led", "投屏"],
    "停车": ["车位", "车辆"],
}

# 类别前缀 → 中文名（需求项编号规则：EQ设备 / LS语言 / CT餐饮 / VN场地 / OT其他）
CATEGORY_MAP = {
    "EQ": "设备", "LS": "语言服务", "CT": "餐饮", "VN": "场地", "OT": "其他",
}


# ============================================================
# 【核心复用模块 1】需求项数据结构 + markdown 解析
# 阶段 2：解析逻辑保留，存储换成 SQLAlchemy 的 requirement_items 表
# ============================================================

@dataclass
class RequirementItem:
    """一条会议服务需求项 —— 对应架构文档 requirement_items 表的内存版。"""
    item_no: str            # 系统内部编号（如 EQ-01）
    category: str           # 类别（设备/语言服务/餐饮/场地/其他）
    content: str            # 需求描述
    status: str             # 五态：缺失/未确认/已确认/已定版/已交接
    owner: str              # 责任人（验收要素①，f02）
    source_tool: str        # 来源工具（验收要素③：服务台/台账/文件库/手工补录）
    source_ref: str         # 来源位置（f15：缺项清单必须带来源位置）
    biz_occurred_at: str    # 双时间戳①：业务发生时间（f34）
    received_at: str        # 双时间戳②：系统接收/补录时间（回填≠发生）
    change_note: str        # 变更备注（E-01 场景：临时变更未同步）

    def search_text(self) -> str:
        """参与检索的文本：编号 + 类别 + 描述 + 备注。"""
        return f"{self.item_no} {self.category} {self.content} {self.change_note}"


def _infer_status(raw_status: str, has_owner: bool, has_source: bool) -> str:
    """
    【核心复用】五态推断（状态机最小版）：
    规则来自 f31（缺失与未确认必须分列，混列则统计全失真）与 f02（责任人+确认状态）。
    """
    s = (raw_status or "").strip()
    if s in ("已确认", "已定版", "已交接"):
        return s
    if "缺失" in s or not s:
        return "缺失"          # 字段残缺，连"未确认"都算不上
    if "未确认" in s or "待确认" in s:
        return "未确认"
    # 标了"确认"但缺责任人/来源 → 降级为未确认（验收口径要求三要素齐）
    if not has_owner or not has_source:
        return "未确认"
    return "已确认"


def parse_requirements(md_text: str) -> list:
    """
    【核心复用】把 markdown 需求清单解析成 RequirementItem 列表。
    阶段 2 该函数对应 import_service 的"手工/CSV 路"解析器，
    接口路（服务台/台账/文件库）在 app 侧换成三源适配器，输出同一结构。
    """
    items = []
    cur = None
    for line in md_text.splitlines():
        line = line.strip()
        # 形如 "### [EQ-01] 无线手持话筒"
        m = re.match(r"^#{2,4}\s*\[([A-Z]{2}-\d+)\]\s*(.+)$", line)
        if m:
            if cur:
                items.append(cur)
            prefix = m.group(1).split("-")[0]
            cur = RequirementItem(
                item_no=m.group(1),
                category=CATEGORY_MAP.get(prefix, "其他"),
                content=m.group(2),
                status="", owner="", source_tool="", source_ref="",
                biz_occurred_at="", received_at="", change_note="",
            )
            continue
        if cur is None:
            continue
        # 形如 "- 状态: 已确认" 的字段行
        fm = re.match(r"^-\s*(\S+)[：:]\s*(.+)$", line)
        if fm:
            key, val = fm.group(1), fm.group(2).strip()
            if key == "内容":
                cur.content += " " + val
            elif key == "状态":
                cur.status = val
            elif key == "责任人":
                cur.owner = val
            elif key == "来源":
                # 格式：工具|单据号|位置，如 服务台|SD-2026-0112|第3行
                parts = [p.strip() for p in val.split("|")]
                cur.source_tool = parts[0] if parts else ""
                cur.source_ref = "|".join(parts[1:]) if len(parts) > 1 else ""
            elif key == "发生时间":
                cur.biz_occurred_at = val
            elif key == "补录时间":
                cur.received_at = val
            elif key == "变更":
                cur.change_note = val
    if cur:
        items.append(cur)

    # 五态判定：状态 + 责任人 + 来源 三者齐才算"已确认"
    for it in items:
        it.status = _infer_status(it.status, bool(it.owner), bool(it.source_tool))
    return items


# ============================================================
# 【核心复用模块 2】混合检索器：BM25 + 向量(TF-IDF) RRF 融合
# 阶段 2 原样搬进 app/agents/retriever.py；
# 二期接 RAG 时同一检索器对象换语料即可（接口不变）。
# ============================================================

def _expand(question: str) -> str:
    """同义词扩展 + 数字归一化：解决'话筒/麦克风''一百/100'匹配不上的问题。"""
    q = question
    for k, vs in SYNONYMS.items():
        if k in q:
            q += " " + " ".join(vs)          # 命中词则把同义词都拼进查询
    # 中文数字粗归一化（覆盖演示所需的量级）
    cn_num = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    def repl(mm):
        s = mm.group(0)
        if s == "十":
            return " 10 "
        if len(s) == 2 and s[0] == "十":
            return f" 1{cn_num[s[1]]} "
        if len(s) == 2 and s[1] == "十":
            return f" {cn_num[s[0]]*10} "
        return f" {cn_num[s[0]]*10 + cn_num.get(s[1:], 0)} " if len(s) >= 2 else s
    q = re.sub(r"[零一二两三四五六七八九十]{1,2}", repl, q)
    return q


class HybridRetriever:
    """
    混合检索：jieba 分词 BM25（字面召回）+ TF-IDF 余弦（语义兜底）+ RRF 融合。
    说明：需求项是短文本，BM25 为主；向量只做兜底，因此用纯 numpy TF-IDF，
    若环境装有 faiss-cpu 则自动用 FAISS 索引（与二期 RAG 同一套接口习惯）。
    """

    def __init__(self, items: list):
        import jieba
        from rank_bm25 import BM25Okapi
        self.items = items
        self.jieba = jieba
        corpus = [list(jieba.cut_for_search(it.search_text())) for it in items]
        self.bm25 = BM25Okapi(corpus)
        self._build_tfidf(corpus)

    def _build_tfidf(self, corpus):
        """轻量 TF-IDF 向量：有 faiss 用 faiss，没有用 numpy 余弦。"""
        import numpy as np
        self.np = np
        vocab = {}
        for doc in corpus:
            for w in set(doc):
                vocab[w] = vocab.get(w, 0) + 1
        self.vocab = {w: i for i, w in enumerate(vocab)}
        n = len(corpus)
        mat = np.zeros((n, len(self.vocab)), dtype="float32")
        for i, doc in enumerate(corpus):
            for w in doc:
                if w in self.vocab:
                    mat[i, self.vocab[w]] += 1
        # TF-IDF 权重 + L2 归一化（余弦）
        df = (mat > 0).sum(axis=0)
        idf = np.log((n + 1) / (df + 1)) + 1
        mat = mat * idf
        norm = np.linalg.norm(mat, axis=1, keepdims=True)
        norm[norm == 0] = 1
        self.vecs = mat / norm
        self.idf = idf
        # 可选 FAISS：接口习惯与二期 RAG 一致
        try:
            import faiss
            self.faiss_index = faiss.IndexFlatIP(self.vecs.shape[1])
            self.faiss_index.add(np.ascontiguousarray(self.vecs))
        except Exception:
            self.faiss_index = None

    def _vec_query(self, question: str):
        import numpy as np
        tokens = list(self.jieba.cut_for_search(question))
        v = np.zeros((1, len(self.vocab)), dtype="float32")
        for w in tokens:
            if w in self.vocab:
                v[0, self.vocab[w]] += 1
        v = v * self.idf
        norm = np.linalg.norm(v)
        if norm > 0:
            v = v / norm
        return v

    def search(self, question: str, top_k: int = TOP_K):
        """BM25 分数 + 向量余弦 → RRF 融合排序（k=60，标准参数）。"""
        q = _expand(question)
        tokens = list(self.jieba.cut_for_search(q))
        # BM25 分数（过滤全零，防止任意下标污染融合结果——阶段1踩过的坑）
        bm_scores = self.bm25.get_scores(tokens)
        bm_rank = [i for i in (-bm_scores).argsort() if bm_scores[i] > 0]
        # 向量余弦：只保留相似度超过阈值的项。
        # 不设阈值的话，任何问题都能凑出 3 条（零相似度也会排进名次），
        # "清单未覆盖"就永远触发不了——这是检索器最隐蔽的正确性 bug。
        vq = self._vec_query(q)
        vec_rank = []
        if self.faiss_index is not None:
            scores, vec_idx = self.faiss_index.search(vq, len(self.items))
            vec_rank = [int(i) for i, s in zip(vec_idx[0], scores[0]) if i >= 0 and s >= 0.08]
        else:
            sims = self.vecs @ vq[0]
            order = (-sims).argsort()
            vec_rank = [int(i) for i in order if sims[i] >= 0.08]
        # RRF 融合
        rrf = {}
        for rank, idx in enumerate(bm_rank[:20]):
            rrf[idx] = rrf.get(idx, 0) + 1 / (60 + rank + 1)
        for rank, idx in enumerate(vec_rank[:20]):
            rrf[idx] = rrf.get(idx, 0) + 1 / (60 + rank + 1)
        ranked = sorted(rrf.items(), key=lambda x: -x[1])[:top_k]
        return [(self.items[idx], score) for idx, score in ranked]


# ============================================================
# 【核心复用模块 3】缺项清单生成（REQ-4 的交付形态，f15）
# 阶段 2 搬进 app/services/board_service.py + 看板模板
# ============================================================

STATUS_ORDER = {"缺失": 0, "未确认": 1, "已确认": 2, "已定版": 3, "已交接": 4}


def format_gap_list(hits) -> str:
    """输出'一页缺项清单 + 来源位置'——不要长摘要（f15 原话）。"""
    lines = ["编号     类别     状态     需求内容                 责任人   来源位置",
             "-" * 88]
    for it, _ in hits:
        lines.append(
            f"{it.item_no:<8} {it.category:<6} {it.status:<6} "
            f"{it.content[:20]:<20} {it.owner or '—':<6} "
            f"{(it.source_tool + ' ' + it.source_ref) if it.source_tool else '来源缺失!'}"
        )
    return "\n".join(lines)


def full_gap_report(items) -> str:
    """全量缺项报告：缺失与未确认分列统计（f31 口径纪律）。"""
    missing = [it for it in items if it.status == "缺失"]
    unconfirmed = [it for it in items if it.status == "未确认"]
    out = [f"【缺项统计】共 {len(items)} 项：缺失 {len(missing)} 项（分列①）、"
           f"未确认 {len(unconfirmed)} 项（分列②）——两类严禁混并（f31）", ""]
    out.append("▍缺失（字段残缺，连确认都无从谈起）：")
    out.append(format_gap_list([(it, 0) for it in missing]) if missing else "  无")
    out.append("")
    out.append("▍未确认（有内容，等责任人签字）：")
    out.append(format_gap_list([(it, 0) for it in unconfirmed]) if unconfirmed else "  无")
    return "\n".join(out)


# ============================================================
# 【核心复用模块 4】交接卡口三要素校验（f20，拦截式）
# 阶段 2 搬进 app/workflow/gate_rules.py，服务端强制
# ============================================================

def check_handover(item, receiver: str, deadline: str, open_items: int) -> tuple:
    """
    交接三要素：①接收人 ②未确认项数 ③截止时间（f20）。
    返回 (是否放行, 原因)。拦截式而非提醒式——行业结论：提醒会被忽略。
    """
    problems = []
    if not receiver:
        problems.append("缺要素①接收人")
    if open_items is None or open_items < 0:
        problems.append("缺要素②未确认项数（未确认项不得归对方名下，f25）")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", deadline or ""):
        problems.append("缺要素③截止时间（格式 YYYY-MM-DD）")
    if item.status in ("缺失", "未确认"):
        problems.append(f"需求项状态为[{item.status}]，未定版不得交接（f24 双条件）")
    return (len(problems) == 0, "；".join(problems) if problems else "通过")


# ============================================================
# 【核心复用模块 5】LLM 网关：供应商可配置（硬约束）
# 阶段 2 搬进 app/workflow/llm_gateway/；一期默认 off（f01 红线）
# ============================================================

SYSTEM_PROMPT = (
    "你是会议服务需求汇总助手。只依据给出的需求项原文回答，"
    "逐条标注编号与来源位置；清单未覆盖的内容必须回答'清单未覆盖'，禁止编造。"
)


def call_llm(question: str, contexts: list) -> str:
    """
    LLM 调用封装（独立函数，供应商可配置）。
    off          → 直接返回 None，走纯检索输出（一期默认，f01：不做自动化）
    mock         → 离线演示：拼接摘录，不联网
    openai_compat→ 任何 OpenAI 兼容接口（改 BASE_URL/MODEL 即换供应商）
    """
    ctx = "\n".join(
        f"[{it.item_no}|{it.source_tool} {it.source_ref}] {it.category}：{it.content}（{it.status}）"
        for it, _ in contexts
    )
    user_prompt = f"需求项：\n{ctx}\n\n问题：{question}\n要求逐条带编号与来源。"

    if LLM_PROVIDER == "off":
        return None
    if LLM_PROVIDER == "mock":
        return f"（mock 模式）围绕你的问题，检索到 {len(contexts)} 项，详见上方清单。"
    if LLM_PROVIDER == "openai_compat":
        try:
            from openai import OpenAI
            client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": user_prompt}],
                timeout=15,
            )
            return resp.choices[0].message.content
        except Exception as e:
            # 降级：LLM 任何失败都不影响缺项清单输出（清单才是主交付物）
            return f"（LLM 调用失败已降级：{e}）"
    return None


# ============================================================
# 主程序：读文件 → 建索引 → 命令行交互
# ============================================================

def main():
    if not os.path.exists(DEMO_FILE):
        print(f"[错误] 找不到样例文件：{DEMO_FILE}")
        sys.exit(1)
    with open(DEMO_FILE, "r", encoding="utf-8") as f:
        items = parse_requirements(f.read())
    print(f"[加载] {len(items)} 条需求项（来源：{DEMO_FILE}）")
    retriever = HybridRetriever(items)
    print("命令：直接输入关键词=检索｜缺项=全量缺项清单｜交接 编号 接收人 截止日期｜quit 退出\n")

    for line in sys.stdin:
        q = line.strip()
        if not q:
            continue
        if q.lower() in ("quit", "exit"):
            break
        if q == "缺项":
            print(full_gap_report(items) + "\n")
            continue
        # 交接卡口模拟：交接 EQ-01 张三 2026-11-05 [未确认数]
        if q.startswith("交接"):
            parts = q.split()
            no = parts[1] if len(parts) > 1 else ""
            target = next((it for it in items if it.item_no == no), None)
            if target is None:
                print(f"[卡口] 编号 {no} 不存在\n")
                continue
            receiver = parts[2] if len(parts) > 2 else ""
            deadline = parts[3] if len(parts) > 3 else ""
            open_cnt = sum(1 for it in items if it.status in ("缺失", "未确认"))
            ok, reason = check_handover(target, receiver, deadline, open_cnt)
            print(f"[交接卡口] {no} → {receiver or '无'} @ {deadline or '无'}："
                  f"{'✅ 放行' if ok else '⛔ 拦截'}（{reason}）\n")
            continue
        # 默认：混合检索 top3 → 缺项清单格式 → 可选 LLM 汇总
        hits = retriever.search(q)
        if not hits:
            print("清单未覆盖。（可输入'缺项'查看全量缺项）\n")
            continue
        print(f"【检索 top{len(hits)}】{q}")
        print(format_gap_list(hits))
        answer = call_llm(q, hits)
        if answer:
            print(f"\n【LLM 汇总·{LLM_PROVIDER}】{answer}")
        print()


if __name__ == "__main__":
    main()
