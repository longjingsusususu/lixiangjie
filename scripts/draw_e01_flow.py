# -*- coding: utf-8 -*-
"""
draw_e01_flow.py —— 改造前流程图（融入典型事件卡 E-01）

设计原则：
1. 骨架沿用既有《现状流程图》的 13 个节点（9 个处理 + 4 个判断），保证与前图可比；
2. 在五个交接节点上打 E-01 徽标，标注该节点在事件卡里的取证状态（25 字段中可填 0 个）；
3. 右侧只保留 4 个断点注释，并挂上问题编号 P-01～P-08，避免图上出现无出处结论；
4. 底部单列“五段链路 × 六字段”表，全部写“待采集 / 无记录”，不填任何估算值；
5. 配色沿用：白底、深蓝矩形、深蓝菱形、砖红警示，不使用黄色。
"""

import os

from PIL import Image, ImageDraw, ImageFont

# ── 画布与配色 ────────────────────────────────────────────────
W = 1400
M = 70                      # 左右页边距
BG = "#FFFFFF"
NAVY = "#1F3A5F"
GRAY = "#5B6676"
LIGHT = "#8A94A6"
LINE = "#B7C2D2"
RED = "#C0392B"
RED_FILL = "#FBEEEC"
BLUE_FILL = "#F4F7FB"
PANEL = "#F7F9FC"
GRID = "#DCE3EC"

FONT_DIR = "C:/Windows/Fonts"
F_REG = os.path.join(FONT_DIR, "msyh.ttc")      # 微软雅黑
F_BOLD = os.path.join(FONT_DIR, "msyhbd.ttc")


def font(size, bold=False):
    return ImageFont.truetype(F_BOLD if bold else F_REG, size)


# ── 文本工具 ──────────────────────────────────────────────────
def text_w(draw, s, f):
    return draw.textlength(s, font=f)


def wrap(draw, s, f, max_w):
    """按像素宽度折行，中文逐字断，英文按词断。"""
    lines, cur = [], ""
    for ch in s:
        trial = cur + ch
        if text_w(draw, trial, f) <= max_w:
            cur = trial
        else:
            if ch in "，。、；：）】」”" and cur:      # 避头标点跟随上一行
                cur += ch
                lines.append(cur)
                cur = ""
                continue
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def draw_par(draw, xy, s, f, fill, max_w, lh=None, align="left"):
    """折行绘制，返回占用高度。"""
    lh = lh or int(f.size * 1.55)
    lines = wrap(draw, s, f, max_w)
    x, y = xy
    for ln in lines:
        if align == "center":
            draw.text((x + (max_w - text_w(draw, ln, f)) / 2, y), ln, font=f, fill=fill)
        elif align == "right":
            draw.text((x + max_w - text_w(draw, ln, f), y), ln, font=f, fill=fill)
        else:
            draw.text((x, y), ln, font=f, fill=fill)
        y += lh
    return y - xy[1]


def chip(draw, x, y, text, f, fg, bg, border):
    """小标签，返回宽度。"""
    pad = 10
    w = text_w(draw, text, f) + pad * 2
    h = f.size + 12
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, fill=bg, outline=border, width=1)
    draw.text((x + pad, y + (h - f.size) / 2 - 2), text, font=f, fill=fg)
    return w


# ── 节点定义（骨架与既有现状图一致）────────────────────────────
# kind: box=处理  diamond=判断
# role: 左侧泳道角色
# e01:  E-01 徽标文案（None 表示非交接节点）
# note: 节点内第二行（现状动作）
NODES = [
    dict(kind="box", role="主办方 / 销售顾问", title="议程确认", note="收集需求、设定场地要求", stage="S1",
         e01="E-01 ①议程确认｜录入/确认/产出/耗时/证据：全待采集"),
    dict(kind="diamond", role="一线 / 协调", title="需求是否由同一人录入？", note="", e01=None),
    dict(kind="box", role="一线 · 林亦舟", title="群消息 / 纸质表先兜底", note="事后回系统补录、抄送真伪靠人记", stage="S3",
         e01=None),
    dict(kind="box", role="协调 · 唐映秋", title="汇总成 50 项清单", note="多版本来回人工排期、格式不一", stage="S4",
         e01="E-01 ②嘉宾需求｜需求清单无版本标识，无确认人"),
    dict(kind="diamond", role="协调 / 数字化", title="根本原因能否确定？", note="判断口径未书面化", e01=None),
    dict(kind="box", role="交接 · 供应商", title="供应商交接", note="需附清单 / 版本 / 截止时间", stage="S6",
         e01="E-01 ③供应商交接｜问题高发点，交接单无确认状态"),
    dict(kind="diamond", role="协同 / 接收方", title="三要素是否齐全？", note="缺项仍可作为「已处理」流转", e01=None),
    dict(kind="box", role="供应商 / 执行", title="按原方案执行", note="按旧版本执行，变更只临时口头补", stage="S7",
         e01="E-01 ④执行｜变更以口头/群消息发生，执行侧按旧版准备"),
    dict(kind="diamond", role="执行 / 业务人员", title="异常是否被发现？", note="无入口、无值守、无时限", e01=None),
    dict(kind="box", role="业务人员", title="对外承诺", note="口头确认，无书面授权，报价一旦发出即不可撤销", stage="S7",
         e01=None),
    dict(kind="box", role="数字化 · 秦芮", title="跨三套工具核对", note="服务台 / 台账 / 文件库，无统一编号", stage="S9",
         e01=None),
    dict(kind="box", role="审核 · 傅景", title="50 项逐项验收", note="责任 + 确认状态齐全才签", stage="S9", e01=None),
    dict(kind="box", role="协调 · 叶书", title="客诉登记与每周复盘", note="只登记原样信息，不追根源，无闭环责任人", stage="S9",
         e01="E-01 ⑤反馈｜反馈与最初清单的对应关系不清"),
]

# 右侧断点注释：只写有出处的
MARKERS = [
    dict(node=1, tag="断点 1", title="同一需求反复录入", body="需求在议程、嘉宾、交接三处各录一次，靠人眼比对；FDE 访谈未量化，M2 待采集。", pid="P-01"),
    dict(node=4, tag="断点 2", title="版本无生效标识", body="靠文件名猜版本，旧版只要被判「有效」就继续流转；变更后各方口径不一致。", pid="P-02"),
    dict(node=6, tag="断点 3", title="责任在交接处断开", body="交接三要素（清单/接收人/时间窗）缺失仍可流转，无确认状态即无责任人。", pid="P-03"),
    dict(node=8, tag="断点 4", title="异常无入口", body="现场异常靠人跑、靠群里喊；无队列、无值守、无时限，也不得默认通过。", pid="P-04 / P-08"),
]

# 底部五段链路表（E-01 §2.2 原表，25 格全空）
E01_COLS = [("交接节点", 200), ("谁录入", 150), ("谁确认", 150), ("产出物", 250), ("单次耗时", 150), ("证据状态", 200)]
E01_ROWS = [
    ("① 议程确认", "议程与初步清单"),
    ("② 嘉宾需求", "嘉宾需求清单"),
    ("③ 供应商交接", "交接单"),
    ("④ 执行", "执行记录"),
    ("⑤ 反馈", "反馈汇总"),
]


def build():
    # 先用临时画布量文本高度（折行结果与最终一致）
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    f_title = font(34, True)
    f_sub = font(15)
    f_chip = font(12)
    f_node_t = font(18, True)
    f_node_n = font(13)
    f_badge = font(12)
    f_role = font(12)
    f_mark_t = font(14, True)
    f_mark_b = font(12.5)
    f_sec = font(17, True)
    f_th = font(12, True)
    f_td = font(12)
    f_foot = font(11.5)

    # 节点高度
    NAV_X, NAV_W = M, 200
    NODE_X, NODE_W = M + 215, 560
    MARK_X, MARK_W = NODE_X + NODE_W + 42, W - M - (NODE_X + NODE_W + 42)
    GAP = 30

    heights = []
    for n in NODES:
        if n["kind"] == "diamond":
            heights.append(104)
        else:
            h = 30 + 26 + 22
            if n["e01"]:
                h += 24
            heights.append(h)

    # 头部高度（标题 + 标签 + 事件条 + 分隔线）
    head_h = 272
    # 底部表格 + 说明
    table_h = 44 + 26 + len(E01_ROWS) * 40 + 30
    foot_h = 132
    total_h = head_h + sum(heights) + GAP * (len(NODES) - 1) + 40 + table_h + foot_h + 40

    img = Image.new("RGB", (W, total_h), BG)
    d = ImageDraw.Draw(img)

    # ── 页眉 ──
    d.text((M, 48), "会议活动服务需求汇总 · 改造前流程图", font=f_title, fill=NAVY)
    d.text((M, 96), "融入典型事件卡 E-01（2026-11-06 六轮访谈取证）｜ 现状链路：议程确认 → 嘉宾需求 → 供应商交接 → 执行 → 反馈（f01）",
           font=f_sub, fill=GRAY)

    # 三个事实标签
    cx = M
    for txt, fg, bg, bd in [
        ("E-01 证据等级：B 级（访谈陈述 5 条，A 级原始记录 0 份）", RED, RED_FILL, "#E6B9B2"),
        ("五个交接节点 25 个字段，当前可填 0 个", NAVY, BLUE_FILL, "#C3D2E4"),
        ("业务量化值 0 项｜不填估算数", "#8A6A1F", "#FDF6E7", "#E4D3A8"),
    ]:
        cx += chip(d, cx, 128, txt, f_chip, fg, bg, bd) + 10

    # 事件一句话：让图单独拿出去也能说清是哪起事件
    ey = 176
    d.rectangle([M, ey, W - M, ey + 56], fill=PANEL, outline=GRID)
    d.rectangle([M, ey, M + 4, ey + 56], fill=NAVY)
    d.text((M + 18, ey + 8), "事件 E-01｜会议活动服务需求汇总中的重复核对与交接口径失真",
           font=font(14.5, True), fill=NAVY)
    d.text((M + 18, ey + 32),
           "直接原因：交接点无统一确认状态、变更后各方口径不同步　｜　结构性原因：登记分散、无单点权威版本、交接无强制确认（待原始记录验证）",
           font=f_chip, fill=GRAY)
    d.line([(M, 252), (W - M, 252)], fill=GRID, width=1)

    # ── 主体流程 ──
    y = head_h
    node_pos = []          # 记录每个节点的几何，用于画箭头与断点连线
    for i, n in enumerate(NODES):
        h = heights[i]
        cy = y + h / 2
        if n["kind"] == "diamond":
            dw, dh = 330, 96
            dx = NODE_X + (NODE_W - dw) / 2
            pts = [(dx + dw / 2, cy - dh / 2), (dx + dw, cy), (dx + dw / 2, cy + dh / 2), (dx, cy)]
            d.polygon(pts, fill=BG, outline=NAVY)
            d.line(pts + [pts[0]], fill=NAVY, width=2)
            # 菱形内文字（判断句 + 口径说明）
            tw = text_w(probe, n["title"], f_node_t)
            d.text((dx + (dw - tw) / 2, cy - 22), n["title"], font=f_node_t, fill=NAVY)
            if n["note"]:
                nw = text_w(probe, n["note"], f_node_n)
                d.text((dx + (dw - nw) / 2, cy + 8), n["note"], font=f_node_n, fill=RED if "仍可" in n["note"] or "无" in n["note"] else LIGHT)
            node_pos.append(dict(x=NODE_X, y=y, w=NODE_W, h=h, cx=NODE_X + NODE_W / 2, cy=cy))
        else:
            d.rounded_rectangle([NODE_X, y, NODE_X + NODE_W, y + h], radius=6, fill=BG, outline=NAVY, width=2)
            d.text((NODE_X + 18, y + 13), n["title"], font=f_node_t, fill=NAVY)
            if n["note"]:
                d.text((NODE_X + 18, y + 43), n["note"], font=f_node_n, fill=GRAY)
            if n.get("stage"):
                lab = "改造后 → " + n["stage"]
                lf = font(11.5, True)
                d.text((NODE_X + NODE_W - 16 - text_w(probe, lab, lf), y + 15), lab, font=lf, fill="#9BA9BE")
            if n["e01"]:
                by = y + h - 26
                bw = text_w(probe, n["e01"], f_badge) + 20
                d.rounded_rectangle([NODE_X + 14, by - 3, NODE_X + 14 + bw, by + 19], radius=4,
                                    fill=RED_FILL, outline="#E6B9B2")
                d.text((NODE_X + 24, by), n["e01"], font=f_badge, fill=RED)
            node_pos.append(dict(x=NODE_X, y=y, w=NODE_W, h=h, cx=NODE_X + NODE_W / 2, cy=cy))

        # 左侧角色
        role_lines = wrap(probe, n["role"], f_role, NAV_W - 10)
        ry = cy - len(role_lines) * 9
        for ln in role_lines:
            d.text((NAV_X + NAV_W - 10 - text_w(probe, ln, f_role), ry), ln, font=f_role, fill=LIGHT)
            ry += 18

        # 箭头
        if i < len(NODES) - 1:
            ay0, ay1 = y + h, y + h + GAP
            d.line([(NODE_X + NODE_W / 2, ay0), (NODE_X + NODE_W / 2, ay1 - 6)], fill=LINE, width=2)
            d.polygon([(NODE_X + NODE_W / 2 - 5, ay1 - 8), (NODE_X + NODE_W / 2 + 5, ay1 - 8),
                       (NODE_X + NODE_W / 2, ay1)], fill=LINE)
        y += h + GAP

    # ── 右侧断点注释（对齐到对应节点）──
    for mk in MARKERS:
        p = node_pos[mk["node"]]
        lines = wrap(probe, mk["body"], f_mark_b, MARK_W - 26)
        bh = 34 + len(lines) * 19 + 16
        top = p["cy"] - bh / 2
        d.rounded_rectangle([MARK_X, top, MARK_X + MARK_W, top + bh], radius=5, fill=RED_FILL, outline="#E6B9B2")
        d.rectangle([MARK_X, top, MARK_X + 4, top + bh], fill=RED)
        d.text((MARK_X + 16, top + 10), mk["tag"] + "　" + mk["title"], font=f_mark_t, fill=RED)
        yy = top + 33
        for ln in lines:
            d.text((MARK_X + 16, yy), ln, font=f_mark_b, fill="#7B3B33")
            yy += 19
        d.text((MARK_X + MARK_W - 16 - text_w(probe, mk["pid"], f_badge), top + bh - 22), mk["pid"],
               font=f_badge, fill=RED)
        # 指向连线
        d.line([(NODE_X + NODE_W + 4, p["cy"]), (MARK_X - 2, p["cy"])], fill="#E6B9B2", width=1)

    # ── 底部：五段链路 × 六字段 ──
    ty = y + 20
    d.line([(M, ty), (W - M, ty)], fill=GRID, width=1)
    d.text((M, ty + 16), "事件卡 E-01 的五个交接节点：25 个字段的取证状态", font=f_sec, fill=NAVY)
    d.text((W - M - text_w(probe, "表内「待采集 / 无记录」为访谈当时的真实状态，非资料缺陷", f_badge), ty + 22),
           "表内「待采集 / 无记录」为访谈当时的真实状态，非资料缺陷", font=f_badge, fill=LIGHT)

    tx, th_y = M, ty + 52
    cols = E01_COLS
    widths = [c[1] for c in cols]
    total_w = sum(widths)
    scale = (W - M * 2) / total_w
    widths = [w * scale for w in widths]

    # 表头
    d.rectangle([tx, th_y, tx + sum(widths), th_y + 36], fill=PANEL, outline=GRID)
    cxp = tx
    for (name, _), cw in zip(cols, widths):
        d.text((cxp + 12, th_y + 10), name, font=f_th, fill=NAVY)
        d.line([(cxp, th_y), (cxp, th_y + 36)], fill=GRID)
        cxp += cw
    # 表体
    ry = th_y + 36
    for name, out in E01_ROWS:
        d.rectangle([tx, ry, tx + sum(widths), ry + 40], fill=BG, outline=GRID)
        vals = [name, "待采集", "待采集", out, "待采集", "无记录"]
        cxp = tx
        for i, ((_, _), cw) in enumerate(zip(cols, widths)):
            col = RED if vals[i] in ("待采集", "无记录") else NAVY
            fnt = f_td if i == 0 else f_td
            d.text((cxp + 12, ry + 12), vals[i], font=fnt, fill=col)
            if i:
                d.line([(cxp, ry), (cxp, ry + 40)], fill=GRID)
            cxp += cw
        ry += 40

    d.text((tx, ry + 8), "取数对象：林亦舟（现场记录）、唐映秋（交接台账）、傅景（判定口径）—— 由记录持有人填数，访谈者不代填。",
           font=f_badge, fill=GRAY)

    # ── 页脚：结论可用性 + 数据纪律 + 图例 ──
    fy = ry + 40
    d.line([(M, fy), (W - M, fy)], fill=GRID, width=1)
    fy += 14

    # 图例
    lx = M
    d.rectangle([lx, fy + 4, lx + 26, fy + 20], outline=NAVY, width=2)
    d.text((lx + 34, fy), "处理", font=f_badge, fill=GRAY)
    lx += 86
    d.polygon([(lx + 13, fy), (lx + 26, fy + 12), (lx + 13, fy + 24), (lx, fy + 12)], outline=NAVY)
    d.text((lx + 34, fy), "判断", font=f_badge, fill=GRAY)
    lx += 86
    d.rectangle([lx, fy + 2, lx + 26, fy + 22], fill=RED_FILL, outline="#E6B9B2")
    d.text((lx + 34, fy), "断点 / E-01 取证缺口", font=f_badge, fill=GRAY)

    # 两句话结论
    d.text((M, fy + 34), "结论可用性：本图可用于确定问题方向与试点边界；不可用于设定目标值或计算收益——五个节点的耗时、频次均无实测值。",
           font=f_foot, fill=GRAY)
    d.text((M, fy + 56), "数据纪律：50 项为验收口径值而非实测；图中不出现任何估算工时，避免把推断当基线。业务目标以「减少重复核对 ＋ 保留人工确认与来源可查」为准（f00）。",
           font=f_foot, fill=GRAY)

    # 右上角来源标记
    d.text((W - M - text_w(probe, "f24015105 · 事件卡 E-01", f_badge), total_h - 28),
           "f24015105 · 事件卡 E-01", font=f_badge, fill="#B7C2D2")

    return img


if __name__ == "__main__":
    out = os.path.join("docs", "03-architecture", "images", "会议活动服务需求汇总_改造前流程图_融入E-01.png")
    img = build()
    img.save(out)
    print(f"已生成 {out}  {img.size[0]}x{img.size[1]}")
