#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""话题棱镜 · 文档导出。

把 selected.json 导出成便于在电脑上翻阅的正式文档：
  - docx：只用 Python 标准库手写 OOXML，零第三方依赖（含封面、目录域、页脚页码）
  - pdf ：可选依赖 reportlab（中文用内置 CID 字体 STSong-Light；目录带真实页码）
  - md  ：直接复用核心渲染

文档结构：
  封面 → 目录 → 一、检索口径 → 二、分类矩阵（含舆论倾向）→ 三、条目明细
  → 附录 A 信源清单（全链接）→ 附录 B 热度未知 → 附录 C 落选条目 → 四、方法学与限制

用法：
  python3 scripts/export_doc.py --in out/selected.json --format docx --out out/report.docx
  python3 scripts/export_doc.py --in out/selected.json --format pdf  --out out/report.pdf
"""

import argparse
import json
import os
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

LAST_TOC = []  # 最近一次 PDF 生成的目录条目，供自检使用

DIMENSIONS = ["主题域", "内容性质", "信息确定性", "影响面", "立场倾向", "信源权威度", "时效形态"]
STANCE_VALUES = ["正面", "中性", "负面", "质疑"]
SAFE_URL_SCHEMES = ("http://", "https://")


# ---------------------------------------------------------------- 数据整理
def safe_url(u):
    if not u:
        return ""
    raw = str(u).strip()
    return raw if raw.lower().startswith(SAFE_URL_SCHEMES) else ""


def clean(v):
    if v is None:
        return ""
    return " ".join(str(v).split())


def stance_summary(items):
    counts = {}
    for e in items:
        v = (e.get("dimensions") or {}).get("立场倾向")
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return ""
    n = sum(counts.values())
    order = [v for v in STANCE_VALUES if v in counts] + [v for v in counts if v not in STANCE_VALUES]
    return " / ".join("%s %d 条（%.0f%%）" % (v, counts[v], 100.0 * counts[v] / n) for v in order)


def build_outline(payload, topic=""):
    """把 payload 摊平成文档大纲：dict 列表，docx 与 pdf 共用。"""
    st = payload.get("stats") or {}
    pr = payload.get("probe") or {}
    sel = payload.get("selected") or []
    app = payload.get("appendix") or []
    rejected = payload.get("rejected") or []
    topic = topic or payload.get("topic") or "信息汇总"

    out = []
    add = out.append

    # 封面
    add({"kind": "cover", "title": "话题棱镜 · %s" % clean(topic),
         "lines": [
             "生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
             "候选 %d 条 ｜ 去重后 %d 个事件 ｜ 纳入 %d 条（正文 %d + 附录 %d）"
             % (st.get("candidates", 0), st.get("clusters", 0),
                st.get("selected", len(sel)) + st.get("appendix", len(app)),
                st.get("selected", len(sel)), st.get("appendix", len(app))),
             "判定模式：%s" % pr.get("mode", "—"),
             "由 话题棱镜 TopicPrism v1.2.5 生成",
         ]})
    add({"kind": "pagebreak"})
    add({"kind": "toc"})
    add({"kind": "pagebreak"})

    # 一、口径
    add({"kind": "h1", "text": "一、检索与筛选口径"})
    add({"kind": "p", "text": "候选池：%d 条（原始检索结果）" % st.get("candidates", 0)})
    add({"kind": "p", "text": "热度放行：%d 条 ｜ 事件去重后：%d 个唯一事件"
         % (st.get("passed_gate", 0), st.get("clusters", 0))})
    add({"kind": "p", "text": "最终纳入：正文 %d 条（参与排名）+ 附录 %d 条（热度未知，不参与排名）"
         % (st.get("selected", len(sel)), st.get("appendix", len(app)))})
    if pr:
        add({"kind": "p", "text": "判定模式：%s —— %s" % (pr.get("mode", ""), pr.get("explain", ""))})
        if pr.get("reliable") is False:
            add({"kind": "note", "text": "探水提醒：%s" % pr.get("sample_note", "样本量偏少")})

    # 二、分类矩阵
    if sel:
        add({"kind": "h1", "text": "二、分类矩阵"})
        for dim in DIMENSIONS:
            counts = {}
            for e in sel:
                v = (e.get("dimensions") or {}).get(dim) or "未标注"
                counts[v] = counts.get(v, 0) + 1
            items = sorted(counts.items(), key=lambda kv: -kv[1])
            add({"kind": "p", "text": "%s：%s" % (dim, " / ".join("%s %d 条" % (k, v) for k, v in items))})
        ss = stance_summary(sel)
        if ss:
            add({"kind": "note", "text": "舆论倾向小结 —— " + ss})

    # 三、时间线
    add({"kind": "h1", "text": "三、时间线（按日期倒序，越新越靠前）"})
    dated = []
    undated = []
    for e in list(sel) + list(app):
        d = str(e.get("published_at") or "")[:10]
        (dated if d else undated).append((d, e))
    dated.sort(key=lambda x: (x[0], x[1].get("score") or 0), reverse=True)
    buckets = {}
    for d, e in dated:
        buckets.setdefault(d, []).append(e)
    for d in sorted(buckets, reverse=True):
        add({"kind": "h2", "text": "%s（%d 条）" % (d, len(buckets[d]))})
        for e in buckets[d]:
            dims = e.get("dimensions") or {}
            bits = [e.get("cluster_id", ""), clean(e.get("title"))]
            if e.get("is_new"):
                bits.insert(1, "「新」")
            meta = []
            if e.get("platforms"):
                meta.append("、".join(e["platforms"]))
            if dims.get("立场倾向"):
                meta.append(clean(dims["立场倾向"]))
            meta.append("热度 %s" % (("%.1f" % e["score"]) if e.get("score") is not None else "未知"))
            add({"kind": "p", "text": " ".join(bits) + ((" ｜ " + " ｜ ".join(meta)) if meta else "")})
    if undated:
        add({"kind": "h2", "text": "日期未知（%d 条）" % len(undated)})
        for _d, e in undated:
            add({"kind": "p", "text": "%s %s" % (e.get("cluster_id", ""), clean(e.get("title")))})

    # 四、条目明细
    add({"kind": "h1", "text": "四、条目明细（按热度降序）"})
    groups = {}
    for e in sel:
        groups.setdefault((e.get("dimensions") or {}).get("主题域") or "未归类", []).append(e)
    for gname, items in groups.items():
        add({"kind": "h2", "text": "%s（%d 条）" % (clean(gname), len(items))})
        for e in items:
            dims = e.get("dimensions") or {}
            add({"kind": "h3", "text": "%s %s" % (e.get("cluster_id", ""), clean(e.get("title")))})
            meta = ["热度分 %.1f" % (e.get("score") or 0)]
            if e.get("platforms"):
                meta.append("平台：" + "、".join(e["platforms"]))
            if e.get("author"):
                meta.append("作者：" + clean(e.get("author")))
            if e.get("duplicate_count", 1) > 1:
                meta.append("合并 %d 条重复" % e["duplicate_count"])
            add({"kind": "p", "text": " ｜ ".join(meta)})
            if dims:
                add({"kind": "p", "text": "分类：" + "；".join(
                    "%s=%s" % (d, clean(dims.get(d))) for d in DIMENSIONS if dims.get(d))})
            if e.get("excerpt"):
                add({"kind": "p", "text": "摘要：" + clean(e.get("excerpt"))})
            u = safe_url(e.get("url"))
            if u:
                add({"kind": "link", "text": u, "url": u})

    # 附录 A
    add({"kind": "pagebreak"})
    add({"kind": "h1", "text": "附录 A、信源清单（全部链接）"})
    add({"kind": "note", "text": "按条目汇总每个信源的平台与原始地址。同一事件在多平台出现时逐条列出，便于逐一定位核对。"})
    idx = 0
    for e in list(sel) + list(app):
        sources = e.get("sources") or ([{
            "platform": (e.get("platforms") or [""])[0], "url": e.get("url"), "metric": None}])
        add({"kind": "h3", "text": "%s %s" % (e.get("cluster_id", ""), clean(e.get("title")))})
        for s in sources:
            idx += 1
            u = safe_url(s.get("url"))
            plat = clean(s.get("platform")) or "未标注平台"
            metric = s.get("metric")
            line = "[%d] %s" % (idx, plat)
            if metric not in (None, ""):
                line += " ｜ 数值 %s" % metric
            if u:
                add({"kind": "link", "text": line, "url": u})
            else:
                add({"kind": "p", "text": line + " ｜ 链接缺失或非 http(s)"})

    # 附录 B
    if app:
        add({"kind": "pagebreak"})
        add({"kind": "h1", "text": "附录 B、热度未知条目（不参与排名）"})
        add({"kind": "note", "text": "平台未公开阅读/播放/互动数据，无法给出热度分；价值在内容本身，采纳前请自行判断信源。"})
        for e in app:
            dims = e.get("dimensions") or {}
            add({"kind": "h3", "text": "%s %s" % (e.get("cluster_id", ""), clean(e.get("title")))})
            add({"kind": "p", "text": "平台：%s ｜ 作者：%s"
                 % ("、".join(e.get("platforms") or []), clean(e.get("author")))})
            if dims:
                add({"kind": "p", "text": "分类：" + "；".join(
                    "%s=%s" % (d, clean(dims.get(d))) for d in DIMENSIONS if dims.get(d))})
            if e.get("excerpt"):
                add({"kind": "p", "text": "摘要：" + clean(e.get("excerpt"))})
            u = safe_url(e.get("url"))
            if u:
                add({"kind": "link", "text": u, "url": u})

    # 附录 C
    if rejected:
        add({"kind": "pagebreak"})
        add({"kind": "h1", "text": "附录 C、未纳入条目与原因"})
        add({"kind": "note", "text": "把落选理由摊开写，方便判断筛选口径是否过严。以下条目不计入正文。"})
        for e in rejected[:80]:
            add({"kind": "p", "text": "%s ｜ %s ｜ %s" % (
                clean(e.get("platform") or ""), clean(e.get("title")), clean(e.get("reason")))})
        if len(rejected) > 80:
            add({"kind": "note", "text": "另有 %d 条未列出。" % (len(rejected) - 80)})

    # 五、与上次对比
    diff = payload.get("diff")
    if diff:
        add({"kind": "pagebreak"})
        add({"kind": "h1", "text": "五、与上次对比"})
        add({"kind": "p", "text": "基线：%s（上次共 %d 条）" % (clean(diff.get("baseline_at")) or "上次结果",
                                                      diff.get("baseline_total", 0))})
        add({"kind": "p", "text": "本次新增 %d 条 / 消失 %d 条 / 延续 %d 条"
             % (len(diff.get("added") or []), len(diff.get("removed") or []), diff.get("kept", 0))})
        if diff.get("added"):
            add({"kind": "h2", "text": "新增（%d 条）" % len(diff["added"])})
            for a in diff["added"]:
                add({"kind": "p", "text": "%s ｜ %s ｜ %s ｜ 热度 %s" % (
                    a.get("cluster_id", ""), clean(a.get("title")),
                    clean(a.get("published_at")) or "日期未知",
                    a.get("score") if a.get("score") is not None else "未知")})
        if diff.get("removed"):
            add({"kind": "h2", "text": "消失（%d 条）" % len(diff["removed"])})
            for r in diff["removed"]:
                add({"kind": "p", "text": "%s ｜ 上次热度 %s" % (
                    clean(r.get("title")), r.get("score") if r.get("score") is not None else "未知")})

    # 六、方法学
    add({"kind": "pagebreak"})
    add({"kind": "h1", "text": "六、方法学与限制"})
    for line in [
        "热度门槛：绝对地板 100；大流量平台 1000、中等平台 300、垂直/小众源 100。",
        "热度分 = 50% 本档内归一（含同池分位）+ 22% 跨平台分布 + 18% 互动量 + 10% 时间新鲜度。",
        "分数表示本话题候选池内的相对位置，不是全网浏览量排名；各平台指标口径不同，不可当绝对值横向比较。",
        "同一事件的多份拷贝已合并；合并后跨平台分布计入加分。",
        "「立场倾向」按结论落点判定，不等于事实判断；「信息确定性」未达「已证实」的条目，采纳前需自行复核。",
        "时间线按发布日倒序排列；日期取自信源标注，可能与实际发布时间存在偏差。",
        "源链接指向第三方站点，内容与安全性由该站点负责；本工具只转述摘要，不代理访问。",
    ]:
        add({"kind": "p", "text": line})
    return out


# ---------------------------------------------------------------- DOCX
DOCX_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

DOCX_SETTINGS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:updateFields w:val="true"/>
</w:settings>"""

DOCX_FOOTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:p><w:pPr><w:jc w:val="center"/></w:pPr>
<w:r><w:rPr><w:sz w:val="18"/></w:rPr><w:t xml:space="preserve">话题棱镜 ｜ 第 </w:t></w:r>
<w:r><w:fldChar w:fldCharType="begin"/></w:r>
<w:r><w:instrText xml:space="preserve">PAGE</w:instrText></w:r>
<w:r><w:fldChar w:fldCharType="separate"/></w:r>
<w:r><w:t>1</w:t></w:r>
<w:r><w:fldChar w:fldCharType="end"/></w:r>
<w:r><w:rPr><w:sz w:val="18"/></w:rPr><w:t xml:space="preserve"> 页</w:t></w:r>
</w:p></w:ftr>"""

DOCX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:eastAsia="SimSun" w:hAnsi="Calibri"/>
<w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr><w:rPr><w:b/><w:sz w:val="44"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="360" w:after="120"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
<w:pPr><w:outlineLvl w:val="1"/><w:spacing w:before="240" w:after="80"/></w:pPr><w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>
<w:pPr><w:outlineLvl w:val="2"/><w:spacing w:before="180" w:after="60"/></w:pPr><w:rPr><w:b/><w:sz w:val="23"/></w:rPr></w:style>
</w:styles>"""

DOCX_CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>%s</dc:title><dc:creator>话题棱镜 TopicPrism</dc:creator>
</cp:coreProperties>"""

DOCX_APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>TopicPrism</Application></Properties>"""


def _p(text, style=None, bold=False):
    ppr = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return '<w:p>%s<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (ppr, rpr, escape(text or ""))


def _link(text, url, rid):
    return ('<w:p><w:hyperlink r:id="%s"><w:r><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr>'
            '<w:t xml:space="preserve">%s</w:t></w:r></w:hyperlink></w:p>' % (rid, escape(text or url)))


def _toc_field():
    """目录域：Word/WPS 打开时按 updateFields 自动生成，也可右键“更新域”。"""
    return ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText xml:space="preserve">TOC \\o "1-3" \\h \\z \\u</w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            '<w:r><w:t>目录将在打开文档时自动生成；若未显示，右键此处选择“更新域”。</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')


def to_docx(outline, path, title=""):
    body, rels = [], []
    n = 0
    for item in outline:
        k = item["kind"]
        if k == "cover":
            body.append(_p(item["title"], "Title"))
            for ln in item.get("lines") or []:
                body.append(_p(ln))
        elif k == "toc":
            body.append(_p("目录", "Heading1"))
            body.append(_toc_field())
        elif k == "meta":
            body.append(_p(item["text"]))
        elif k == "h1":
            body.append(_p(item["text"], "Heading1"))
        elif k == "h2":
            body.append(_p(item["text"], "Heading2"))
        elif k == "h3":
            body.append(_p(item["text"], "Heading3", bold=True))
        elif k == "note":
            body.append(_p("※ " + item["text"]))
        elif k == "pagebreak":
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        elif k == "link":
            n += 1
            rid = "rIdL%d" % n
            rels.append('<Relationship Id="%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="%s" TargetMode="External"/>'
                        % (rid, escape(item["url"], {'"': "&quot;"})))
            body.append(_link(item["text"], item["url"], rid))
        else:
            body.append(_p(item.get("text", "")))

    sect = ('<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter1"/>'
            '<w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
            'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
                + "".join(body) + sect + '</w:body></w:document>')

    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                '<Relationship Id="rIdSettings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
                '<Relationship Id="rIdFooter1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
                + "".join(rels) + "</Relationships>")

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", DOCX_CT)
        z.writestr("_rels/.rels", DOCX_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", DOCX_STYLES)
        z.writestr("word/settings.xml", DOCX_SETTINGS)
        z.writestr("word/footer1.xml", DOCX_FOOTER)
        z.writestr("docProps/core.xml", DOCX_CORE % escape(title or "话题棱镜报告"))
        z.writestr("docProps/app.xml", DOCX_APP)
    return path


# ---------------------------------------------------------------- PDF
def to_pdf(outline, path, title=""):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer)
        from reportlab.platypus.tableofcontents import TableOfContents
    except ImportError:
        raise SystemExit("缺少可选依赖 reportlab：pip install reportlab（或改用 --format docx）")

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    base = getSampleStyleSheet()
    S = {
        "title": ParagraphStyle("PrismTitle", parent=base["Title"], fontName="STSong-Light",
                                fontSize=24, leading=34, spaceAfter=18),
        "coverline": ParagraphStyle("PrismCover", parent=base["BodyText"], fontName="STSong-Light",
                                    fontSize=10.5, leading=18, textColor=colors.HexColor("#444444")),
        "h1": ParagraphStyle("PrismH1", parent=base["Heading1"], fontName="STSong-Light",
                             fontSize=15, leading=22, spaceBefore=14, spaceAfter=6),
        "h2": ParagraphStyle("PrismH2", parent=base["Heading2"], fontName="STSong-Light",
                             fontSize=12.5, leading=19, spaceBefore=10, spaceAfter=4),
        "h3": ParagraphStyle("PrismH3", parent=base["Heading3"], fontName="STSong-Light",
                             fontSize=11, leading=17),
        "p": ParagraphStyle("PrismP", parent=base["BodyText"], fontName="STSong-Light",
                            fontSize=9.5, leading=15),
        "toch": ParagraphStyle("PrismTOCH", parent=base["Heading1"], fontName="STSong-Light",
                               fontSize=15, leading=22, spaceBefore=14, spaceAfter=6),
        "note": ParagraphStyle("PrismNote", parent=base["BodyText"], fontName="STSong-Light",
                               fontSize=9, leading=14, textColor=colors.HexColor("#666666")),
        "link": ParagraphStyle("PrismLink", parent=base["BodyText"], fontName="STSong-Light",
                               fontSize=8.5, leading=13, textColor=colors.HexColor("#0563C1")),
    }

    class PrismDoc(SimpleDocTemplate):
        """带目录通知的模板：一级/二级标题进目录，multiBuild 回填真实页码。"""

        def afterFlowable(self, flowable):
            if isinstance(flowable, Paragraph):
                style = flowable.style.name
                if style in ("PrismH1", "PrismH2"):
                    level = 0 if style == "PrismH1" else 1
                    self.notify("TOCEntry", (level, flowable.getPlainText(), self.page))

    flow = []
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("TOC1", fontName="STSong-Light", fontSize=11, leading=20, leftIndent=6),
        ParagraphStyle("TOC2", fontName="STSong-Light", fontSize=9.5, leading=17, leftIndent=22,
                       textColor=colors.HexColor("#555555")),
    ]
    toc.dotsMinLevel = 0

    for item in outline:
        k = item["kind"]
        if k == "cover":
            flow.append(Spacer(1, 120 * mm / 3))
            flow.append(Paragraph(escape(item["title"]), S["title"]))
            for ln in item.get("lines") or []:
                flow.append(Paragraph(escape(ln), S["coverline"]))
        elif k == "toc":
            flow.append(Paragraph("目录", S["toch"]))
            flow.append(toc)
        elif k == "pagebreak":
            flow.append(PageBreak())
        elif k == "link":
            flow.append(Paragraph(escape(item["text"]), S["link"]))
        elif k in S:
            flow.append(Paragraph(escape(item.get("text", "")), S[k]))
        else:
            flow.append(Paragraph(escape(item.get("text", "")), S["p"]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawCentredString(A4[0] / 2.0, 12 * mm, "话题棱镜 ｜ 第 %d 页" % canvas.getPageNumber())
        canvas.restoreState()

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    doc = PrismDoc(path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                   topMargin=18 * mm, bottomMargin=18 * mm, title=title or "话题棱镜报告")
    doc.multiBuild(flow, onFirstPage=footer, onLaterPages=footer)
    global LAST_TOC
    LAST_TOC = list(getattr(toc, "_lastEntries", []) or [])
    return path


# ---------------------------------------------------------------- 自检
def selftest():
    payload = {
        "topic": "自检示例",
        "stats": {"candidates": 2, "passed_gate": 1, "clusters": 1, "selected": 1, "appendix": 0},
        "probe": {"mode": "absolute", "explain": "自检", "reliable": True},
        "selected": [{
            "cluster_id": "E001", "title": "示例事件 <非法标签> [链接](javascript:1)",
            "author": "某人", "platforms": ["weibo"], "score": 50.0, "excerpt": "摘要 A | B",
            "url": "https://example.com/a", "duplicate_count": 2,
            "sources": [{"platform": "weibo", "url": "https://example.com/a", "metric": 1000},
                        {"platform": "zhihu", "url": "javascript:alert(1)", "metric": None}],
            "dimensions": {"主题域": "科技 / 产品", "内容性质": "事实", "信息确定性": "已证实",
                           "影响面": "行业", "立场倾向": "正面",
                           "信源权威度": "权威媒体 / 专业机构", "时效形态": "突发"},
        }],
        "appendix": [],
        "rejected": [{"title": "落选例子", "platform": "weibo", "reason": "低于绝对地板 100",
                      "url": "https://example.com/r"}],
        "diff": {"baseline_at": "2026-10-19 10:00", "baseline_total": 2, "kept": 0,
                 "added": [{"cluster_id": "E001", "title": "新增事件", "published_at": "2026-10-18", "score": 50}],
                 "removed": [{"title": "消失事件", "score": 30}]},
    }
    checks = []
    outline = build_outline(payload, "自检示例")
    checks.append(("大纲生成", len(outline) > 20))
    checks.append(("含封面", any(i["kind"] == "cover" for i in outline)))
    checks.append(("含目录占位", any(i["kind"] == "toc" for i in outline)))
    checks.append(("含舆论倾向小结", any("舆论倾向小结" in (i.get("text") or "") for i in outline)))
    checks.append(("含时间线章节", any((i.get("text") or "").startswith("三、时间线") for i in outline)))
    checks.append(("含增量对比章节", any(i.get("text") == "五、与上次对比" for i in outline)))
    checks.append(("含信源清单章节", any(i.get("text") == "附录 A、信源清单（全部链接）" for i in outline)))
    checks.append(("含落选原因章节", any(i.get("text") == "附录 C、未纳入条目与原因" for i in outline)))
    checks.append(("非 http(s) 链接被过滤",
                   not any(i.get("url", "").startswith("javascript:") for i in outline if i["kind"] == "link")))

    import tempfile
    from xml.etree import ElementTree as ET
    tmp = tempfile.mkdtemp()
    dp = os.path.join(tmp, "t.docx")
    to_docx(outline, dp, "自检")
    z = zipfile.ZipFile(dp)
    ok_xml = True
    for n in z.namelist():
        if n.endswith((".xml", ".rels")):
            try:
                ET.fromstring(z.read(n))
            except ET.ParseError:
                ok_xml = False
    names = z.namelist()
    checks.append(("docx 结构合法", ok_xml and "word/document.xml" in names))
    doc = z.read("word/document.xml").decode()
    checks.append(("含目录域", "TOC \\o" in doc))
    footer_name = next((n for n in names if n.endswith("footer1.xml")), "")
    checks.append(("页脚有页码域", bool(footer_name) and "PAGE" in z.read(footer_name).decode()))
    checks.append(("外部文本已转义", "&lt;非法标签&gt;" in doc and "<非法标签>" not in doc))
    checks.append(("超链接以内嵌关系写入", "<w:hyperlink" in doc))
    checks.append(("导出含信源权威度维度", "信源权威度" in doc))

    pdf_ok = None
    try:
        pp = os.path.join(tmp, "t.pdf")
        to_pdf(outline, pp, "自检")
        raw = open(pp, "rb").read()
        pdf_ok = raw[:5] == b"%PDF-" and raw.count(b"/Type /Page") >= 2
    except SystemExit:
        pdf_ok = None
    checks.append(("pdf 生成且多页（未装 reportlab 则跳过）", pdf_ok is not False))
    if pdf_ok:
        titles = [e[1] for e in LAST_TOC]
        checks.append(("pdf 目录带条目与页码",
                       len(titles) >= 3 and all(isinstance(e[2], int) and e[2] >= 1 for e in LAST_TOC)))
        checks.append(("pdf 目录收录时间线章节", any("时间线" in x for x in titles)))
        checks.append(("pdf 目录不含自身标题", all(x != "目录" for x in titles)))

    for name, v in checks:
        print(("  PASS  " if v else "  FAIL  ") + name)
    return 0 if all(v for _, v in checks) else 1


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="话题棱镜 · 文档导出")
    ap.add_argument("--in", dest="src", help="selected.json")
    ap.add_argument("--format", choices=["docx", "pdf", "md"], default="docx")
    ap.add_argument("--out")
    ap.add_argument("--topic", default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.src or not args.out:
        ap.error("--in 与 --out 必填")

    with open(args.src, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    topic = args.topic or payload.get("topic") or "信息汇总"
    outline = build_outline(payload, topic)

    if args.format == "docx":
        to_docx(outline, args.out, topic)
    elif args.format == "pdf":
        to_pdf(outline, args.out, topic)
    else:
        raise SystemExit("md 格式请直接用 prism.py report 渲染")
    print("已导出 %s（%d 个段落块）" % (args.out, len(outline)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
