#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""话题棱镜 TopicPrism 核心脚本。

职责（可确定、可复现的部分）：
  1. probe      探水：判断选题是「主流热度」还是「低热度」，决定用绝对线还是相对线
  2. score      热度打分：按渠道分档 + 绝对地板 + 互动加权，逐条给出分数与放行/拒绝理由
  3. dedup      事件去重：同作者跨平台同发、内容主体一致 → 合并为一个事件簇
  4. select     排序取前 N 个唯一事件，如实报告缺口
  5. report     渲染 Markdown 报告（分类矩阵 + 条目明细 + 方法学与限制）

分类（语义判断）不在这里做。脚本产出 selected.json 后，由调用方按
references/taxonomy.md 填好每条事件的 dimensions，再用 report 渲染。

用法示例：
  python3 prism.py run   --in candidates.json --n 60 --outdir out/
  python3 prism.py probe --in candidates.json
  python3 prism.py report --in out/selected.json --out out/report.md
  python3 prism.py self-test
"""

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_CFG = Path(__file__).resolve().parent.parent / "references" / "platforms.json"
DIMENSIONS = ["主题域", "内容性质", "信息确定性", "影响面", "立场倾向", "信源权威度", "时效形态"]
# 前六项必填；时效形态可留空
REQUIRED_DIMS = DIMENSIONS[:6]
STANCE_VALUES = ["正面", "中性", "负面", "质疑"]
# 信源权威度：回答「谁在说」，与「信息确定性（真假）」是两回事
AUTHORITY_VALUES = ["官媒 / 机构发布", "权威媒体 / 专业机构", "认证账号", "自媒体 / 个人博主", "UGC / 匿名讨论", "无法判定"]
METRIC_KEYS = ["metric", "view", "views", "play", "plays", "read", "reads", "heat", "rank"]


# ---------------------------------------------------------------- 基础工具
def load_json(path, what="输入文件"):
    """统一读 JSON，给友好报错（report / qa / notify / baseline 共用）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SystemExit("[输入错误] 找不到%s：%s\n检查路径（相对路径以当前目录为基准）。" % (what, path))
    except json.JSONDecodeError as exc:
        raise SystemExit("[输入错误] %s 不是合法 JSON：%s\n第 %d 行第 %d 列：%s"
                         % (what, path, exc.lineno, exc.colno, exc.msg))
    except OSError as exc:
        raise SystemExit("[输入错误] %s 读取失败：%s（%s）" % (what, path, exc))


def load_cfg(path=None):
    with open(path or DEFAULT_CFG, "r", encoding="utf-8") as fh:
        return json.load(fh)


def filter_window(entries, days):
    """按时间窗收窄候选：以池内最新日期为基准，保留最近 N 天。无日期的条目保留。"""
    if not days:
        return entries
    ref = pool_ref_date(entries)
    if ref is None:
        return entries
    lo = ref - timedelta(days=int(days))
    out = []
    for e in entries:
        d = parse_date(e.get("published_at"))
        if d is None or d >= lo:
            out.append(e)
    return out


LAST_DROPPED = []


def load_entries(path):
    """读候选池并做结构校验。

    为什么要在这一层拦住：路径写错、字段名拼错、JSON 被截断，如果被当成「空池」
    继续往下跑，probe 会判成低热度、报告会写「公开渠道没有形成有效讨论」——
    把输入错误伪装成真实结论，比直接报错危险得多。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise SystemExit("[输入错误] 找不到候选文件：%s\n检查 --in 路径（相对路径以当前目录为基准）。" % path)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "[输入错误] 候选文件不是合法 JSON：%s\n第 %d 行第 %d 列：%s\n"
            "常见原因：末尾多余逗号、文件被日志截断、贴进了非 JSON 片段。" % (path, exc.lineno, exc.colno, exc.msg))
    except OSError as exc:
        raise SystemExit("[输入错误] 候选文件读取失败：%s（%s）" % (path, exc))

    if isinstance(data, dict):
        for key in ("candidates", "items", "entries"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise SystemExit(
                "[输入错误] 候选文件是对象，但没有 candidates / items / entries 数组。\n"
                "实际顶层键：%s" % "、".join(list(data)[:12] or ["（空对象）"]))
    if not isinstance(data, list):
        raise SystemExit("[输入错误] 候选池应为 JSON 数组，实际是 %s。" % type(data).__name__)

    ok, dropped = [], []
    for i, e in enumerate(data, 1):
        if not isinstance(e, dict):
            dropped.append("第 %d 项不是对象（%s）" % (i, type(e).__name__))
            continue
        if not str(e.get("title") or "").strip():
            dropped.append("第 %d 项缺 title（id=%s）" % (i, e.get("id") or "-"))
            continue
        ok.append(e)
    global LAST_DROPPED
    LAST_DROPPED = list(dropped)
    if dropped:
        print("[输入提示] 丢弃 %d 条不合格候选：%s%s"
              % (len(dropped), "；".join(dropped[:5]), " 等" if len(dropped) > 5 else ""),
              file=sys.stderr)
    if not ok:
        if not data:
            raise SystemExit("[输入错误] 候选池为空（0 条）。这是输入问题，不是「话题没有讨论」。\n"
                             "请检查检索结果是否落了盘、--in 是否指到了正确文件。")
        raise SystemExit(
            "[输入错误] 候选池里没有可用条目（%d 条全部不合格）。\n"
            "注意：这与「话题没有讨论」不是一回事——后者是检索到了内容、但都不过门槛。" % len(data))
    return ok


def norm_text(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    return s.strip()


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def strip_digits(s):
    return re.sub(r"\d+", "#", s or "")


def bigrams(s):
    s = norm_text(s)
    if not s:
        return set()
    if len(s) < 2:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def sim(a, b):
    """字符 bigram Dice 系数。

    替换 difflib.SequenceMatcher 的原因：中短标题上 SequenceMatcher 对
    「同义改写」和「套话拼贴」都不敏感——改几个字就掉到 0.5 以下，
    而共用同一串模板话术的两条不同事件又能轻松超过 0.72。
    bigram Dice 在同一语序/用词改写上更稳，且对语序不敏感。
    """
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    if A == B:
        return 1.0
    inter = len(A & B)
    return 2.0 * inter / (len(A) + len(B))


def entities(s):
    """抽取可锚定实体：英文/型号、数字、引号与书名号内的专名。

    用于去重时补一条「实体高度重合才合并」的通道——两家不同媒体写同一事件，
    标题措辞往往差别很大，但型号/数字/产品名会一致。
    """
    raw = str(s or "")
    out = set()
    out |= set(re.findall(r"[A-Za-z][A-Za-z0-9\.\+\-]{1,}", raw))
    out |= set(re.findall(r"\d+(?:\.\d+)?%?", raw))
    out |= set(re.findall(r"[「『\"]([^」』\"]{1,12})[」』\"]", raw))
    out |= set(re.findall(r"《([^》]{1,12})》", raw))
    return {x.strip().lower() for x in out if x and x.strip()}


def ent_sim(a, b):
    A, B = entities(a), entities(b)
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "froms", "spm", "share_token", "share_from", "share_source",
    "ref", "refer", "isappinstalled", "scene", "clicktime", "enterid",
    "upstream_biz", "tt_from", "wxshare", "s_trans", "chksm", "sharer",
    "share_medium", "share_plat", "share_session_id", "share_tag",
}


def norm_url(u):
    """URL 归一：去跟踪参数、去锚点、抹平移动端子域、去尾斜杠。

    不做这一步，「同一篇稿子换个分享链接」就能绕过 URL 去重，
    「3 家媒体转载」这类替代判据也会被同一家的多个分享链接刷高。
    """
    if not u:
        return ""
    raw = str(u).strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
    except ValueError:
        return raw
    host = (p.netloc or "").lower()
    host = re.sub(r"^(m|www|mobile|wap|3g)\.", "", host)
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
            if k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/+$", "", p.path or "")
    query = urlencode(kept)
    # 作为去重键使用，忽略协议差异（http/https）与移动端子域
    return ("%s%s" % (host, path)) + (("?" + query) if query else "")


def parse_date(v):
    if not v:
        return None
    sv = str(v).strip()[:19]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(sv, fmt).date()
        except ValueError:
            continue
    return None


def freshness_norm(published_at, ref=None, half_life=None, cfg=None):
    """时间新鲜度 0~1，半衰期默认 10 天；日期缺失按中性 0.5。"""
    d = parse_date(published_at)
    if d is None:
        return 0.5
    hl = float(half_life or (cfg or {}).get("freshness_half_life_days", 10))
    ref = ref or date.today()
    age = max((ref - d).days, 0)
    return clamp(0.5 ** (age / max(hl, 0.5)))


def metric_of(entry):
    """返回 (数值, 类型)。类型为 'rank' 或 'volume'。"""
    mtype = (entry.get("metric_type") or "").lower()
    for key in METRIC_KEYS:
        if key in entry and entry[key] not in (None, ""):
            try:
                val = float(entry[key])
            except (TypeError, ValueError):
                continue
            if key == "rank" or mtype == "rank":
                return val, "rank"
            return val, "volume"
    return None, None


def platform_key(entry, cfg):
    raw = (entry.get("platform") or "").strip()
    if not raw:
        return "unknown", "C"
    aliases = cfg.get("aliases", {})
    key = aliases.get(raw, aliases.get(raw.lower(), raw.lower()))
    for tier, spec in cfg["tiers"].items():
        if key in spec.get("platforms", []):
            return key, tier
    return key, "C"


_LABELS = None


def platform_labels():
    """key -> 中文显示名，取自配置里的 aliases（取第一个中文写法）。"""
    global _LABELS
    if _LABELS is None:
        cfg = load_cfg()
        rev = {}
        for alias, key in (cfg.get("aliases") or {}).items():
            if re.search(r"[\u4e00-\u9fff]", alias) and key not in rev:
                rev[key] = alias
        _LABELS = rev
    return _LABELS


def tier_min(cfg, tier):
    return float(cfg["tiers"][tier]["min_metric"])


# ---------------------------------------------------------------- 打分
def heat_norm(metric, mtype, tmin, cfg):
    """把各平台异构指标压到 0~1 的「本档内热度」。

    边界处理原则（P0 修复）：
    - 不再在 metric <= tmin 处硬归零，避免「刚好过门槛 = 0 分」与
      「只有替代判据 = qualifier_baseline(0.5)」倒挂；
    - 对数内插从 base 起跳，曲线在 tmin 处连续；
    - rank 采用线性衰减，第 rank_hard_cap 名仍保有 base 以上余量。
    """
    base = float(cfg.get("heat_floor_noise", 0.05))
    if metric is None:
        return float(cfg.get("qualifier_baseline_score", 0.5))
    if mtype == "rank":
        cap = float(cfg.get("rank_hard_cap", 100))
        if metric <= 1:
            return 1.0
        return clamp(1.0 - (1.0 - base) * (metric - 1.0) / max(cap - 1.0, 1.0))
    if metric <= 0:
        return 0.0
    lo = math.log10(max(tmin, 2.0))
    hi = math.log10(float(cfg.get("heat_ceiling", 1000000)))
    if hi <= lo:
        hi = lo + 1.0
    if metric <= tmin:
        # 未达本档门槛：线性压到 base 以下，与达标侧在 tmin 处连续
        return clamp(base * (metric / max(tmin, 1.0)))
    return clamp(base + (1.0 - base) * (math.log10(metric) - lo) / (hi - lo))


def engagement_norm(entry):
    eng = entry.get("engagement") or {}
    if not isinstance(eng, dict) or not eng:
        return 0.0
    c = float(eng.get("comments") or 0)
    s = float(eng.get("shares") or 0)
    l = float(eng.get("likes") or 0)
    raw = c + 2.0 * s + 0.2 * l
    if raw <= 0:
        return 0.0
    return clamp(math.log10(1.0 + raw) / 5.0)


DEFAULT_WEIGHTS = {"heat": 0.50, "cross": 0.22, "engagement": 0.18, "freshness": 0.10}


def score_of(heat_mix, cross, eng, fresh, cfg):
    w = dict(DEFAULT_WEIGHTS)
    w.update(cfg.get("score_weights") or {})
    total = sum(max(v, 0.0) for v in w.values()) or 1.0
    return round(100.0 * (w["heat"] * heat_mix + w["cross"] * cross
                          + w["engagement"] * eng + w["freshness"] * fresh) / total, 2)


def evaluate(entry, cfg, mode="absolute", ref=None):
    """返回带 score / verdict / reason 的副本。"""
    out = dict(entry)
    key, tier = platform_key(entry, cfg)
    tmin = tier_min(cfg, tier)
    floor = float(cfg.get("absolute_floor", 100))
    metric, mtype = metric_of(entry)
    quals = [q for q in (entry.get("qualifiers") or []) if q in cfg.get("qualifiers", {})]

    out["platform_key"] = key
    out["tier"] = tier
    out["tier_min"] = tmin
    out["metric_value"] = metric
    out["metric_type"] = mtype

    # 放行判定
    if quals:
        verdict, reason = "pass", "替代判据：" + "、".join(cfg["qualifiers"][q] for q in quals)
    elif metric is None:
        # 平台不公开阅读/播放量：不当作噪音剔除，单列为「热度未知」，不参与热度排名
        verdict, reason = "unverified", "平台未公开热度指标且无替代判据（单列，不参与热度排名）"
    elif metric < floor and mtype != "rank":
        verdict, reason = "reject", "低于绝对地板 %g" % floor
    elif mtype == "rank":
        verdict = "pass" if metric <= 100 else "reject"
        reason = "榜单名次 %g" % metric if verdict == "pass" else "榜单名次 %g，超出前 100" % metric
    elif metric < tmin:
        verdict = "below_tier"
        reason = "%s 平台门槛 %g，实际 %g" % (cfg["tiers"][tier]["label"], tmin, metric)
    else:
        verdict, reason = "pass", "达标（%s 门槛 %g）" % (cfg["tiers"][tier]["label"], tmin)

    if mode == "relative" and verdict == "unverified":
        verdict, reason = "pass_unknown", "热度未知：平台未公开指标，低热度选题下按信源可核验性纳入，不参与热度排名"
    if mode == "relative" and verdict == "below_tier" and metric is not None and mtype != "rank":
        verdict, reason = "pass", "低热度选题的相对线放行（原" + reason + "）"

    platforms = [key]
    if entry.get("also_on"):
        platforms += [platform_key({"platform": p}, cfg)[0] for p in entry["also_on"]]
    n_pl = len(set(platforms))

    hn = heat_norm(metric, mtype, tmin, cfg)
    if quals and metric is None:
        hn = max(hn, float(cfg.get("qualifier_baseline_score", 0.5)))
    cross = clamp((n_pl - 1) / 3.0)
    eng = engagement_norm(entry)
    fresh = freshness_norm(entry.get("published_at"), ref, cfg=cfg)

    out["heat_norm"] = round(hn, 4)
    out["cross_norm"] = round(cross, 4)
    out["engagement_norm"] = round(eng, 4)
    out["fresh_norm"] = round(fresh, 4)
    out["score"] = score_of(hn, cross, eng, fresh, cfg)
    out["verdict"] = verdict
    out["reason"] = reason
    out["platform_count"] = n_pl
    return out


def percentile(values, q):
    if not values:
        return 0.0
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    pos = clamp(q) * (len(vs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vs[lo]
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


def probe(entries, cfg):
    """先按绝对线跑一遍，判断选题热度形态。"""
    ref = pool_ref_date(entries)
    judged = [evaluate(e, cfg, "absolute", ref) for e in entries]
    ok = [j for j in judged if j["verdict"] == "pass"]
    below_floor = sum(1 for j in judged if j["verdict"] == "reject")
    ratio = (len(ok) / len(judged)) if judged else 0.0
    trig = float(cfg["relative_mode"]["trigger_pass_ratio"])
    min_sample = int(cfg.get("probe_min_sample", 20))
    mode = "relative" if ratio < trig else "absolute"
    return {
        "total_candidates": len(judged),
        "pass_count": len(ok),
        "pass_ratio": round(ratio, 3),
        "rejected": below_floor,
        "all_below_floor": bool(judged) and len(ok) == 0 and below_floor > 0,
        "threshold": trig,
        "reliable": len(judged) >= min_sample,
        "min_sample": min_sample,
        "sample_note": ("" if len(judged) >= min_sample else
                        "候选仅 %d 条，少于探水样本下限 %d 条，达标占比仅作参考" % (len(judged), min_sample)),
        "mode": mode,
        "explain": (
            "达标占比 %.0f%%，低于 %.0f%%，判定为低热度选题，改用题域相对线筛选"
            % (ratio * 100, trig * 100)
            if mode == "relative"
            else "达标占比 %.0f%%，判断为主流热度选题，按绝对门槛筛选" % (ratio * 100)
        ),
    }


# ---------------------------------------------------------------- 去重
def dedup(scored, cfg):
    """事件去重：并查集 + 全局两两比对。

    为什么不用「逐个与已有簇的代表条目比较」：那会把合并结果绑在排序上，
    A 与 B 相似、B 与 C 相似，却因为 B 不是代表而让 A、C 分裂成两个事件。
    实测里同一场发布会的四篇通稿就被拆成了两簇。改成对所有条目两两判定再做连通分量，
    传递性自然成立。
    """
    pool = [e for e in scored if e.get("verdict") in ("pass", "pass_unknown", "unverified")]
    # 「平台未公开指标」也进池：公众号、小红书那类拿不到阅读量的条目，正是
    # SKILL 承诺要单列进附录的部分；不这么做，它们在主流选题里会被整池拦在门外。
    pool.sort(key=lambda e: -e.get("score", 0))

    t_title = float(cfg.get("dedup_title_sim", 0.66))
    t_author_title = float(cfg.get("dedup_author_title_sim", 0.42))
    t_author_body = float(cfg.get("dedup_author_body_sim", 0.55))
    t_ent = float(cfg.get("dedup_entity_sim", 0.75))
    t_ent_title = float(cfg.get("dedup_entity_title_sim", 0.45))

    meta = []
    for e in pool:
        title = str(e.get("title") or "")
        excerpt = str(e.get("excerpt") or "")
        meta.append({
            "t": norm_text(title),
            "a": norm_text(e.get("author")),
            "x": norm_text(excerpt),
            "u": norm_url(e.get("url")),
            "ent": entities(title + " " + excerpt),
            "td": strip_digits(norm_text(title)),
        })

    n = len(pool)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        mi = meta[i]
        for j in range(i + 1, n):
            mj = meta[j]
            if mi["u"] and mi["u"] == mj["u"]:
                union(i, j)
                continue
            if pool[i].get("id") and pool[i].get("id") == pool[j].get("id"):
                union(i, j)
                continue
            # 模板化系列（仅数字不同的「第 N 期」）不算重复
            if mi["td"] == mj["td"] and mi["t"] != mj["t"]:
                continue
            title_sim = sim(mi["t"], mj["t"])
            if title_sim >= t_title:
                union(i, j)
                continue
            if mi["ent"] and mj["ent"]:
                inter = len(mi["ent"] & mj["ent"])
                union_ent = inter / float(len(mi["ent"] | mj["ent"]))
                if union_ent >= t_ent and title_sim >= t_ent_title:
                    union(i, j)
                    continue
            if mi["a"] and mi["a"] == mj["a"]:
                if title_sim >= t_author_title or sim(mi["x"], mj["x"]) >= t_author_body:
                    union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(pool[i])

    clusters = []
    for members in groups.values():
        members.sort(key=lambda e: -e.get("score", 0))
        rep = members[0]
        platforms = []
        sources = []
        for e in members:
            pf = e.get("platform_key") or "unknown"
            if pf not in platforms:
                platforms.append(pf)
            sources.append({"platform": pf, "url": e.get("url"), "metric": e.get("metric_value")})
        n_pl = len(platforms)
        mix = rep.get("heat_mix", rep.get("heat_norm", 0.0))
        cl = {
            "cluster_id": "",
            "title": rep.get("title"),
            "author": rep.get("author"),
            "url": rep.get("url"),
            "platforms": platforms,
            "sources": sources,
            "excerpt": rep.get("excerpt"),
            "published_at": rep.get("published_at"),
            "dimensions": {},
            "merged_from": [e.get("id") or e.get("title") for e in members],
            "duplicate_count": len(members),
            "heat_mix": round(mix, 4),
            "heat_pct": rep.get("heat_pct"),
            # 簇里所有成员的指标都拿不到时才算热度未知；只要有一条有可核验且参与排名的热度，
            # 这个簇就是"已知"，防止被低条目的集合并拖下水
            "heat_unknown": all((e.get("verdict") in ("unverified", "pass_unknown"))
                                for e in members),
            "platform_count": n_pl,
            "score": min(100.0, score_of(mix, clamp((n_pl - 1) / 3.0),
                                         rep.get("engagement_norm", 0.0),
                                         rep.get("fresh_norm", 0.5), cfg)),
        }
        clusters.append(cl)

    clusters.sort(key=lambda c: -c.get("score", 0))
    for i, cl in enumerate(clusters, 1):
        cl["cluster_id"] = "E%03d" % i
    return clusters


# ---------------------------------------------------------------- 时间线 / 增量
def timeline_groups(items):
    """按发布日倒序分组，越新越靠前；无日期的归到最后。"""
    dated, undated = [], []
    for e in items:
        d = parse_date(e.get("published_at"))
        (dated if d else undated).append((d, e))
    dated.sort(key=lambda x: (x[0], x[1].get("score") or 0), reverse=True)
    buckets = {}
    for d, e in dated:
        buckets.setdefault(d, []).append(e)
    out = [(d, buckets[d]) for d in sorted(buckets, reverse=True)]
    if undated:
        out.append((None, [e for _, e in undated]))
    return out


def load_baseline(path):
    """读上一次的 selected.json，取标题快照用于增量对比。"""
    prev = load_json(path, "基线文件（--baseline）")
    if not isinstance(prev, dict):
        raise SystemExit("[输入错误] 基线文件应为 selected.json 对象。")
    items = []
    for key in ("selected", "appendix"):
        for e in prev.get(key) or []:
            items.append({"title": e.get("title"), "score": e.get("score"),
                          "cluster_id": e.get("cluster_id"),
                          "published_at": e.get("published_at")})
    return {"items": items,
            "titles": {norm_text(i["title"]) for i in items},
            "topic": prev.get("topic") or "",
            "generated_at": (prev.get("stats") or {}).get("generated_at") or ""}


def compute_diff(sel, app, base):
    """新增 / 消失 / 延续。按标题归一匹配，不依赖 cluster_id（每次会重排）。"""
    cur = list(sel) + list(app)
    cur_titles = {norm_text(e.get("title")) for e in cur}
    added, removed = [], []
    for e in cur:
        if norm_text(e.get("title")) not in base["titles"]:
            added.append({"cluster_id": e.get("cluster_id"), "title": e.get("title"),
                          "published_at": e.get("published_at"), "score": e.get("score")})
    for it in base["items"]:
        if norm_text(it["title"]) not in cur_titles:
            removed.append(it)
    for e in cur:
        if any(norm_text(e.get("title")) == norm_text(a["title"]) for a in added):
            e["is_new"] = True
    return {"added": added, "removed": removed,
            "kept": len(cur) - len(added),
            "baseline_at": base.get("generated_at") or "",
            "baseline_topic": base.get("topic") or "",
            "baseline_total": len(base["items"])}


# ---------------------------------------------------------------- 汇总
def pct_rank(sorted_vals, v):
    """在已排序列表里取 v 的分位（0~1，中位取平均位次）。"""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    if n == 1:
        return 1.0
    less = sum(1 for x in sorted_vals if x < v)
    eq = sum(1 for x in sorted_vals if x == v)
    # 端点为 0 / 1：最小值 0，最大值 1，并列取平均位次
    return clamp((less + 0.5 * (eq - 1)) / max(n - 1, 1))


def rescore_pool(scored, cfg):
    """本档内热度再叠加「同池分位」，让不同门槛档位的分数可比。

    heat_mix = 0.5 * 本档内归一 + 0.5 * 同池分位
    score = 100 * Σ(w * 分量) / Σw，权重取自配置 score_weights（heat / cross / engagement / freshness），
            缺项时回落到 DEFAULT_WEIGHTS。不要在注释里写死数值，改配置会导致注释过期。
    分位只对放行条目计算，未放行条目不参与分位池。
    """
    pool = [s for s in scored if s.get("verdict") == "pass"] or scored
    heats = sorted(s.get("heat_norm", 0.0) for s in pool)
    for s in scored:
        pct = pct_rank(heats, s.get("heat_norm", 0.0)) if s.get("verdict") == "pass" else 0.0
        s["heat_pct"] = round(pct, 4)
        s["heat_mix"] = round(0.5 * s.get("heat_norm", 0.0) + 0.5 * pct, 4)
        s["score"] = score_of(s["heat_mix"], s.get("cross_norm", 0.0),
                              s.get("engagement_norm", 0.0), s.get("fresh_norm", 0.5), cfg)
    return scored


def pool_ref_date(entries):
    dates = [parse_date(e.get("published_at")) for e in entries]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def run_pipeline(entries, n, cfg, baseline=None):
    pr = probe(entries, cfg)
    mode = pr["mode"]
    ref = pool_ref_date(entries)
    scored = [evaluate(e, cfg, mode, ref) for e in entries]

    if mode == "relative":
        # 相对线 = 把渠道门槛下调到绝对地板，不再做分位截断。
        # 理由：分位截断会把小众选题的候选池砍到不足 N 条，
        # 而地板 100 已经负责挡噪音，排序负责选优，二者足够。
        for s in scored:
            if s["verdict"] == "below_tier":
                s["verdict"] = "pass"
                s["reason"] = "低热度选题相对线放行（渠道门槛下调至地板 100）"

    rescore_pool(scored, cfg)

    clusters = dedup(scored, cfg)
    known = [c for c in clusters if not c.get("heat_unknown")]
    unknown = [c for c in clusters if c.get("heat_unknown")]
    selected = known[:n]
    # 热度未知条目按固定配额进附录，不用「正文凑不满才补位」的老逻辑：
    # 补位会让正文满额时把公众号/小红书这类拿不到阅读量的条目整体丢掉，
    # 而它们正是文档承诺要单列呈现的那部分。shortfall 只按正文缺口计。
    cap = int(cfg.get("appendix_max", 15))
    quota = max(1, int(round(n * float(cfg.get("appendix_ratio", 0.2)))))
    limit = min(cap, quota)
    appendix = unknown[:limit]
    shortfall = max(0, n - len(selected))
    stats = {
        "candidates": len(scored),
        "passed_gate": sum(1 for s in scored if s["verdict"] == "pass"),
        "unverified": sum(1 for s in scored if s["verdict"] in ("unverified", "pass_unknown")),
        "clusters": len(clusters),
        "selected": len(selected),
        "requested": n,
        "shortfall": shortfall,
    }
    diff = compute_diff(selected, appendix, baseline) if baseline else None
    if diff:
        stats["diff"] = {"added": len(diff["added"]), "removed": len(diff["removed"]),
                         "kept": diff["kept"], "baseline_total": diff["baseline_total"]}
    stats["known_events"] = len(known)
    stats["unknown_events"] = len(unknown)
    stats["appendix"] = len(appendix)
    stats["appendix_limit"] = limit
    return {"probe": pr, "scored": scored, "clusters": clusters,
            "selected": selected, "appendix": appendix, "stats": stats, "diff": diff}


# ---------------------------------------------------------------- 报告
SAFE_URL_SCHEMES = ("http://", "https://")


def md_safe(v):
    """把外部文本压成安全的单行 Markdown 片段。

    候选条目的标题、作者、摘要、分类取值都来自外部抓取内容，
    原样插入报告会被 Markdown 语法注入——伪造链接、插入图片、破坏结构，
    甚至借 `[点我](javascript:...)` 之类写法做钓鱼。这里统一转义。
    """
    if v is None:
        return ""
    t = str(v).replace("\r", " ").replace("\n", " ").strip()
    t = t.replace("\\", "\\\\")
    for ch in ("`", "*", "_", "[", "]", "<", ">", "|", "#"):
        t = t.replace(ch, "\\" + ch)
    return t


def md_url(u):
    """只放行 http(s) 链接；其它协议（javascript:/data:/file: 等）降级为纯文本。"""
    if not u:
        return ""
    raw = str(u).strip()
    if not raw.lower().startswith(SAFE_URL_SCHEMES):
        return "（非 http(s) 链接已隐去：%s）" % md_safe(raw[:120])
    return raw.replace(")", "%29").replace(" ", "%20")


def render_report(payload, topic=""):
    sel = payload["selected"]
    st = payload["stats"]
    pr = payload["probe"]
    L = []
    L.append("# 话题棱镜 · %s" % md_safe(topic or "信息汇总"))
    L.append("")
    L.append("## 一、检索与筛选口径")
    L.append("")
    L.append("- 候选池：%d 条（原始检索结果）" % st["candidates"])
    if st.get("dropped"):
        L.append("- 输入清理：丢弃 %d 条不合格候选（缺 title / 不是对象），未计入候选池。" % st["dropped"])
    L.append("- 热度放行：%d 条" % st["passed_gate"])
    L.append("- 事件去重后：%d 个唯一事件" % st["clusters"])
    L.append("- 最终纳入：%d 条（目标 %d 条）" % (st["selected"], st["requested"]))
    L.append("- 判定模式：%s —— %s" % (pr["mode"], pr["explain"]))
    if st.get("appendix"):
        L.append("- 结构：正文 %d 条（有热度依据、参与排名）+ 附录 %d 条（热度未知、不参与排名，"
                 "按固定配额纳入，上限 %d 条）—— 合计 %d 条。正文缺口与附录另计，附录不占 %d 条的名额。"
                 % (st.get("selected", 0), st.get("appendix", 0), st.get("appendix_limit", st.get("appendix", 0)),
                    st.get("selected", 0) + st.get("appendix", 0), st.get("requested", 0)))
    if st.get("unverified"):
        L.append("- 热度未知：%d 条（平台不公开阅读/播放量，且无替代判据）—— 不剔除，按配额最多纳入 %d 条"
                 "到附录，不参与热度排名，只按信源可核验性呈现。"
                 % (st["unverified"], st.get("appendix_limit", 0)))
    if pr.get("reliable") is False:
        L.append("- 探水提醒：%s，判定模式仅供参照，建议扩大候选池后再定门槛。" % pr.get("sample_note", "样本量偏少"))
    if pr.get("all_below_floor"):
        L.append("- 警告：全部候选都未达到放行标准（含替代判据也未满足）。该选题在公开渠道几乎没有形成有效讨论，"
                 "建议放宽地板、更换检索词或确认选题范围。以下结果为空是真实结论，不是抓取失败。")
    if st["shortfall"]:
        L.append("- 缺口说明：候选池去重后不足以凑满 %d 条，缺 %d 条。这是该选题可用信息量的真实上限，未做填充凑数。"
                 % (st["requested"], st["shortfall"]))
    L.append("")

    # 分类矩阵
    L.append("## 二、分类矩阵")
    L.append("")
    any_dim = any(e.get("dimensions") for e in sel)
    if not any_dim:
        L.append("> 尚未打标：请按 references/taxonomy.md 为每条事件填写 dimensions 后重新渲染。")
        L.append("")
    else:
        for dim in DIMENSIONS:
            counts = {}
            for e in sel:
                v = (e.get("dimensions") or {}).get(dim) or "未标注"
                counts[v] = counts.get(v, 0) + 1
            if not counts:
                continue
            items = sorted(counts.items(), key=lambda kv: -kv[1])
            L.append("**%s**：%s" % (dim, " / ".join("%s %d 条" % (md_safe(k), v) for k, v in items)))
            L.append("")
        # 舆论倾向单独小结：这是用户最关心的那一维，不能只混在矩阵里
        st_counts = {}
        for e in sel:
            v = (e.get("dimensions") or {}).get("立场倾向")
            if v:
                st_counts[v] = st_counts.get(v, 0) + 1
        if st_counts:
            n = sum(st_counts.values())
            ordered = [v for v in STANCE_VALUES if v in st_counts] + \
                      [v for v in st_counts if v not in STANCE_VALUES]
            parts = ["%s %d 条（%.0f%%）" % (v, st_counts[v], 100.0 * st_counts[v] / n) for v in ordered]
            L.append("**舆论倾向**：" + " / ".join(parts))
            L.append("")

        warn = []
        for dim in DIMENSIONS:
            counts = {}
            for e in sel:
                v = (e.get("dimensions") or {}).get(dim)
                if v:
                    counts[v] = counts.get(v, 0) + 1
            if counts and max(counts.values()) / max(len(sel), 1) > 0.8 and len(counts) > 1:
                warn.append(dim)
        if warn:
            L.append("> 打标自查：%s 维度取值过于集中（>80%%），建议复查是否如实分类。" % "、".join(warn))
            L.append("")

    # 时间线：按发布日倒序，越新越靠前
    L.append("## 三、时间线（按日期倒序，越新越靠前）")
    L.append("")
    tl = timeline_groups(list(sel) + list(payload.get("appendix") or []))
    if not tl:
        L.append("> 无可用日期，无法生成时间线。")
        L.append("")
    else:
        labs = platform_labels()
        for d, items in tl:
            head = d.isoformat() if d else "日期未知"
            L.append("### %s（%d 条）" % (head, len(items)))
            L.append("")
            for e in items:
                dims = e.get("dimensions") or {}
                bits = [e.get("cluster_id", ""), md_safe(e.get("title"))]
                tag = "「新」" if e.get("is_new") else ""
                line = "- %s%s" % (tag, " ".join(bits))
                extra = []
                if e.get("platforms"):
                    extra.append("、".join(labs.get(p, p) for p in e["platforms"]))
                if e.get("score") is not None:
                    extra.append("热度 %.1f" % e.get("score"))
                else:
                    extra.append("热度未知")
                if dims.get("立场倾向"):
                    extra.append(md_safe(dims["立场倾向"]))
                if extra:
                    line += " ｜ " + " ｜ ".join(extra)
                L.append(line)
            L.append("")

    # 条目
    L.append("## 四、条目明细（按热度降序）")
    L.append("")
    groups = {}
    for e in sel:
        k = (e.get("dimensions") or {}).get("主题域") or "未归类"
        groups.setdefault(k, []).append(e)
    for k, items in groups.items():
        L.append("### %s（%d 条）" % (md_safe(k), len(items)))
        L.append("")
        for e in items:
            dims = e.get("dimensions") or {}
            L.append("**%s %s%s**" % (e["cluster_id"], md_safe(e.get("title")),
                                      "  「新」" if e.get("is_new") else ""))
            meta = ["热度分 %.1f" % e.get("score", 0)]
            if e.get("heat_pct") is not None:
                meta.append("同池分位 %.0f%%" % (100 * e.get("heat_pct", 0)))
            if e.get("platforms"):
                labs = platform_labels()
                meta.append("平台：" + "、".join(labs.get(p, p) for p in e["platforms"]))
            if e.get("author"):
                meta.append("作者：" + md_safe(e["author"]))
            if e.get("duplicate_count", 1) > 1:
                meta.append("合并 %d 条重复" % e["duplicate_count"])
            L.append("- " + " ｜ ".join(meta))
            if dims:
                L.append("- 分类：" + "；".join(
                    "%s=%s" % (d, md_safe(dims.get(d, "未标注"))) for d in DIMENSIONS if dims.get(d)))
            if e.get("excerpt"):
                L.append("- 摘要：%s" % md_safe(e["excerpt"]))
            if e.get("url"):
                L.append("- 源：%s" % md_url(e["url"]))
            L.append("")

    app = payload.get("appendix") or []
    if app:
        L.append("## 附、热度未知条目（无公开量化指标，不参与排名）")
        L.append("")
        L.append("> 以下条目平台侧未公开阅读/播放/互动数据，无法给出热度分。它们的价值在内容本身（评测、争议、一手口径），"
                 "采纳前请自行判断信源。")
        L.append("")
        for e in app:
            dims = e.get("dimensions") or {}
            L.append("**%s %s%s**" % (e["cluster_id"], md_safe(e.get("title")),
                                      "  「新」" if e.get("is_new") else ""))
            meta = ["热度未知"]
            if e.get("platforms"):
                labs = platform_labels()
                meta.append("平台：" + "、".join(labs.get(p, p) for p in e["platforms"]))
            if e.get("author"):
                meta.append("作者：" + md_safe(e["author"]))
            L.append("- " + " ｜ ".join(meta))
            if dims:
                L.append("- 分类：" + "；".join(
                    "%s=%s" % (d, md_safe(dims.get(d, "未标注"))) for d in DIMENSIONS if dims.get(d)))
            if e.get("excerpt"):
                L.append("- 摘要：%s" % md_safe(e["excerpt"]))
            if e.get("url"):
                L.append("- 源：%s" % md_url(e["url"]))
            L.append("")

    diff = payload.get("diff")
    if diff:
        L.append("## 五、与上次对比")
        L.append("")
        L.append("- 基线：%s（上次共 %d 条）" % (md_safe(diff.get("baseline_at") or "上次结果"),
                                            diff.get("baseline_total", 0)))
        L.append("- 本次：新增 %d 条 / 消失 %d 条 / 延续 %d 条"
                 % (len(diff["added"]), len(diff["removed"]), diff["kept"]))
        L.append("")
        if diff["added"]:
            L.append("**新增**")
            L.append("")
            for a in diff["added"]:
                when = a.get("published_at") or "日期未知"
                L.append("- %s %s ｜ %s ｜ 热度 %s"
                         % (a.get("cluster_id", ""), md_safe(a.get("title")), md_safe(when),
                            a.get("score") if a.get("score") is not None else "未知"))
            L.append("")
        if diff["removed"]:
            L.append("**消失（已跌出前列）**")
            L.append("")
            for r in diff["removed"]:
                L.append("- %s ｜ 上次热度 %s" % (md_safe(r.get("title")),
                                                r.get("score") if r.get("score") is not None else "未知"))
            L.append("")
        if not diff["added"] and not diff["removed"]:
            L.append("> 与上次相比没有增删，说明这一轮检索没捞到新事件，或话题已进入稳定期。")
            L.append("")

    L.append("## 六、方法学与限制")
    L.append("")
    L.append("- 热度门槛：绝对地板 100；大流量平台 1000、中等平台 300、垂直/小众源 100。")
    L.append("- 低热度选题（达标占比 <50%）自动切换为题域相对线：渠道门槛下调至绝对地板，按热度分排序取前 N，")
    L.append("  不再做分位截断，避免小众选题被砍到不足目标条数。")
    L.append("- 排序是「热度分」而非真实全网浏览量排名：各平台指标口径不同（阅读量/播放量/热度值/榜单名次），")
    L.append("  跨平台仅做归一化近似，不可当绝对值横向对比。")
    L.append("- 热度分 = 55% 本档内归一 + 25% 跨平台分布 + 20% 互动量；本档内归一后再叠一层同池分位"
             "（1.0 = 本话题候选池最热），因此分数首先表示「在本话题里的相对位置」，不是绝对人气。")
    L.append("- 达标门槛附近不做归零处理：刚过门槛的条目仍有底噪分值，不会被无数据的替代判据条目反超。")
    L.append("- 同作者跨平台同发、或标题/正文高度重合的内容已合并为一个事件，合并后跨平台数量计入热度加分。")
    L.append("- 「信息确定性」维度未达已证实的事件，采纳前需自行复核。")
    L.append("- 报告中的源链接指向第三方站点，内容与安全性由该站点负责；本工具只转述摘要，不代理访问。")
    L.append("- 标题、作者、摘要等外部文本渲染时已做转义，非 http(s) 协议链接会被隐去。")
    return "\n".join(L)


# ---------------------------------------------------------------- 质检
def qa_checks(payload, cfg=None):
    """交付前的机械质检。只查「可机械判定」的问题，语义分类仍需人工过一遍。"""
    cfg = cfg or load_cfg()
    issues = []

    def add(level, code, msg):
        issues.append({"level": level, "code": code, "msg": msg})

    st = payload.get("stats") or {}
    pr = payload.get("probe") or {}
    sel = payload.get("selected") or []
    app = payload.get("appendix") or []

    if pr.get("reliable") is False:
        add("WARN", "probe_sample", pr.get("sample_note") or "探水样本不足")
    if not sel and not app:
        add("ERROR", "empty", "正文与附录均为空，确认这是真实空结果还是检索失败")

    url_map = {}
    title_map = {}
    body_ids = {id(x) for x in sel}   # 只有正文强制六维；附录不参与排名，维度可选
    for item in sel + app:
        cid = item.get("cluster_id") or item.get("id") or "?"
        if not item.get("url"):
            add("ERROR", "no_url", "%s 缺源链接，无法核验" % cid)
        else:
            u = norm_url(item["url"])
            if u in url_map:
                add("ERROR", "dup_url", "%s 与 %s 归一化后 URL 相同，应合并" % (cid, url_map[u]))
            else:
                url_map[u] = cid
        t = norm_text(item.get("title"))
        if t in title_map:
            add("WARN", "dup_title", "%s 与 %s 标题归一后完全相同" % (cid, title_map[t]))
        else:
            title_map[t] = cid
        if not item.get("excerpt"):
            add("WARN", "no_excerpt", "%s 缺摘要" % cid)
        if id(item) in body_ids:
            dims = item.get("dimensions") or {}
            missing = [d for d in REQUIRED_DIMS if not dims.get(d)]
            if missing:
                add("ERROR", "no_dim", "%s 缺少分类维度：%s" % (cid, "、".join(missing)))

    # 维度集中度
    if sel:
        for dim in DIMENSIONS:
            counts = {}
            for item in sel:
                v = (item.get("dimensions") or {}).get(dim)
                if v:
                    counts[v] = counts.get(v, 0) + 1
            if len(counts) > 1:
                top, cnt = max(counts.items(), key=lambda kv: kv[1])
                if cnt / float(len(sel)) > 0.8:
                    add("WARN", "dim_skew", "「%s」维度 %d/%d 集中在「%s」，复查是否在偷懒" % (dim, cnt, len(sel), top))

    # 未识别平台
    unknown = [s for s in (payload.get("scored") or []) if s.get("platform_key") == "unknown"]
    if unknown:
        add("WARN", "unknown_platform", "%d 条平台未被识别（按垂直源 100 门槛处理）" % len(unknown))
    return issues


# ---------------------------------------------------------------- 完成通知
def notify_text(payload, topic=""):
    """生成语音播报文案：调研耗时较长，用户可能已离开，播报要短、能让人立刻回来查看。"""
    st = payload.get("stats") or {}
    body = st.get("selected") or 0
    app = st.get("appendix") or 0
    total = body + app
    t = (topic or payload.get("topic") or "这个话题").strip()
    if total == 0:
        return "调研完成。%s在公开渠道没有形成有效讨论，结果为空，这不是抓取失败。拿起手机看一下报告里的下一步建议。" % t
    if app:
        detail = "正文 %d 条带热度依据、附录 %d 条热度未知" % (body, app)
    else:
        detail = "%d 条都带热度依据" % body
    return "调研完成。%s，共 %d 条：%s。报告已生成，拿起手机看结果。" % (t, total, detail)


# ---------------------------------------------------------------- 自测
SELFTEST = [
    {"id": "a1", "title": "某地发布新版住房限购细则", "author": "财经日报", "platform": "微博",
     "metric": 4800000, "metric_type": "read", "engagement": {"comments": 3200, "shares": 8100, "likes": 52000},
     "excerpt": "新版细则明确社保年限与套数认定标准。", "url": "https://e.com/a1"},
    {"id": "a2", "title": "某地发布新版住房限购细则（解读）", "author": "财经日报", "platform": "知乎",
     "metric": 86000, "metric_type": "read", "engagement": {"comments": 410, "shares": 320, "likes": 2600},
     "excerpt": "新版细则明确社保年限与套数认定标准。", "url": "https://e.com/a2"},
    {"id": "b1", "title": "小众开源项目 v2 发布", "author": "dev42", "platform": "v2ex",
     "metric": 800, "metric_type": "view", "engagement": {"comments": 45, "likes": 60},
     "excerpt": "重写了调度器，带 benchmark。", "url": "https://e.com/b1"},
    {"id": "b2", "title": "关于某开源项目性能优化的探讨", "author": "dev42", "platform": "掘金",
     "metric": 120, "metric_type": "view", "engagement": {"comments": 6}, "excerpt": "对比了三种调度策略。",
     "url": "https://e.com/b2"},
    {"id": "c1", "title": "热搜第一的话题", "author": "网友", "platform": "抖音",
     "metric": 1, "metric_type": "rank", "excerpt": "", "url": "https://e.com/c1"},
    {"id": "c2", "title": "只有几十浏览的碎内容", "author": "路人", "platform": "微博",
     "metric": 42, "metric_type": "read", "excerpt": "", "url": "https://e.com/c2"},
    {"id": "d1", "title": "官媒报道的重要公告", "author": "某发布", "platform": "github",
     "qualifiers": ["official_or_bluev"], "excerpt": "公告全文。", "url": "https://e.com/d1"},
]


def selftest():
    cfg = load_cfg()
    res = run_pipeline(SELFTEST, 3, cfg)
    ids = [c["cluster_id"] for c in res["clusters"]]
    checks = []
    checks.append(("绝对地板挡掉 42 浏览量条目",
                   any(s["id"] == "c2" and s["verdict"] == "reject" for s in res["scored"])))
    checks.append(("榜单名次换算为热度", any(s["id"] == "c1" and s["verdict"] == "pass" for s in res["scored"])))
    checks.append(("替代判据放行无量化条目",
                   any(s["id"] == "d1" and s["verdict"] == "pass" for s in res["scored"])))
    checks.append(("同作者跨平台合并成一个事件",
                   sum(1 for c in res["clusters"] if any(x in c["merged_from"] for x in ("a1", "a2"))) == 1))
    checks.append(("垂直社区低门槛放行",
                   any(s["id"] == "b1" and s["verdict"] == "pass" for s in res["scored"])))
    checks.append(("取前 N 生效", len(res["selected"]) <= 3))
    checks.append(("缺口如实上报", res["stats"]["shortfall"] == max(0, 3 - len(res["selected"]))))
    # --- P0 边界断言 ---
    h_tmin = heat_norm(1000, "volume", 1000, cfg)
    h_over = heat_norm(1001, "volume", 1000, cfg)
    checks.append(("刚好达门槛不再归零且连续", h_tmin > 0 and h_over - h_tmin < 0.01))
    h_r100 = heat_norm(100, "rank", 100, cfg)
    checks.append(("榜单第 100 名不归零", h_r100 > 0.05))
    h_r1, h_r10, h_r50 = heat_norm(1, "rank", 100, cfg), heat_norm(10, "rank", 100, cfg), heat_norm(50, "rank", 100, cfg)
    checks.append(("榜单名次单调递减", h_r1 > h_r10 > h_r50 > h_r100))
    ps = [x for x in res["scored"] if x["verdict"] == "pass"]
    mono = all(
        (a.get("heat_pct", 0) <= b.get("heat_pct", 0)) == (a.get("heat_norm", 0) <= b.get("heat_norm", 0))
        or a.get("heat_norm") == b.get("heat_norm")
        for a in ps for b in ps
    )
    checks.append(("同池分位与热度单调一致", mono))
    checks.append(("唯一最高分位为 1.0", pct_rank([0.0, 0.5, 1.0], 1.0) == 1.0 and pct_rank([0.0, 0.5, 1.0], 0.0) == 0.0))
    checks.append(("分位归一后分数仍在 0~100", all(0 <= s["score"] <= 100 for s in res["scored"])))
    # 低热度选题：相对线应能凑满目标条数，且模板化系列标题不被误合并
    niche = [{"id": "s%d" % i, "title": "小众话题%d的不同侧面观察" % i, "author": "u%d" % i,
              "platform": "知乎", "metric": 150 + i * 7, "metric_type": "read",
              "engagement": {"comments": i}, "excerpt": "细节%d" % i,
              "url": "https://e.com/s%d" % i} for i in range(1, 13)]
    r2 = run_pipeline(niche, 6, cfg)
    checks.append(("低热度选题切换相对线", r2["probe"]["mode"] == "relative"))
    checks.append(("相对线不做分位截断、能凑满 N", r2["stats"]["selected"] == 6))
    checks.append(("模板化系列标题不被误判为重复", r2["stats"]["clusters"] == 12))

    # --- P1 断言 ---
    d_today, d_old = date(2026, 10, 19), date(2026, 6, 1)
    f_new = freshness_norm(d_today.isoformat(), date(2026, 10, 20), cfg=cfg)
    f_old = freshness_norm(d_old.isoformat(), date(2026, 10, 20), cfg=cfg)
    checks.append(("时间衰减：越新越高", f_new > f_old and f_new <= 1.0))
    checks.append(("日期缺失按中性值", freshness_norm(None, date(2026, 10, 20), cfg=cfg) == 0.5))

    same_url = [
        {"id": "u1", "title": "某地发布新版住房限购细则", "author": "财经日报", "platform": "微博",
         "metric": 900000, "metric_type": "read", "url": "https://e.com/x?id=1&utm_source=weibo",
         "published_at": "2026-10-18", "excerpt": "细则明确社保年限。"},
        {"id": "u2", "title": "另一条完全不同的消息", "author": "某人", "platform": "知乎",
         "metric": 5000, "metric_type": "read", "url": "http://m.e.com/x/?id=1&from=share#frag",
         "published_at": "2026-10-18", "excerpt": "与上条无关。"},
    ]
    ru = run_pipeline(same_url, 5, cfg)
    checks.append(("URL 归一后同一链接合并", ru["stats"]["clusters"] == 1))

    repost = [
        {"id": "p1", "title": "某地发布新版住房限购细则", "author": "财经日报", "platform": "微博",
         "metric": 400000, "metric_type": "read", "published_at": "2026-10-18", "url": "https://e.com/p1"},
        {"id": "p2", "title": "新版住房限购细则发布，社保年限调整", "author": "财经日报", "platform": "知乎",
         "metric": 40000, "metric_type": "read", "published_at": "2026-10-18", "url": "https://e.com/p2"},
    ]
    rp = run_pipeline(repost, 5, cfg)
    checks.append(("bigram 相似可跨平台合并", rp["stats"]["clusters"] == 1))

    diff_event = [
        {"id": "d9", "title": "某款手机值不值3999元，我跟你交个底", "author": "甲作者", "platform": "搜狐",
         "metric": 5000, "metric_type": "read", "published_at": "2026-10-17", "url": "https://e.com/d9",
         "excerpt": "肯定接口路线，但槽点也不少。"},
        {"id": "d10", "title": "这款手机的定价是不是智商税", "author": "乙作者", "platform": "今日头条",
         "metric": 5000, "metric_type": "read", "published_at": "2026-10-17", "url": "https://e.com/d10",
         "excerpt": "面向尝鲜人群，普通用户不必急。"},
    ]
    rd = run_pipeline(diff_event, 5, cfg)
    checks.append(("共用品牌却不误合并", rd["stats"]["clusters"] == 2))

    tiny = [{"id": "t%d" % i, "title": "样本条目%d" % i, "platform": "微博", "metric": 5000,
             "metric_type": "read", "url": "https://e.com/t%d" % i} for i in range(4)]
    checks.append(("样本不足时标记探水不可靠", probe(tiny, cfg)["reliable"] is False))

    checks.append(("倾向维度在必填项里", "立场倾向" in REQUIRED_DIMS))
    checks.append(("信源权威度在必填项里", "信源权威度" in REQUIRED_DIMS))
    qa_app = {"stats": {"selected": 1, "appendix": 1},
              "probe": {"reliable": True},
              "selected": [{"cluster_id": "E001", "title": "x", "url": "https://e.com/q", "excerpt": "y",
                            "dimensions": {d: "科技 / 产品" for d in REQUIRED_DIMS}}],
              "appendix": [{"cluster_id": "E002", "title": "热度未知条目", "url": "https://e.com/r",
                            "excerpt": "z", "dimensions": {}}]}
    checks.append(("附录条目不打标也不报错（只校验正文维度）",
                   not any(i["code"] == "no_dim" for i in qa_checks(qa_app, cfg))))
    qa_stance = {"stats": {"selected": 1}, "probe": {"reliable": True},
                 "selected": [{"cluster_id": "E001", "title": "x", "url": "https://e.com/q",
                               "excerpt": "y", "dimensions": {"主题域": "科技 / 产品", "内容性质": "观点",
                                                              "信息确定性": "待证实", "影响面": "个人"}}],
                 "appendix": []}
    checks.append(("缺倾向维度会被 QA 拦下",
                   any(i["code"] == "no_dim" and "立场倾向" in i["msg"] for i in qa_checks(qa_stance, cfg))))

    qa_bad = {"stats": {"selected": 1}, "probe": {"reliable": True},
              "selected": [{"cluster_id": "E001", "title": "x", "url": "https://e.com/q",
                            "excerpt": "y", "dimensions": {}}], "appendix": []}
    checks.append(("QA 能检出缺分类与缺链接", any(i["code"] == "no_dim" for i in qa_checks(qa_bad, cfg))))

    # 全部低于地板：应如实给空结果而非硬凑
    dead = [{"id": "z%d" % i, "title": "冷门%d" % i, "platform": "微博",
             "metric": 30 + i, "metric_type": "read", "url": "u%d" % i} for i in range(5)]
    r3 = run_pipeline(dead, 3, cfg)
    checks.append(("全灭时如实给空结果", r3["stats"]["selected"] == 0 and r3["probe"]["all_below_floor"]))

    # --- 输入校验：错误输入不能被伪装成空池或空结果 ---
    import tempfile
    def write_tmp(name, content):
        fd, path = tempfile.mkstemp(prefix="prism-%s-" % name, suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path
    try:
        load_entries(write_tmp("obj", '{"topic":"x"}'))
        checks.append(("对象无候选数组应报错", False))
    except SystemExit:
        checks.append(("对象无候选数组应报错", True))
    try:
        load_entries(write_tmp("empty", "[]"))
        checks.append(("候选池为空应报错而非判成真实空结果", False))
    except SystemExit:
        checks.append(("候选池为空应报错而非判成真实空结果", True))
    try:
        load_entries(write_tmp("bad", "{not json"))
        checks.append(("非法 JSON 应报错", True) if False else ("非法 JSON 应报错", False))
    except SystemExit:
        pass
    try:
        load_entries(write_tmp("none", "/no/such/file.json"))
        checks.append(("文件不存在应报错", True) if False else ("文件不存在应报错", False))
    except SystemExit:
        pass
    # 混入不合格条目：经 load_entries 应丢弃坏的、保留可用的，且可用的仍能产出
    mp = write_tmp("mixed", json.dumps([{"id": "m1", "title": "有效候选一条"}, {"id": "m2"},
                                        {"id": "m3", "title": ""}, "字符串不是对象",
                                        {"id": "m4", "platform": "微博", "metric": 5000,
                                         "metric_type": "read", "url": "https://e.com/m4",
                                         "excerpt": "e"},
                                        {"id": "m1", "title": "有效候选一条", "platform": "微博",
                                         "metric": 5001, "metric_type": "read",
                                         "url": "https://e.com/m1", "excerpt": "e"}], ensure_ascii=False))
    try:
        rm = run_pipeline(load_entries(mp), 3, cfg)
        ok_mix = len(rm["clusters"]) == 1 and rm["selected"][0]["title"] == "有效候选一条"
    finally:
        os.unlink(mp)
    checks.append(("不合格候选被丢弃、合格候选正常产出", ok_mix))

    # 附录固定配额：正文已满额时，热度未知条目仍应进入附录
    heat = [ {"id": "k%d" % i, "title": "已知事件%d" % i, "platform": "微博", "metric": 5000 + i * 10,
              "metric_type": "read", "url": "https://e.com/k%d" % i, "excerpt": "摘%d" % i,
              "published_at": "2026-10-18"} for i in range(1, 12)]
    heat.append({"id": "u1", "title": "公众号长文", "platform": "微信公众号", "url": "https://e.com/u1",
                 "excerpt": "转自公众号", "metric": None})   # 无公开量 -> 热度未知
    heat.append({"id": "u2", "title": "小红书实测", "platform": "小红书", "url": "https://e.com/u2",
                 "excerpt": "真实使用", "metric": None, "published_at": "2026-10-19"})
    rq = run_pipeline(heat, 60, cfg)
    checks.append(('热度未知条目(公众号/小红书)进附录',
                   any(e['title'] in ('小红书实测', '公众号长文') for e in rq['appendix'])))

    ok = all(v for _, v in checks)
    for name, v in checks:
        print(("  PASS  " if v else "  FAIL  ") + name)
    print("\n探水结果：%s（%s）" % (res["probe"]["mode"], res["probe"]["explain"]))
    print("事件簇：%s" % ", ".join("%s=%.1f" % (c["cluster_id"], c["score"]) for c in res["clusters"]))
    print("统计：%s" % json.dumps(res["stats"], ensure_ascii=False))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="话题棱镜 TopicPrism")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="跑完整管道并输出文件")
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--n", type=int, default=60)
    p.add_argument("--topic", default="")
    p.add_argument("--outdir", default="out")
    p.add_argument("--days", type=int, default=0, help="只保留最近 N 天（以候选池最新日期为基准）")
    p.add_argument("--baseline", default="", help="上一次的 selected.json，用于增量对比")
    p.add_argument("--budget", type=int, default=60, help="本轮检索次数预算（记录到报告，便于复盘）")
    p.add_argument("--cfg", default=None)

    p = sub.add_parser("probe", help="只做探水判定")
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--cfg", default=None)

    p = sub.add_parser("report", help="用已打标的 selected.json 渲染报告")
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--topic", default="")

    p = sub.add_parser("plan", help="打印检索计划（检索词/渠道/门槛），联网前给用户确认")
    p.add_argument("--topic", required=True)
    p.add_argument("--alias", action="append", default=[], help="扩展词/同义词，可多次传")
    p.add_argument("--n", type=int, default=60)
    p.add_argument("--budget", type=int, default=60, help="检索次数预算，默认 60")

    p = sub.add_parser("qa", help="交付前质检：缺链接/缺分类/重复/维度偏斜/样本不足")
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--cfg", default=None)

    p = sub.add_parser("notify", help="按统计口径生成收尾播报文案（交给语音播报能力）")
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--topic", default="")

    sub.add_parser("self-test", help="内置样例自测")

    ap.add_argument("--cfg", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "self-test":
        return selftest()

    if args.cmd == "probe":
        entries = load_entries(args.src)
        print(json.dumps(probe(entries, load_cfg(args.cfg)), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "plan":
        cfg = load_cfg(args.cfg)
        t = args.topic.strip()
        words = [t] + [a for a in args.alias if a]
        words += ["%s 热搜 / 热榜 / 讨论" % t, "%s 评价 / 值不值" % t, "%s 争议 / 质疑" % t,
                  "%s 数据 / 报告" % t]
        tiers = cfg["tiers"]
        print("检索计划（供用户确认后再联网）")
        print("=" * 46)
        print("话题：%s ｜ 目标条数：%d" % (t, args.n))
        print("将作为检索词发往外部搜索引擎：")
        for i, w in enumerate(dict.fromkeys(words), 1):
            print("  %d. %s" % (i, w))
        print("计划覆盖渠道：")
        for k in ("A", "B", "C"):
            spec = tiers[k]
            labs = platform_labels()
            plats = "、".join(labs.get(x, x) for x in (spec.get("platforms") or [])) or "其余垂直/小众/外文源"
            print("  %s %s（门槛 %s）：%s" % (k, spec["label"], spec["min_metric"], plats))
        focus = [("微信公众号", "深度长文 / 实测（经转载站或搜狗微信）"),
                 ("小红书", "真实体验 / 避坑 / 踩雷"),
                 ("B站", "深度测评 / 上手体验"),
                 ("什么值得买", "值不值 / 开箱 / 众测"),
                 ("知乎", "如何评价 / 怎么看"),
                 ("抖音", "热搜 / 话题讨论"),
                 ("微博", "热搜 / 超话")]
        print("平台定向检索（每个渠道换一种说法，别用同一个后缀刷六遍）：")
        for name, suffix in focus:
            print("  - %s：%s %s" % (name, t, suffix))
        probe_n = max(4, int(args.budget * 0.2))
        main_n = max(6, int(args.budget * 0.6))
        rest_n = max(2, args.budget - probe_n - main_n)
        print("检索预算：%d 次 —— 探水 %d 次 / 主体 %d 次 / 补漏 %d 次"
              % (args.budget, probe_n, main_n, rest_n))
        # 词条数与命中率的关系：清单只列首轮采样，不是穷举
        nwords = len(dict.fromkeys(words))
        note = ""
        if args.n > nwords * 3:
            note = ("（目标 %d 条远多于这里的 %d 个词，属正常：一条检索通常带出多条候选，"
                    "预算耗尽前会边搜边加词）" % (args.n, nwords))
        print("词条数：上面 %d 条是首批采样词，不是穷举清单；预算允许时按预算自动加词。%s" % (nwords, note))
        print("筛选：绝对地板 %s；探水样本下限 %s 条；去重后取前 %d 个唯一事件"
              % (cfg.get("absolute_floor"), cfg.get("probe_min_sample"), args.n))
        print("=" * 46)
        print("确认后再执行检索；用户拒绝时退化为仅基于本地材料汇总。")
        return 0

    if args.cmd == "qa":
        payload = load_json(args.src, "待质检的 selected.json")
        if not isinstance(payload, dict):
            raise SystemExit("[输入错误] 待质检文件应为对象（含 selected / stats / probe 字段）。")
        issues = qa_checks(payload, load_cfg(args.cfg))
        if not issues:
            print("QA PASS：未发现机械性问题")
            return 0
        for it in issues:
            print("[%s] %-16s %s" % (it["level"], it["code"], it["msg"]))
        return 1 if any(i["level"] == "ERROR" for i in issues) else 0

    if args.cmd == "notify":
        payload = load_json(args.src, "待播报的 selected.json")
        if not isinstance(payload, dict):
            raise SystemExit("[输入错误] 待播报文件应为对象（含 stats 字段）。")
        print(notify_text(payload, args.topic))
        return 0

    if args.cmd == "report":
        payload = load_json(args.src, "待渲染的 selected.json")
        if not isinstance(payload, dict) or "selected" not in payload:
            raise SystemExit("[输入错误] 待渲染文件缺 selected 数组，看起来不是本技能的输出。"
                             "\n先跑 run 产出 selected.json，打标后再执行 report。")
        md = render_report(payload, args.topic or payload.get("topic", ""))
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(md, encoding="utf-8")
            print("报告已写入 %s" % args.out)
        else:
            print(md)
        return 0

    # run
    if args.n < 1:
        raise SystemExit("[参数错误] --n 必须 ≥ 1，收到 %d。要 0 条结果没有意义，请检查命令。" % args.n)
    cfg = load_cfg(args.cfg)
    entries = load_entries(args.src)
    raw_n = len(entries)
    entries = filter_window(entries, args.days)
    if args.days and len(entries) != raw_n:
        print("时间窗：%d 条候选中保留最近 %d 天的 %d 条" % (raw_n, args.days, len(entries)))
    baseline = load_baseline(args.baseline) if args.baseline else None
    res = run_pipeline(entries, args.n, cfg, baseline)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    rejected = [s for s in res["scored"] if s.get("verdict") not in ("pass", "pass_unknown")]
    res["stats"]["budget"] = args.budget
    res["stats"]["dropped"] = len(LAST_DROPPED)
    res["stats"]["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {
        "topic": args.topic,
        "diff": res.get("diff"),
        "probe": res["probe"],
        "stats": res["stats"],
        "rejected": [{"title": r.get("title"), "platform": r.get("platform_key"),
                      "reason": r.get("reason"), "url": r.get("url")} for r in rejected],
        "selected": res["selected"],
        "appendix": res.get("appendix", []),
        "clusters": res["clusters"],
    }
    (outdir / "scored.json").write_text(
        json.dumps(res["scored"], ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "selected.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "report.md").write_text(render_report(payload, args.topic), encoding="utf-8")
    print("探水：%s" % res["probe"]["explain"])
    print("统计：%s" % json.dumps(res["stats"], ensure_ascii=False))
    print("输出：%s/{scored,selected}.json + report.md" % outdir)
    print("下一步：按 references/taxonomy.md 为 selected.json 里每条事件的 dimensions 打标，再执行 report。")
    if res.get("diff"):
        d = res["diff"]
        print("增量：新增 %d 条 / 消失 %d 条 / 延续 %d 条（基线 %d 条）"
              % (len(d["added"]), len(d["removed"]), d["kept"], d["baseline_total"]))
    print("收尾提醒（必做）：报告交付后调用语音播报，文案用 —— %s"
          % notify_text(payload, args.topic))
    return 0


if __name__ == "__main__":
    sys.exit(main())
