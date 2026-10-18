# 夜诵 · 你不在的时候，它替你守着世界

> 原作者：ZhongQuinnKing（主号署名）
> 许可：见 [LICENSE](LICENSE)（分层：代码 MIT，内容与文档 CC BY-NC-ND 4.0；五档：免费用／自改自由／转发署名／改版发布先授权／商用零豁免——本许可本身不授予商用权）
> 商用授权与合作：需商用请先联系（通道：本仓库 issue）取得书面授权

终端一关，你的 AI 就睡了——世界不会。夜诵是一个离线信息哨兵：按你列的清单，
它定期去看那些你关心的地方（热搜、科技榜、GitHub 动态、任意网页变化），把新东西
攒进一个收件箱；你的 AI 下一次上线时自动读完，该办的办、该报的报。

v2.0 在采集之后加了一条事件管线：相似条目合并成事件，判热度，出日报。收件箱里
一百条消息，日报里也许只有二十行——重复的合并了，冷门的沉底了。

- **只读**，低频（6 小时一轮），只盯你列出的源。
- **不调用任何大模型**——采集与管线全程零 AI 成本、零 token。
- **安静**：热榜类默认不吵你，只有你设的关键词命中才弹通知。

> **English TL;DR** — A read-only, offline information sentinel for AI agent users
> on macOS. It polls the sources you list (hot lists, GitHub, webpages) on a timer,
> drops new items into an inbox that your AI agent reads on its next session, then
> merges similar items into events and writes a daily digest. No AI calls, no token
> cost.

## 它是什么、不是什么

是：机械眼加一层机械筛。采集、存档、合并、排热度、出日报。它不懂语义、不做判断，
读和筛是你 AI 的活（这正是不烧钱的原因）。管线里的双打分与聚类都是确定性算法，
同一输入重跑逐位一致。

不是：爬虫（只读、低频、只盯你列的源）；也不是"盯 AI 干活"的那种工具——
那种盯的是 agent 有没有卡住、要不要审批；夜诵盯的是 agent 睡着时外面的世界，
正好补上另一半。不做任何写操作。

## 工作原理

1. 开机时立即跑一轮，之后每 6 小时一轮。采集是无状态的——这轮没抓到的源
   （比如浏览器没开），下轮自动续上，不丢、不用管。每个源连抓三次（间隔 60 秒
   与 300 秒），连续失败满 5 次标 failing 并在收件箱落自检消息，源恢复自动解除。
2. 有新内容 → 写进收件箱 inbox.jsonl；热榜类默认安静，只有 notify_filter
   命中的才弹 macOS 通知。
3. 每天 08:00 出日报 daily/YYYY-MM-DD.md，四段：今日事件榜、新进展、分歧件栏、
   源健康表摘要。事件热度按来源权重、位次、时间衰减（24 小时半衰）排。
4. 你的 AI 每次开工自动读完收件箱（靠一个 SessionStart 钩子）；读过的条目转入
   归档，7 天后自动清理——看过的不占地方。

## 安装（macOS）

```bash
git clone https://github.com/ZhongQuinnKing/yesong.git
cd yesong
bash install.sh
```

install.sh 会把哨兵装进定时任务（每 6 小时一轮加开机即跑），装好先静默建基线，
下一轮才开始报新东西。AI 上线读箱靠 hooks/ 里的会话钩子，装时一并落位。

依赖都是"有则用、没有自动跳过"的：
- `gh`（GitHub 类源）：`brew install gh && gh auth login`
- `opencli`（热榜类源）：[安装说明](https://github.com/jackwener/OpenCLI)
- 系统 `curl`（网页源），无需另装

Linux：引擎全兼容，定时用 cron 替 launchd（安装脚本会提示那一行）。

## 能盯什么（watches.json 的五种源）

| 类型 | 盯什么 | 依赖 | 示例 |
|------|--------|------|------|
| `gh_repo_issues` | 某仓库的新 Issue / PR | gh 已登录 | `{"repo": "owner/repo"}` |
| `gh_issue_comments` | 某 Issue 下的新评论 | 同上 | `{"repo": "owner/repo", "number": 12}` |
| `gh_new` | 新风口雷达：近 N 天创建的高星新项目 | 同上 | `{"days": 7, "min_stars": 300}` |
| `hotlist` | 各平台热榜 / 新闻榜 | 头条、HN 免浏览器；其余需 Chrome + opencli 扩展 | `{"platform": "hackernews", "top": 15}` |
| `web` | 任意网页变化（快照 diff） | 无（普通访问） | `{"url": "https://..."}` |

`hotlist` 可选平台：`toutiao`（头条）、`hackernews`（HN）、`weibo`（微博）、
`bilibili`（B站）、`zhihu`（知乎）、`36kr`（36氪）——国内与全球常见信息源都有。
源没装好或浏览器没开时自动跳过，不报错、不折腾。

浏览器类源（微博 / B站 / 知乎 / 36氪）依赖本机 Chrome 与 opencli 扩展在链。
取数时若发现桥没连上，夜诵会自动 `launchctl kickstart` 你在 `watches.json`
顶层 `"bridge_window"` 配的窗口 label，等 30 秒再重试一次；没配就照常跳过。
例：`"bridge_window": "com.you.browser-window"`。

热榜类每条可加 `"notify_filter"`（关键词数组，"*" = 全部弹窗）：命中的才弹通知，
全部新上榜的都会进收件箱给 AI 读——通知管"吵不吵你"，收件箱管"AI 全知道"。

## 一次跑一轮试试

```bash
python3 sentinel.py --no-notify   # 只采集一轮，不弹窗
python3 sentinel.py health        # 源健康表
python3 sentinel.py digest        # 出当日日报
```

配置词条与 CLI 全表见 [CONFIG.md](CONFIG.md)，v1 到 v2 的逐项差异见
[CHANGELOG.md](CHANGELOG.md)。

## 让 AI 自动读收件箱（钩子）

安装脚本会问你要不要装（向 `~/.claude/settings.json` 追加一个 SessionStart 钩子，
原文件自动备份）。装了之后，你的 AI 每次开工第一眼就是夜诵采到的东西。

手动装：把 `hooks/sentinel-inbox.mjs` 拷到 `~/.claude/hooks/`，并在 settings.json 的
`hooks.SessionStart` 数组里加：

```json
{ "hooks": [ { "type": "command", "command": "node \"$HOME/.claude/hooks/sentinel-inbox.mjs\"" } ] }
```

## 与「采诗」搭配

[采诗](https://github.com/ZhongQuinnKing/caishi) 管"读"（AI 随时读中文互联网），
夜诵管"守"（AI 不在时替它看）。采诗夜诵，同出《汉书·礼乐志》"乃立乐府，采诗夜诵"。

## 在国产 AI（豆包等）上？

夜诵是需要本机环境的小工具（Python + 系统定时），云端 AI 无法直接运行它。值守范围如实说：电脑开着就按点值守（不必开着终端或 AI 会话）；关机或睡眠期间的时段不跑，下次开机后自动补跑一轮。它的设计思路可以照搬——比如用豆包的「定时任务」：跟它说"每天帮我汇总 AI 圈的新闻"，就有一个简版值守了。盯什么、怎么过滤、给谁看，照 `watches.json` 的想法来配。

## 成本

采集零花费：全部走公开接口与普通网页访问，且不调用任何大模型。
AI 读收件箱发生在正常对话里，不额外产生任何服务费用。

## 姊妹篇

同一个家族的另外三个免费开源项目：

- [采诗](https://github.com/ZhongQuinnKing/caishi) —— 中文互联网能力包：
  让 AI 直连 33+ 个中文平台（抖音 / B站 / 小红书 / 微博 / 知乎 / 公众号…）
- [拾级](https://github.com/ZhongQuinnKing/shiji) —— 从高三到工位的军师：
  装进 AI 助手的成长顾问，152 篇覆盖高考志愿 / 大学 / 考研 / 考公 /
  求职 / 职场 / 论文 / 留学等十二条线
- [玉尺](https://github.com/ZhongQuinnKing/yuchi) —— 中文写作的一把尺：
  量节奏、点套话、标平仄，文章与诗都能过一遍（写完自查的趁手家伙）

## 纪律

- 只读、低频、只盯你列出的源；不绕过任何登录与付费；不批量采集
- 请遵守各平台的服务条款；热榜数据来自平台官方接口，夜诵只搬运、不加工，
  真实性由源头保证

## 已知限制

- 定时托管目前是 macOS（launchd）；Linux 用 cron
- 部分热榜源需要本机 Chrome（或 Chromium）开着、且 opencli 扩展在链；桥窗口的 label 配在 `watches.json` 的 `bridge_window`（断了会自动拉起重试）
- 网页源是普通访问：纯 JS 渲染的页面可能抓不全
- 事件聚类是字符级算法：跨语言同事件（英译中标题）字面相似度为零，合并不了。
  这是方法边界，不是 bug；LLM 钩子接口预留着，默认不接

## 收录

2026 年 10 月起，收录于 [chinese-independent-developer](https://github.com/1c7/chinese-independent-developer)
（国内独立开发者项目清单，6 万+ 星）程序员版。

## License（双轨）

- **内容与文档**（README、策展文字、清单）：**CC BY-NC-ND 4.0**。自己用、自己改（不发布不分享）完全自由；阅读与原样分享免费（署名即可）；修改后对外发布须先取得授权；商业使用（无论是否修改）须先联系商谈并取得授权，未获授权一律禁止。
- **代码**（`sentinel.py`、`hooks/`、`install.sh`）：**MIT**。

五档口径与商用通道见 [LICENSE](LICENSE)。授权洽谈：本仓库提 issue，标题注明【商用授权】或【改编授权】。
