# -*- coding: utf-8 -*-
"""
build_report_docx.py —— 把需求报告 Markdown 渲染成排版好的 .docx

支持：# / ## / ### 标题、管道表格、无序与有序列表、引用块、**加粗**、分隔线。
风格：A4、宋体正文 10.5pt/1.5 倍行距、微软雅黑标题、深蓝主色、表头浅蓝底、页脚页码。
用法：python scripts/build_report_docx.py <input.md> <output.docx>
"""

import re
import sys

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

NAVY = RGBColor(0x1F, 0x3A, 0x5F)      # 主色：深蓝
GRAY = RGBColor(0x5B, 0x66, 0x76)      # 辅助文字
HEAD_FILL = "E8EEF6"                   # 表头底色
ZEBRA_FILL = "F7F9FC"                  # 隔行底色


def set_font(run, size=10.5, bold=False, color=None, east="宋体", latin="Times New Roman"):
    """中英文字体同时设置（python-docx 只设 ascii 会导致中文回落）。"""
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = latin
    run._element.rPr.rFonts.set(qn("w:eastAsia"), east)
    if color is not None:
        run.font.color.rgb = color


def shade(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    el = OxmlElement("w:shd")
    el.set(qn("w:val"), "clear")
    el.set(qn("w:color"), "auto")
    el.set(qn("w:fill"), fill)
    tcPr.append(el)


def cell_margins(table, top=40, bottom=40, left=90, right=90):
    tblPr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for tag, val in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        node = OxmlElement(f"w:{tag}")
        node.set(qn("w:w"), str(val))
        node.set(qn("w:type"), "dxa")
        mar.append(node)
    tblPr.append(mar)


def repeat_header(row):
    trPr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    trPr.append(el)


def add_field(paragraph, instr):
    """插入 Word 域（用于页码）。"""
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr_el = OxmlElement("w:instrText")
    instr_el.set(qn("xml:space"), "preserve")
    instr_el.text = instr
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr_el)
    run._r.append(fld_end)
    set_font(run, 9, color=GRAY)


def add_rich(paragraph, text, size=10.5, color=None, bold_all=False, gray=False):
    """解析 **加粗** 与 `代码`，其余按普通文本。"""
    parts = re.split(r"(\*\*.+?\*\*|`.+?`)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            set_font(run, size, True, NAVY if color is None else color)
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            set_font(run, size - 0.5, bold_all, RGBColor(0x8B, 0x2E, 0x2E), east="Consolas", latin="Consolas")
        else:
            run = paragraph.add_run(part)
            set_font(run, size, bold_all, GRAY if gray else color)


def add_table(doc, rows):
    """rows: [[cell,...], ...] 首行为表头。"""
    cols = max(len(r) for r in rows)
    table = doc.add_table(rows=0, cols=cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    cell_margins(table)
    for ri, row in enumerate(rows):
        cells = table.add_row().cells
        for ci in range(cols):
            text = row[ci] if ci < len(row) else ""
            cell = cells[ci]
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.line_spacing = 1.15
            add_rich(p, text, size=8.5, bold_all=(ri == 0))
            if ri == 0:
                shade(cell, HEAD_FILL)
            elif ri % 2 == 0:
                shade(cell, ZEBRA_FILL)
    repeat_header(table.rows[0])
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return table


def parse_md(md):
    """极简解析：返回块列表 (kind, payload)。"""
    blocks, i = [], 0
    lines = md.splitlines()
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.startswith("|"):                      # 表格块
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                raw = lines[i].strip().strip("|")
                cells = [c.strip() for c in raw.split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            blocks.append((f"h{level}", line[level:].strip()))
        elif line.startswith(">"):
            blocks.append(("quote", line.lstrip("> ").strip()))
        elif re.match(r"^[-*]\s+", line):
            blocks.append(("bullet", re.sub(r"^[-*]\s+", "", line)))
        elif re.match(r"^\d+\.\s+", line):
            blocks.append(("num", line))
        elif re.fullmatch(r"-{3,}", line.strip()):
            blocks.append(("hr", ""))
        else:
            blocks.append(("p", line.strip()))
        i += 1
    return blocks


def build(md_path, out_path, doc_kind="", subtitle=""):
    md = open(md_path, encoding="utf-8").read()
    blocks = parse_md(md)
    # 封面标题从 Markdown 首行取，支持「主标题 · 文档类型」写法
    title = md.split("\n", 1)[0].lstrip("# ").strip()
    head_title, _, tail = title.partition(" · ")
    kind = tail or doc_kind or "报 告"
    kind_spaced = " ".join(kind) if len(kind) <= 5 else kind
    # 封面元信息优先从正文「文档控制」表提取，取不到再用默认值
    def pick(pattern, default):
        m = re.search(pattern, md)
        return m.group(1).strip() if m else default
    doc_code = pick(r"\|\s*(?:资料|报告)编号\s*\|\s*([^|]+)\|", "f24015105-DOC-01")
    doc_ver = pick(r"\|\s*版本[^|]*\|[^|]*?v([\d.]+)", "1.0")
    doc_date = pick(r"(20\d{2}-\d{2}-\d{2})", "2026-11-06")
    doc_label = "资料编号" if "资料编号" in md else "报告编号"

    doc = Document()
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    sec.top_margin = Cm(2.4)
    sec.bottom_margin = Cm(2.2)
    sec.left_margin = Cm(2.6)
    sec.right_margin = Cm(2.4)

    # 正文默认样式
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(4)

    # ── 封面 ──
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(90)
    r = p.add_run("上海靖达国际商务会展旅行有限公司")
    set_font(r, 13, False, GRAY, east="微软雅黑")

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(6)
    r = p.add_run(head_title)
    set_font(r, 30, True, NAVY, east="微软雅黑")

    p = doc.add_paragraph()
    r = p.add_run(kind_spaced)
    set_font(r, 26, True, NAVY, east="微软雅黑")

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    r = p.add_run(subtitle)
    set_font(r, 10.5, False, GRAY, east="微软雅黑")

    doc.add_paragraph().paragraph_format.space_before = Pt(40)

    meta = [
        [doc_label, doc_code, "版本", f"v{doc_ver}"],
        ["编制日期", doc_date, "文档性质", "合成演练资料"],
        ["确认状态", "待确认（开放口径未书面确认）", "适用范围", "一期试点"],
    ]
    t = doc.add_table(rows=0, cols=4)
    t.style = "Table Grid"
    cell_margins(t, 60, 60, 110, 110)
    for row in meta:
        cells = t.add_row().cells
        for ci, val in enumerate(row):
            cells[ci].text = ""
            pp = cells[ci].paragraphs[0]
            add_rich(pp, val, size=9, bold_all=(ci % 2 == 0))
            if ci % 2 == 0:
                shade(cells[ci], HEAD_FILL)

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(16)
    r = p.add_run("本报告为合成训练资料，不代表真实授权、业务执行或客户验收。所有数字以客户书面确认为准。")
    set_font(r, 8.5, False, GRAY, east="楷体")

    doc.add_page_break()

    # ── 文档控制（正文之前的元信息块，单独成页首） ──
    head, body, seen_h2 = [], [], False
    for kind, payload in blocks:
        if kind == "h2":
            seen_h2 = True
        (body if seen_h2 else head).append((kind, payload))

    p = doc.add_paragraph()
    r = p.add_run("文档控制")
    set_font(r, 13, True, NAVY, east="微软雅黑")
    p.paragraph_format.space_after = Pt(8)
    for kind, payload in head:
        if kind == "table":
            add_table(doc, payload)
        elif kind == "p":
            pp = doc.add_paragraph()
            add_rich(pp, payload, size=10)

    doc.add_page_break()

    # ── 目录（手工两列：章节 + 内容摘要） ──
    p = doc.add_paragraph()
    r = p.add_run("目　录")
    set_font(r, 18, True, NAVY, east="微软雅黑")
    p.paragraph_format.space_after = Pt(10)

    # 目录由正文一级标题自动生成（章节号 + 标题）
    toc = []
    for kind_b, payload_b in body:
        if kind_b != "h2":
            continue
        m = re.match(r"^([0-9]+)\.?\s*(.*)$", payload_b)
        if m:
            toc.append((m.group(1), m.group(2)))
        else:
            toc.append(("附", payload_b.replace("附录 ", "")))
    t = doc.add_table(rows=0, cols=2)
    t.style = "Table Grid"
    cell_margins(t, 40, 40, 90, 90)
    for num, title in toc:
        cells = t.add_row().cells
        cells[0].text = ""
        cells[0].width = Cm(1.6)
        pp = cells[0].paragraphs[0]
        pp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_rich(pp, num, size=9, bold_all=True)
        shade(cells[0], HEAD_FILL)
        cells[1].text = ""
        add_rich(cells[1].paragraphs[0], title, size=9)

    doc.add_page_break()

    # ── 正文 ──
    first_h1 = True
    for kind, payload in body:
        if kind in ("h1", "h2", "h3", "h4"):
            level = int(kind[1])
            if level == 1:                             # 文档大标题（已在封面呈现）
                continue
            text = payload
            if level == 2:
                if not first_h1:
                    doc.add_page_break()               # 每个一级章节另起页
                first_h1 = False
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(2)
                p.paragraph_format.space_after = Pt(8)
                p.paragraph_format.keep_with_next = True
                r = p.add_run(text)
                set_font(r, 17, True, NAVY, east="微软雅黑")
                # 章节标题下加细横线
                pPr = p._p.get_or_add_pPr()
                pbdr = OxmlElement("w:pBdr")
                bottom = OxmlElement("w:bottom")
                bottom.set(qn("w:val"), "single")
                bottom.set(qn("w:sz"), "8")
                bottom.set(qn("w:space"), "4")
                bottom.set(qn("w:color"), "1F3A5F")
                pbdr.append(bottom)
                pPr.append(pbdr)
            elif level == 3:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(10)
                p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.keep_with_next = True
                r = p.add_run(text)
                set_font(r, 12.5, True, RGBColor(0x1B, 0x4F, 0x8A), east="微软雅黑")
            else:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(6)
                r = p.add_run(text)
                set_font(r, 11, True, NAVY, east="微软雅黑")
        elif kind == "table":
            add_table(doc, payload)
        elif kind == "bullet":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.75)
            p.paragraph_format.space_after = Pt(3)
            r = p.add_run("· ")
            set_font(r, 10.5, True, NAVY)
            add_rich(p, payload)
        elif kind == "num":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.75)
            p.paragraph_format.space_after = Pt(3)
            add_rich(p, payload)
        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(6)
            pPr = p._p.get_or_add_pPr()
            pbdr = OxmlElement("w:pBdr")
            left = OxmlElement("w:left")
            left.set(qn("w:val"), "single")
            left.set(qn("w:sz"), "18")
            left.set(qn("w:space"), "8")
            left.set(qn("w:color"), "9DB2CC")
            pbdr.append(left)
            pPr.append(pbdr)
            add_rich(p, payload, size=9.5, gray=True)
        elif kind == "hr":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            pPr = p._p.get_or_add_pPr()
            pbdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "4")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "D5DBE3")
            pbdr.append(bottom)
            pPr.append(pbdr)
        else:
            p = doc.add_paragraph()
            p.paragraph_format.first_line_indent = Pt(21)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            add_rich(p, payload)

    # ── 页脚：页码 ──
    footer = sec.footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = fp.add_run(f"{title}　·　第 ")
    set_font(r, 9, False, GRAY)
    add_field(fp, "PAGE")
    r = fp.add_run(" 页 / 共 ")
    set_font(r, 9, False, GRAY)
    add_field(fp, "NUMPAGES")
    r = fp.add_run(" 页")
    set_font(r, 9, False, GRAY)

    doc.save(out_path)
    print("生成:", out_path)


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2],
          sys.argv[3] if len(sys.argv) > 3 else "",
          sys.argv[4] if len(sys.argv) > 4 else "")
