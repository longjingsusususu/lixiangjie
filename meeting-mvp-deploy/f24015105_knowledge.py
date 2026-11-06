# -*- coding: utf-8 -*-
"""
会议活动服务需求汇总系统 MVP · 知识库模块（f24015105_knowledge.py）

一期零 LLM（f01 红线）：知识库为结构化规则库（f24015105_kb_data.json），
检索用中文 bigram + 关键词加权打分（BM25 思想的简化实现），无任何模型调用。
二期接 RAG 时：本模块对外接口不变，内部把规则库换成向量库即可（槽位设计）。

对外接口：
    get_kb().search(question) -> {"hits": [...], "covered": bool, "score": float, "kb_version": str}
覆盖门：最高分低于 COVERAGE_THRESHOLD 判"未覆盖"，Agent 将转人工（人工审核点④）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
KB_PATH = BASE / "f24015105_kb_data.json"
COVERAGE_THRESHOLD = 0.25   # 覆盖门：校准值——有效业务问题实测 ≥0.29，无关问题 0.0（防幻觉的规则版）

# 停用碎片：bigram 切分后过滤无意义组合
STOP = {"的了", "是与", "和及", "或者", "我是", "你是", "系统", "一下", "请问",
        "什么", "怎么", "这个", "那个", "有没有", "是否"}


def _bigrams(text: str) -> set[str]:
    """中文按字 bigram + 英数按词切分，去停用碎片，返回集合。"""
    text = re.sub(r"\s+", "", text.lower())
    grams: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        grams.append(seg) if len(seg) == 1 else grams.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    grams += re.findall(r"[a-z0-9_]{2,}", text)
    return {g for g in grams if g not in STOP}


class KnowledgeBase:
    def __init__(self, path: Path = KB_PATH):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.articles = data["articles"]
        self.version = data.get("version", "1.0")
        self._doc_grams = [
            set(_bigrams(a["title"] + " " + a["text"] + " " + " ".join(a.get("keywords", []))))
            for a in self.articles
        ]

    def search(self, question: str, top_n: int = 3) -> dict:
        q = _bigrams(question)
        if not q:
            return {"hits": [], "covered": False, "score": 0.0, "kb_version": self.version}
        scored: list[tuple[float, set, int]] = []
        for idx, grams in enumerate(self._doc_grams):
            inter = q & grams
            if not inter:
                continue
            # 加权：查询覆盖占比 ×（1 + 命中占比），防长文碾压
            score = round(len(inter) / len(q) * (1 + len(inter) / max(len(grams), 1)), 2)
            scored.append((score, inter, idx))
        scored.sort(key=lambda x: -x[0])
        hits = [{
            "id": self.articles[idx]["id"],
            "title": self.articles[idx]["title"],
            "text": self.articles[idx]["text"],
            "evidence": self.articles[idx].get("evidence", ""),
            "score": score,
            "matched": sorted(inter)[:8],
        } for score, inter, idx in scored[:top_n]]
        top = hits[0]["score"] if hits else 0.0
        return {"hits": hits, "covered": top >= COVERAGE_THRESHOLD, "score": top,
                "kb_version": self.version}


_kb: KnowledgeBase | None = None


def get_kb() -> KnowledgeBase:
    """单例：首次调用时加载规则库。"""
    global _kb
    if _kb is None:
        _kb = KnowledgeBase()
    return _kb
