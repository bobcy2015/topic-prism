#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""话题棱镜 · 文档导出。

把 selected.json 导出成便于在电脑上翻阅的正式文档：
  - docx：只用 Python 标准库手写 OOXML，零第三方依赖（含封面、目录域、页脚页码）
  - pdf ：可选依赖 reportlab（中文用内置 CID 字体 STSong-Light；目录带真实页码）
  - md  ：直接复用核心渲染

文档结构：
  封面 → 目录 → 一、检索口径 → 二、分类矩阵（含舆论倾向）→ 三、时间线
  → 四、条目明细（标题 → 摘要 → 信源链接）→ 附录 A 信源清单（含摘要与可见链接）
  → 附录 B 热度未知 → 附录 C 落选条目 → 五、方法学与限制

用法：
  python3 scripts/export_doc.py --in out/selected.json --format docx --out out/report.docx
  python3 scripts/export_doc.py --in out/selected.json --format pdf  --out out/report.pdf
"""

import argparse
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.sax.saxutils import escape

LAST_TOC = []  # 最近一次 PDF 生成的目录条目，供自检使用

DIMENSIONS = ["主题域", "内容性质", "信息确定性", "影响面", "立场倾向", "信源权威度", "时效形态"]
STANCE_VALUES = ["正面", "中性", "负面", "质疑"]
SAFE_URL_SCHEMES = ("http://", "https://")
EXCERPT_LIMIT = 200      # 单条摘要上限（字），超出截断并加省略号
DIGEST_MIN_ITEMS = 3     # 「本期要点」至少几条


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


def clip_excerpt(text, limit=EXCERPT_LIMIT):
    """摘要裁剪：超过 limit 字截断并加省略号。

    脚本不生成摘要——摘要由执行方检索时写进候选池的 excerpt 字段。
    这里只负责长度控制，避免一条啰嗦的摘要把文档撑得难读。
    """
    t = clean(text)
    if not t:
        return ""
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


_CFG = None


def load_cfg():
    """读 references/platforms.json：平台显示名与质量旗标文案都从这里取。"""
    global _CFG
    if _CFG is None:
        try:
            cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "references", "platforms.json")
            with open(cfg_path, "r", encoding="utf-8") as fh:
                _CFG = json.load(fh) or {}
        except Exception:
            _CFG = {}
    return _CFG


def quality_text(entry):
    """来源质量的展示文案：分数 + 命中旗标的中文说明。"""
    parts = []
    q = entry.get("quality_norm")
    if isinstance(q, (int, float)):
        maxs = float((load_cfg().get("credibility") or {}).get("max_score", 5)) or 5.0
        parts.append("来源质量 %.0f/%.0f" % (q * maxs, maxs))
    known = (load_cfg().get("credibility") or {}).get("flags") or {}
    labels = [known.get(f, f).split("：")[0] for f in (entry.get("quality_flags") or [])]
    if labels:
        parts.append("⚠ " + "、".join(labels))
    return " ｜ ".join(parts)


_PLAT_LABELS = None


def platform_label(key):
    """平台 key -> 中文显示名，取自 references/platforms.json 的 aliases。

    report.md 与 docx/pdf 必须显示同一套名字：文档里出现 cls、qbitai 这种
    内部 key，读者不知道是谁。取不到中文名时原样返回 key。
    """
    global _PLAT_LABELS
    if _PLAT_LABELS is None:
        _PLAT_LABELS = {}
        try:
            cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "references", "platforms.json")
            with open(cfg_path, "r", encoding="utf-8") as fh:
                aliases = (json.load(fh) or {}).get("aliases") or {}
            for alias, k in aliases.items():
                if alias.startswith("_"):
                    continue          # 说明键不是别名
                if re.search(r"[一-鿿]", alias) and k not in _PLAT_LABELS:
                    _PLAT_LABELS[k] = alias
        except Exception:
            _PLAT_LABELS = {}
    return _PLAT_LABELS.get(key, key)


def platform_text(keys):
    """平台 key 列表 -> 中文名串（去重、保序）。"""
    seen, out = set(), []
    for k in keys or []:
        lab = platform_label(k)
        if lab not in seen:
            seen.add(lab)
            out.append(lab)
    return "、".join(out)


def fmt_metric(m):
    """热度数值显示：整数值不带 .0，None/空值返回空串。"""
    if m in (None, ""):
        return ""
    try:
        f = float(m)
    except (TypeError, ValueError):
        return str(m)
    return str(int(f)) if f.is_integer() else str(f)


_VERSION = None


def skill_version():
    """读当前技能版本，供封面等处显示。

    以前这里写死过 "v1.2.5"，结果技能升到 1.4.0 后封面还在印旧版本——
    用户报障时报的就是错的版本号。现在以 SKILL.md 的 frontmatter 为准，
    读不到再退到 plugin.json，都读不到时明确写 unknown，不猜。
    """
    global _VERSION
    if _VERSION is None:
        _VERSION = "unknown"
        here = os.path.dirname(os.path.abspath(__file__))
        for rel, pattern in (("../SKILL.md", r"^version:\s*(\S+)\s*$"),
                             ("../../../plugin.json", None)):
            path = os.path.join(here, rel)
            try:
                if pattern:
                    with open(path, "r", encoding="utf-8") as fh:
                        head = fh.read(4096)
                    m = re.search(pattern, head, re.M)
                    if m:
                        _VERSION = m.group(1).strip().strip('"').strip("'")
                        break
                else:
                    with open(path, "r", encoding="utf-8") as fh:
                        _VERSION = str((json.load(fh) or {}).get("version") or "unknown")
                    if _VERSION != "unknown":
                        break
            except (OSError, ValueError):
                continue
    return _VERSION


def weight_line():
    """按配置生成权重说明，避免文案里的数字与 score_weights 脱节。"""
    w = load_cfg().get("score_weights") or {}
    names = {"heat": "本档内归一（含同池分位）", "cross": "跨平台分布",
             "engagement": "互动量", "freshness": "时间新鲜度", "credibility": "来源质量"}
    total = sum(max(v, 0.0) for v in w.values()) or 1.0
    parts = ["%s %d%%" % (names.get(k, k), round(100.0 * v / total))
             for k, v in w.items() if v > 0]
    return "热度分 = " + " + ".join(parts) + "。"


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


FACT_LIMIT = 40          # 关键数据表最多几行，太多就不是"关键"了
VERIFY_STATUS = ["已证实", "已辟谣", "有争议", "待核实"]
STANCE_GROUPS = ["正面", "负面", "质疑", "中性"]


def _dedup_key(*parts):
    return "|".join(" ".join(str(p or "").split()).lower() for p in parts)


def collect_facts(items, limit=FACT_LIMIT):
    """把各条目的 facts 聚成一张表：同一条数据只留一行，按更新时间倒序。

    脚本不生成事实——facts 由执行方抓正文时提取，脚本只做去重、排序和渲染。
    """
    seen, rows = set(), []
    for e in items:
        src = platform_text(e.get("platforms")) or clean(e.get("author")) or "未标注"
        for f in (e.get("facts") or []):
            if not isinstance(f, dict):
                continue
            label = clean(f.get("label"))
            value = clean(f.get("value"))
            if not label or not value:
                continue
            unit = clean(f.get("unit"))
            shown = value + (unit if unit and not value.endswith(unit) else "")
            k = _dedup_key(label, shown)
            if k in seen:
                continue
            seen.add(k)
            rows.append({
                "label": label,
                "value": shown,
                "as_of": clean(f.get("as_of")) or "—",
                "source": clean(f.get("source")) or src,
                "note": clean(f.get("note")),
                "_sort": clean(f.get("as_of")),
            })
    rows.sort(key=lambda r: r["_sort"], reverse=True)
    return rows[:limit]


def collect_verifications(items):
    """汇总核查记录：谁核查了什么、结论是什么、依据在哪。"""
    seen, rows = set(), []
    for e in items:
        for v in (e.get("verifications") or []):
            if not isinstance(v, dict):
                continue
            claim = clean(v.get("claim"))
            status = clean(v.get("status"))
            if not claim or not status:
                continue
            k = _dedup_key(claim, status)
            if k in seen:
                continue
            seen.add(k)
            rows.append({
                "claim": claim,
                "status": status if status in VERIFY_STATUS else status,
                "by": clean(v.get("by")) or "—",
                "date": clean(v.get("date")) or "—",
                "url": safe_url(v.get("url")),
                "note": clean(v.get("note")),
            })
    order = {s: i for i, s in enumerate(VERIFY_STATUS)}
    rows.sort(key=lambda r: (order.get(r["status"], 99), r["date"]), reverse=False)
    return rows


def viewpoint_groups(payload):
    """观点分布的规范化：只要 stance/summary/points/refs 四项可用就渲染。"""
    vp = payload.get("viewpoints") or {}
    if not isinstance(vp, dict):
        return "", []
    controversy = clean(vp.get("controversy"))
    groups = []
    for g in (vp.get("groups") or []):
        if not isinstance(g, dict):
            continue
        stance = clean(g.get("stance"))
        summary = clean(g.get("summary"))
        points = [clean(p) for p in (g.get("points") or []) if clean(p)]
        refs = [clean(r) for r in (g.get("refs") or []) if clean(r)]
        if not (stance or summary or points):
            continue
        groups.append({"stance": stance, "summary": summary, "points": points, "refs": refs})
    return controversy, groups


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
             "由 话题棱镜 TopicPrism v%s 生成" % skill_version(),
         ]})
    add({"kind": "pagebreak"})
    add({"kind": "toc"})
    add({"kind": "pagebreak"})

    # 本期要点：执行方依据下文各条摘要提炼，脚本只渲染、不代写
    digest = payload.get("digest") or []
    if isinstance(digest, str):
        digest = [digest] if digest.strip() else []
    digest = [clean(x) for x in digest if clean(x)]
    add({"kind": "h1", "text": "本期要点"})
    if digest:
        add({"kind": "note", "text": "以下要点由执行方依据下文各条内容摘要提炼，供快速判断是否值得细读全文。"})
        for i, d in enumerate(digest, 1):
            add({"kind": "p", "text": "%d. %s" % (i, d)})
    else:
        add({"kind": "note", "text": "本期要点未填写：执行方应在交付前依据各条摘要提炼 %d 条以上最重要信息写进 selected.json 的 digest 字段。"
                      % DIGEST_MIN_ITEMS})

    # 一、关键数据：把所有条目的结构化数据点聚成一张表，按更新时间倒序
    facts = collect_facts(list(sel) + list(app))
    add({"kind": "h1", "text": "一、关键数据"})
    if facts:
        add({"kind": "note", "text": "以下数据点由执行方抓取正文时提取，同一数据只保留一行，按更新时间倒序。核对时请以信源原文为准。"})
        add({"kind": "table",
             "headers": ["数据项", "数值", "截至", "来源", "说明"],
             "rows": [[f["label"], f["value"], f["as_of"], f["source"], f["note"] or "—"]
                      for f in facts]})
    else:
        add({"kind": "note", "text": "本轮未提取到结构化数据点。若话题涉及价格、利率、指标、日期等，执行方应在抓正文时写进各条的 facts 字段。"})

    # 二、观点分布：按立场分组列论点和出处，而不是只给条目计数
    controversy, vgroups = viewpoint_groups(payload)
    add({"kind": "h1", "text": "二、观点分布"})
    if controversy:
        add({"kind": "note", "text": "争议焦点：%s" % controversy})
    if vgroups:
        for g in vgroups:
            head = g["stance"] or "未标注立场"
            if g["refs"]:
                head += "（%s）" % "、".join(g["refs"])
            add({"kind": "h3", "text": head})
            if g["summary"]:
                add({"kind": "p", "text": g["summary"]})
            for p in g["points"]:
                add({"kind": "p", "text": "· " + p})
    else:
        add({"kind": "note", "text": "本轮未整理观点分布。执行方应在交付前把各方立场、核心论点和对应条目写进 selected.json 的 viewpoints 字段。"})
    ss = stance_summary(sel)
    if ss:
        add({"kind": "p", "text": "条目计数（按「立场倾向」维度）：" + ss})

    # 三、核查情况：谁核查了什么、结论是什么、依据在哪
    verifications = collect_verifications(list(sel) + list(app))
    add({"kind": "h1", "text": "三、核查情况"})
    if verifications:
        add({"kind": "note", "text": "把「已证实 / 已辟谣 / 有争议 / 待核实」摊开写，方便判断哪些结论可以直接引用、哪些还需自行复核。"})
        add({"kind": "table",
             "headers": ["待核事实", "结论", "核查方", "时间", "依据"],
             "rows": [[v["claim"], v["status"], v["by"], v["date"], v["url"] or v["note"] or "—"]
                      for v in verifications]})
    else:
        add({"kind": "note", "text": "本轮未记录核查结论。争议类话题执行方应把核查结果写进各条的 verifications 字段；没有核查不等于没有争议，别用「已证实」标签代替核查。"})

    # 四、口径
    add({"kind": "h1", "text": "四、检索与筛选口径"})
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
        add({"kind": "h1", "text": "五、分类矩阵"})
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
    add({"kind": "h1", "text": "六、时间线（按日期倒序，越新越靠前）"})
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
                meta.append(platform_text(e["platforms"]))
            if dims.get("立场倾向"):
                meta.append(clean(dims["立场倾向"]))
            meta.append("热度 %s" % (("%.1f" % e["score"]) if e.get("score") is not None else "未知"))
            add({"kind": "p", "text": " ".join(bits) + ((" ｜ " + " ｜ ".join(meta)) if meta else "")})
    if undated:
        add({"kind": "h2", "text": "日期未知（%d 条）" % len(undated)})
        for _d, e in undated:
            add({"kind": "p", "text": "%s %s" % (e.get("cluster_id", ""), clean(e.get("title")))})

    # 四、条目明细
    add({"kind": "h1", "text": "七、条目明细（按热度降序）"})
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
                meta.append("平台：" + platform_text(e["platforms"]))
            if e.get("author"):
                meta.append("作者：" + clean(e.get("author")))
            if e.get("duplicate_count", 1) > 1:
                meta.append("合并 %d 条重复" % e["duplicate_count"])
            qt = quality_text(e)
            if qt:
                meta.append(qt)
            # 摘要紧跟标题，信源链接放最后：标题 → 摘要 → 元信息 → 分类 → 链接
            ex = clip_excerpt(e.get("excerpt"))
            if ex:
                add({"kind": "p", "text": "摘要：" + ex})
            else:
                add({"kind": "note", "text": "本条未提供摘要（excerpt 为空），文档摘要区跳过。"})
            add({"kind": "p", "text": " ｜ ".join(meta)})
            if dims:
                add({"kind": "p", "text": "分类：" + "；".join(
                    "%s=%s" % (d, clean(dims.get(d))) for d in DIMENSIONS if dims.get(d))})
            u = safe_url(e.get("url"))
            if u:
                add({"kind": "link", "text": u, "url": u})
            else:
                add({"kind": "p", "text": "链接缺失或非 http(s)，未纳入文档。"})

    # 八、与上次对比：没有基线也占位，避免章节编号跳号
    add({"kind": "pagebreak"})
    add({"kind": "h1", "text": "八、与上次对比"})
    diff = payload.get("diff")
    if diff:
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
    else:
        add({"kind": "note", "text": "本轮未指定基线，没有新增 / 消失 / 延续对比。"
                                    "需要做增量时，用 --baseline 传入上一轮的 selected.json 重新渲染。"})

    # 九、方法学
    add({"kind": "pagebreak"})
    add({"kind": "h1", "text": "九、方法学与限制"})
    for line in [
        "热度门槛：绝对地板 100；大流量平台 1000、中等平台 300、垂直/小众源 100。",
        weight_line(),
        "来源质量按站点 / 账号 / 页面三级证据判断，命中标题党 / AI 水文 / 营销号的内容不进正文。",
        "分数表示本话题候选池内的相对位置，不是全网浏览量排名；各平台指标口径不同，不可当绝对值横向比较。",
        "同一事件的多份拷贝已合并；合并后跨平台分布计入加分。",
        "「立场倾向」按结论落点判定，不等于事实判断；「信息确定性」未达「已证实」的条目，采纳前需自行复核。",
        "时间线按发布日倒序排列；日期取自信源标注，可能与实际发布时间存在偏差。",
        "源链接指向第三方站点，内容与安全性由该站点负责；本工具只转述摘要，不代理访问。",
    ]:
        add({"kind": "p", "text": line})

    # 十、值得优先关注的几条
    hl = [h for h in (payload.get("highlights") or [])
          if isinstance(h, dict) and (clean(h.get("title")) or clean(h.get("cluster_id")))]
    if hl:
        add({"kind": "pagebreak"})
        add({"kind": "h1", "text": "十、值得优先关注的几条"})
        add({"kind": "note", "text": "从结果里挑出的重点条目及理由，供决定先读哪几条。"})
        for h in hl[:8]:
            head = " ".join(x for x in [h.get("cluster_id", ""), clean(h.get("title"))] if x)
            add({"kind": "h3", "text": head})
            why = clean(h.get("why"))
            if why:
                add({"kind": "p", "text": why})
    # 附录 A
    add({"kind": "pagebreak"})
    add({"kind": "h1", "text": "附录 A、信源清单（全部链接）"})
    add({"kind": "note", "text": "按条目汇总每个信源的中文平台名、热度数值、内容摘要与原始地址。同一事件在多平台出现时逐条列出，便于逐一定位核对。"})
    idx = 0
    for e in list(sel) + list(app):
        sources = e.get("sources") or ([{
            "platform": (e.get("platforms") or [""])[0], "url": e.get("url"), "metric": None}])
        add({"kind": "h3", "text": "%s %s" % (e.get("cluster_id", ""), clean(e.get("title")))})
        # 本条摘要与正文条目明细一致（同样受 EXCERPT_LIMIT 限制）
        ex = clip_excerpt(e.get("excerpt"))
        if ex:
            add({"kind": "p", "text": "摘要：" + ex})
        for s in sources:
            idx += 1
            u = safe_url(s.get("url"))
            plat = platform_label(clean(s.get("platform"))) or "未标注平台"
            metric = s.get("metric")
            line = "[%d] %s" % (idx, plat)
            if metric not in (None, ""):
                line += " ｜ 数值 %s" % fmt_metric(metric)
            add({"kind": "p", "text": line})
            if u:
                # 地址既做可见文本又做超链接目标：打印、复制、纯文本粘贴都能看到网址
                add({"kind": "link", "text": u, "url": u})
            else:
                add({"kind": "p", "text": "链接缺失或非 http(s)，未纳入文档。"})

    # 附录 B
    if app:
        add({"kind": "pagebreak"})
        add({"kind": "h1", "text": "附录 B、热度未知条目（无公开量化指标，不参与排名）"})
        add({"kind": "note", "text": "平台未公开阅读/播放/互动数据，无法给出热度分；价值在内容本身，采纳前请自行判断信源。"})
        for e in app:
            dims = e.get("dimensions") or {}
            add({"kind": "h3", "text": "%s %s" % (e.get("cluster_id", ""), clean(e.get("title")))})
            add({"kind": "p", "text": "平台：%s ｜ 作者：%s"
                 % (platform_text(e.get("platforms")), clean(e.get("author")))})
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


def _cell(text, bold=False):
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return ('<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
            '<w:p><w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p></w:tc>'
            % (rpr, escape(str(text if text is not None else ""))))


def _table(headers, rows):
    """手写 w:tbl：不依赖 styles.xml 里的表格样式，边框直接写在 tblPr 上。"""
    border = ("<w:tblBorders>"
              + "".join('<w:%s w:val="single" w:sz="4" w:space="0" w:color="D9D9D9"/>' % s
                        for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
              + "</w:tblBorders>")
    out = ['<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>' + border + '</w:tblPr>']
    out.append("<w:tr>" + "".join(_cell(h, True) for h in headers) + "</w:tr>")
    for r in rows:
        out.append("<w:tr>" + "".join(_cell(c) for c in r) + "</w:tr>")
    out.append("</w:tbl>")
    # 表格后补一个空段落：连续两个表格紧挨着时 Word 会把它们并成一张
    out.append("<w:p/>")
    return "".join(out)


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
        elif k == "table":
            rows = item.get("rows") or []
            if rows:
                body.append(_table(item.get("headers") or [], rows))
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
        from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)
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
        "th": ParagraphStyle("PrismTH", parent=base["BodyText"], fontName="STSong-Light",
                             fontSize=8.5, leading=13),
        "td": ParagraphStyle("PrismTD", parent=base["BodyText"], fontName="STSong-Light",
                             fontSize=8, leading=12),
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
        elif k == "table":
            rows = item.get("rows") or []
            if rows:
                data = [[Paragraph(escape(str(h)), S["th"]) for h in (item.get("headers") or [])]]
                for r in rows:
                    data.append([Paragraph(escape(str(c if c is not None else "")), S["td"]) for c in r])
                t = Table(data, repeatRows=1)
                t.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F5F5F5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]))
                flow.append(t)
                flow.append(Spacer(1, 4))
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
            "source_quality": 5, "quality_norm": 1.0, "quality_flags": [],
            "facts": [{"label": "示例指标", "value": "1.95", "unit": "%",
                       "as_of": "2026-10-18", "note": "自检用"}],
            "verifications": [{"claim": "示例事实", "status": "已证实", "by": "官方公告",
                               "date": "2026-10-18", "url": "https://example.com/v"}],
        }],
        "appendix": [],
        "rejected": [{"title": "落选例子", "platform": "weibo", "reason": "低于绝对地板 100",
                      "url": "https://example.com/r"}],
        "digest": ["要点一：示例事件已获权威信源证实。", "要点二：舆论整体偏正面。"],
        "highlights": [{"cluster_id": "E001", "title": "示例事件", "why": "一手材料且跨平台共识，建议先读。"}],
        "viewpoints": {
            "controversy": "争议焦点：示例口径是否统一",
            "groups": [{"stance": "正面", "summary": "支持方认为数据可核验。",
                        "points": ["示例论点一"], "refs": ["E001"]}],
        },
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
    checks.append(("含时间线章节", any((i.get("text") or "").startswith("六、时间线") for i in outline)))
    checks.append(("含增量对比章节", any(i.get("text") == "八、与上次对比" for i in outline)))
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

    # --out 路径边界：绝对路径与 .. 一律拒绝，工作区内相对路径放行
    def _rejected(p):
        try:
            guard_out_path(p)
            return False
        except SystemExit:
            return True

    checks.append(("拒绝绝对路径作为 --out",
                   _rejected("/tmp/x.docx") and _rejected("~/x.docx") and _rejected("C:\\x.docx")))
    checks.append(("拒绝含 .. 的 --out", _rejected("../x.docx") and _rejected("a/../../x.docx")))
    checks.append(("放行工作区内相对路径", guard_out_path("out/report.docx") == "out/report.docx"))

    # 本期要点 / 重点条目：执行方填写、脚本渲染，缺失时给出明确提示
    checks.append(("含本期要点章节", any(i.get("text") == "本期要点" for i in outline)))
    checks.append(("渲染 digest 各条", all(any(("%d. %s" % (n, d)) in (i.get("text") or "")
                                                for i in outline)
                                              for n, d in enumerate(payload["digest"], 1))))
    checks.append(("digest 缺失时给出提示",
                   any("本期要点未填写" in (i.get("text") or "")
                       for i in build_outline({k: v for k, v in payload.items() if k != "digest"}, "自检示例"))))
    checks.append(("含重点条目章节", any(i.get("text") == "十、值得优先关注的几条" for i in outline)))
    checks.append(("渲染 highlights 的理由",
                   any("一手材料且跨平台共识" in (i.get("text") or "") for i in outline)))

    # 平台显示统一中文名，不暴露内部 key
    checks.append(("平台显示中文名而非 key",
                   any("平台：微博" in (i.get("text") or "") for i in outline)
                   and not any(re.search(r"平台：(weibo|cls|qbitai)\b", (i.get("text") or ""))
                               for i in outline)))
    checks.append(("附录 A 平台同为中文名",
                   any("[1] 微博" in (i.get("text") or "") for i in outline)))
    checks.append(("附录 A 链接地址可见", any(i.get("url") == "https://example.com/a"
                                            and (i.get("text") or "") == "https://example.com/a"
                                            for i in outline)))
    checks.append(("附录 A 带本条摘要", any("摘要：摘要 A | B" in (i.get("text") or "") for i in outline)))

    # 摘要长度上限
    checks.append(("摘要超 200 字被截断", clip_excerpt("啊" * 250).endswith("…")
                   and len(clip_excerpt("啊" * 250)) == EXCERPT_LIMIT + 1))
    checks.append(("短摘要原样保留", clip_excerpt("摘要 A | B") == "摘要 A | B"))
    checks.append(("空摘要返回空串", clip_excerpt(None) == "" and clip_excerpt("") == ""))
    checks.append(("整数值不显示 .0", fmt_metric(980000.0) == "980000" and fmt_metric(12.5) == "12.5"))

    # 三章 + 表格
    checks.append(("含关键数据章节", any(i.get("text") == "一、关键数据" for i in outline)))
    checks.append(("含观点分布章节", any(i.get("text") == "二、观点分布" for i in outline)))
    checks.append(("含核查情况章节", any(i.get("text") == "三、核查情况" for i in outline)))
    tables = [i for i in outline if i.get("kind") == "table"]
    checks.append(("生成两张表（关键数据 + 核查）", len(tables) == 2))
    checks.append(("关键数据表含表头与数据行",
                   bool(tables) and tables[0]["headers"][:2] == ["数据项", "数值"]
                   and len(tables[0]["rows"]) >= 1))
    checks.append(("同一数据只留一行",
                   len(collect_facts([{"facts": [{"label": "a", "value": "1"},
                                                 {"label": "a", "value": "1"}]}])) == 1))
    checks.append(("核查结论按类型排序",
                   [v["status"] for v in collect_verifications(
                       [{"verifications": [{"claim": "x", "status": "待核实"},
                                           {"claim": "y", "status": "已证实"}]}])] == ["已证实", "待核实"]))
    checks.append(("观点分布解析出争议焦点",
                   viewpoint_groups(payload)[0] == "争议焦点：示例口径是否统一"))
    checks.append(("来源质量显示为 5/5",
                   "来源质量 5/5" in quality_text({"quality_norm": 1.0})))
    # 封面版本号必须跟着技能版本走：写死过一次 v1.2.5，升到 1.4.0 还在印旧号
    checks.append(("版本号可读取", skill_version() != "unknown"))
    checks.append(("封面版本号与当前版本一致",
                   any(("TopicPrism v%s 生成" % skill_version()) in ln
                       for i in outline if i.get("kind") == "cover"
                       for ln in (i.get("lines") or []))))
    # 章节结构：正文编号连续，附录排在正文之后（曾跳号、附录夹在正文中间）
    h1s = [i["text"] for i in outline if i.get("kind") == "h1"]
    nums = [t.split("、")[0] for t in h1s if re.match(r"^[一二三四五六七八九十]、", t)]
    checks.append(("正文章节编号连续", nums == list("一二三四五六七八九十")))
    checks.append(("附录在正文之后",
                   h1s.index("附录 A、信源清单（全部链接）") > h1s.index("九、方法学与限制")))

    # docx 里确实写出了 w:tbl
    tmp2 = tempfile.mkdtemp()
    dp = os.path.join(tmp2, "t2.docx")
    to_docx(outline, dp, "自检")
    dx = zipfile.ZipFile(dp).read("word/document.xml").decode("utf-8")
    checks.append(("docx 含表格元素", dx.count("<w:tbl>") == 2))
    checks.append(("docx 表格 XML 可解析",
                   ET.fromstring(dx) is not None))

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
def guard_out_path(path):
    """--out 只收工作区内的相对路径：绝对路径与含 .. 的路径一律拒绝。

    与 SKILL.md「十一、权限与数据边界」声明的文件边界一致——不往调用方指定的
    工作区之外写文件。lark-cli 侧同样只收相对路径，两边口径统一。
    """
    p = str(path or "")
    if not p:
        raise SystemExit("导出失败：--out 不能为空")
    if os.path.isabs(p) or p.startswith("~") or (len(p) > 1 and p[1] == ":"):
        raise SystemExit("导出失败：--out 只接受工作区内的相对路径，拒绝绝对路径：%s" % p)
    parts = p.replace("\\", "/").split("/")
    if ".." in parts:
        raise SystemExit("导出失败：--out 不允许包含 .. 上级目录：%s" % p)
    return p


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
    args.out = guard_out_path(args.out)

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
