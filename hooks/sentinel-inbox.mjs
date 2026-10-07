#!/usr/bin/env node
// 夜诵 · SessionStart 钩子：把收件箱的未读内容注入新会话上下文，
// 读过的即归档（archive.jsonl）并清空收件箱——看过的别占地方。
// 归档由哨兵每轮清理，只留 7 天。
// 装法：install.sh 会帮你装；手动装法见 README。

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const DIR = path.join(os.homedir(), '.claude/tools/sentinel');
const INBOX = path.join(DIR, 'inbox.jsonl');
const ARCHIVE = path.join(DIR, 'archive.jsonl');

const readLines = (p) =>
  fs.existsSync(p) ? fs.readFileSync(p, 'utf8').split('\n').filter((l) => l.trim()) : [];

try {
  const before = readLines(INBOX);
  if (before.length > 0) {
    const items = before.map((l) => {
      try {
        const j = JSON.parse(l);
        return `- [${j.time}] ${j.watch}: ${j.text}`;
      } catch {
        return `- ${l}`;
      }
    });
    const ctx = `【夜诵 · ${before.length} 条未读】你的 AI 上线先读这些，该办的办、该报的报：\n${items.join('\n')}`;
    // 归档 + 清空；保留读取间隙新追加的行，不丢
    fs.appendFileSync(ARCHIVE, before.join('\n') + '\n');
    const after = readLines(INBOX);
    const rest = after.slice(before.length);
    fs.writeFileSync(INBOX, rest.length ? rest.join('\n') + '\n' : '');
    process.stdout.write(
      JSON.stringify({
        suppressOutput: true,
        hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: ctx },
      })
    );
  }
} catch {
  // 钩子绝不能挡住开工
}
process.exit(0);
