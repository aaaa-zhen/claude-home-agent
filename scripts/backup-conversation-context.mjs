import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync, backup } from 'node:sqlite';
import { ROOT } from './conversation-context.mjs';
const source = path.join(ROOT, 'runtime/conversation-context.db');
const target = process.argv[2];
if (!target) throw new Error('backup destination required');
if (fs.existsSync(source)) {
  const db = new DatabaseSync(source, {readOnly: true});
  try {
    fs.mkdirSync(path.dirname(path.resolve(target)), {recursive: true});
    await backup(db, path.resolve(target));
    fs.chmodSync(target, 0o600);
    console.log('conversation context snapshot saved');
  } finally { db.close(); }
}
