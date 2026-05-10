import fs from 'node:fs';
import path from 'node:path';

export function formatLocalMinute(date = new Date(), timeZone = process.env.TZ || 'Asia/Shanghai') {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date).reduce((acc, part) => {
    if (part.type !== 'literal') acc[part.type] = part.value;
    return acc;
  }, {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

export function appendRecentContext(memoryDir, entry, options = {}) {
  const maxEntries = options.maxEntries || 10;
  const contextFile = path.join(memoryDir, 'recent-context.md');
  fs.mkdirSync(memoryDir, { recursive: true });

  let existing = '';
  try { existing = fs.readFileSync(contextFile, 'utf8'); } catch {}

  const header = [];
  const entries = [];
  for (const line of existing.split('\n')) {
    if (!line.trim()) continue;
    if (line.startsWith('[')) entries.push(line);
    else header.push(line);
  }

  entries.push(entry.trim());
  const kept = entries.slice(-maxEntries);
  const output = [...header, ...kept].join('\n') + '\n';
  fs.writeFileSync(contextFile, output, 'utf8');
}
