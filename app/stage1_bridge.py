# -*- coding: utf-8 -*-
"""
stage1_bridge.py —— 阶段 1 核心模块的"搭桥"文件

目的：不复制粘贴，直接把阶段 1 单脚本里的两个核心模块拿过来复用：
  · parse_requirements()  —— markdown 需求清单解析 + 五态推断
  · HybridRetriever       —— BM25 + 向量 RRF 混合检索

做法：用 importlib 按文件路径动态加载阶段 1 脚本。
阶段 1 脚本有 `if __name__ == "__main__"` 保护，import 时不会执行 main()。
阶段 2 正式工程里，这两个模块会分别落到
  app/services/import_service.py 与 app/agents/retriever.py，本文件即可删除。
"""

import importlib.util
import os

# 本文件所在目录：.../app
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 阶段 1 单脚本：优先取随仓库携带的 stage1_prototype.py，兼容早期工作区布局
_CANDIDATES = [
    os.path.join(BASE_DIR, "stage1_prototype.py"),
    os.path.join(BASE_DIR, "..", "会议活动需求_阶段1_最小原型.py"),
]
STAGE1_PATH = next((p for p in _CANDIDATES if os.path.exists(p)), _CANDIDATES[0])

if not os.path.exists(STAGE1_PATH):
    raise RuntimeError(f"找不到阶段 1 原型脚本：{STAGE1_PATH}")

# ── 动态加载：等价于 import，但按绝对路径定位（文件名含中文，普通 import 不行）──
_spec = importlib.util.spec_from_file_location("stage1_proto", STAGE1_PATH)
_stage1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stage1)

# 对外暴露可复用的符号
parse_requirements = _stage1.parse_requirements
HybridRetriever = _stage1.HybridRetriever
RequirementItem = _stage1.RequirementItem
SYNONYMS = _stage1.SYNONYMS


def set_synonyms(mapping: dict):
    """
    热更新同义词表（后台改配置后调用，下一次查询立刻生效，无需重启）。
    做法：原地清空并重填阶段 1 模块里的 SYNONYMS 字典——
    _expand() 引用的是模块全局对象，原地改才能让它看到新值。
    """
    _stage1.SYNONYMS.clear()
    _stage1.SYNONYMS.update({k: list(v) for k, v in mapping.items()})


def get_synonyms() -> dict:
    return {k: list(v) for k, v in _stage1.SYNONYMS.items()}
