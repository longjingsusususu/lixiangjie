# -*- coding: utf-8 -*-
"""
f24015105_test_diagrams.py —— 生成测试方案与五份场景文档所需的 6 张图。
输出：mvp/test_diagrams/*.png（中文用微软雅黑，浅底深字，适配 docx 嵌入）
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
matplotlib.rcParams["axes.unicode_minus"] = False

OUT = Path(__file__).resolve().parent.parent / "mvp" / "test_diagrams"
OUT.mkdir(parents=True, exist_ok=True)
NAVY, ORANGE, GREEN, RED, GRAY = "#1f3a5f", "#e67e22", "#27ae60", "#c0392b", "#5b6676"

res = json.loads((Path(__file__).resolve().parent.parent / "mvp" / "test_results.json").read_text(encoding="utf-8"))
R = res["results"]


def save(fig, name):
    fig.savefig(OUT / name, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("生成", name)


# ── 图 0：五类场景用例数量与结果总览 ─────────────────────────────
def chart_overview():
    names = list(R.keys())
    passed = [R[n]["passed"] for n in names]
    failed = [R[n]["failed"] for n in names]
    fig, ax = plt.subplots(figsize=(8.6, 4.0))
    x = range(len(names))
    ax.bar([i - 0.18 for i in x], passed, width=0.36, color=NAVY, label="通过")
    ax.bar([i + 0.18 for i in x], failed, width=0.36, color=RED, label="失败")
    for i, v in enumerate(passed):
        ax.text(i - 0.18, v + 0.4, str(v), ha="center", fontsize=11, color=NAVY, fontweight="bold")
    for i, v in enumerate(failed):
        if v:
            ax.text(i + 0.18, v + 0.4, str(v), ha="center", fontsize=11, color=RED)
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, fontsize=11)
    ax.set_ylabel("断言数量（项）")
    ax.set_title(f"五类场景量化测试结果总览 ｜ 合计通过 {sum(passed)} / {sum(passed)+sum(failed)} 项断言", fontsize=13, color=NAVY, pad=12)
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    save(fig, "overview.png")


# ── 图 1：正常场景端到端业务链路 ─────────────────────────────
def chart_normal():
    fig, ax = plt.subplots(figsize=(10.5, 3.2))
    ax.axis("off")
    steps = [("登录\n五角色", 5), ("登记\n需求项", 2), ("确认签字\n业务负责人", 4), ("CSV导入\n幂等", 2),
             ("建版本\nV-0001", 2), ("定版\nf24校验", 2), ("三要素\n交接", 2), ("接收\n确认", 2), ("看板\nM6/M7", 4)]
    total = sum(s[1] for s in steps)
    x = 0.5
    for name, n in steps:
        w = n / total * 92
        color = GREEN if "确认" in name or "定版" in name else NAVY
        ax.add_patch(FancyBboxPatch((x, 0.35), w - 1.2, 0.4, boxstyle="round,pad=0.02",
                                    fc=color, ec="none"))
        ax.text(x + (w - 1.2) / 2, 0.55, name, ha="center", va="center", color="white", fontsize=9.5)
        x += w
    ax.text(50, 0.12, "23 项断言全部通过 ｜ 双时间戳可区分 ｜ 留痕四项（查/版本/建议/确认）完整落库",
            ha="center", fontsize=10.5, color=GRAY)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 1)
    save(fig, "normal_flow.png")


# ── 图 2：边界场景拦截漏斗 ─────────────────────────────
def chart_boundary():
    fig, ax = plt.subplots(figsize=(9.0, 4.2))
    layers = [
        ("字段极值（空描述/超长/格式错/非法枚举）", 6, ORANGE),
        ("版本条件（空版本/过期有效期/含未确认项/跨批次）", 5, NAVY),
        ("交接未结项数（声明≠实际 → 拦截）", 1, RED),
        ("CSV 边界（空文件/缺列/责任人缺失→missing 不丢弃）", 3, GREEN),
        ("状态机非法迁移（已定版再确认/已定版再作废）", 2, GRAY),
    ]
    y = len(layers)
    for label, n, color in layers:
        w = n / 17 * 84
        ax.add_patch(FancyBboxPatch((1, y - 0.62), max(w, 12), 0.5, boxstyle="round,pad=0.02", fc=color, ec="none", alpha=0.92))
        ax.text(1 + max(w, 12) / 2, y - 0.37, label, ha="center", va="center", color="white", fontsize=9.5)
        ax.text(1 + max(w, 12) + 2, y - 0.37, f"{n} 项", va="center", fontsize=11, color=color, fontweight="bold")
        y -= 1
    ax.set_xlim(0, 100)
    ax.set_ylim(0, len(layers) + 0.6)
    ax.axis("off")
    ax.set_title("边界场景 16 项断言：全部在服务端被显式拒绝（400/422），无静默吞错", fontsize=13, color=NAVY, pad=10)
    save(fig, "boundary_layers.png")


# ── 图 3：对抗场景攻击面 ─────────────────────────────
def chart_adversary():
    fig, ax = plt.subplots(figsize=(9.5, 4.4))
    ax.axis("off")
    attacks = [("伪造 token", "401"), ("篡改签名", "401"), ("过期凭证", "401"), ("截断签名", "401"),
               ("SQL 注入", "参数化吸收"), ("XSS 载荷", "入库+前端转义"), ("绕过前端直调卡口", "3 类全拦"), ("诱导自动通过", "队列无此路径")]
    cols = 4
    for i, (a, r) in enumerate(attacks):
        cx = 12 + (i % cols) * 25
        cy = 0.72 - (i // cols) * 0.42
        ax.add_patch(FancyBboxPatch((cx - 10, cy - 0.14), 20, 0.3, boxstyle="round,pad=0.02", fc="#fdeeee", ec=RED, lw=1.2))
        ax.text(cx, cy + 0.04, a, ha="center", fontsize=10.5, color=RED)
        ax.text(cx, cy - 0.09, f"→ {r}", ha="center", fontsize=9, color=GRAY)
        ax.annotate("", xy=(cx + 10.8, cy), xytext=(cx + 9.9, cy),
                    arrowprops=dict(arrowstyle="->", color=GREEN, lw=2)) if i % cols < cols - 1 else None
    ax.text(50, 0.02, "攻击路径 8 条，全部被服务端防御 ｜ 队列状态集合 {open, processing, closed} 不含自动通过（f44）",
            ha="center", fontsize=10.5, color=GREEN)
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.1, 1.0)
    ax.set_title("对抗场景 13 项断言：凭证伪造 / 注入 / 绕过卡口 / 诱导放行 全部拦截", fontsize=13, color=NAVY, pad=10)
    save(fig, "adversary_surface.png")


# ── 图 4：权限矩阵热图 ─────────────────────────────
def chart_permission():
    ops = ["登记/补录", "确认签字", "建版本", "定版", "发起交接", "CSV导入", "异常认领", "异常闭环", "全量审计"]
    roles = ["一线专员", "协调员", "业务负责人", "质量验收", "管理员"]
    # 0 拒绝 1 允许 2 唯一角色
    M = [
        [1, 0, 0, 0, 1],   # 登记
        [0, 0, 2, 0, 0],   # 确认
        [0, 1, 0, 0, 0],   # 建版本
        [0, 1, 0, 0, 0],   # 定版
        [0, 1, 0, 0, 0],   # 交接
        [0, 0, 0, 0, 1],   # 导入
        [0, 0, 0, 1, 1],   # 认领
        [0, 0, 0, 2, 0],   # 闭环
        [0, 0, 1, 1, 1],   # 审计
    ]
    fig, ax = plt.subplots(figsize=(9.2, 4.6))
    for i in range(len(ops)):
        for j in range(len(roles)):
            v = M[i][j]
            color = GREEN if v == 1 else (NAVY if v == 2 else "#e8ebf0")
            text = "✓" if v == 1 else ("唯一" if v == 2 else "✗")
            tcolor = "white" if v else "#b8c0ca"
            ax.add_patch(plt.Rectangle((j, i), 1, 1, fc=color, ec="white", lw=2))
            ax.text(j + 0.5, i + 0.5, text, ha="center", va="center", color=tcolor, fontsize=11, fontweight="bold")
    ax.set_xticks([j + 0.5 for j in range(len(roles))])
    ax.set_xticklabels(roles, fontsize=10.5)
    ax.set_yticks([i + 0.5 for i in range(len(ops))])
    ax.set_yticklabels(ops, fontsize=10.5)
    ax.set_xlim(0, len(roles))
    ax.set_ylim(len(ops), 0)
    ax.invert_yaxis()
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(length=0)
    ax.set_title("权限矩阵实测结果（19 项断言）：编辑权与确认权分离，确认/闭环各有唯一角色（f22/f44）",
                 fontsize=12.5, color=NAVY, pad=12)
    save(fig, "permission_matrix.png")


# ── 图 5：故障场景恢复时间线 ─────────────────────────────
def chart_fault():
    fig, ax = plt.subplots(figsize=(10.0, 3.6))
    ax.axis("off")
    events = [
        ("脏数据入队", 1, ORANGE), ("qa 认领", 2, NAVY), ("填结论闭环", 3, GREEN), ("原行保留 f33", 4, GREEN),
        ("连接重建", 6, NAVY), ("数据完整", 7, GREEN), ("在线备份", 9, NAVY), ("恢复演练", 10, GREEN),
        ("故障注入", 12, RED), ("400 显式拒绝", 13, GREEN), ("密钥轮换", 15, RED), ("旧token失效", 16, GREEN),
    ]
    ax.hlines(0.5, 0, 17.5, color="#dde3ea", lw=2)
    for label, x, color in events:
        ax.plot(x, 0.5, "o", color=color, markersize=11, zorder=3)
        ax.text(x, 0.62 if x % 2 else 0.34, label, ha="center", fontsize=9.5,
                color=color, rotation=0, fontweight="bold")
    ax.text(8.7, 0.9, "故障场景 10 项断言：注入 → 防御 → 恢复全程留痕，恢复动作全部可演练复现",
            ha="center", fontsize=11.5, color=NAVY)
    ax.set_xlim(0, 18)
    ax.set_ylim(0, 1.1)
    save(fig, "fault_timeline.png")


# ── 图 6：测试方案五场景金字塔 ─────────────────────────────
def chart_pyramid():
    fig, ax = plt.subplots(figsize=(8.8, 4.0))
    ax.axis("off")
    layers = [("对抗场景", 13, "伪造/注入/绕过卡口/诱导放行 —— 信任边界必须服务端强制", RED),
              ("权限场景", 19, "五角色越权 403，确认权/闭环权唯一（f22/f44）", NAVY),
              ("故障场景", 10, "持久性/备份恢复/故障注入/密钥轮换", GRAY),
              ("边界场景", 16, "字段极值/条件拒绝/幂等/状态机非法迁移", ORANGE),
              ("正常场景", 23, "端到端业务链路 + M6/M7 看板出数", GREEN)]
    y = 4.4
    for name, n, desc, color in layers:
        w = 14 + n * 2.4
        ax.add_patch(FancyBboxPatch((50 - w / 2, y - 0.68), w, 0.56, boxstyle="round,pad=0.02", fc=color, ec="none", alpha=0.93))
        ax.text(50, y - 0.4, f"{name}  {n} 项", ha="center", va="center", color="white", fontsize=11.5, fontweight="bold")
        ax.text(96, y - 0.4, desc, ha="right", va="center", fontsize=9.5, color=GRAY)
        y -= 0.92
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 5)
    ax.set_title("量化测试方案 · 五层场景模型（自底向上：先证可用，再证可信）", fontsize=13, color=NAVY, pad=10)
    save(fig, "pyramid.png")


if __name__ == "__main__":
    chart_overview()
    chart_normal()
    chart_boundary()
    chart_adversary()
    chart_permission()
    chart_fault()
    chart_pyramid()
    print("全部图表生成完毕 →", OUT)
