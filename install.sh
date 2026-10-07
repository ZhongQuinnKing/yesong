#!/usr/bin/env bash
# 夜诵 · 一键安装（幂等，可重复跑；重复跑会保留你现有的 watches.json）
# 装什么：
#   1) 引擎      → ~/.claude/tools/sentinel/
#   2) 定时托管  → launchd（macOS；开机自动跑，此后每 6 小时一轮）
#   3) 可选钩子  → 让 AI 每次开工自动读收件箱（会改 ~/.claude/settings.json，自动备份）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.claude/tools/sentinel"
PLIST="$HOME/Library/LaunchAgents/com.yesong.sentinel.plist"
LABEL="com.yesong.sentinel"
OS="$(uname -s)"

echo "夜诵 · 安装"
echo

# 0. 依赖检查（有则用、没有自动跳过对应源，不拦安装）
command -v python3 >/dev/null 2>&1 || { echo "✗ 未找到 python3，请先安装 python3"; exit 1; }
PY="$(command -v python3)"
command -v gh >/dev/null 2>&1 || echo "⚠ 未找到 gh：GitHub 类源将自动跳过（装法：brew install gh && gh auth login）"
command -v opencli >/dev/null 2>&1 || echo "⚠ 未找到 opencli：热榜类源将自动跳过（装法见 https://github.com/jackwener/OpenCLI）"

# 旗标：--no-launchd（跳过定时注册）、--no-hook（跳过钩子安装）
NO_LAUNCHD=0; NO_HOOK=0
for a in "$@"; do
  [ "$a" = "--no-launchd" ] && NO_LAUNCHD=1
  [ "$a" = "--no-hook" ] && NO_HOOK=1
done

# 1. 安装引擎
mkdir -p "$DIR"
cp "$HERE/sentinel.py" "$DIR/sentinel.py"
if [ ! -f "$DIR/watches.json" ]; then
  cp "$HERE/watches.example.json" "$DIR/watches.json"
  echo "✓ 引擎已装；配置已生成：$DIR/watches.json（照示例改成你要盯的源）"
else
  echo "✓ 引擎已更新（保留你现有的 watches.json）"
fi

# 2. 定时托管
if [ "$OS" = "Darwin" ] && [ "$NO_LAUNCHD" = "0" ]; then
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$DIR/sentinel.py</string>
    </array>
    <key>StartInterval</key>
    <integer>21600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$DIR/sentinel.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/sentinel.log</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "✓ 哨兵已上岗：开机自动跑，此后每 6 小时一轮（关了终端也活着）"
elif [ "$OS" != "Darwin" ]; then
  echo "★ 非 macOS：引擎已装好，定时请用 cron，加这一行即可："
  echo "   0 */6 * * * $PY $DIR/sentinel.py"
else
  echo "（已跳过定时注册 --no-launchd）"
fi

# 3. 首次运行（只建基线、不打扰——这是刻意的）
echo
echo "首次运行（只记录当前状态做基线，不会打扰你）…"
"$PY" "$DIR/sentinel.py" || true

# 4. 可选：SessionStart 钩子（让 AI 自动读收件箱）
if [ "$NO_HOOK" = "0" ] && [ -d "$HOME/.claude" ]; then
  echo
  printf "要不要装「收件箱自动注入」钩子？它让你的 AI 每次开工先把夜诵采到的东西读一遍。\n（会向 ~/.claude/settings.json 追加一条 SessionStart 钩子，原文件自动备份）[y/N] "
  read -r ANS || ANS="n"
  case "$ANS" in
    y|Y|yes|YES)
      mkdir -p "$HOME/.claude/hooks"
      cp "$HERE/hooks/sentinel-inbox.mjs" "$HOME/.claude/hooks/sentinel-inbox.mjs"
      node - "$HOME/.claude/settings.json" "$HOME/.claude/hooks/sentinel-inbox.mjs" <<'NODEEOF'
const fs = require('fs');
const [, , settingsPath, hookPath] = process.argv;
let j = {};
if (fs.existsSync(settingsPath)) {
  fs.copyFileSync(settingsPath, settingsPath + '.bak');
  try { j = JSON.parse(fs.readFileSync(settingsPath, 'utf8') || '{}'); } catch { j = {}; }
}
j.hooks = j.hooks || {};
j.hooks.SessionStart = j.hooks.SessionStart || [];
const cmd = `node "${hookPath}"`;
const exists = j.hooks.SessionStart.some((e) =>
  (e.hooks || []).some((h) => (h.command || '').includes('sentinel-inbox')));
if (!exists) j.hooks.SessionStart.push({ hooks: [{ type: 'command', command: cmd }] });
fs.writeFileSync(settingsPath, JSON.stringify(j, null, 2) + '\n');
console.log(exists ? '✓ 钩子已存在，跳过' : '✓ 钩子已装（原 settings.json 已备份为 .bak）');
NODEEOF
      ;;
    *) echo "已跳过。之后想装：重跑本脚本，或照 README 手动加。" ;;
  esac
fi

echo
echo "完成。配置：$DIR/watches.json ｜ 收件箱：$DIR/inbox.jsonl ｜ 日志：$DIR/sentinel.log"
echo "本安装过程没有打开任何网页。"
