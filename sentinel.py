#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sentinel · 夜诵 v2 —— 终端关了也替人值守的机械眼。

两种活法：

  采集（默认）   python3 sentinel.py [--verbose] [--no-notify]
      读 watches.json → 逐个源取新内容 → 新东西进 inbox.jsonl（给 AI 下次
      上线读）+ macOS 通知（给人看）。首次运行只建基线、不惊动。
      --no-notify 只采集不弹窗（测试用）。
      读过即归档：AI 上线读完收件箱后条目转入 archive.jsonl，本脚本每轮
      清掉归档里超过 7 天的——看过的不占地方。

  晨报（--report）  python3 sentinel.py --report
      先自己采一轮 → 把近 26 小时收成按源分组、附各平台热榜快照 →
      写成桌面「夜诵晨报/YYYY-MM-DD 晨报.txt」→ 自动清理 7 天前旧档。

源类型（watches.json 的 "type"）：
  gh_issue_comments  {"repo": "o/r", "number": 12}   某 Issue 的新评论
  gh_repo_issues     {"repo": "o/r"}                 仓库新 Issue / PR
  gh_new             {"days": 7, "min_stars": 300}   新风口雷达：近 N 天创建
                      的高星新项目（近 15 个里新冒头的）
  hotlist            {"platform": "toutiao|weibo|bilibili|zhihu|36kr|hackernews",
                      "top": 10, "notify_filter": ["音乐", "AI"]}
                     热榜 / 新闻榜：新上榜的进收件箱（给 AI 筛）；notify_filter
                      命中的才弹通知（"*" = 全部弹）。
                      toutiao 与 hackernews 免浏览器；其余平台需要主浏览器
                      （Chrome）与 opencli 扩展在链；没连上会自动拉起桥窗口
                      （watches.json 顶层 "bridge_window" 配 label）重试一次，
                      仍不行才跳过
  web                {"url": "https://..."}          网页变化（快照 diff，
                      新增行进收件箱）

依赖：gh（已登录）用于 GitHub 源；opencli 用于热榜源（头条免浏览器；
微博 / B站 等浏览器源断了会自动 kickstart 桥窗口重试一次，label 在
watches.json 顶层 "bridge_window" 配）；web 源走系统 curl，无其他依赖。
"""
import difflib
import glob as globmod
import hashlib
import html as htmllib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time


BASE = pathlib.Path(__file__).parent
STATE = BASE / "state.json"
INBOX = BASE / "inbox.jsonl"
ARCHIVE = BASE / "archive.jsonl"
WATCHES = BASE / "watches.json"
SNAPS = BASE / "snapshots"
REPORT_DIR = pathlib.Path.home() / "Desktop" / "夜诵晨报"
VERBOSE = "--verbose" in sys.argv
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HOT_CMDS = {
    "toutiao": ["toutiao", "hot"],
    "weibo": ["weibo", "hot"],
    "bilibili": ["bilibili", "hot"],
    "zhihu": ["zhihu", "hot"],
    "36kr": ["36kr", "hot"],
    "hackernews": ["hackernews", "top"],
}
PLATFORM_CN = {"toutiao": "头条", "weibo": "微博", "bilibili": "B站",
               "zhihu": "知乎", "36kr": "36氪", "hackernews": "HN"}


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _find_opencli():
    """launchd 的 PATH 摸不到 nvm；在几个常见位置自己找。"""
    p = shutil.which("opencli")
    if p:
        return p
    home = pathlib.Path.home()
    cands = globmod.glob(str(home / ".nvm/versions/node/*/bin/opencli"))
    if cands:
        return sorted(cands, key=lambda c: pathlib.Path(c).stat().st_mtime)[-1]
    for c in (home / ".local/bin/opencli", "/usr/local/bin/opencli",
              "/opt/homebrew/bin/opencli"):
        if pathlib.Path(c).exists():
            return str(c)
    return "opencli"  # 让它在运行时自己报错


def _slug(name):
    return re.sub(r"[/\\:]", "-", str(name)).strip() or "source"


def http_get_text(url, timeout=30):
    """抓网页 → 纯文本（去脚本/样式/标签，压空白）。失败返回 None。
    走系统 curl：证书与网络栈跟浏览器一致，比内置 urllib 皮实。"""
    try:
        r = subprocess.run(["curl", "-sSL", "-m", str(timeout), "-A", UA, url],
                           capture_output=True, timeout=timeout + 10)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    text = r.stdout.decode("utf-8", errors="replace")
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ---------------- 各源取数 ----------------

def fetch_gh_issue_comments(w):
    r = run(["gh", "api", f"repos/{w['repo']}/issues/{w['number']}/comments",
             "--jq", '.[] | [.id, .user.login, .body] | @tsv'])
    if r.returncode != 0:
        return None
    items = []
    for line in r.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        cid, user = parts[0], parts[1] if len(parts) > 1 else "?"
        body = (parts[2] if len(parts) > 2 else "").replace("\n", " ")[:160]
        items.append({"key": str(cid),
                      "text": f"{user} 回复了 Issue #{w['number']}：{body}",
                      "title": f"Issue #{w['number']} 评论 {cid}",
                      "link": f"https://github.com/{w['repo']}/issues/{w['number']}",
                      "platform": "github"})
    return items


def fetch_gh_repo_issues(w):
    r = run(["gh", "api", f"repos/{w['repo']}/issues?state=all&per_page=30",
             "--jq", '.[] | [.number, .user.login, (.pull_request != null), .title] | @tsv'])
    if r.returncode != 0:
        return None
    items = []
    for line in r.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        num, user = parts[0], parts[1] if len(parts) > 1 else "?"
        is_pr = (parts[2].lower() == "true") if len(parts) > 2 else False
        title = parts[3] if len(parts) > 3 else ""
        kind = "PR" if is_pr else "issue"
        items.append({"key": str(num),
                      "text": f"{user} 发了新 {kind} #{num}：{title[:120]}",
                      "title": title,
                      "link": f"https://github.com/{w['repo']}/issues/{num}",
                      "platform": "github"})
    return items


def fetch_gh_new(w):
    """新风口雷达：近 N 天创建的高星新项目（近 15 个里新冒头的）。"""
    days = int(w.get("days", 7))
    min_stars = int(w.get("min_stars", 300))
    since = time.strftime("%Y-%m-%d",
                          time.localtime(time.time() - days * 86400))
    r = run(["gh", "api",
             f"search/repositories?q=created:>{since}+stars:>{min_stars}&sort=stars&order=desc&per_page=15",
             "--jq", '.items[] | [.full_name, (.stargazers_count|tostring), (.description // "" | .[:100])] | @tsv'])
    if r.returncode != 0:
        return None
    items = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        full, stars = parts[0], parts[1]
        desc = parts[2].strip() if len(parts) > 2 else ""
        items.append({"key": full,
                      "text": f"【新项目】{full}（{stars}★）{desc}",
                      "title": full,
                      "link": f"https://github.com/{full}",
                      "platform": "github"})
    return items


_WINDOW_KICKED = False  # 每轮采集最多拉一次桥窗口，四个源不用各等一遍


def _bridge_window_label():
    """浏览器桥窗口的 launchd label——在 watches.json 顶层配 "bridge_window"。"""
    try:
        data = json.loads(WATCHES.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("bridge_window") or "").strip()


def _ensure_browser_bridge(err_text):
    """浏览器桥没连上时，把桥窗口（带 opencli 扩展的那个）拉起来再重试。
    每轮最多拉一次；返回 True 表示已尽力拉起，调用方可重试取数。"""
    global _WINDOW_KICKED
    if "BROWSER_CONNECT" not in err_text:
        return False
    label = _bridge_window_label()
    if not label:
        return False
    if _WINDOW_KICKED:
        return True
    _WINDOW_KICKED = True
    if VERBOSE:
        print(f"[哨兵] 浏览器桥未连：拉起 {label}，30 秒后重试")
    try:
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, timeout=20)
    except Exception:
        return False
    time.sleep(30)  # 等窗口起来 + 扩展连上桥
    return True


def fetch_hotlist(w):
    platform = w.get("platform", "")
    cmd = HOT_CMDS.get(platform)
    if not cmd:
        return None
    top = int(w.get("top", 10))
    r = run([_find_opencli(), *cmd, "-f", "json"])
    if r.returncode != 0 and _ensure_browser_bridge((r.stderr or "") + (r.stdout or "")):
        r = run([_find_opencli(), *cmd, "-f", "json"])
    if r.returncode != 0:
        if VERBOSE:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            print(f"[哨兵] {w['name']}: 取榜失败（{err[-1][:80] if err else '未知'}）")
        return None
    try:
        data = json.loads(r.stdout)
    except Exception:
        return None
    if isinstance(data, dict) and data.get("ok") is False:
        return None
    if not isinstance(data, list):
        return None
    items, snap = [], []
    for entry in data[:top]:
        title = str(entry.get("title") or entry.get("name")
                    or entry.get("word") or "").strip()
        if not title:
            continue
        rank = entry.get("rank", "?")
        hot = (entry.get("hot_value") or entry.get("hot")
               or entry.get("score") or entry.get("stars") or "")
        hot_s = f"，热度 {hot}" if hot else ""
        snap.append({"rank": rank, "title": title, "hot": hot})
        cn = PLATFORM_CN.get(platform, platform)
        items.append({"key": f"{platform}:{title}",
                      "text": f"【{cn} 热榜 #{rank}{hot_s}】{title}",
                      "title": title, "rank": rank, "hot": hot,
                      "link": "", "platform": platform})
    # 榜单快照（晨报的"大环境"读它）
    SNAPS.mkdir(exist_ok=True)
    (SNAPS / f"hot-{platform}.json").write_text(json.dumps(
        {"time": time.strftime("%Y-%m-%d %H:%M"), "platform": platform,
         "items": snap}, ensure_ascii=False, indent=1), encoding="utf-8")
    return items


def fetch_web(w):
    url = w["url"]
    text = http_get_text(url)
    if text is None:
        return None
    k = hashlib.sha1(text.encode()).hexdigest()[:12]
    return [{"key": k, "text": text,
             "title": f"{w.get('name', '网页')} 更新 {k}",
             "link": url, "platform": "web"}]


FETCHERS = {
    "gh_issue_comments": fetch_gh_issue_comments,
    "gh_repo_issues": fetch_gh_repo_issues,
    "gh_new": fetch_gh_new,
    "hotlist": fetch_hotlist,
    "web": fetch_web,
}


def fetch(w):
    fn = FETCHERS.get(w.get("type", ""))
    return fn(w) if fn else None


def _should_alert(w, text):
    """弹窗提醒（给人看）的判断：热榜只看 notify_filter 命中，其余源都弹。
    注意——收件箱总是全收（那是给 AI 看的情报流），这里只管要不要弹窗吵人。"""
    if w.get("type") != "hotlist":
        return True
    flt = w.get("notify_filter") or []
    if "*" in flt:
        return True
    return any(kw and kw in text for kw in flt)


# ---------------- 采集 ----------------

def _inbox_atomic(entry):
    """收件箱追加一条（原子写，格式与 v1 一字不差）。"""
    prev = INBOX.read_text(encoding="utf-8") if INBOX.exists() else ""
    v2.atomic_write(INBOX, prev + json.dumps(entry, ensure_ascii=False) + "\n")


def collect():
    watches = json.loads(WATCHES.read_text(encoding="utf-8"))["watches"]
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    first_run = not state
    new_total = 0
    for w in watches:
        name = w["name"]
        prev_failing = v2.is_failing(v2._health(state).get(name, {}))
        items = v2.fetch_with_retry(w, state, fetch)
        if items is None:
            if VERBOSE:
                print(f"[哨兵] {name}: 查询失败（网络/权限/浏览器未连），跳过")
            if v2.is_failing(v2._health(state).get(name, {})) and not prev_failing:
                _inbox_atomic({"time": time.strftime("%Y-%m-%d %H:%M"),
                               "watch": name, "key": f"health:{name}",
                               "text": f"【哨兵自检】源「{name}」连续失败满 5 次，已标 failing。"})
            continue
        if prev_failing:
            _inbox_atomic({"time": time.strftime("%Y-%m-%d %H:%M"),
                           "watch": name, "key": f"health:{name}",
                           "text": f"【哨兵自检】源「{name}」恢复。"})
        if w.get("type") == "web":
            items = _web_diff(w, items)
            if items is None:
                continue
        known = set(state.get(name, []))
        fresh = [it for it in items if it["key"] not in known]
        if first_run or name not in state:
            # 全局首轮、或新加的源：只建基线，不轰炸
            state[name] = [it["key"] for it in items]
        else:
            for it in fresh:
                _inbox_atomic({"time": time.strftime("%Y-%m-%d %H:%M"),
                               "watch": name, "key": it["key"],
                               "text": it["text"]})
                if _should_alert(w, it["text"]):
                    new_total += 1
                if VERBOSE:
                    print(f"[哨兵] 新: {it['text'][:100]}")
            if w.get("type") == "hotlist":
                # 热榜滚动：掉榜再回榜不该重复报——累积去重，留最近 2000 条
                merged = list(dict.fromkeys(state.get(name, [])
                                            + [it["key"] for it in items]))
                state[name] = merged[-2000:]
            else:
                state[name] = [it["key"] for it in items]
        # v2 管线：全部条目入池（预筛内部去重；热榜同键走位次刷新）
        for it in items:
            v2.ingest(BASE, w, it)
    v2.atomic_write(STATE, json.dumps(state, ensure_ascii=False, indent=1))
    v2.maybe_digest(BASE)
    _prune_archive()
    return new_total, first_run


def _prune_archive(days=7):
    """归档与日报只留最近 N 天——看过的不占地方。"""
    if ARCHIVE.exists():
        cutoff = time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(time.time() - days * 86400))
        kept = []
        for line in ARCHIVE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                t = json.loads(line).get("time", "")
            except Exception:
                kept.append(line)  # 解析不了的保留，不误杀
                continue
            if t >= cutoff:
                kept.append(line)
        v2.atomic_write(ARCHIVE, "\n".join(kept) + ("\n" if kept else ""))
    daily = BASE / "daily"
    if daily.exists():
        keep_after = time.strftime("%Y-%m-%d",
                                   time.localtime(time.time() - days * 86400))
        for f in daily.glob("*.md"):
            if f.name[:10] < keep_after:
                f.unlink()


def _web_diff(w, items):
    """web 源：对比上次快照，变了才报（首次只建快照）。"""
    name = w["name"]
    it = items[0]
    k, text = it["key"], it["text"]
    SNAPS.mkdir(exist_ok=True)
    snap = SNAPS / f"web-{_slug(name)}.txt"
    prev = snap.read_text(encoding="utf-8") if snap.exists() else None
    snap.write_text(text, encoding="utf-8")
    if prev is None:
        if VERBOSE:
            print(f"[哨兵] {name}: 首次抓取，已建快照")
        return []
    if prev == text:
        return []
    added = []
    for ln in difflib.unified_diff(prev.splitlines(), text.splitlines(),
                                   lineterm="", n=0):
        if ln.startswith("+") and not ln.startswith("+++"):
            s = ln[1:].strip()
            if s:
                added.append(s)
    if not added:
        return []
    preview = " / ".join(added[:5])[:220]
    return [{"key": k,
             "text": f"【网页更新】{name}：新增 {len(added)} 行——{preview}",
             "title": f"{name} 更新 {k}",
             "link": w["url"], "platform": "web"}]


# ---------------- 通知 ----------------

def notify(text):
    # 提醒式弹窗：不点不消失，以免错过（横幅会几秒自动消退）。
    # 走 osascript 参数传递（免转义），Popen 不等待——弹窗独立挂着，哨兵跑完即退。
    if "--no-notify" in sys.argv:
        print(f"[哨兵]（通知已关）{text}")
        return
    script = ('on run argv\n'
              'display alert "夜诵 · 哨兵" message (item 1 of argv) '
              'buttons {"知道了"}\n'
              'end run')
    try:
        subprocess.Popen(["osascript", "-e", script, text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------- 晨报 ----------------

def do_report():
    new_total, first_run = collect()
    if first_run:
        print("[夜诵] 首次运行已建基线，晨报暂不生成。")
        return
    cutoff = time.strftime("%Y-%m-%d %H:%M",
                           time.localtime(time.time() - 26 * 3600))
    entries = []
    for src in (ARCHIVE, INBOX):
        if not src.exists():
            continue
        for line in src.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("time", "") >= cutoff:
                entries.append(j)

    out = []
    out.append(f"夜诵晨报 · {time.strftime('%Y年%m月%d日')}")
    out.append("")
    if entries:
        out.append(f"近一天收成：{len(entries)} 条新动静。")
    else:
        out.append("昨夜无新动静，世界很安静。")
    out.append("")

    groups = {}
    order = []
    for j in entries:
        w = j.get("watch", "?")
        if w not in groups:
            groups[w] = []
            order.append(w)
        groups[w].append(j)
    for w in order:
        out.append(f"── {w} ──")
        for j in groups[w]:
            out.append(f"· [{j.get('time', '')}] {j.get('text', '')}")
        out.append("")

    hot_files = sorted(SNAPS.glob("hot-*.json")) if SNAPS.exists() else []
    if hot_files:
        out.append("── 大环境 ──")
        out.append("")
        for hf in hot_files:
            try:
                d = json.loads(hf.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append(f"【{d.get('platform', '?')} 热榜】（{d.get('time', '')} 采集）")
            for it in d.get("items", [])[:10]:
                out.append(f"  {it.get('rank', '?')}. {it.get('title', '')}")
            out.append("")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = time.strftime("%Y-%m-%d") + " 晨报.txt"
    (REPORT_DIR / fname).write_text("\n".join(out), encoding="utf-8")

    # 只留最近 7 天
    keep_after = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    removed = 0
    for f in REPORT_DIR.glob("* 晨报.txt"):
        if f.name[:10] < keep_after:
            f.unlink()
            removed += 1
    print(f"[夜诵] 晨报已写：{REPORT_DIR / fname}（新动静 {new_total} 条，清理旧档 {removed} 份）")


def main():
    if "--report" in sys.argv:
        do_report()
        return
    if "digest" in sys.argv:
        v2.do_digest(BASE)
        return
    if "cluster" in sys.argv:
        if "--recompute" in sys.argv:
            v2.do_recompute(BASE)
        else:
            print("[夜诵] 用法：python3 sentinel.py cluster --recompute（重算事件池）")
        return
    if "health" in sys.argv:
        v2.do_health(BASE)
        return
    new_total, first_run = collect()
    if first_run:
        print("[哨兵] 首次运行：已建基线，不打扰。")
    elif new_total:
        notify(f"有 {new_total} 条新消息，下次见面读给您")
        print(f"[哨兵] 采集到 {new_total} 条新消息，已入收件箱。")
    else:
        print("[哨兵] 无新消息。")


if __name__ == "__main__":
    main()

# ---------------- 内联 v2（发布打包合并单文件） ----------------

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""夜诵 v2.0 事件管线 —— 零模型四层：归一化、预筛、双打分、事件聚类、热度、日报。

纯标准库（sqlite3），零外部依赖，默认路径零模型调用（LLM 钩子留段三）。
全部函数确定性：同一输入重跑逐位一致。

层序（两头不动，中间插入）：
  采集层（哨兵五源，新增重试与健康表）
    ↓
  归一化  每条产出规范条目：标题、正文、时间、来源、链接、平台
    ↓
  预筛    稳定键去重（sha16：源标识＋规范化标题＋日期）、新鲜度窗、旧文过滤
          热榜同键当轮重见 = 位次刷新（判据 B 的 6 小时位次对比靠它），不新增行
    ↓
  双打分  判据 A 来源权重 × 位次 × 新鲜度；判据 B 跨源印证 × 位次变化 × 平均位次
          两判据归一化差超分歧线（默认 0.6）进待复核栏，只列不判
    ↓
  事件聚类  标题精确归一 → 字符 bigram（去停用词＋数字折叠）双向阈值 0.35/0.25
          → 数字＋实体签名（分歧区抬升为可辨候选）；48 小时增量窗口
          → 事件簇 = 相似度连通组件（贪心对代表匹配）
    ↓
  热度排序  事件热度 = Σ 源权重 × 位次分 × 时间衰减（24 小时半衰）
    ↓
  日报     daily/YYYY-MM-DD.md 四段：今日事件榜／新进展／分歧件栏／源健康表摘要
    ↓
  归档与通知（现行，不破）

接口契约：
  init_db(base)            建 events.db 五表
  ingest(base, w, item)    单条入管线，返回 dup/refresh/stale/new/merged
  do_digest(base, now)     出日报，返回文件路径
  maybe_digest(base, now)  到点且当日未出则出
  do_recompute(base, now)  清库重算（热榜走 snapshots，其余走 archive+inbox）
  do_health(base)          打印源健康表
  fetch_with_retry(w, state, fetch_fn)  三次尝试，间隔 60/300 秒
  stable_key / norm_title / bigram_sim / score_a / score_b  可复算验收点
"""
import hashlib
import json
import math
import os
import pathlib
import re
import sqlite3
import time

# cluster.json 可选，缺省即默认。键名与设计件一字对应：
# win_h 窗口小时数 48｜big_hi 0.35｜big_lo 0.25｜disagree 0.6（判据分差，归一化）
# digest_at 日报时刻 08:00｜weights 来源权重表（官方＞聚合＞个人）｜source_tier 按源类型覆写
DEFAULTS = {
    "win_h": 48,
    "big_hi": 0.35,
    # big_lo 0.22：段三真拉 25 组标注标定（Dice 口径）。0.22-0.25 区间
    # 仅「顺序调换型同实体」正例（0.235），下调让实体抬升救援网覆盖之；
    # 负例进带只列不判，不构成误合并。
    "big_lo": 0.22,
    # disagree 0.85：段三真拉标定（39 簇 45 分歧刷屏；31/33 条 gap=0.8
    # 固定形态＝单源官方新条目 A 满分 B 底分，只列不判失去甄别意义）。
    # 0.85 卡在固定形态之上，分歧栏只留真正极端分歧。
    "disagree": 0.85,
    "digest_at": "08:00",
    "weights": {"official": 1.0, "media": 0.7, "personal": 0.4},
    "source_tier": {},
}


def _cfg(base):
    d = dict(DEFAULTS)
    f = pathlib.Path(base) / "cluster.json"
    try:
        if f.exists():
            user = json.loads(f.read_text(encoding="utf-8"))
            for k in d:
                if k in user:
                    d[k] = user[k]
    except Exception:
        pass
    return d


# ---------------- 归一化与指纹 ----------------

def norm_title(s):
    """标题精确归一：小写、去空白、去标点。数字保留（精确层不折叠）。"""
    s = str(s).lower()
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE)


_DIGIT = re.compile(r"\d+")


def fold_digits(s):
    """数字归一：阿拉伯数字折叠成占位符 #，防无关条目靠共享数字沾上相似度。"""
    return _DIGIT.sub("#", s)


# 热榜高频虚词。作用在 bigram 层，不作用在精确归一层。
STOPWORDS = [
    "的", "了", "吗", "呢", "啊", "是", "在", "有", "和", "与", "及", "或",
    "但", "就", "都", "被", "把", "让", "一个", "什么", "怎么", "为什么",
    "因为", "所以", "如果", "虽然", "但是", "已经", "正在", "今天", "明天",
    "昨天", "目前", "现在", "记者", "网友", "官方", "发布", "回应", "最新",
    "视频", "消息", "热搜", "曝光", "刚刚", "突发", "表示", "称",
    "the", "a", "an", "of", "to", "in", "on", "and", "or", "is", "are",
    "was", "were", "it", "this", "that", "for", "with", "as", "by", "at",
    "from", "not", "be", "do", "new", "will", "has", "have",
]
_STOP_RE = None


def _stop_re():
    global _STOP_RE
    if _STOP_RE is None:
        words = sorted(set(STOPWORDS), key=len, reverse=True)
        _STOP_RE = re.compile("|".join(re.escape(w) for w in words),
                              re.UNICODE)
    return _STOP_RE


def bigram_text(title):
    """bigram 输入串：精确归一 → 数字折叠 → 去停用词。"""
    s = norm_title(title)
    s = fold_digits(s)
    return _stop_re().sub("", s)


def bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)}


def bigram_sim(a, b):
    """字符 bigram 相似度：Dice 系数（2×重叠/(两集合和)）。
    段三真拉标定（25 组人工标注）：min 分母对长短悬殊英文对虚高（6 例误合并），
    Dice 对长短悬殊自动压分，中文正例不受影响。阈值 0.35/0.25 维持。"""
    sa, sb = bigrams(bigram_text(a)), bigrams(bigram_text(b))
    if not sa or not sb:
        return 0.0
    return 2 * len(sa & sb) / (len(sa) + len(sb))


# 数字＋实体签名小词典（公司名、产品名、人名），可扩充。
ENTITIES = [
    "苹果", "apple", "华为", "huawei", "小米", "xiaomi", "腾讯", "tencent",
    "阿里", "阿里巴巴", "alibaba", "字节", "抖音", "tiktok", "byd", "比亚迪",
    "特斯拉", "tesla", "openai", "谷歌", "google", "微软", "microsoft",
    "英伟达", "nvidia", "三星", "samsung", "索尼", "sony", "meta", "facebook",
    "亚马逊", "amazon", "netflix", "网易", "京东", "拼多多", "美团", "百度",
    "baidu", "联想", "intel", "amd", "chatgpt",
]


def entity_sig(title):
    t = norm_title(title)
    return {e for e in ENTITIES if e in t}


def stable_key(source, title, day):
    """稳定键：sha16（源标识＋规范化标题＋日期）。键算法不许变，变了是兼容性事故。"""
    h = hashlib.sha256(
        f"{source}|{norm_title(title)}|{day}".encode("utf-8")).hexdigest()
    return h[:16]


# ---------------- 打分 ----------------

def rank_score(rank):
    """位次衰减：榜首 1.0，逐位衰减（1/√位次）。无位次（未上榜源）按 1.0 中性处理。"""
    if rank is None or rank in ("", "?"):
        return 1.0
    try:
        r = float(rank)
    except (TypeError, ValueError):
        return 1.0
    return 1.0 / math.sqrt(max(r, 1.0))


def freshness(ts, now=None):
    """新鲜度衰减：24 小时半衰。"""
    now = now if now is not None else time.time()
    age_h = max(0.0, (now - ts) / 3600.0)
    return 0.5 ** (age_h / 24.0)


def score_a(weight, rank, ts, now=None):
    """判据 A：来源权威 × 位次 × 新鲜度。这条东西本身有多重要。"""
    now = now if now is not None else time.time()
    return round(100.0 * (weight or 0.0) * rank_score(rank)
                 * freshness(ts, now), 2)


def score_b(platforms, trend, avg_rank):
    """判据 B：跨源印证分档 + 位次变化分，乘平均位次。多少人在说，且在变多吗。"""
    n = len(platforms or [])
    if n >= 4:
        corrob = 0.9
    elif n == 3:
        corrob = 0.7
    elif n == 2:
        corrob = 0.45
    else:
        corrob = 0.2
    return round(min(100.0, 100.0 * (corrob + (trend or 0.0))
                     * (avg_rank or 0.0)), 2)


def _source_weight(cfg, w):
    """来源权重：官方发布通道高于聚合媒体高于个人源，权重表进 cluster.json 可调。
    gh 类源默认官方档，热榜与网页默认聚合媒体档。"""
    t = str(w.get("type") or "")
    tier = cfg.get("source_tier", {}).get(t)
    if tier is None:
        tier = "official" if t.startswith("gh_") else "media"
    return float((cfg.get("weights") or {}).get(tier, 0.7))


# ---------------- 落盘 ----------------

def atomic_write(path, text):
    """原子写：临时文件 + os.replace，中途断电不产生半截文件。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY,
        key TEXT, title TEXT, norm TEXT, body TEXT,
        time TEXT, source TEXT, link TEXT, platform TEXT,
        rank REAL, rank6h REAL, prev_ts REAL,
        weight REAL, score_a REAL, score_b REAL,
        cluster_id INTEGER, ts REAL)""",
    """CREATE TABLE IF NOT EXISTS clusters(
        id INTEGER PRIMARY KEY,
        created REAL, last_seen REAL, frozen INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS disagreements(
        id INTEGER PRIMARY KEY, item_id INTEGER, cluster_id INTEGER,
        sim REAL, kind TEXT, time TEXT)""",
    """CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""",
]


def init_db(base):
    base = pathlib.Path(base)
    base.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(base / "events.db"))
    for s in _SCHEMA:
        con.execute(s)
    con.commit()
    return con


# ---------------- 聚类 ----------------

def _freeze_sweep(con, win_h, now):
    """48 小时无新条目即冻结；冻结簇不再参与匹配，保留在库中可追溯。"""
    con.execute("UPDATE clusters SET frozen=1 WHERE frozen=0 AND last_seen<?",
                (now - win_h * 3600.0,))


def _reps(con, win_h, now):
    """非冻结簇各取最多 3 条代表（最新 2 条 + 热度最高 1 条，去重）。
    新条目只对簇代表算相似度，开销与簇数线性。"""
    _freeze_sweep(con, win_h, now)
    out = {}
    for (cid,) in con.execute(
            "SELECT id FROM clusters WHERE frozen=0").fetchall():
        rows = con.execute(
            "SELECT id, norm, title, score_a FROM items "
            "WHERE cluster_id=? ORDER BY ts DESC", (cid,)).fetchall()
        ids = [r[0] for r in rows[:2]]
        if rows:
            ids.append(sorted(rows, key=lambda r: -(r[3] or 0.0))[0][0])
        keep = set(ids)
        out[cid] = [(r[1] or norm_title(r[2]), r[2]) for r in rows
                    if r[0] in keep]
    return out


def _trend(con, cid, rank):
    """位次变化：新条目位次优于簇内 6 小时前位次，加 0.2（蹿升加分）。"""
    if rank is None:
        return 0.0
    rows = con.execute(
        "SELECT rank6h FROM items WHERE cluster_id=? AND rank6h IS NOT NULL",
        (cid,)).fetchall()
    if not rows:
        return 0.0
    best6 = min(r[0] for r in rows)
    return 0.2 if rank < best6 else 0.0


def ingest(base, w, item, now=None):
    """单条入管线：预筛 → 双打分 → 聚类 → 热度。返回动作标签。

    返回 dup（同键重见，非热榜直接丢）/ refresh（热榜同键，位次刷新）/
    stale（超出新鲜度窗）/ new（立新簇）/ merged（并簇）。
    """
    now = now if now is not None else time.time()
    cfg = _cfg(base)
    con = init_db(base)
    source = str(w.get("name") or w.get("platform") or "?")
    title = str(item.get("title") or "").strip()
    if not title:
        return "skip"
    # 日期只入热榜键（位次对比随日重置）；非热榜源跨日同标题应判 dup。
    # 段三真拉发现：跨午夜 gh 仓名以新键重插（簇热度双计）。键函数不变，
    # 非热榜传空日期，键算法与热榜键完全兼容。
    is_hot = str(w.get("type") or "") == "hotlist"
    day = time.strftime("%Y-%m-%d", time.localtime(now)) if is_hot else ""
    key = stable_key(source, title, day)
    platform = str(item.get("platform") or w.get("type") or "?")
    ts = float(item.get("ts") or now)

    # 预筛一：稳定键。热榜同键当轮重见 = 位次刷新（判据 B 的 6 小时对比靠它）；
    # 其余源同键直接丢。
    row = con.execute("SELECT id, rank, prev_ts, rank6h FROM items "
                      "WHERE key=?", (key,)).fetchone()
    if row:
        if str(w.get("type") or "") == "hotlist":
            iid, prev_rank, prev_ts, rank6h = row
            new_r6 = rank6h
            if prev_ts and (now - prev_ts) >= 6 * 3600.0:
                new_r6 = prev_rank
            con.execute("UPDATE items SET rank=?, rank6h=?, prev_ts=?, ts=? "
                        "WHERE id=?", (_num(item.get("rank")), new_r6, now,
                                       ts, iid))
            con.commit()
            return "refresh"
        return "dup"

    # 预筛二：新鲜度窗（48 小时增量窗口）与旧文过滤
    if now - ts > cfg["win_h"] * 3600.0:
        return "stale"

    weight = _source_weight(cfg, w)
    rank = _num(item.get("rank"))
    sc_a = score_a(weight, rank, ts, now)
    nt = norm_title(title)

    # 聚类：先精确归一，再 bigram 双向阈值
    merge_cid = None
    best_cid, best_sim = None, 0.0
    reps = _reps(con, cfg["win_h"], now)
    for cid, rlist in reps.items():
        hit = False
        for rep_norm, _ in rlist:
            if rep_norm == nt:
                merge_cid = cid
                hit = True
                break
        if hit:
            break
        for rep_norm, rep_title in rlist:
            sim = bigram_sim(rep_title or rep_norm, title)
            if sim > best_sim:
                best_sim, best_cid = sim, cid
    promoted = None
    if merge_cid is None and best_cid is not None:
        if best_sim > cfg["big_hi"]:
            merge_cid = best_cid
        elif best_sim >= cfg["big_lo"]:
            sig = entity_sig(title)
            rep_sigs = [entity_sig(t) for _, t in reps.get(best_cid, [])]
            if sig and any(sig & rs for rs in rep_sigs if rs):
                promoted = (best_cid, best_sim)

    if merge_cid is None:
        cur = con.execute("INSERT INTO clusters(created, last_seen, frozen) "
                          "VALUES(?,?,0)", (now, now))
        cid = cur.lastrowid
        action = "new"
    else:
        cid = merge_cid
        con.execute("UPDATE clusters SET last_seen=? WHERE id=?", (now, cid))
        action = "merged"

    # 判据 B：簇内独立平台集 + 位次变化 + 平均位次
    rows = con.execute("SELECT platform, rank FROM items WHERE cluster_id=?",
                       (cid,)).fetchall()
    plats = {r[0] for r in rows} | {platform}
    ranks = [r[1] for r in rows] + [rank]
    avg = sum(rank_score(r) for r in ranks) / len(ranks)
    trend = _trend(con, cid, rank)
    sc_b = score_b(plats, trend, avg)

    cur = con.execute(
        "INSERT INTO items(key,title,norm,body,time,source,link,platform,"
        "rank,rank6h,prev_ts,weight,score_a,score_b,cluster_id,ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, title, nt, str(item.get("body") or "")[:400],
         time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
         source, str(item.get("link") or ""), platform,
         rank, None, None, weight, sc_a, sc_b, cid, ts))
    iid = cur.lastrowid

    # 分歧件：双打分差超分歧线 → 待复核栏（只列不判）
    if abs(sc_a - sc_b) / 100.0 > cfg["disagree"]:
        con.execute("INSERT INTO disagreements(item_id, cluster_id, sim, kind,"
                    " time) VALUES(?,?,?,?,?)",
                    (iid, cid, None, "score_divergence",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(now))))
    if promoted:
        con.execute("INSERT INTO disagreements(item_id, cluster_id, sim, kind,"
                    " time) VALUES(?,?,?,?,?)",
                    (iid, promoted[0], promoted[1], "entity_promoted",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(now))))
    elif (merge_cid is None and best_cid is not None
          and cfg["big_lo"] <= best_sim <= cfg["big_hi"]):
        con.execute("INSERT INTO disagreements(item_id, cluster_id, sim, kind,"
                    " time) VALUES(?,?,?,?,?)",
                    (iid, best_cid, best_sim, "similarity",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(now))))
    con.commit()
    return action


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------- 热度 ----------------

def cluster_heat(con, cid, now=None):
    """事件热度 = Σ 源权重 × 位次分 × 时间衰减（24 小时半衰，48 小时窗口）。"""
    now = now if now is not None else time.time()
    rows = con.execute("SELECT weight, rank, ts FROM items WHERE cluster_id=?",
                       (cid,)).fetchall()
    total = 0.0
    for weight, rank, ts in rows:
        age_h = (now - ts) / 3600.0
        if age_h < 0 or age_h > 48.0:
            continue
        total += (weight or 0.0) * rank_score(rank) * (0.5 ** (age_h / 24.0))
    return round(total, 3)


# ---------------- 日报 ----------------

def do_digest(base, now=None):
    """日报四段：今日事件榜／新进展／分歧件栏／源健康表摘要。报过的事件只有
    新进展才再报（meta 存各簇已报最大 id）。写 daily/YYYY-MM-DD.md。"""
    now = now if now is not None else time.time()
    con = init_db(base)
    cfg = _cfg(base)
    _freeze_sweep(con, cfg["win_h"], now)
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    out = [f"# 夜诵日报 · {today}", ""]

    events = []
    for (cid,) in con.execute(
            "SELECT id FROM clusters WHERE frozen=0").fetchall():
        heat = cluster_heat(con, cid, now)
        if heat <= 0:
            continue
        rows = con.execute("SELECT title, link, source, ts, score_a FROM items"
                           " WHERE cluster_id=? ORDER BY ts DESC",
                           (cid,)).fetchall()
        if not rows:
            continue
        title = rows[0][0]
        link = next((r[1] for r in rows if r[1]), "")
        nsources = len({r[2] for r in rows})
        first_ts = min(r[3] for r in rows)
        heat6 = cluster_heat(con, cid, now - 6 * 3600.0)
        marks = []
        if now - first_ts <= 6 * 3600.0:
            marks.append("新")
        if heat6 > 0 and heat > heat6 * 1.15:
            marks.append("升")
        events.append((heat, cid, title, nsources, link, marks))
    events.sort(key=lambda e: -e[0])

    out.append("## 今日事件榜")
    out.append("")
    if events:
        for i, (heat, cid, title, n, link, marks) in enumerate(events, 1):
            mk = "".join(f"[{m}]" for m in marks)
            out.append(f"{i}. {title}（热度 {heat}，来源 {n}）{mk} {link}".rstrip())
    else:
        out.append("事件池空。")
    out.append("")

    out.append("## 新进展")
    out.append("")
    newlines = []
    for heat, cid, title, n, link, marks in events:
        row = con.execute("SELECT v FROM meta WHERE k=?", (f"rep_{cid}",)).fetchone()
        upto = int(row[0]) if row else 0
        added = con.execute(
            "SELECT title, source, time FROM items WHERE cluster_id=? AND id>?"
            " ORDER BY id", (cid, upto)).fetchall()
        if added:
            newlines.append(f"- 「{title}」新增 {len(added)} 条：")
            for t, s, tm in added:
                newlines.append(f"  · [{tm}] {s}：{t}")
    if newlines:
        out.extend(newlines)
    else:
        out.append("无新进展。")
    out.append("")

    out.append("## 分歧件栏")
    out.append("")
    kind_cn = {"score_divergence": "双打分分歧", "similarity": "聚类分歧候选",
               "entity_promoted": "实体抬升候选"}
    rows = con.execute(
        "SELECT d.item_id, d.cluster_id, d.sim, d.kind, i.title FROM "
        "disagreements d JOIN items i ON i.id=d.item_id "
        "ORDER BY d.id DESC LIMIT 30").fetchall()
    if rows:
        for iid, cid2, sim, kind, title in rows:
            sim_s = f"（相似度 {round(sim, 2)}）" if sim is not None else ""
            out.append(f"- [{kind_cn.get(kind, kind)}] {title}{sim_s}")
    else:
        out.append("无分歧件。")
    out.append("")

    out.append("## 源健康表摘要")
    out.append("")
    state = _read_state(base)
    h = (state or {}).get("health", {})
    if h:
        for name in sorted(h):
            rec = h[name]
            flag = "failing" if is_failing(rec) else "ok"
            out.append(f"- {name}：{flag}（成功 {rec.get('ok', 0)}，"
                       f"失败 {rec.get('fail', 0)}，"
                       f"连续 {rec.get('consec', 0)}，"
                       f"上次 {rec.get('last_ms', 0)}ms）")
    else:
        out.append("暂无健康记录。")

    frozen = con.execute("SELECT COUNT(*) FROM clusters WHERE frozen=1").fetchone()[0]
    if frozen:
        out.append("")
        out.append(f"已冻结事件簇 {frozen} 个（48 小时无新条目，不再参与匹配，"
                   f"留在库中可追溯）。")

    daily = pathlib.Path(base) / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    fname = daily / f"{today}.md"
    atomic_write(fname, "\n".join(out) + "\n")

    for heat, cid, title, n, link, marks in events:
        mx = con.execute("SELECT MAX(id) FROM items WHERE cluster_id=?",
                         (cid,)).fetchone()[0]
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                    (f"rep_{cid}", str(mx)))
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_digest',?)",
                (today,))
    con.commit()
    print(f"[夜诵] 日报已写：{fname}（事件 {len(events)} 个）")
    return str(fname)


def maybe_digest(base, now=None):
    """到点（digest_at）且当日未出过，则出日报。"""
    now = now if now is not None else time.time()
    cfg = _cfg(base)
    hm = str(cfg["digest_at"]).split(":")
    lt = time.localtime(now)
    if (lt.tm_hour, lt.tm_min) < (int(hm[0]), int(hm[1])):
        return False
    today = time.strftime("%Y-%m-%d", lt)
    con = init_db(base)
    row = con.execute("SELECT v FROM meta WHERE k='last_digest'").fetchone()
    if row and row[0] == today:
        return False
    do_digest(base, now)
    return True


# ---------------- 健康表与重试 ----------------

def _read_state(base):
    f = pathlib.Path(base) / "state.json"
    try:
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _health(state):
    """健康表挂 state.json 新键 health，老键不动。"""
    return state.setdefault("health", {})


def record_health(state, name, ok, ms):
    """每源五字段：成功数、失败数、连续失败数、上次耗时、最后成功时刻。
    fail 累计不清零；consec 连续失败，成功后清零——连续满五次标 failing。"""
    rec = _health(state).setdefault(
        name, {"ok": 0, "fail": 0, "consec": 0, "last_ms": 0, "last_ok": ""})
    if ok:
        rec["ok"] += 1
        rec["consec"] = 0
        rec["last_ms"] = ms
        rec["last_ok"] = time.strftime("%Y-%m-%d %H:%M")
    else:
        rec["fail"] += 1
        rec["consec"] += 1
        rec["last_ms"] = ms


def is_failing(rec):
    return int((rec or {}).get("consec", 0)) >= 5


def fetch_with_retry(w, state, fetch_fn):
    """抓取失败重试两次，间隔 60 秒、300 秒，全败才记失败。
    cursor 规则：失败不推进任何游标（state 只在成功路径更新）。"""
    name = w.get("name", "?")
    for attempt in (1, 2, 3):
        t0 = time.time()
        try:
            items = fetch_fn(w)
        except Exception:
            items = None
        ms = int((time.time() - t0) * 1000)
        if items is not None:
            record_health(state, name, True, ms)
            return items
        record_health(state, name, False, ms)
        if attempt < 3:
            time.sleep(60 if attempt == 1 else 300)
    return None


def do_health(base):
    """sentinel health：打印源健康表。"""
    state = _read_state(base)
    h = state.get("health", {})
    if not h:
        print("[夜诵] 暂无健康记录。")
        return
    print(f"{'源':<24} {'状态':<8} {'成功':>5} {'失败':>5} {'连续':>5} {'上次耗时':>8} {'最后成功'}")
    for name in sorted(h):
        rec = h[name]
        flag = "failing" if is_failing(rec) else "ok"
        print(f"{name:<24} {flag:<8} {rec.get('ok', 0):>5} "
              f"{rec.get('fail', 0):>5} {rec.get('consec', 0):>5} "
              f"{rec.get('last_ms', 0):>8}ms {rec.get('last_ok', '')}")


# ---------------- 重算 ----------------

def do_recompute(base, now=None):
    """sentinel cluster --recompute：清库重算事件池。
    热榜走 snapshots/hot-*.json（有干净标题与位次），其余源走 archive+inbox。"""
    now = now if now is not None else time.time()
    base = pathlib.Path(base)
    con = init_db(base)
    for t in ("items", "clusters", "disagreements", "meta"):
        con.execute(f"DELETE FROM {t}")
    con.commit()
    watches = []
    wf = base / "watches.json"
    if wf.exists():
        try:
            watches = json.loads(wf.read_text(encoding="utf-8")).get(
                "watches", [])
        except Exception:
            pass
    wmap = {w.get("name"): w for w in watches}
    n = 0

    snaps = base / "snapshots"
    if snaps.exists():
        for hf in sorted(snaps.glob("hot-*.json")):
            try:
                d = json.loads(hf.read_text(encoding="utf-8"))
            except Exception:
                continue
            plat = d.get("platform")
            w = next((x for x in watches
                      if x.get("type") == "hotlist"
                      and x.get("platform") == plat), None)
            if w is None:
                continue
            try:
                ts = time.mktime(time.strptime(d.get("time", ""),
                                               "%Y-%m-%d %H:%M"))
            except Exception:
                ts = now
            for it in d.get("items", []):
                title = str(it.get("title") or "").strip()
                if not title:
                    continue
                ingest(base, w, {"key": f"{plat}:{title}", "title": title,
                                 "rank": it.get("rank"), "hot": it.get("hot"),
                                 "link": "", "platform": plat, "ts": ts})
                n += 1

    for src_name in ("archive.jsonl", "inbox.jsonl"):
        f = base / src_name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            w = wmap.get(j.get("watch", ""))
            if w is None or w.get("type") == "hotlist":
                continue
            try:
                ts = time.mktime(time.strptime(j.get("time", ""),
                                               "%Y-%m-%d %H:%M"))
            except Exception:
                ts = now
            title = (str(j.get("title") or "").strip()
                     or str(j.get("text", ""))[:60])
            ingest(base, w, {"key": j.get("key", ""), "title": title,
                             "text": j.get("text", ""), "link": "",
                             "platform": w.get("type"), "ts": ts})
            n += 1
    print(f"[夜诵] 事件池重算完成：{n} 条入池。")
    return n
