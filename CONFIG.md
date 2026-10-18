# 配置说明

两份配置，各管一层。watches.json 管采集，cluster.json 管管线。

## watches.json：盯哪些源

顶层一个 `watches` 数组，每项一个源。字段随类型走：

| 类型 | 字段 | 说明 |
|---|---|---|
| gh_new | days、min_stars | 新风口雷达：近 N 天创建且星数不低于下限的新仓 |
| gh_repo_issues | repo | 仓库新 Issue 与 PR（形如 作者/仓名） |
| hotlist | platform、top、notify_filter | 热榜：toutiao、weibo、bilibili、zhihu、36kr、hackernews；notify_filter 命中的才弹通知，「*」全弹 |
| web | url | 网页变化：快照 diff，新增行进收件箱 |

顶层另可配 `bridge_window`（浏览器桥窗口 label），热榜浏览器源断开时自动拉起重试一次。

五源默认值见 `watches.example.json`，复制成 watches.json 即用。toutiao 与 hackernews 免浏览器；其余热榜平台需要主浏览器与 opencli 扩展在链。

## cluster.json：管线怎么判

六项，全部可缺，缺了用默认：

| 键 | 默认 | 作用 |
|---|---|---|
| win_h | 48 | 聚类增量窗口小时数 |
| big_hi | 0.35 | 相似度上阈：以上判同事件 |
| big_lo | 0.22 | 相似度下阈：以下判异事件，之间进数字加实体签名救援 |
| disagree | 0.85 | 分歧线：两判据归一化差超此值进待复核栏 |
| digest_at | 08:00 | 日报出报时刻 |
| weights | official 1.0、media 0.7、personal 0.4 | 判据 A 的来源权重 |

阈值怎么来的，一句话：big_lo 0.22 与 disagree 0.85 是 25 组人工标注标定的，不是拍的。0.22 到 0.25 区间只有一组「顺序调换型同实体」正例（苹果华为合作新闻的两种写法，相似度 0.235），下调让实体签名救回它；负例进带只列不判。disagree 原 0.6 时，39 个簇落 45 条分数分歧，其中 31 条 gap 恒为 0.8，形态是单源官方新条目判据 A 满分、判据 B 只有印证底分，每条新条目都进待复核栏，栏失去甄别意义，0.85 卡在这个固定形态之上。

## CLI 一览

| 命令 | 作用 |
|---|---|
| python3 sentinel.py | 采集一轮，新内容进收件箱 |
| python3 sentinel.py --no-notify | 同上，只采集不弹窗（测试用） |
| python3 sentinel.py --report | 先采一轮，出桌面晨报并清理 7 天前旧档 |
| python3 sentinel.py health | 打印源健康表 |
| python3 sentinel.py digest | 出当日日报 |
| python3 sentinel.py cluster --recompute | 清库重算事件池（热榜走 snapshots，其余走归档加收件箱） |
