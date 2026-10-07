# 夜诵 · 你不在的时候，它替你守着世界

终端一关，你的 AI 就睡了——世界不会。夜诵是一个**离线信息哨兵**：按你列的清单，
它定期去看那些你关心的地方（热搜、科技榜、GitHub 动态、任意网页变化），把新东西
攒进一个收件箱；你的 AI 下一次上线时自动读完，该办的办、该报的报。

- **只读**，低频（6 小时一轮），只盯你列出的源。
- **不调用任何大模型**——采集本身零 AI 成本、零 token。
- **安静**：热榜类默认不吵你，只有你设的关键词命中才弹通知。

> **English TL;DR** — A read-only, offline information sentinel for AI agent users
> on macOS. It polls the sources you list (hot lists, GitHub, webpages) on a timer
> and drops everything new into an inbox that your AI agent reads automatically on
> its next session. No AI calls, no token cost.

## 它是什么、不是什么

是：**机械眼**。采集、存档、提醒。它不懂语义、不做判断——读和筛是你 AI 的活
（这正是不烧钱的原因）。

不是：爬虫（只读、低频、只盯你列的源）；也不是"盯 AI 干活"的那种工具——
那种盯的是 agent 有没有卡住、要不要审批；夜诵盯的是 **agent 睡着时外面的世界**，
正好补上另一半。不做任何写操作。

## 工作原理（三句话）

1. 开机时立即跑一轮，之后每 6 小时一轮。采集是无状态的——这轮没抓到的源
   （比如浏览器没开），下轮自动续上，不丢、不用管。
2. 有新内容 → 写进收件箱 `inbox.jsonl`；热榜类默认安静，只有 `notify_filter`
   命中的才弹 macOS 通知。
3. 你的 AI 每次开工自动读完收件箱（靠一个 SessionStart 钩子）；读过的条目转入
   归档，7 天后自动清理——看过的不占地方。

## 安装（macOS）

```bash
git clone https://github.com/ZhongQuinnKing/yesong
cd yesong
bash install.sh
```

装完照 `~/.claude/tools/sentinel/watches.json` 里的示例，改成你自己要盯的源。

依赖都是"有则用、没有自动跳过"的：
- `gh`（GitHub 类源）：`brew install gh && gh auth login`
- `opencli`（热榜类源）：[安装说明](https://github.com/jackwener/OpenCLI)

Linux：引擎全兼容，定时用 cron 替 launchd（安装脚本会提示那一行）。

## 能盯什么（watches.json 的五种源）

| 类型 | 盯什么 | 依赖 | 示例 |
|------|--------|------|------|
| `gh_repo_issues` | 某仓库的新 Issue / PR | gh 已登录 | `{"repo": "owner/repo"}` |
| `gh_issue_comments` | 某 Issue 下的新评论 | 同上 | `{"repo": "owner/repo", "number": 12}` |
| `gh_new` | **新风口雷达**：近 N 天创建的高星新项目 | 同上 | `{"days": 7, "min_stars": 300}` |
| `hotlist` | 各平台热榜 / 新闻榜 | 头条、HN 免浏览器；其余需 Chrome + opencli 扩展 | `{"platform": "hackernews", "top": 15}` |
| `web` | 任意网页变化（快照 diff） | 无（普通访问） | `{"url": "https://..."}` |

`hotlist` 可选平台：`toutiao`（头条）、`hackernews`（HN）、`weibo`（微博）、
`bilibili`（B站）、`zhihu`（知乎）、`36kr`（36氪）——国内与全球常见信息源都有。
源没装好或浏览器没开时**自动跳过**，不报错、不折腾。

热榜类每条可加 `"notify_filter"`（关键词数组，"*" = 全部弹窗）：命中的才弹通知，
全部新上榜的都会进收件箱给 AI 读——**通知管"吵不吵你"，收件箱管"AI 全知道"**。

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

## 成本

采集零花费：全部走公开接口与普通网页访问，且**不调用任何大模型**。
AI 读收件箱发生在正常对话里，不额外产生任何服务费用。

## 纪律

- 只读、低频、只盯你列出的源；不绕过任何登录与付费；不批量采集
- 请遵守各平台的服务条款；热榜数据来自平台官方接口，夜诵只搬运、不加工，
  真实性由源头保证

## 已知限制

- 定时托管目前是 macOS（launchd）；Linux 用 cron
- 部分热榜源需要本机 Chrome（或 Chromium）开着、且 opencli 扩展在链
- 网页源是普通访问：纯 JS 渲染的页面可能抓不全

## License

MIT
