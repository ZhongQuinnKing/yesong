#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""夜诵 v2.1 品类雷达 · 单入口（零模型、确定性、只读）。

调度与闸：由 v2.maybe_radar / sentinel.py --radar 调用；21:00 档。
本文件不读收件箱、不弹窗、不改哨兵对外产物——只做品类雷达自己这一件事。

来源（均为公开接口；PyPI 尚未接入，边界如实标记）：
  gh-search     GitHub Search API，17 词条 topic: 搜，空回落关键词，动态 90 天窗口
  gh-trending   GitHub Trending daily 页面解析
  gh-topics     GitHub Topics 官方页（词条官方性核验 200/404）
  hn            Hacker News Algolia API（跨源旁证：帖内 GitHub 仓库）
  producthunt   Product Hunt Atom feed
  huggingface   HF 模型 API，词条按新创建排序
  npm           npm registry 搜索

打分与出库：
  score = 星档(0-4) + 新鲜度(0-2) + 品类命中(0-3 封顶，同义组内只计一次) + 跨源(旁证+1/外源+2)
  新鲜度锚点 = 运行当天
  推送闸 = 创建 ≤90 天 且 词条实证非空 且（score≥6 或（跨源≥1 且星档≥2））
  出库闸 = 逐条 gh api 真值回填复核，404 剔除

产物（都在哨兵目录下，带日期，同日重跑幂等覆盖）：
  radar/raw/YYYY-MM-DD/<source>.json       各源原始候选
  radar/daily/YYYY-MM-DD.md                 雷达日报（健康表＋推送清单）
  radar/daily/YYYY-MM-DD_pushes.jsonl       推送明细
命令行：
  python3 radar_run.py [--date YYYY-MM-DD]
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = pathlib.Path(__file__).parent
RADAR = BASE / "radar"
TODAY = time.strftime("%Y-%m-%d")
ARG_DATE = TODAY
if "--date" in sys.argv:
    ARG_DATE = sys.argv[sys.argv.index("--date") + 1]
DAY = ARG_DATE.replace("-", "")
RAW = RADAR / "raw" / ARG_DATE

UA = {"User-Agent": "radar-run/2.1 (read-only research)"}

# 17 个词条＋示例（含 bilibili 弱词、不主力搜）；示例用中性品类词，不列具名项目
TERM_SAMPLE = {
    "document-conversion": "文档转换类",
    "context-management": "上下文管理类",
    "agent-memory": "Agent 记忆类",
    "agent-behavior": "Agent 行为配置类",
    "video-rendering": "视频渲染类",
    "marketing": "营销类",
    "agent-messaging": "Agent 消息互通类",
    "bilibili": "B站相关工具类",
    "browser-automation": "浏览器自动化类",
    "mcp-server": "MCP 服务端类",
    "design-system": "设计系统类",
    "speech-to-text": "语音转写类",
    "text-to-speech": "语音合成类",
    "voice-cloning": "声音克隆类",
    "agent-skills": "Agent 技能包类",
    "computer-use": "电脑操作类",
    "music-generation": "音乐生成类",
}
TERMS = set(TERM_SAMPLE)
# 同义词条组：组内多命中只计一次（防同义堆叠刷分）
TERM_GROUPS = [
    {"text-to-speech", "voice-cloning", "speech-to-text"},
    {"context-management", "agent-memory", "agent-os"},
    {"browser-automation", "computer-use"},
]
HF_TERMS = ["text-to-speech", "voice-cloning", "music-generation",
            "speech-to-text", "agent", "computer-use"]
NP_TERMS = ["mcp", "agent", "text-to-speech", "voice-cloning",
            "browser-automation", "document-conversion", "music-generation"]
GH_SOURCES = {"gh-search", "gh-trending", "gh-topics"}
GH_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.I)
GH_JUNK = set("features topics about explore marketplace sponsors pricing blog "
              "education login signup settings notifications new search issues "
              "orgs apps pulls collections events trending discussions contact "
              "contact".split())

import datetime

# 日期护栏：只许 2024 至明日（防错传历史日期引发误算与垃圾产物）
try:
    _g = datetime.date.fromisoformat(ARG_DATE)
except ValueError:
    sys.exit(f"[雷达] --date {ARG_DATE} 非法日期")
if _g < datetime.date(2024, 1, 1) or _g > datetime.date.today() + datetime.timedelta(days=1):
    sys.exit(f"[雷达] --date {ARG_DATE} 越界，拒绝执行")


def cutoff_90d():
    d = datetime.date.fromisoformat(ARG_DATE) - datetime.timedelta(days=90)
    return d.isoformat()


def fetch_text(url, tries=3, timeout=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep([5, 30][i])
    raise last


def fetch_json(url, tries=3):
    return json.loads(fetch_text(url, tries))


def dump(src, data):
    RAW.mkdir(parents=True, exist_ok=True)
    with open(RAW / f"{src}.json", "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


health = []


def note(src, st, n, msg=""):
    health.append((src, st, n, msg))
    print(f"[雷达] [{st}] {src}: {n} | {msg}")


def gh_search(q):
    r = subprocess.run(["gh", "api", "-X", "GET", "search/repositories",
                        "-f", f"q={q}", "-f", "sort=stars", "-f", "per_page=20"],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:200])
    return json.loads(r.stdout)


def source_gh():
    """主源：GitHub Search API，17 词条 topic 搜，空回落关键词，90 天窗口。"""
    cands, meta = [], {}
    for t in TERMS:
        try:
            d = gh_search(f"topic:{t} created:>{cutoff_90d()} stars:>50")
            meta[t] = {"mode": "topic", "total": d.get("total_count", 0)}
            if d.get("total_count", 0) == 0:
                d = gh_search(f"{t} in:name,description created:>{cutoff_90d()} stars:>50")
                meta[t] = {"mode": "keyword", "total": d.get("total_count", 0)}
            for it in d.get("items", []):
                cands.append({
                    "name": it["full_name"], "full_name": it["full_name"],
                    "stars": it["stargazers_count"], "created": it["created_at"][:10],
                    "desc": (it.get("description") or "")[:200],
                    "topics": it.get("topics", []), "term": t, "source": "gh-search",
                    "license": (it.get("license") or {}).get("spdx_id", ""),
                })
            time.sleep(2.2)  # 搜索 30/min，留余量
        except Exception as e:
            meta[t] = {"mode": "FAIL", "err": str(e)[:120]}
    dump("gh-search", {"meta": meta, "cands": cands})
    n_ok = sum(1 for v in meta.values() if isinstance(v, dict)
               and v.get("mode") in ("topic", "keyword"))
    n_topic = sum(1 for v in meta.values() if isinstance(v, dict) and v.get("mode") == "topic")
    note("gh-search", "ok" if n_ok else "fail", len(cands),
         f"词条 {n_ok}/{len(TERMS)} 轮跑（topic {n_topic} / 回落 {n_ok - n_topic}）")


def source_trending():
    """GitHub Trending daily：按 Box-row 块提取仓库（页面结构易变，块级容错）。"""
    html = fetch_text("https://github.com/trending?since=daily")
    cands, seen = [], set()
    for article in re.finditer(r'<article class="Box-row">(.*?)</article>', html, re.S):
        m = re.search(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"',
                      article.group(1))
        if not m:
            continue
        full = m.group(1)
        if full in seen:
            continue
        seen.add(full)
        cands.append({"name": full, "full_name": full, "stars": None,
                      "created": "", "desc": "", "topics": [],
                      "term": "", "source": "gh-trending", "license": ""})
    dump("gh-trending", cands)
    note("gh-trending", "ok" if cands else "fail", len(cands), "页面解析")


def source_topics():
    """GitHub Topics 页：核词条官方性（200/404），列表不另解（Search 已覆盖）。"""
    res = {}
    for t in TERMS:
        try:
            req = urllib.request.Request(f"https://github.com/topics/{t}", headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                res[t] = "official(200)"
        except urllib.error.HTTPError as e:
            res[t] = f"HTTP{e.code}"
        except Exception as e:
            res[t] = f"FAIL:{str(e)[:60]}"
        time.sleep(0.5)
    dump("gh-topics", res)
    n_off = sum(1 for v in res.values() if v == "official(200)")
    note("gh-topics", "ok", n_off, f"官方 topic {n_off}/{len(TERMS)}；非官方走关键词回落")


def source_hn():
    """HN Algolia：词条搜帖，提取 GitHub 仓库（跨源旁证）。"""
    cands, per = [], {}
    for t in TERMS:
        try:
            d = fetch_json("https://hn.algolia.com/api/v1/search_by_date?"
                           f"query={urllib.parse.quote(t)}&tags=story&hitsPerPage=20")
            per[t] = len(d.get("hits", []))
            for h in d.get("hits", []):
                m = GH_RE.search(h.get("url") or "")
                if m and not m.group(1).lower().endswith(("/issues", "/pull", "/discussions")):
                    cands.append({"name": m.group(1), "full_name": m.group(1),
                                  "stars": None, "created": "",
                                  "desc": h.get("title", "")[:200],
                                  "topics": [], "term": t, "source": "hn", "license": ""})
        except Exception as e:
            per[t] = f"FAIL:{str(e)[:60]}"
        time.sleep(0.4)
    dump("hn", {"meta": per, "cands": cands})
    n_ok = sum(1 for v in per.values() if isinstance(v, int))
    note("hn", "ok" if n_ok else "fail", len(cands), f"词条 {n_ok}/{len(TERMS)}")


def source_ph():
    """Product Hunt：feed 是 Atom 格式，按 entry 解析；被挡则如实标边界。"""
    try:
        html = fetch_text("https://www.producthunt.com/feed")
        items = re.findall(r"<entry>(.*?)</entry>", html, re.S)
        cands = []
        for it in items:
            title = re.search(r"<title>(.*?)</title>", it)
            link = re.search(r'href="([^"]+)"', it)
            pub = re.search(r"<published>([^<]+)</published>", it)
            gh = GH_RE.search(it)
            cands.append({"name": title.group(1) if title else "",
                          "full_name": gh.group(1) if gh else "",
                          "url": link.group(1) if link else "",
                          "created": (pub.group(1)[:10] if pub else ""),
                          "source": "producthunt",
                          "desc": (title.group(1) if title else "")[:200]})
        dump("producthunt", cands)
        note("producthunt", "ok" if cands else "fail", len(cands), "Atom feed 解析")
    except Exception as e:
        dump("producthunt", {"blocked": str(e)[:200]})
        note("producthunt", "blocked", 0, f"feed 不通：{str(e)[:80]}（如实标边界）")


def source_hf():
    """Hugging Face 模型 API：语音/音乐/agent 词条按新创建排序。
    全源不通时快速判 blocked（如实标边界，不干等重试）。"""
    cands, per, ok_n = [], {}, 0
    for t in HF_TERMS:
        try:
            d = fetch_json("https://huggingface.co/api/models?"
                           f"search={urllib.parse.quote(t)}&sort=createdAt&direction=-1&limit=20",
                           tries=2)
            per[t] = len(d)
            ok_n += 1
            for m in d:
                cands.append({"name": m.get("id", ""), "full_name": "",
                              "stars": m.get("downloads", 0),
                              "created": str(m.get("createdAt", ""))[:10],
                              "desc": "", "topics": m.get("tags", [])[:10],
                              "term": t, "source": "huggingface", "license": ""})
        except Exception as e:
            per[t] = f"FAIL:{str(e)[:60]}"
        time.sleep(0.4)
    dump("huggingface", {"meta": per, "cands": cands})
    if ok_n == 0:
        note("huggingface", "blocked", 0,
             f"{len(HF_TERMS)} 词条全不通（{list(per.values())[0][:70]}），如实标边界")
    else:
        note("huggingface", "ok", len(cands), f"词条 {ok_n}/{len(HF_TERMS)}")


def source_npm():
    """npm registry 搜索。"""
    cands, per = [], {}
    for t in NP_TERMS:
        try:
            d = fetch_json("https://registry.npmjs.org/-/v1/search?"
                           f"text={urllib.parse.quote(t)}&size=15")
            per[t] = len(d.get("objects", []))
            for o in d.get("objects", []):
                p = o.get("package", {})
                repo = p.get("links", {}).get("repository", "") or ""
                m = GH_RE.search(repo)
                cands.append({"name": p.get("name", ""), "full_name": m.group(1) if m else "",
                              "stars": None, "created": str(p.get("date", ""))[:10],
                              "desc": (p.get("description") or "")[:200],
                              "topics": o.get("keywords") or p.get("keywords", [])[:10],
                              "term": t, "source": "npm", "license": ""})
        except Exception as e:
            per[t] = f"FAIL:{str(e)[:60]}"
        time.sleep(0.4)
    dump("npm", {"meta": per, "cands": cands})
    n_ok = sum(1 for v in per.values() if isinstance(v, int))
    note("npm", "ok" if n_ok else "fail", len(cands), f"词条 {n_ok}/{len(NP_TERMS)}")


# ---------------- 打分与出库闸（radar_score.py 同口径，新鲜度锚点改为当天） ----------------

def star_band(s):
    if s is None:
        return 0
    if s >= 10000:
        return 4
    if s >= 3000:
        return 3
    if s >= 1000:
        return 2
    if s >= 100:
        return 1
    return 0


def fresh(created):
    if not created or len(created) < 10:
        return 0
    try:
        d = datetime.date.fromisoformat(created[:10])
    except ValueError:
        return 0
    days = (datetime.date.fromisoformat(ARG_DATE) - d).days
    return 2 if days <= 30 else (1 if days <= 90 else 0)


def group_count(hit_terms):
    n = len(hit_terms)
    for g in TERM_GROUPS:
        if g & set(hit_terms):
            n -= len(g & set(hit_terms)) - 1  # 组内多命中并作一次
    return n


def load_all():
    out = []
    if RAW.exists():
        for fn in sorted(RAW.glob("*.json")):
            d = json.loads(fn.read_text(encoding="utf-8"))
            cands = d.get("cands", []) if isinstance(d, dict) else d
            out.extend(cands)
    return out


def main():
    if not (BASE / "watches.json").exists():
        pass  # 不依赖 watches；保持纯雷达
    for fn in [source_gh, source_trending, source_topics, source_hn,
               source_ph, source_hf, source_npm]:
        try:
            fn()
        except Exception as e:
            note(fn.__name__.replace("source_", ""), "fail", 0, str(e)[:100])

    cands = load_all()
    print("[雷达] 候选总数:", len(cands))
    merged = {}
    for c in cands:
        fn = (c.get("full_name") or "").lower().strip().rstrip(".git")
        key = fn if fn else f"{c.get('source')}:{c.get('name', '')}"
        if not key or key.endswith(":"):
            continue
        if key not in merged:
            merged[key] = {"full_name": fn,
                           "name": c.get("name") or "", "stars": None,
                           "dl": None, "created": c.get("created") or "",
                           "desc": c.get("desc") or "",
                           "topics": [], "terms": set(), "sources": set(),
                           "term": c.get("term") or "", "license": c.get("license") or ""}
        m = merged[key]
        if c.get("source") == "huggingface":
            if c.get("stars") is not None and m["dl"] is None:
                m["dl"] = c["stars"]
        elif c.get("stars") is not None and m["stars"] is None:
            m["stars"] = c["stars"]
        if (c.get("created") or "") > m["created"]:
            m["created"] = c["created"]
        if c.get("desc") and not m["desc"]:
            m["desc"] = c["desc"]
        if c.get("topics"):
            m["topics"] = list(set(m["topics"]) | set(c["topics"]))
        if c.get("term"):
            m["terms"].add(c["term"])
        m["sources"].add(c.get("source", ""))
        if c.get("license") and not m["license"]:
            m["license"] = c["license"]

    pushes = []
    for key, m in merged.items():
        sb = star_band(m["stars"])
        fr = fresh(m["created"])
        hit_terms = sorted(t for t in m["topics"] if t in TERMS)
        ch = min(group_count(hit_terms), 3)
        gh_hits = m["sources"] & GH_SOURCES
        xsrc = 2 * max(0, len(m["sources"]) - 1)
        if len(gh_hits) >= 2:
            xsrc += 1
        m["score"] = sb + fr + ch + xsrc
        m["_fr"] = fr
        m["_ch"] = ch
        m["_xsrc"] = xsrc
        m["hit_terms"] = hit_terms
        m["sources"] = sorted(m["sources"])
        samples = set()
        for t in hit_terms:
            samples.update(TERM_SAMPLE[t].split(" / "))
        m["why"] = " / ".join(sorted(samples)) or "（词条外命中，待抽查）"
        m["topics"] = m["topics"][:10]
        m["terms"] = sorted(m["terms"])
        if m["_fr"] >= 1 and m["hit_terms"] and (m["score"] >= 6 or (xsrc >= 1 and sb >= 2)):
            pushes.append(m)

    # 出库闸＝真值校准：gh api 逐条回填 stars+topics 重算，404 剔除
    alive, dead = [], []
    for m in pushes:
        fn = m.get("full_name")
        if not fn or "/" not in fn:
            alive.append(m)
            continue
        r = subprocess.run(["gh", "api", f"repos/{fn}", "--jq",
                            "{s:.stargazers_count, t:[.topics[]]}"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            try:
                d = json.loads(r.stdout)
                m["stars"] = d["s"]
                m["topics"] = d["t"]
                m["hit_terms"] = sorted(t for t in d["t"] if t in TERMS)
                m["_ch"] = min(group_count(m["hit_terms"]), 3)
                m["score"] = star_band(m["stars"]) + m["_fr"] + m["_ch"] + m["_xsrc"]
                samples = set()
                for t in m["hit_terms"]:
                    samples.update(TERM_SAMPLE[t].split(" / "))
                m["why"] = " / ".join(sorted(samples)) or "（词条外命中，待抽查）"
                if m["_fr"] >= 1 and m["hit_terms"]:
                    alive.append(m)
                else:
                    dead.append((m, "闸后复核：旧仓库或无词条实证"))
            except (ValueError, KeyError, json.JSONDecodeError):
                dead.append((m, "解析失败"))
        else:
            dead.append((m, f"HTTP {r.stderr.strip()[:40]}"))
    pushes = alive
    pushes.sort(key=lambda x: (-x["score"], -(x["stars"] or 0)))

    print(f"[雷达] 合并 {len(merged)} → 推送 {len(pushes)}"
          + (f"；剔除 {len(dead)}" if dead else ""))
    daily = RADAR / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    md = daily / f"{ARG_DATE}.md"
    lines = [
        f"# 品类雷达 · {ARG_DATE}",
        "",
        f"运行时刻 {time.strftime('%Y-%m-%d %H:%M')}，零模型确定性管线。",
        f"候选 {len(cands)} → 合并 {len(merged)} → 推送 {len(pushes)}。",
        "",
        "## 来源健康表",
        "",
        "| 源 | 状态 | 条数 | 备注 |",
        "|---|---|---|---|",
    ]
    for src, st, n, msg in health:
        lines.append(f"| {src} | {st} | {n} | {msg} |")
    lines += ["", f"## 推送清单（{len(pushes)}）", "",
              "| # | 项目 | 星 | 分 | 命中词条 | 为什么相关 | 来源 |",
              "|---|---|---|---|---|---|---|"]
    for i, p in enumerate(pushes, 1):
        star_disp = p["stars"] if p["stars"] is not None else (
            f"dl:{p['dl']}" if p.get("dl") else "-")
        lines.append(f"| {i} | {p['full_name'] or p['name']} | {star_disp} | "
                     f"{p['score']} | {'、'.join(p['hit_terms'])} | {p['why']} | "
                     f"{'/'.join(p['sources'])} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with open(daily / f"{ARG_DATE}_pushes.jsonl", "w") as f:
        for p in pushes:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"[雷达] OUT: {md}（推送 {len(pushes)} 条）")


if __name__ == "__main__":
    main()