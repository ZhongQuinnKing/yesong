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
                      （Chrome）与 opencli 扩展在链，没开就自动跳过
  web                {"url": "https://..."}          网页变化（快照 diff，
                      新增行进收件箱）

依赖：gh（已登录）用于 GitHub 源；opencli 用于热榜源（头条免浏览器；
微博 / B站 需要 Chrome + 扩展在链）；web 源走系统 curl，无其他依赖。
"""
import difflib
import glob as globmod
import hashlib
import html as htmllib
import json
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
        items.append((str(cid), f"{user} 回复了 Issue #{w['number']}：{body}"))
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
        items.append((str(num), f"{user} 发了新 {kind} #{num}：{title[:120]}"))
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
        items.append((full, f"【新项目】{full}（{stars}★）{desc}"))
    return items


def fetch_hotlist(w):
    platform = w.get("platform", "")
    cmd = HOT_CMDS.get(platform)
    if not cmd:
        return None
    top = int(w.get("top", 10))
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
        title = str(entry.get("title") or entry.get("name") or "").strip()
        if not title:
            continue
        rank = entry.get("rank", "?")
        hot = (entry.get("hot_value") or entry.get("hot")
               or entry.get("score") or entry.get("stars") or "")
        hot_s = f"，热度 {hot}" if hot else ""
        snap.append({"rank": rank, "title": title, "hot": hot})
        cn = PLATFORM_CN.get(platform, platform)
        items.append((f"{platform}:{title}",
                      f"【{cn} 热榜 #{rank}{hot_s}】{title}"))
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
    return [(hashlib.sha1(text.encode()).hexdigest()[:12], text)]


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

def collect():
    watches = json.loads(WATCHES.read_text(encoding="utf-8"))["watches"]
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    first_run = not state
    new_total = 0
    for w in watches:
        name = w["name"]
        items = fetch(w)
        if items is None:
            if VERBOSE:
                print(f"[哨兵] {name}: 查询失败（网络/权限/浏览器未连），跳过")
            continue
        if w.get("type") == "web":
            items = _web_diff(w, items)
            if items is None:
                continue
        known = set(state.get(name, []))
        fresh = [(k, t) for (k, t) in items if k not in known]
        if first_run or name not in state:
            # 全局首轮、或新加的源：只建基线，不轰炸
            state[name] = [k for (k, _) in items]
            continue
        for k, t in fresh:
            with INBOX.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M"),
                                    "watch": name, "key": k, "text": t},
                                   ensure_ascii=False) + "\n")
            if _should_alert(w, t):
                new_total += 1
            if VERBOSE:
                print(f"[哨兵] 新: {t[:100]}")
        if w.get("type") == "hotlist":
            # 热榜滚动：掉榜再回榜不该重复报——累积去重，留最近 2000 条
            merged = list(dict.fromkeys(state.get(name, [])
                                        + [k for (k, _) in items]))
            state[name] = merged[-2000:]
        else:
            state[name] = [k for (k, _) in items]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    _prune_archive()
    return new_total, first_run


def _prune_archive(days=7):
    """归档只留最近 N 天——看过的不占地方。"""
    if not ARCHIVE.exists():
        return
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
    ARCHIVE.write_text("\n".join(kept) + ("\n" if kept else ""),
                       encoding="utf-8")


def _web_diff(w, items):
    """web 源：对比上次快照，变了才报（首次只建快照）。"""
    name = w["name"]
    k, text = items[0]
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
    return [(k, f"【网页更新】{name}：新增 {len(added)} 行——{preview}")]


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
