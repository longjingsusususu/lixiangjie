# -*- coding: utf-8 -*-
"""
报销助手智能体 · 阶段 1 最小原型（单文件版，不涉及 Web）
================================================================

功能链路：
  读取本地报销制度 markdown → 按 500 字切片 → 向量化存入 FAISS
  → 命令行输入问题 → BM25 + 向量混合检索 top3 → 拼入提示词 → 调用 LLM 回答

运行前安装依赖：
  pip install faiss-cpu rank_bm25 jieba numpy openai

运行方式：
  python 报销助手_阶段1_最小原型.py 制度文件.md
  可用环境变量覆盖默认配置：
    LLM_PROVIDER   = openai_compat | mock     （供应商可配置，默认 mock 便于离线演示）
    LLM_BASE_URL   = https://api.deepseek.com （OpenAI 兼容端点，可换任何兼容供应商）
    LLM_API_KEY    = sk-xxx
    LLM_MODEL      = deepseek-chat
    EMBEDDING_MODE = local | api               （local=本地哈希词袋，免联网；api=OpenAI 兼容向量接口）

模块标注约定：
  【核心模块·后续复用】= 阶段 2 搬进 FastAPI 服务层时原样或近原样复用
  【原型专用】         = 只在命令行原型里存在，上 Web 后会被路由/会话层替换
"""

import os
import re
import sys
import hashlib

import numpy as np

# ======================================================================
# 第一部分：配置区 【核心模块·后续复用】
#   阶段 2 会原样迁移到 app/core/config.py（pydantic-settings）。
#   设计要点：所有"会变的"东西只从这里读，业务代码里不出现裸字符串配置。
# ======================================================================

CFG = {
    # ---- LLM 供应商配置：换供应商只改这三个环境变量，不改任何代码 ----
    "LLM_PROVIDER": os.getenv("LLM_PROVIDER", "mock"),        # mock=离线演示，openai_compat=真实调用
    "LLM_BASE_URL": os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    "LLM_API_KEY":  os.getenv("LLM_API_KEY", ""),
    "LLM_MODEL":    os.getenv("LLM_MODEL", "deepseek-chat"),

    # ---- 向量化配置 ----
    "EMBEDDING_MODE": os.getenv("EMBEDDING_MODE", "local"),   # local 免联网，api 走 OpenAI 兼容接口
    "EMBEDDING_DIM":  512,                                    # 本地哈希词袋的维度（api 模式下以接口返回为准）

    # ---- 切片与检索参数（行业常用起点值，试点首周按命中率调优）----
    "CHUNK_SIZE":    500,   # 每片目标字符数（题目要求 500 字）
    "CHUNK_OVERLAP":  50,   # 相邻片段重叠字符数，防止关键句被切断
    "TOP_K":           3,   # 混合检索后取前 3 片
    "BM25_K":          3,   # BM25 单路召回条数
    "VEC_K":           3,   # 向量单路召回条数
    "RRF_K":          60,   # Reciprocal Rank Fusion 常数，经验值 60

    # ---- 回答模式：extractive=直接摘录制度原句（无需 API Key，开箱即用）----
    #      llm=走大模型生成（需配置 LLM_PROVIDER=openai_compat + Key）
    "ANSWER_MODE": os.getenv("ANSWER_MODE", "auto"),  # auto: mock供应商自动用 extractive
}


# ======================================================================
# 第二部分：文本切片 【核心模块·后续复用】
#   职责：把 markdown 按"标题感知 + 500 字窗口"切成带元数据的片段。
#   为什么先按标题分组：制度文档的条款天然挂在标题下，跨标题切片会把
#   "住宿标准 400 元"切到与之无关的章节里，检索时引用就会错位。
# ======================================================================

def chunk_markdown(text: str, chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    """
    切片策略（两级）：
      1) 按 markdown 标题（#/##/###）切成"语义段"；
      2) 单个语义段超过 chunk_size 时，按段落聚合切成约 chunk_size 的窗口，
         窗口间保留 overlap 字符重叠。
    返回：[{"chunk_id", "heading", "text"}, ...]
    """
    chunks: list[dict] = []
    # ---- 第一级：按标题切语义段，标题作为每片的元数据（引用溯源要用）----
    sections = re.split(r"\n(?=#{1,4}\s)", text)          # 在标题行前切开
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        # 取第一行作为标题；无标题的散文本归入"总则"
        first_line, _, body = sec.partition("\n")
        heading = first_line.lstrip("# ").strip() if first_line.startswith("#") else "总则"
        if not first_line.startswith("#"):
            body = sec.strip()
        body = body.strip()
        if not body:
            continue          # 只有标题没有正文（如文档一级标题）→ 不生成垃圾片段

        # ---- 第二级：段落聚合滑窗 ----
        paras = [p.strip() for p in body.split("\n") if p.strip()]
        buf = ""                                          # 当前窗口的累积文本
        for para in paras:
            # 单段落本身就超长时（如一整段条款），先硬切保底
            while len(para) > chunk_size:
                chunks.append({"heading": heading, "text": para[:chunk_size]})
                para = para[chunk_size - overlap:]        # 带重叠继续切
            # 尝试把段落放进当前窗口；放不下则先结算当前窗口
            if buf and len(buf) + len(para) + 1 > chunk_size:
                chunks.append({"heading": heading, "text": buf})
                buf = buf[-overlap:]                      # 保留尾部 overlap，衔接上下文
            buf = (buf + "\n" + para).strip()
        if buf:
            chunks.append({"heading": heading, "text": buf})

    # 编排 chunk_id：标题拼正文首 12 字，人工核对时一眼能认出是哪条制度
    for i, c in enumerate(chunks):
        c["chunk_id"] = f"[{i + 1}]《{c['heading']}》{c['text'][:12]}…"
    return chunks


# ======================================================================
# 第三部分：向量化抽象层 【核心模块·后续复用】
#   两种实现：
#     local —— jieba 分词 + 特征哈希 + TF-IDF 加权，零依赖免联网，
#              质量足够支撑原型演示与检索联调（不是真实语义向量，属占位）；
#     api   —— 任何 OpenAI 兼容 /v1/embeddings 接口（硅基流动、通义等）。
#   阶段 2 的替换计划：local 换成 bge-small-zh 本地模型，接口签名不变。
# ======================================================================

import jieba  # 中文分词：BM25 与本地向量都依赖它


def _hash_bucket(token: str, dim: int) -> int:
    """特征哈希：把任意词映射到 [0, dim) 的桶下标（确定性，无需维护词表）。"""
    return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % dim


class LocalEmbedder:
    """本地向量化器：哈希词袋 + TF-IDF 权重 + L2 归一化。【核心模块·后续复用接口】"""

    def __init__(self, dim: int):
        self.dim = dim
        self.df: dict[str, int] = {}   # 词 -> 出现过的片段数（算 IDF 用）
        self.n_docs = 0

    def _tokens(self, text: str) -> list[str]:
        return [t for t in jieba.lcut(text) if t.strip()]

    def fit(self, texts: list[str]):
        """统计 IDF：在全部切片上 fit，再对每片 transform。"""
        self.n_docs = len(texts)
        for t in texts:
            for w in set(self._tokens(t)):
                self.df[w] = self.df.get(w, 0) + 1

    def transform(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for w in self._tokens(text):
            # TF-IDF：词频 × 逆文档频率；经 md5 哈希落到固定维度
            idf = np.log((self.n_docs + 1) / (self.df.get(w, 0) + 1)) + 1.0
            vec[_hash_bucket(w, self.dim)] += idf
        n = np.linalg.norm(vec)
        return vec / n if n > 0 else vec          # L2 归一化后，内积 = 余弦相似度


class ApiEmbedder:
    """OpenAI 兼容向量接口封装。【核心模块·后续复用】"""

    def __init__(self, base_url: str, api_key: str, model: str = "text-embedding-v3"):
        from openai import OpenAI                 # 延迟导入：local 模式不装 openai 也能跑
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    def transform(self, text: str) -> np.ndarray:
        resp = self.client.embeddings.create(model=self.model, input=[text])
        v = np.asarray(resp.data[0].embedding, dtype=np.float32)
        return v / np.linalg.norm(v)


# ======================================================================
# 第四部分：混合检索器 【核心模块·后续复用】★ 本原型最核心的模块
#   BM25（关键词精确命中，如"住宿标准"）+ 向量（语义近似，如"住一晚多少钱"）
#   两路各取 top-k，用 RRF（倒数排名融合）合并 —— 单路检索总有一类问题吃亏，
#   混合是行业默认起点。阶段 2 原样搬进 app/agents/retriever.py。
# ======================================================================

class HybridRetriever:
    def __init__(self, chunks: list[dict], cfg: dict):
        self.chunks = chunks
        texts = [c["heading"] + "\n" + c["text"] for c in chunks]

        # ---- 路 1：BM25（基于 jieba 分词）----
        from rank_bm25 import BM25Okapi
        self.bm25 = BM25Okapi([jieba.lcut(t) for t in texts])

        # ---- 路 2：向量 + FAISS ----
        import faiss
        if cfg["EMBEDDING_MODE"] == "api":
            self.embedder = ApiEmbedder(cfg["LLM_BASE_URL"], cfg["LLM_API_KEY"])
            dim = None
        else:
            self.embedder = LocalEmbedder(cfg["EMBEDDING_DIM"])
            self.embedder.fit(texts)
            dim = cfg["EMBEDDING_DIM"]
        mat = np.stack([self.embedder.transform(t) for t in texts])
        if mat.shape[1] != (dim or mat.shape[1]):         # api 模式下以实际返回维度为准
            dim = mat.shape[1]
        self.index = faiss.IndexFlatIP(dim)               # 内积索引（向量已归一化 = 余弦）
        self.index.add(mat.astype("float32"))

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """双路召回 → RRF 融合 → 返回 top_k 片段（带融合分与两路排名，便于调试）。"""
        # BM25 路：取分数最高的 BM25_K 条；全零分（无关键词命中）时不产生候选，
        # 否则 argsort 的任意下标会拿到不该有的融合分，污染 top3
        bm_scores = self.bm25.get_scores(jieba.lcut(query))
        bm_rank = [int(i) for i in np.argsort(-bm_scores)[: CFG["BM25_K"]] if bm_scores[i] > 0]

        # 向量路：FAISS 取最相似的 VEC_K 条
        qv = self.embedder.transform(query).reshape(1, -1).astype("float32")
        _, vec_idx = self.index.search(qv, CFG["VEC_K"])
        vec_rank = list(vec_idx[0])

        # RRF 融合：score = Σ 1/(RRF_K + rank)，排名越靠前贡献越大
        rrf: dict[int, float] = {}
        for rank, idx in enumerate(bm_rank):
            rrf[int(idx)] = rrf.get(int(idx), 0.0) + 1.0 / (CFG["RRF_K"] + rank + 1)
        for rank, idx in enumerate(vec_rank):
            rrf[int(idx)] = rrf.get(int(idx), 0.0) + 1.0 / (CFG["RRF_K"] + rank + 1)

        top = sorted(rrf.items(), key=lambda x: -x[1])[:top_k]
        results = []
        for idx, score in top:
            c = self.chunks[idx]
            results.append({
                "chunk_id": c["chunk_id"],
                "heading": c["heading"],
                "text": c["text"],
                "rrf_score": round(score, 5),
                "hit_by": ("both" if idx in set(bm_rank) & set(vec_rank)
                           else "bm25" if idx in set(bm_rank) else "vector"),
            })
        return results


# ======================================================================
# 第五部分：LLM 调用封装 【核心模块·后续复用】
#   独立函数 + 供应商配置化，是"供应商可替换"硬约束的最小实现。
#   阶段 2 搬进 app/agents/llm/，在这里加超时、重试、降级、成本记账。
# ======================================================================

def call_llm(system_prompt: str, user_prompt: str, cfg: dict) -> str:
    """
    统一入口。provider：
      openai_compat —— 任何 OpenAI 兼容端点（DeepSeek/通义/Kimi/本地 vLLM…），
                       换供应商 = 改 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY 三个环境变量；
      mock          —— 不联网，直接把检索上下文打印出来返回固定话术，
                       用于离线演示与冒烟测试。
    """
    if cfg["LLM_PROVIDER"] == "mock":
        # 【原型专用】mock：验证"检索→拼装提示词"链路时不需要真实 API Key
        print("\n[mock] 实际发送的提示词预览（前 400 字）：")
        print(user_prompt[:400], "……")
        return "[mock 回答] 已完成检索与提示词拼装。配置 LLM_PROVIDER=openai_compat 及 API Key 后，此处将返回真实回答。"

    if cfg["LLM_PROVIDER"] == "openai_compat":
        from openai import OpenAI
        client = OpenAI(base_url=cfg["LLM_BASE_URL"], api_key=cfg["LLM_API_KEY"])
        resp = client.chat.completions.create(
            model=cfg["LLM_MODEL"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,     # 制度问答要稳定，温度压低
        )
        return resp.choices[0].message.content

    raise ValueError(f"未知 LLM_PROVIDER: {cfg['LLM_PROVIDER']}")


# ======================================================================
# 第六部分：提示词模板 【核心模块·后续复用】
#   两条铁律写死在 system 里：只依据片段回答 + 未覆盖必须明说。
#   这是防幻觉的最后一道闸，比任何检索调参都重要。
# ======================================================================

SYSTEM_PROMPT = (
    "你是企业报销制度助手。回答规则：\n"
    "1. 只依据【制度片段】回答，不得使用制度之外的任何知识；\n"
    "2. 引用所依据的片段编号，格式如 [1][3]；\n"
    "3. 若片段未覆盖问题，明确回答'现行制度未覆盖该问题，请联系财务确认'，禁止编造；\n"
    "4. 回答简短，先给结论再给条款出处。"
)


def build_user_prompt(question: str, hits: list[dict]) -> str:
    """把 top3 片段拼成带编号的上下文。【核心模块·后续复用】"""
    ctx = "\n\n".join(f"【制度片段 {h['chunk_id']}】\n{h['text']}" for h in hits)
    return f"【制度片段】\n{ctx}\n\n【问题】{question}"


# ----------------------------------------------------------------------
# 摘录式回答 【核心模块·后续复用】（阶段2 作为 LLM 不可用时的降级通道保留）
#   思路：不做生成，直接从命中片段里挑出与问题词重合度最高的制度原句。
#   没有 API Key 也能用，且答案 100% 是制度原文、零幻觉——
#   缺点是"不会组织语言"，所以阶段 2 它退居 LLM 故障时的兜底出口。
# ----------------------------------------------------------------------

def extractive_answer(question: str, hits: list[dict]) -> str:
    # 数字归一化：口语"一万"对不上制度里的"10000"，先替换再分词
    q = question
    for k, v in {"一万五": "15000", "一万": "10000", "两万": "20000", "五万": "50000",
                 "五千": "5000", "两千": "2000", "三千": "3000"}.items():
        q = q.replace(k, v)
    q_tokens = [t for t in jieba.lcut(q) if len(t.strip()) >= 2]        # 词级匹配
    grams = list({q[i:i + 2] for i in range(len(q) - 1)})               # 字符二元组（兜底用）

    def _collect(match_fn):
        out = []
        for h in hits:
            for sent in re.split(r"(?<=[。！!？?])", h["text"]):        # 只按句末标点拆，保留完整条款
                sent = sent.strip()
                if sent:
                    out.append((match_fn(sent), sent, h["chunk_id"]))
        return [c for c in out if c[0] > 0]

    # 先用词级重合度；为空时降级到字符二元组（覆盖"住一晚"vs"每晚"这类分词不一致）
    candidates = _collect(lambda s: sum(1 for t in q_tokens if t in s))
    if not candidates:
        candidates = _collect(lambda s: sum(1 for g in grams if g in s))
    if not candidates:
        return "现行制度未覆盖该问题，请联系财务确认。（检索片段中无直接对应条款）"
    candidates.sort(key=lambda x: -x[0])          # 词重合度高的句子排前面
    return "\n".join(f"· {s}　—— 出处 {cid}" for _, s, cid in candidates[:3])


# ======================================================================
# 第七部分：命令行主流程 【原型专用】
#   阶段 2 会拆成 api/chat.py（SSE 流式）+ services/chat_service.py。
# ======================================================================

def main():
    if len(sys.argv) < 2:
        print("用法：python 报销助手_阶段1_最小原型.py <制度markdown文件>")
        sys.exit(1)

    # 1) 读文件、切片
    with open(sys.argv[1], encoding="utf-8") as f:
        text = f.read()
    chunks = chunk_markdown(text, CFG["CHUNK_SIZE"], CFG["CHUNK_OVERLAP"])
    print(f"√ 已切片：{len(chunks)} 片（目标 {CFG['CHUNK_SIZE']} 字/片，重叠 {CFG['CHUNK_OVERLAP']} 字）")

    # 2) 建索引（BM25 + FAISS）
    retriever = HybridRetriever(chunks, CFG)
    print(f"√ 索引就绪：BM25 + FAISS（{CFG['EMBEDDING_MODE']} 向量），供应商 = {CFG['LLM_PROVIDER']}")
    print("输入问题开始问答，输入 q 退出。\n")

    # 3) 问答循环
    while True:
        try:
            q = input("问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in {"q", "quit", "exit"}:
            break

        hits = retriever.search(q, CFG["TOP_K"])
        print("\n── 检索命中 ──")
        for h in hits:
            print(f"  {h['chunk_id']}  融合分 {h['rrf_score']}  命中路 {h['hit_by']}")

        # 回答模式分流：extractive（或 mock 供应商）→ 直接摘录制度原句，开箱即用；
        # 配置了真实 LLM → 走生成式回答
        if CFG["ANSWER_MODE"] == "extractive" or CFG["LLM_PROVIDER"] == "mock":
            answer = extractive_answer(q, hits)
        else:
            answer = call_llm(SYSTEM_PROMPT, build_user_prompt(q, hits), CFG)
        print(f"\n── 回答 ──\n{answer}\n")


if __name__ == "__main__":
    main()
