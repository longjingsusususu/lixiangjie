# -*- coding: utf-8 -*-
"""
build_report_html.py —— 把需求报告 Markdown 渲染成自包含单页 HTML

用途：不装 Word 也能在浏览器/预览面板里直接看，Ctrl+P 可另存 PDF。
用法：python scripts/build_report_html.py <input.md> <output.html>
"""

import html
import re
import sys

CSS = """
:root{--navy:#1f3a5f;--ink:#20262e;--gray:#5b6676;--line:#dfe5ee;--bg:#f4f6f9;--card:#fff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.85 "Microsoft YaHei","PingFang SC",sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:28px 22px 60px}
.cover{background:var(--navy);color:#fff;border-radius:14px;padding:38px 34px;margin-bottom:26px}
.cover .org{font-size:14px;opacity:.78;letter-spacing:1px}
.cover h1{font-size:32px;margin:14px 0 2px;font-weight:700;letter-spacing:1px}
.cover .sub{font-size:15px;opacity:.85;margin-top:8px}
.cover .meta{margin-top:22px;display:flex;flex-wrap:wrap;gap:8px 26px;font-size:13px;opacity:.85}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px 28px;margin:16px 0}
h2{font-size:21px;color:var(--navy);margin:34px 0 12px;padding-bottom:8px;
   border-bottom:2px solid var(--navy)}
h3{font-size:16px;color:#1b4f8a;margin:22px 0 8px}
h4{font-size:14px;color:var(--navy);margin:16px 0 6px}
p{margin:8px 0;text-align:justify}
ul,ol{margin:8px 0 8px 22px;padding:0}
li{margin:4px 0}
table{width:100%;border-collapse:collapse;margin:12px 0 16px;font-size:13px;
      table-layout:fixed;word-break:break-word}
th,td{border:1px solid var(--line);padding:7px 9px;vertical-align:top;line-height:1.65}
th{background:#e8eef6;color:var(--navy);font-weight:700;text-align:left}
tbody tr:nth-child(even){background:#f7f9fc}
blockquote{margin:12px 0;padding:10px 16px;border-left:4px solid #9db2cc;
           background:#f1f5fa;color:var(--gray);font-size:13.5px;border-radius:0 8px 8px 0}
hr{border:0;border-top:1px solid var(--line);margin:20px 0}
code{background:#f3f4f6;color:#8b2e2e;padding:1px 5px;border-radius:4px;font-size:13px}
.tag{display:inline-block;background:#e8eef6;color:var(--navy);border-radius:999px;
     padding:1px 9px;font-size:12px;margin-right:6px}
@media print{
  body{background:#fff} .wrap{max-width:none;padding:0}
  .card{border:0;padding:0;margin:0} .cover{border-radius:0}
  h2{page-break-after:avoid} table{page-break-inside:auto} tr{page-break-inside:avoid}
}
"""


def inline(text: str) -> str:
    """行内标记：**粗体** → <strong>，`code` → <code>，转义其余。"""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    return out


def render(md: str) -> str:
    lines = md.splitlines()
    out, i = [], 0
    in_list = None

    def close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            close_list()
            i += 1
            continue
        if line.startswith("|"):
            close_list()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>")
            for r in body:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue
        if line.startswith("#"):
            close_list()
            level = len(line) - len(line.lstrip("#"))
            txt = line[level:].strip()
            if level == 1:
                out.append(f"<h1>{inline(txt)}</h1>")
            elif level == 2:
                out.append(f"<h2>{inline(txt)}</h2>")
            elif level == 3:
                out.append(f"<h3>{inline(txt)}</h3>")
            else:
                out.append(f"<h4>{inline(txt)}</h4>")
        elif line.startswith(">"):
            close_list()
            out.append(f"<blockquote>{inline(line.lstrip('> ').strip())}</blockquote>")
        elif re.match(r"^[-*]\s+", line):
            if in_list != "ul":
                close_list()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{inline(re.sub(r'^[-*]\\s+', '', line))}</li>")
        elif re.match(r"^\d+\.\s+", line):
            if in_list != "ol":
                close_list()
                out.append("<ol>")
                in_list = "ol"
            out.append(f"<li>{inline(line)}</li>")
        elif re.fullmatch(r"-{3,}", line.strip()):
            close_list()
            out.append("<hr>")
        else:
            close_list()
            out.append(f"<p>{inline(line.strip())}</p>")
        i += 1
    close_list()
    return "\n".join(out)


def build(md_path, out_path, subtitle=""):
    md = open(md_path, encoding="utf-8").read()
    # 封面取首行标题（支持「主标题 · 文档类型」），其余进正文
    first, rest = md.split("\n", 1)
    title = first.lstrip("# ").strip()
    head_title, _, kind = title.partition(" · ")
    kind = kind or "需求报告"
    body = render(rest)
    import re as _re
    code = (_re.search(r"\|\s*(?:资料|报告)编号\s*\|\s*([^|]+)\|", md) or [None, "编号待补"])[1].strip()
    ver = (_re.search(r"\|\s*版本[^|]*\|[^|]*?v([\d.]+)", md) or [None, "1.0"])[1].strip()
    date = (_re.search(r"(20\d{2}-\d{2}-\d{2})", md) or [None, ""])[1].strip()
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<div class="cover">
  <div class="org">上海靖达国际商务会展旅行有限公司 · 合成训练资料</div>
  <h1>{html.escape(head_title)}</h1>
  <h1 style="font-size:26px;margin-top:0">{html.escape(kind)}</h1>
  <div class="sub">{html.escape(subtitle)}</div>
  <div class="meta">
    <span>{html.escape(code)}</span><span>版本 v{html.escape(ver)}</span>
    <span>{html.escape(date)}</span><span>状态：待确认</span>
  </div>
</div>
<div class="card">{body}</div>
</div></body></html>"""
    open(out_path, "w", encoding="utf-8").write(doc)
    print("生成:", out_path)


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
